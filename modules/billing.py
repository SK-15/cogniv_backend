import time
import hmac
import hashlib
import logging
import razorpay
from fastapi import HTTPException
from modules.config import settings
from modules.database import fetch_one, fetch_all, execute, get_pool

logger = logging.getLogger(__name__)

rz_client = razorpay.Client(auth=(settings.razorpay_key_id, settings.razorpay_key_secret))

FREE_SESSION_LIMIT = 2

PLAN_CONFIG = {
    "starter": {"amount": 79900,  "sessions": 7},
    "pro":     {"amount": 149900, "sessions": 14},
}

PLAN_TIER_RANK = {"free": 0, "starter": 1, "pro": 2}

# Session length caps, enforced server-side. The desktop app ends a session at
# the cap; the grace period absorbs clock skew and the final in-flight answer.
SESSION_LIMIT_FREE_SECONDS = 10 * 60
SESSION_LIMIT_PAID_SECONDS = 30 * 60
SESSION_GRACE_SECONDS = 2 * 60


def session_limit_for_tier(plan_tier: str | None) -> int:
    """Cap for a plan tier. Anything other than `free` counts as paid."""
    if plan_tier and plan_tier != "free":
        return SESSION_LIMIT_PAID_SECONDS
    return SESSION_LIMIT_FREE_SECONDS


def session_state(ended_at, elapsed_seconds: float, plan_tier: str | None) -> str:
    """Pure logic — 'active', 'ended' (closed by client) or 'expired' (past cap + grace)."""
    if ended_at is not None:
        return "ended"
    if elapsed_seconds > session_limit_for_tier(plan_tier) + SESSION_GRACE_SECONDS:
        return "expired"
    return "active"


def _compute_remaining(sub: dict | None, usage: dict | None) -> int:
    """Compute remaining sessions from subscription and usage data."""
    sessions_used = usage["sessions_used"] if usage else 0
    sessions_purchased = sub["sessions_purchased"] if sub else 0
    return max(0, FREE_SESSION_LIMIT + sessions_purchased - sessions_used)


def _gate_purchase(sessions_remaining: int, current_tier: str, requested_plan: str) -> None:
    """Pure logic — raises HTTPException if purchase not allowed. Testable without DB."""
    if sessions_remaining > 0:
        current_rank = PLAN_TIER_RANK.get(current_tier, 0)
        requested_rank = PLAN_TIER_RANK.get(requested_plan, 0)
        if requested_rank <= current_rank:
            raise HTTPException(
                status_code=409,
                detail="You still have active sessions. Upgrade to a higher plan or wait until sessions run out.",
            )


async def check_can_purchase(user_id: str, plan_id: str) -> None:
    """Async wrapper — fetches live state then calls _gate_purchase."""
    sub = await get_subscription(user_id)
    usage = await get_free_usage(user_id)
    remaining = _compute_remaining(sub, usage)
    current_tier = sub["plan_tier"] if sub else "free"
    _gate_purchase(remaining, current_tier, plan_id)


# ── Subscription provisioning ──────────────────────────────────────────────

async def provision_free_subscription(user_id: str) -> None:
    """INSERT OR IGNORE — safe to call on existing users."""
    await execute(
        """
        INSERT INTO public.subscriptions (user_id)
        VALUES ($1::uuid)
        ON CONFLICT (user_id) DO NOTHING
        """,
        user_id,
    )
    await execute(
        """
        INSERT INTO public.free_usage (user_id)
        VALUES ($1)
        ON CONFLICT (user_id) DO NOTHING
        """,
        user_id,
    )


# ── Quota check ────────────────────────────────────────────────────────────

async def get_subscription(user_id: str) -> dict | None:
    return await fetch_one(
        "SELECT * FROM public.subscriptions WHERE user_id = $1::uuid",
        user_id,
    )


async def get_free_usage(user_id: str) -> dict | None:
    return await fetch_one(
        "SELECT * FROM public.free_usage WHERE user_id = $1",
        user_id,
    )


