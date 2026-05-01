# Payment & Subscription Integration Plan — Backend

## Context

- Framework: **FastAPI** + **asyncpg** (Neon PostgreSQL)
- Auth: Neon Auth (email/password) + Google OAuth — both converge at `require_user_id` dep in `app/main.py`, returns `user_id: str` (UUID as string)
- Modules pattern: each concern lives in `modules/<name>.py`, imported into `app/main.py`
- All DB access via helpers in `modules/database.py`: `fetch_one`, `fetch_all`, `execute`

## Free Tier Rules

- Every new user gets **3 free interview sessions**, max **10 minutes each**
- Session count enforced server-side (block new session when quota hit)
- Session duration stored server-side (client calls end endpoint; no server-side kill)
- On quota exceeded: return HTTP `402` with `upgrade_url` field

---

## Step 1 — Database Migrations

### 1a. `subscriptions` table — ALREADY EXISTS

Schema:
```sql
id                    UUID PRIMARY KEY DEFAULT gen_random_uuid()
user_id               UUID UNIQUE NOT NULL REFERENCES neon_auth."user"(id) ON DELETE CASCADE
plan_tier             TEXT NOT NULL DEFAULT 'free'         -- 'free' | 'pro'
status                TEXT NOT NULL DEFAULT 'active'       -- 'active' | 'cancelled' | 'past_due'
stripe_customer_id    TEXT
stripe_subscription_id TEXT
current_period_end    TIMESTAMP WITH TIME ZONE
created_at            TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
updated_at            TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
```

Indexes already exist: primary key on `id`, unique on `user_id`, btree on `stripe_customer_id`.

### 1b. `free_usage` table — CREATE THIS

```sql
CREATE TABLE IF NOT EXISTS public.free_usage (
    user_id       TEXT PRIMARY KEY REFERENCES neon_auth."user"(id) ON DELETE CASCADE,
    sessions_used INTEGER NOT NULL DEFAULT 0,
    created_at    TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);
```

Note: `user_id` here is TEXT (not UUID) to match how asyncpg returns neon_auth user ids as strings.

### 1c. Alter `interview_sessions` — ADD COLUMN

```sql
ALTER TABLE public.interview_sessions
    ADD COLUMN IF NOT EXISTS duration_seconds INTEGER;
```

Save this migration to `migrations/add_free_usage_and_session_duration.sql`.

---

## Step 2 — Environment Variables

Add to `.env` and register in `modules/config.py`:

```
STRIPE_SECRET_KEY=sk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_PRO_PRICE_ID=price_...
STRIPE_SUCCESS_URL=https://yourapp.com/payment/success
STRIPE_CANCEL_URL=https://yourapp.com/payment/cancel
```

In `modules/config.py`, add to the `Settings` class:

```python
stripe_secret_key: str = ""
stripe_webhook_secret: str = ""
stripe_pro_price_id: str = ""
stripe_success_url: str = ""
stripe_cancel_url: str = ""
```

---

## Step 3 — Add stripe to requirements.txt

```
stripe>=8.0.0
```

---

## Step 4 — Create `modules/billing.py`

This module owns all billing logic. Full implementation:

