import hmac
import hashlib
import razorpay
from modules.config import settings
from modules.database import fetch_one, execute

rz_client = razorpay.Client(auth=(settings.razorpay_key_id, settings.razorpay_key_secret))

FREE_SESSION_LIMIT = 3


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
    """Returns {"allowed": bool, "reason": str, "sessions_remaining": int | None}."""
    sub = await get_subscription(user_id)
    if not sub:
        await provision_free_subscription(user_id)
        sub = await get_subscription(user_id)

    plan = sub["plan_tier"] if sub else "free"
    status = sub["status"] if sub else "active"

    if plan == "pro" and status == "active":
        return {"allowed": True, "reason": "pro", "sessions_remaining": None}

    usage = await get_free_usage(user_id)
    sessions_used = usage["sessions_used"] if usage else 0
    remaining = FREE_SESSION_LIMIT - sessions_used

    if remaining > 0:
        return {"allowed": True, "reason": "free", "sessions_remaining": remaining}

    return {"allowed": False, "reason": "free_quota_exceeded", "sessions_remaining": 0}


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


# ── Razorpay Standard Checkout ─────────────────────────────────────────────

async def get_or_create_razorpay_customer(user_id: str, email: str) -> str:
    """Returns existing razorpay_customer_id or creates a new one."""
    sub = await get_subscription(user_id)
    if sub and sub.get("razorpay_customer_id"):
        return sub["razorpay_customer_id"]

    customer = rz_client.customer.create({"email": email, "notes": {"user_id": user_id}})
    await execute(
        """
        UPDATE public.subscriptions
        SET razorpay_customer_id = $2, updated_at = now()
        WHERE user_id = $1::uuid
        """,
        user_id,
        customer["id"],
    )
    return customer["id"]


async def create_subscription(user_id: str, email: str) -> dict:
    """
    Creates a Razorpay subscription and returns {subscription_id, key_id} for
    the frontend to open the checkout.js modal with subscription_id (not order_id).
    """
    customer_id = await get_or_create_razorpay_customer(user_id, email)
    subscription = rz_client.subscription.create({
        "plan_id": settings.razorpay_pro_plan_id,
        "customer_notify": 1,
        "total_count": 120,
        "customer_id": customer_id,
        "notes": {"user_id": user_id},
    })
    subscription_id = subscription["id"]
    await execute(
        """
        UPDATE public.subscriptions
        SET razorpay_subscription_id = $2, updated_at = now()
        WHERE user_id = $1::uuid
        """,
        user_id,
        subscription_id,
    )
    return {
        "subscription_id": subscription_id,
        "key_id": settings.razorpay_key_id,
    }


def verify_payment_signature(payment_id: str, subscription_id: str, signature: str) -> bool:
    """
    HMAC-SHA256(payment_id + "|" + subscription_id, KEY_SECRET).
    Subscription payments differ from order payments (order uses order_id + "|" + payment_id).
    """
    message = f"{payment_id}|{subscription_id}"
    expected = hmac.new(
        settings.razorpay_key_secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


async def activate_pro(user_id: str) -> None:
    await execute(
        """
        UPDATE public.subscriptions
        SET plan_tier = 'pro', status = 'active', updated_at = now()
        WHERE user_id = $1::uuid
        """,
        user_id,
    )


async def cancel_subscription(user_id: str) -> bool:
    """Cancels at period end. Returns False if no active subscription found."""
    sub = await get_subscription(user_id)
    if not sub or not sub.get("razorpay_subscription_id"):
        return False
    rz_client.subscription.cancel(sub["razorpay_subscription_id"], {"cancel_at_cycle_end": 1})
    return True


# ── Webhook event handlers ─────────────────────────────────────────────────

async def handle_subscription_charged(event_data: dict) -> None:
    """Fired on each successful billing cycle — refreshes period_end and ensures pro status."""
    subscription = event_data.get("subscription", {})
    subscription_id = subscription.get("id")
    if not subscription_id:
        return

    import datetime
    current_end = subscription.get("current_end")
    period_end_dt = (
        datetime.datetime.utcfromtimestamp(current_end).replace(tzinfo=datetime.timezone.utc)
        if current_end else None
    )
    await execute(
        """
        UPDATE public.subscriptions
        SET plan_tier = 'pro', status = 'active', current_period_end = $2, updated_at = now()
        WHERE razorpay_subscription_id = $1
        """,
        subscription_id,
        period_end_dt,
    )


async def handle_subscription_halted(event_data: dict) -> None:
    """Razorpay halts subscription after repeated payment failures."""
    subscription_id = event_data.get("subscription", {}).get("id")
    if not subscription_id:
        return
    await execute(
        "UPDATE public.subscriptions SET status = 'past_due', updated_at = now() WHERE razorpay_subscription_id = $1",
        subscription_id,
    )


async def handle_subscription_cancelled(event_data: dict) -> None:
    """Downgrade to free when subscription is cancelled."""
    subscription_id = event_data.get("subscription", {}).get("id")
    if not subscription_id:
        return
    await execute(
        """
        UPDATE public.subscriptions
        SET plan_tier = 'free', status = 'active',
            razorpay_subscription_id = NULL, current_period_end = NULL, updated_at = now()
        WHERE razorpay_subscription_id = $1
        """,
        subscription_id,
    )