async def check_can_start_session(user_id: str) -> dict:
    """Returns {"allowed": bool, "reason": str, "sessions_remaining": int}."""
    sub = await get_subscription(user_id)
    if not sub:
        await provision_free_subscription(user_id)
        sub = await get_subscription(user_id)

    usage = await get_free_usage(user_id)
    remaining = _compute_remaining(sub, usage)

    if remaining > 0:
        return {"allowed": True, "reason": "credits", "sessions_remaining": remaining}

    return {"allowed": False, "reason": "quota_exceeded", "sessions_remaining": 0}


async def increment_free_sessions_used(user_id: str) -> None:
    await execute(
        "UPDATE public.free_usage SET sessions_used = sessions_used + 1 WHERE user_id = $1",
        user_id,
    )


async def consume_session_credit(user_id: str) -> bool:
    """
    Atomically spend one session credit. Returns False when none remain.
    The check and the increment are one statement, so concurrent session
    starts cannot overspend.
    """
    await provision_free_subscription(user_id)
    row = await fetch_one(
        """
        UPDATE public.free_usage f
        SET sessions_used = f.sessions_used + 1
        FROM public.subscriptions s
        WHERE f.user_id = $1
          AND s.user_id = f.user_id
          AND f.sessions_used < $2 + s.sessions_purchased
        RETURNING f.sessions_used
        """,
        user_id,
        FREE_SESSION_LIMIT,
    )
    return row is not None


async def refund_session_credit(user_id: str) -> None:
    """Give back a credit spent on a session that could not be created."""
    await execute(
        """
        UPDATE public.free_usage
        SET sessions_used = GREATEST(0, sessions_used - 1)
        WHERE user_id = $1
        """,
        user_id,
    )


async def adjust_user_sessions(user_id: str, delta: int) -> dict:
    """
    Admin: grant (delta > 0) or revoke (delta < 0) purchased sessions for a user.
    sessions_purchased is clamped at 0. Returns the user's updated session counts.
    """
    await provision_free_subscription(user_id)
    await execute(
        """
        UPDATE public.subscriptions
        SET sessions_purchased = GREATEST(0, sessions_purchased + $2),
            updated_at         = now()
        WHERE user_id = $1::uuid
        """,
        user_id,
        delta,
    )
    sub = await get_subscription(user_id)
    usage = await get_free_usage(user_id)
    sessions_used = usage["sessions_used"] if usage else 0
    sessions_purchased = sub["sessions_purchased"] if sub else 0
    return {
        "user_id": user_id,
        "sessions_used": sessions_used,
        "sessions_purchased": sessions_purchased,
        "sessions_remaining": max(0, FREE_SESSION_LIMIT + sessions_purchased - sessions_used),
        "plan_tier": sub["plan_tier"] if sub else "free",
    }


# ── Session duration ───────────────────────────────────────────────────────

async def end_interview_session(user_id: str, session_id: str, duration_seconds: int) -> bool:
    # The client reports the duration; never record more than actually elapsed.
    result = await execute(
        """
        UPDATE public.interview_sessions
        SET ended_at = now(),
            duration_seconds = LEAST($3, GREATEST(0, EXTRACT(EPOCH FROM now() - started_at)::int))
        WHERE id = $1::uuid AND user_id = $2::uuid AND ended_at IS NULL
        """,
        session_id,
        user_id,
        duration_seconds,
    )
    return result != "UPDATE 0"


# ── Razorpay One-time Order ────────────────────────────────────────────────

class PaymentError(Exception):
    """The payment does not check out against Razorpay — never credit it."""


def create_order(plan_id: str, user_id: str) -> dict:
    """Blocking (Razorpay SDK) — call via asyncio.to_thread."""
    plan = PLAN_CONFIG.get(plan_id)
    if not plan:
        raise ValueError(f"Unknown plan: {plan_id}")
    order = rz_client.order.create({
        "amount": plan["amount"],
        "currency": "INR",
        "receipt": f"rcpt_{plan_id}_{int(time.time())}",
        # The order, not the client, is the source of truth for who pays for what.
        "notes": {"plan_id": plan_id, "user_id": user_id},
    })
    return {
        "order_id": order["id"],
        "amount": order["amount"],
        "currency": order["currency"],
        "key_id": settings.razorpay_key_id,
    }