```python
import stripe
from modules.config import settings
from modules.database import fetch_one, execute

stripe.api_key = settings.stripe_secret_key

FREE_SESSION_LIMIT = 3


# ── Subscription provisioning ──────────────────────────────────────────────

async def provision_free_subscription(user_id: str) -> None:
    """
    Called after user creation (both email/password and Google OAuth).
    INSERT OR IGNORE — safe to call on existing users.
    """
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
    """
    Returns {"allowed": bool, "reason": str, "sessions_remaining": int | None}.
    Raises nothing — callers handle the HTTP response.
    """
    sub = await get_subscription(user_id)
    if not sub:
        # Auto-heal missing subscription row
        await provision_free_subscription(user_id)
        sub = await get_subscription(user_id)

    plan = sub["plan_tier"] if sub else "free"
    status = sub["status"] if sub else "active"

    if plan == "pro" and status == "active":
        return {"allowed": True, "reason": "pro", "sessions_remaining": None}

    # Free tier check
    usage = await get_free_usage(user_id)
    sessions_used = usage["sessions_used"] if usage else 0
    remaining = FREE_SESSION_LIMIT - sessions_used

    if remaining > 0:
        return {"allowed": True, "reason": "free", "sessions_remaining": remaining}

    return {
        "allowed": False,
        "reason": "free_quota_exceeded",
        "sessions_remaining": 0,
    }


async def increment_free_sessions_used(user_id: str) -> None:
    await execute(
        """
        UPDATE public.free_usage
        SET sessions_used = sessions_used + 1
        WHERE user_id = $1
        """,
        user_id,
    )


# ── Session duration ───────────────────────────────────────────────────────

async def end_interview_session(user_id: str, session_id: str, duration_seconds: int) -> bool:
    """
    Marks session ended with duration. Returns True if row updated.
    """
    result = await execute(
        """
        UPDATE public.interview_sessions
        SET ended_at = now(),
            duration_seconds = $3
        WHERE id = $1::uuid AND user_id = $2::uuid AND ended_at IS NULL
        """,
        session_id,
        user_id,
        duration_seconds,
    )
    return result != "UPDATE 0"


# ── Stripe helpers ─────────────────────────────────────────────────────────

async def get_or_create_stripe_customer(user_id: str, email: str) -> str:
    """Returns existing stripe_customer_id or creates a new one."""
    sub = await get_subscription(user_id)
    if sub and sub.get("stripe_customer_id"):
        return sub["stripe_customer_id"]

    customer = stripe.Customer.create(email=email, metadata={"user_id": user_id})
    await execute(
        """
        UPDATE public.subscriptions
        SET stripe_customer_id = $2, updated_at = now()
        WHERE user_id = $1::uuid
        """,
        user_id,
        customer.id,
    )
    return customer.id


async def create_checkout_session(user_id: str, email: str) -> str:
    """Returns Stripe checkout URL."""
    customer_id = await get_or_create_stripe_customer(user_id, email)
    session = stripe.checkout.Session.create(
        customer=customer_id,
        mode="subscription",
        line_items=[{"price": settings.stripe_pro_price_id, "quantity": 1}],
        success_url=settings.stripe_success_url,
        cancel_url=settings.stripe_cancel_url,
        metadata={"user_id": user_id},
    )
    return session.url


async def create_portal_session(user_id: str) -> str | None:
    """Returns Stripe billing portal URL or None if no customer."""
    sub = await get_subscription(user_id)
    if not sub or not sub.get("stripe_customer_id"):
        return None
    session = stripe.billing_portal.Session.create(
        customer=sub["stripe_customer_id"],
        return_url=settings.stripe_cancel_url,
    )
    return session.url


# ── Webhook event handlers ─────────────────────────────────────────────────

async def handle_checkout_completed(event_data: dict) -> None:
    """Upgrade user to pro after successful checkout."""
    session = event_data["object"]
    user_id = session.get("metadata", {}).get("user_id")
    subscription_id = session.get("subscription")
    if not user_id or not subscription_id:
        return
    await execute(
        """
        UPDATE public.subscriptions
        SET plan_tier = 'pro',
            status = 'active',
            stripe_subscription_id = $2,
            updated_at = now()
        WHERE user_id = $1::uuid
        """,
        user_id,
        subscription_id,
    )


async def handle_invoice_paid(event_data: dict) -> None:
    """Refresh current_period_end on renewal."""
    invoice = event_data["object"]
    customer_id = invoice.get("customer")
    period_end = invoice.get("lines", {}).get("data", [{}])[0].get("period", {}).get("end")
    if not customer_id or not period_end:
        return
    import datetime
    period_end_dt = datetime.datetime.utcfromtimestamp(period_end).replace(tzinfo=datetime.timezone.utc)
    await execute(
        """
        UPDATE public.subscriptions
        SET status = 'active',
            current_period_end = $2,
            updated_at = now()
        WHERE stripe_customer_id = $1
        """,
        customer_id,
        period_end_dt,
    )


async def handle_invoice_payment_failed(event_data: dict) -> None:
    customer_id = event_data["object"].get("customer")
    if not customer_id:
        return
    await execute(
        """
        UPDATE public.subscriptions
        SET status = 'past_due', updated_at = now()
        WHERE stripe_customer_id = $1
        """,
        customer_id,
    )


async def handle_subscription_deleted(event_data: dict) -> None:
    """Downgrade back to free when subscription is cancelled."""
    subscription = event_data["object"]
    customer_id = subscription.get("customer")
    if not customer_id:
        return
    await execute(
        """
        UPDATE public.subscriptions
        SET plan_tier = 'free',
            status = 'active',
            stripe_subscription_id = NULL,
            current_period_end = NULL,
            updated_at = now()
        WHERE stripe_customer_id = $1
        """,
        customer_id,
    )
```

