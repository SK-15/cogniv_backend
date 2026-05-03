# Payment & Subscription Integration Plan — Backend

## Context

- Framework: **FastAPI** + **asyncpg** (Neon PostgreSQL)
- Auth: Neon Auth (email/password) + Google OAuth — both converge at `require_user_id` dep in `app/main.py`, returns `user_id: str` (UUID as string)
- Modules pattern: each concern lives in `modules/<name>.py`, imported into `app/main.py`
- All DB access via helpers in `modules/database.py`: `fetch_one`, `fetch_all`, `execute`
- Payment: **Razorpay Standard Checkout** — backend creates subscription, frontend opens checkout.js modal, backend verifies HMAC signature

## Free Tier Rules

- Every new user gets **3 free interview sessions**, max **10 minutes each**
- Session count enforced server-side (block new session when quota hit)
- Session duration stored server-side (client calls end endpoint; no server-side kill)
- On quota exceeded: return HTTP `402` with `upgrade_url` field

---

## Step 1 — Database Migrations

### 1a. `subscriptions` table — ALREADY EXISTS, needs column rename

Rename Stripe columns to Razorpay equivalents:

```sql
ALTER TABLE public.subscriptions
    RENAME COLUMN stripe_customer_id TO razorpay_customer_id;

ALTER TABLE public.subscriptions
    RENAME COLUMN stripe_subscription_id TO razorpay_subscription_id;
```

Updated schema reference:
```
id                       UUID PRIMARY KEY DEFAULT gen_random_uuid()
user_id                  UUID UNIQUE NOT NULL REFERENCES neon_auth."user"(id) ON DELETE CASCADE
plan_tier                TEXT NOT NULL DEFAULT 'free'         -- 'free' | 'pro'
status                   TEXT NOT NULL DEFAULT 'active'       -- 'active' | 'cancelled' | 'past_due'
razorpay_customer_id     TEXT
razorpay_subscription_id TEXT
current_period_end       TIMESTAMP WITH TIME ZONE
created_at               TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
updated_at               TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
```

### 1b. `free_usage` table — CREATE THIS

```sql
CREATE TABLE IF NOT EXISTS public.free_usage (
    user_id       TEXT PRIMARY KEY REFERENCES neon_auth."user"(id) ON DELETE CASCADE,
    sessions_used INTEGER NOT NULL DEFAULT 0,
    created_at    TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);
```

Note: `user_id` is TEXT (not UUID) to match how asyncpg returns neon_auth user ids as strings.

### 1c. Alter `interview_sessions` — ADD COLUMN

```sql
ALTER TABLE public.interview_sessions
    ADD COLUMN IF NOT EXISTS duration_seconds INTEGER;
```

Save all three to `migrations/add_razorpay_and_free_usage.sql`.

---

## Step 2 — Environment Variables

Add to `.env`:

```
RAZORPAY_KEY_ID=rzp_test_SkpUjWU3TfWOeT
RAZORPAY_KEY_SECRET=UPCYaNnQdAw7QdTtXar1zIV8
RAZORPAY_WEBHOOK_SECRET=whsec_...
RAZORPAY_PRO_PLAN_ID=plan_...
```

Note: credentials above are test keys — replace with live keys (`rzp_live_...`) for production.
`KEY_SECRET` must never reach the frontend.

Add to `modules/config.py` `Settings` class:

```python
razorpay_key_id: str = ""
razorpay_key_secret: str = ""
razorpay_webhook_secret: str = ""
razorpay_pro_plan_id: str = ""
```

---

## Step 3 — Add razorpay to requirements.txt

```
razorpay>=1.4.0
```

---

## Step 4 — Create `modules/billing.py`

```python
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
    """Returns existing razorpay_customer_id or creates new one."""
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
    Creates a Razorpay subscription for the pro plan.
    Returns {subscription_id, key_id} for the frontend checkout modal.
    Frontend uses these to open checkout.js with subscription_id instead of order_id.
    """
    customer_id = await get_or_create_razorpay_customer(user_id, email)
    subscription = rz_client.subscription.create({
        "plan_id": settings.razorpay_pro_plan_id,
        "customer_notify": 1,
        "total_count": 120,       # 10 years of monthly billing
        "customer_id": customer_id,
        "notes": {"user_id": user_id},
    })
    subscription_id = subscription["id"]
    # Store pending subscription id so verify endpoint can match it
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
        "key_id": settings.razorpay_key_id,   # safe to expose — public key only
    }


def verify_payment_signature(payment_id: str, subscription_id: str, signature: str) -> bool:
    """
    Verifies Razorpay HMAC-SHA256 signature for subscription payments.
    Algorithm: HMAC-SHA256(payment_id + "|" + subscription_id, KEY_SECRET)
    NOTE: Order payments use order_id + "|" + payment_id — subscriptions are reversed.
    """
    message = f"{payment_id}|{subscription_id}"
    expected = hmac.new(
        settings.razorpay_key_secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


async def activate_pro(user_id: str) -> None:
    """Called after successful signature verification — upgrades user to pro."""
    await execute(
        """
        UPDATE public.subscriptions
        SET plan_tier = 'pro', status = 'active', updated_at = now()
        WHERE user_id = $1::uuid
        """,
        user_id,
    )


async def cancel_subscription(user_id: str) -> bool:
    """
    Cancels at period end. Returns False if no active subscription.
    Razorpay has no customer portal — this endpoint replaces it.
    """
    sub = await get_subscription(user_id)
    if not sub or not sub.get("razorpay_subscription_id"):
        return False
    rz_client.subscription.cancel(sub["razorpay_subscription_id"], {"cancel_at_cycle_end": 1})
    return True


# ── Webhook event handlers ─────────────────────────────────────────────────

async def handle_subscription_charged(event_data: dict) -> None:
    """Fired on each successful subscription renewal — refresh period_end."""
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
    """Fired when Razorpay halts subscription after repeated payment failures."""
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
    create_subscription,
    verify_payment_signature,
    activate_pro,
    cancel_subscription,
    handle_subscription_charged,
    handle_subscription_halted,
    handle_subscription_cancelled,
    FREE_SESSION_LIMIT,
)
import razorpay as razorpay_lib
from modules.config import settings
```