def verify_payment_signature(order_id: str, payment_id: str, signature: str) -> bool:
    message = f"{order_id}|{payment_id}"
    expected = hmac.new(
        settings.razorpay_key_secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def verify_webhook_signature(body: bytes, signature: str) -> bool:
    if not settings.razorpay_webhook_secret or not signature:
        return False
    expected = hmac.new(
        settings.razorpay_webhook_secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def check_paid_order(order: dict, payment: dict) -> dict:
    """
    Pure logic — validate a Razorpay order/payment pair fetched from the API.
    Returns {"plan_id", "user_id", "needs_capture"}; raises PaymentError.
    """
    if payment.get("order_id") != order.get("id"):
        raise PaymentError("Payment does not belong to this order")
    notes = order.get("notes") or {}
    plan_id = notes.get("plan_id")
    plan = PLAN_CONFIG.get(plan_id)
    if not plan:
        raise PaymentError(f"Order has unknown plan: {plan_id!r}")
    if order.get("amount") != plan["amount"] or payment.get("amount") != plan["amount"]:
        raise PaymentError("Paid amount does not match the plan price")
    if order.get("currency") != "INR" or payment.get("currency") != "INR":
        raise PaymentError("Unexpected currency")
    status = payment.get("status")
    if status not in ("authorized", "captured"):
        raise PaymentError(f"Payment is {status!r}, not paid")
    return {
        "plan_id": plan_id,
        "user_id": notes.get("user_id") or None,
        "needs_capture": status == "authorized",
    }


def confirm_paid_order(order_id: str, payment_id: str) -> dict:
    """
    Blocking (Razorpay SDK) — call via asyncio.to_thread.
    Fetches the order and payment from Razorpay, checks them, and captures an
    authorized payment so it cannot be auto-refunded after we credit it.
    Raises PaymentError for a bad payment; any other exception means Razorpay
    could not be reached and the caller must fail closed.
    """
    order = rz_client.order.fetch(order_id)
    payment = rz_client.payment.fetch(payment_id)
    result = check_paid_order(order, payment)
    if result["needs_capture"]:
        try:
            rz_client.payment.capture(payment_id, payment["amount"], {"currency": "INR"})
        except Exception:
            # Another path (webhook vs. client verify) may have captured it first.
            if rz_client.payment.fetch(payment_id).get("status") != "captured":
                raise
    return {"plan_id": result["plan_id"], "user_id": result["user_id"]}


async def credit_sessions(user_id: str, plan_id: str, order_id: str, payment_id: str) -> bool:
    """
    Record the purchase and add its sessions in one transaction.
    Returns False when this order was already credited (idempotent).
    """
    plan = PLAN_CONFIG.get(plan_id)
    if not plan:
        raise ValueError(f"Unknown plan: {plan_id}")

    await provision_free_subscription(user_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO public.purchases (user_id, plan_id, sessions, amount, order_id, payment_id)
                VALUES ($1::uuid, $2, $3, $4, $5, $6)
                ON CONFLICT (order_id) DO NOTHING
                RETURNING id
                """,
                user_id,
                plan_id,
                plan["sessions"],
                plan["amount"],
                order_id,
                payment_id,
            )
            if row is None:
                # Duplicate order_id — already credited
                return False

            result = await conn.execute(
                """
                UPDATE public.subscriptions
                SET sessions_purchased  = sessions_purchased + $2,
                    plan_tier           = $3,
                    current_period_end  = NULL,
                    updated_at          = now()
                WHERE user_id = $1::uuid
                """,
                user_id,
                plan["sessions"],
                plan_id,
            )
            if result == "UPDATE 0":
                # Raising rolls back the purchase row so a retry can credit it.
                raise RuntimeError(f"No subscription row for user {user_id}; credits not applied")
    logger.info("Credited %s sessions (%s) to %s for order %s", plan["sessions"], plan_id, user_id, order_id)
    return True


async def get_purchases(user_id: str) -> list[dict]:
    return await fetch_all(
        """
        SELECT plan_id, sessions, amount, order_id, payment_id, created_at
        FROM public.purchases
        WHERE user_id = $1::uuid
        ORDER BY created_at DESC
        """,
        user_id,
    )