---

## Step 5 — Modify `app/main.py`

### 5a. Add imports at top of file

```python
from modules.billing import (
    provision_free_subscription,
    check_can_start_session,
    increment_free_sessions_used,
    end_interview_session,
    get_subscription,
    get_free_usage,
    create_checkout_session,
    create_portal_session,
    handle_checkout_completed,
    handle_invoice_paid,
    handle_invoice_payment_failed,
    handle_subscription_deleted,
    FREE_SESSION_LIMIT,
)
import stripe as stripe_lib
```

### 5b. New Pydantic models — add after existing models

```python
class EndSessionRequest(BaseModel):
    duration_seconds: int
```

### 5c. Patch `POST /signup` — provision subscription after user creation

Replace the current `/signup` handler body. After `response.user` is confirmed, add:

```python
@app.post("/signup")
async def signup(request: AuthRequest):
    try:
        response = await sign_up_user(request.email, request.password)
        if response.user:
            await provision_free_subscription(response.user.id)
            return {"message": "User created successfully", "user_id": response.user.id}
        raise HTTPException(status_code=400, detail="Signup failed")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
```

### 5d. Patch `_find_or_create_user` in `modules/auth_google.py`

In the `_find_or_create_user` function, after a **new user is inserted** (not found), call `provision_free_subscription`. Add import at top of that file:

```python
from modules.billing import provision_free_subscription
```

After the INSERT block that creates `new_row`, add:

```python
user_id_str = str(new_row["id"])
await provision_free_subscription(user_id_str)
return {"id": user_id_str, "email": new_row["email"]}
```

The existing-user branch (`if row:`) does NOT call provision — idempotency is handled by `ON CONFLICT DO NOTHING` in `provision_free_subscription` anyway, but we skip it for perf.

### 5e. Patch `POST /interview/session` — add quota gate

Find the existing `/interview/session` endpoint. Add quota check before creating the session:

```python
@app.post("/interview/session")
async def start_interview_session(
    request: InterviewSessionRequest,
    user_id: str = Depends(require_user_id),
):
    # Quota check
    quota = await check_can_start_session(user_id)
    if not quota["allowed"]:
        raise HTTPException(
            status_code=402,
            detail={
                "error": "free_quota_exceeded",
                "message": "Free sessions exhausted. Upgrade to Pro for unlimited sessions.",
                "sessions_used": FREE_SESSION_LIMIT,
                "sessions_remaining": 0,
            },
        )

    session_id = await create_interview_session(
        user_id,
        UUID(request.profile_id),
        request.job_title,
        request.job_description,
    )
    if not session_id:
        raise HTTPException(status_code=404, detail="Profile not found or does not belong to user")

    # Increment free usage counter only for free-tier sessions
    if quota["reason"] == "free":
        await increment_free_sessions_used(user_id)

    return {"session_id": str(session_id)}
```

Note: Check the exact current endpoint signature in `main.py` — the handler may currently use `authorization: str = Header(None)` pattern. Convert it to use `Depends(require_user_id)` for consistency if it doesn't already.

### 5f. Add `PATCH /interview/session/{session_id}/end`

```python
@app.patch("/interview/session/{session_id}/end")
async def end_session(
    session_id: str,
    body: EndSessionRequest,
    user_id: str = Depends(require_user_id),
):
    if body.duration_seconds < 0:
        raise HTTPException(status_code=422, detail="duration_seconds must be >= 0")
    updated = await end_interview_session(user_id, session_id, body.duration_seconds)
    if not updated:
        raise HTTPException(status_code=404, detail="Session not found, already ended, or not owned by user")
    return {"ok": True, "duration_seconds": body.duration_seconds}
```

### 5g. Add `GET /subscription/status`

```python
@app.get("/subscription/status")
async def subscription_status(user_id: str = Depends(require_user_id)):
    sub = await get_subscription(user_id)
    usage = await get_free_usage(user_id)

    plan = sub["plan_tier"] if sub else "free"
    status = sub["status"] if sub else "active"
    sessions_used = usage["sessions_used"] if usage else 0
    sessions_remaining = max(0, FREE_SESSION_LIMIT - sessions_used) if plan == "free" else None

    return {
        "plan_tier": plan,
        "status": status,
        "sessions_used": sessions_used if plan == "free" else None,
        "sessions_remaining": sessions_remaining,
        "free_session_limit": FREE_SESSION_LIMIT if plan == "free" else None,
        "current_period_end": sub["current_period_end"].isoformat() if sub and sub.get("current_period_end") else None,
    }
```