### 5b. New Pydantic models

```python
class EndSessionRequest(BaseModel):
    duration_seconds: int

class VerifyPaymentRequest(BaseModel):
    razorpay_payment_id: str
    razorpay_subscription_id: str
    razorpay_signature: str
```

### 5c. Patch `POST /signup`

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

Add import:

```python
from modules.billing import provision_free_subscription
```

After new user INSERT:

```python
user_id_str = str(new_row["id"])
await provision_free_subscription(user_id_str)
return {"id": user_id_str, "email": new_row["email"]}
```

### 5e. Patch `POST /interview/session` — quota gate

```python
@app.post("/interview/session")
async def start_interview_session(
    request: InterviewSessionRequest,
    user_id: str = Depends(require_user_id),
):
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

    if quota["reason"] == "free":
        await increment_free_sessions_used(user_id)

    return {"session_id": str(session_id)}
```

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

### 5h. Add `POST /payment/create-order`

Creates a Razorpay subscription and returns credentials for the frontend checkout modal.

```python
@app.post("/payment/create-order")
async def payment_create_order(user_id: str = Depends(require_user_id)):
    user_row = await fetch_one(
        'SELECT email FROM neon_auth."user" WHERE id = $1::uuid',
        user_id,
    )
    if not user_row:
        raise HTTPException(status_code=404, detail="User not found")
    data = await create_subscription(user_id, user_row["email"])
    return data   # {subscription_id, key_id}
```

### 5i. Add `POST /payment/verify`

Verifies HMAC signature after frontend checkout completes, then activates pro.

```python
@app.post("/payment/verify")
async def payment_verify(
    body: VerifyPaymentRequest,
    user_id: str = Depends(require_user_id),
):
    if not body.razorpay_payment_id or not body.razorpay_subscription_id or not body.razorpay_signature:
        raise HTTPException(status_code=400, detail="Missing payment fields")

    valid = verify_payment_signature(
        body.razorpay_payment_id,
        body.razorpay_subscription_id,
        body.razorpay_signature,
    )
    if not valid:
        raise HTTPException(status_code=400, detail="Signature mismatch — payment not verified")

    await activate_pro(user_id)
    return {"ok": True, "plan_tier": "pro"}
```

### 5j. Add `POST /subscription/cancel`

```python
@app.post("/subscription/cancel")
async def subscription_cancel(user_id: str = Depends(require_user_id)):
    cancelled = await cancel_subscription(user_id)
    if not cancelled:
        raise HTTPException(status_code=400, detail="No active subscription found.")
    return {"ok": True, "message": "Subscription will cancel at end of current billing period."}
```

### 5k. Add `POST /subscription/webhook` — NO AUTH, Razorpay signature validation

```python
@app.post("/subscription/webhook")
async def razorpay_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("x-razorpay-signature")

    try:
        rz = razorpay_lib.Client(auth=(settings.razorpay_key_id, settings.razorpay_key_secret))
        rz.utility.verify_webhook_signature(
            payload.decode("utf-8"),
            sig_header,
            settings.razorpay_webhook_secret,
        )
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid Razorpay signature")

    import json
    event = json.loads(payload)
    event_type = event.get("event")
    event_data = event.get("payload", {})

    if event_type == "subscription.charged":
        await handle_subscription_charged(event_data)
    elif event_type == "subscription.halted":
        await handle_subscription_halted(event_data)
    elif event_type == "subscription.cancelled":
        await handle_subscription_cancelled(event_data)

    return {"received": True}
```

---

## Step 6 — Frontend Integration

Frontend must include the Razorpay checkout script:

```html
<script src="https://checkout.razorpay.com/v1/checkout.js"></script>
```

On "Upgrade to Pro" click:

