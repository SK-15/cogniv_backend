import time
import hmac
import hashlib
import razorpay
from fastapi import HTTPException
from modules.config import settings
from modules.database import fetch_one, fetch_all, execute

rz_client = razorpay.Client(auth=(settings.razorpay_key_id, settings.razorpay_key_secret))

FREE_SESSION_LIMIT = 3

PLAN_CONFIG = {
    "starter": {"amount": 79900,  "sessions": 7},
    "pro":     {"amount": 149900, "sessions": 14},
}

PLAN_TIER_RANK = {"free": 0, "starter": 1, "pro": 2}


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


# ── Session duration ───────────────────────────────────────────────────────

async def end_interview_session(user_id: str, session_id: str, duration_seconds: int) -> bool:
    result = await execute(
        """
        UPDATE public.interview_sessions
        SET ended_at = now(), duration_seconds = $3
        WHERE id = $1::uuid AND user_id = $2::uuid AND ended_at IS NULL
        """,
        session_id,
        user_id,
        duration_seconds,
    )
    return result != "UPDATE 0"


# ── Razorpay One-time Order ────────────────────────────────────────────────

async def create_order(plan_id: str) -> dict:
    plan = PLAN_CONFIG.get(plan_id)
    if not plan:
        raise ValueError(f"Unknown plan: {plan_id}")
    order = rz_client.order.create({
        "amount": plan["amount"],
        "currency": "INR",
        "receipt": f"rcpt_{plan_id}_{int(time.time())}",
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


async def credit_sessions(user_id: str, plan_id: str, order_id: str, payment_id: str) -> None:
    plan = PLAN_CONFIG.get(plan_id)
    if not plan:
        raise ValueError(f"Unknown plan: {plan_id}")
    await execute(
        """
        INSERT INTO public.purchases (user_id, plan_id, sessions, amount, order_id, payment_id)
        VALUES ($1::uuid, $2, $3, $4, $5, $6)
        ON CONFLICT (order_id) DO NOTHING
        """,
        user_id,
        plan_id,
        plan["sessions"],
        plan["amount"],
        order_id,
        payment_id,
    )
    result = await execute(
        """
        UPDATE public.subscriptions
        SET sessions_purchased  = sessions_purchased + $2,
            plan_tier           = $3,
            current_period_end  = now() AT TIME ZONE 'UTC' + INTERVAL '30 days',
            updated_at          = now()
        WHERE user_id = $1::uuid
        """,
        user_id,
        plan["sessions"],
        plan_id,
    )
    if result == "UPDATE 0":
        raise RuntimeError(f"No subscription row for user {user_id}; credits not applied")


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