### 5h. Add `POST /subscription/checkout`

Requires user email — fetch from neon_auth.user:

```python
@app.post("/subscription/checkout")
async def subscription_checkout(user_id: str = Depends(require_user_id)):
    user_row = await fetch_one(
        'SELECT email FROM neon_auth."user" WHERE id = $1::uuid',
        user_id,
    )
    if not user_row:
        raise HTTPException(status_code=404, detail="User not found")
    url = await create_checkout_session(user_id, user_row["email"])
    return {"checkout_url": url}
```

Add `from modules.database import fetch_one` import if not already present.

### 5i. Add `POST /subscription/portal`

```python
@app.post("/subscription/portal")
async def subscription_portal(user_id: str = Depends(require_user_id)):
    url = await create_portal_session(user_id)
    if not url:
        raise HTTPException(status_code=400, detail="No billing account found. Complete a checkout first.")
    return {"portal_url": url}
```

### 5j. Add `POST /subscription/webhook` — NO AUTH, Stripe signature validation

```python
@app.post("/subscription/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        event = stripe_lib.Webhook.construct_event(
            payload, sig_header, settings.stripe_webhook_secret
        )
    except stripe_lib.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid Stripe signature")

    event_type = event["type"]
    event_data = event["data"]

    if event_type == "checkout.session.completed":
        await handle_checkout_completed(event_data)
    elif event_type == "invoice.paid":
        await handle_invoice_paid(event_data)
    elif event_type == "invoice.payment_failed":
        await handle_invoice_payment_failed(event_data)
    elif event_type == "customer.subscription.deleted":
        await handle_subscription_deleted(event_data)

    return {"received": True}
```

---

## Step 6 — Find existing `/interview/session` POST endpoint

Search `app/main.py` for the existing `POST /interview/session` handler. It currently likely looks like:

```python
@app.post("/interview/session")
async def start_interview_session(..., authorization: str = Header(None)):
    ...
    session_id = await create_interview_session(...)
    ...
```

Replace it entirely with the version in Step 5e above.

---

## Summary of Files Changed

| File | Change |
|------|--------|
| `migrations/add_free_usage_and_session_duration.sql` | CREATE free_usage, ALTER interview_sessions |
| `requirements.txt` | Add `stripe>=8.0.0` |
| `modules/config.py` | Add 5 Stripe env vars to Settings |
| `modules/billing.py` | **NEW FILE** — all billing logic |
| `modules/auth_google.py` | Call `provision_free_subscription` for new users |
| `app/main.py` | Patch `/signup`, patch `/interview/session`, add 5 new endpoints |

---

## API Surface After Implementation

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/subscription/status` | Bearer | Plan, usage, remaining sessions |
| POST | `/subscription/checkout` | Bearer | Returns Stripe checkout URL |
| POST | `/subscription/portal` | Bearer | Returns Stripe billing portal URL |
| POST | `/subscription/webhook` | None (Stripe sig) | Handles Stripe events |
| PATCH | `/interview/session/{id}/end` | Bearer | Stores session duration |
| POST | `/interview/session` | Bearer | **Modified** — now checks quota, returns 402 if exceeded |

---

## Stripe Webhook Events Handled

| Event | Action |
|-------|--------|
| `checkout.session.completed` | Set `plan_tier='pro'`, store `stripe_subscription_id` |
| `invoice.paid` | Set `status='active'`, update `current_period_end` |
| `invoice.payment_failed` | Set `status='past_due'` |
| `customer.subscription.deleted` | Downgrade to `plan_tier='free'`, clear Stripe IDs |

---

## Notes for Agent

1. Do NOT run migrations automatically — output the SQL and tell user to run it in Neon console or via psql.
2. The `subscriptions` table already exists — do NOT recreate it. Only run 1b and 1c migrations.
3. `user_id` in `neon_auth."user"` is UUID type but asyncpg returns it as `asyncpg.pgproto.UUID` — always cast with `$1::uuid` in queries targeting that table. For `free_usage.user_id` (TEXT), no cast needed.
4. The webhook endpoint must be excluded from any auth middleware — it uses Stripe signature validation instead.
5. `stripe_lib.Webhook.construct_event` is synchronous — no `await` needed.
6. Test quota logic: create user → confirm `subscriptions` + `free_usage` rows auto-created → start 3 sessions → 4th call must return 402.