```javascript
// 1. Call backend to create subscription
const { subscription_id, key_id } = await fetch("/payment/create-order", {
  method: "POST",
  headers: { Authorization: `Bearer ${token}` },
}).then(r => r.json());

// 2. Open Razorpay modal
const rzp = new Razorpay({
  key: key_id,
  subscription_id: subscription_id,   // use subscription_id, NOT order_id
  name: "Your App Name",
  description: "Pro Plan",
  handler: async function (response) {
    // 3. Verify on backend
    await fetch("/payment/verify", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({
        razorpay_payment_id: response.razorpay_payment_id,
        razorpay_subscription_id: response.razorpay_subscription_id,
        razorpay_signature: response.razorpay_signature,
      }),
    });
    // 4. Refresh subscription status in UI
  },
  modal: {
    ondismiss: () => { /* user cancelled — show message */ },
  },
  prefill: { email: userEmail },
});
rzp.on("payment.failed", (response) => { /* show error */ });
rzp.open();
```

Environment variables for frontend (`key_id` only — never `key_secret`):
- Next.js: `NEXT_PUBLIC_RAZORPAY_KEY_ID`
- Vite: `VITE_RAZORPAY_KEY_ID`
- CRA: `REACT_APP_RAZORPAY_KEY_ID`

Alternatively, the backend returns `key_id` in the `/payment/create-order` response — no frontend env var needed.

---

## Summary of Files Changed

| File | Change |
|------|--------|
| `migrations/add_razorpay_and_free_usage.sql` | Rename stripe→razorpay columns, CREATE free_usage, ALTER interview_sessions |
| `requirements.txt` | Add `razorpay>=1.4.0` |
| `modules/config.py` | Add 4 Razorpay env vars to Settings |
| `modules/billing.py` | **NEW FILE** — all billing logic |
| `modules/auth_google.py` | Call `provision_free_subscription` for new users |
| `app/main.py` | Patch `/signup`, patch `/interview/session`, add 6 new endpoints |

---

## API Surface After Implementation

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/payment/create-order` | Bearer | Creates Razorpay subscription, returns `{subscription_id, key_id}` |
| POST | `/payment/verify` | Bearer | Verifies HMAC signature, activates pro |
| GET | `/subscription/status` | Bearer | Plan, usage, remaining sessions |
| POST | `/subscription/cancel` | Bearer | Cancels at period end |
| POST | `/subscription/webhook` | None (Razorpay sig) | Handles Razorpay lifecycle events |
| PATCH | `/interview/session/{id}/end` | Bearer | Stores session duration |
| POST | `/interview/session` | Bearer | **Modified** — quota check, returns 402 if exceeded |

---

## Razorpay Webhook Events Handled

| Event | Action |
|-------|--------|
| `subscription.charged` | Set `plan_tier='pro'`, `status='active'`, update `current_period_end` |
| `subscription.halted` | Set `status='past_due'` (too many payment failures) |
| `subscription.cancelled` | Downgrade to `plan_tier='free'`, clear Razorpay IDs |

---

## Payment Flow (End-to-End)

```
User clicks "Upgrade"
  → POST /payment/create-order      (backend creates Razorpay subscription, stores pending sub_id)
  → Frontend opens checkout.js modal with subscription_id
  → User pays
  → Razorpay calls handler with {payment_id, subscription_id, signature}
  → POST /payment/verify             (backend verifies HMAC, sets plan_tier='pro')
  → UI shows Pro status

On renewal (monthly):
  → Razorpay fires subscription.charged webhook
  → Backend refreshes current_period_end, keeps status='active'

On failure:
  → Razorpay fires subscription.halted after N retries
  → Backend sets status='past_due'

On cancellation:
  → POST /subscription/cancel        (backend calls Razorpay cancel_at_cycle_end=1)
  → Razorpay fires subscription.cancelled at period end
  → Backend downgrades to plan_tier='free'
```

---

## Notes for Agent

1. Do NOT run migrations automatically — output SQL and tell user to run in Neon console.
2. `subscriptions` table already exists — only run 1a (rename), 1b, 1c.
3. `user_id` in `neon_auth."user"` is UUID — always cast `$1::uuid`. For `free_usage.user_id` (TEXT), no cast.
4. `/subscription/webhook` must be excluded from auth middleware — uses Razorpay signature instead.
5. `verify_webhook_signature()` is synchronous — no `await`.
6. Signature algorithm differs: subscriptions use `payment_id + "|" + subscription_id` (not `order_id + "|" + payment_id` which is for one-time orders).
7. Test credentials in `.env`: `RAZORPAY_KEY_ID=rzp_test_SkpUjWU3TfWOeT` / `RAZORPAY_KEY_SECRET=UPCYaNnQdAw7QdTtXar1zIV8`. Replace with `rzp_live_...` for production.
8. Razorpay plans must be pre-created in the Razorpay dashboard — `RAZORPAY_PRO_PLAN_ID` must match a real plan ID.
9. Test quota: create user → 3 sessions → 4th returns 402. Test payment: use Razorpay test card `4111 1111 1111 1111`.
