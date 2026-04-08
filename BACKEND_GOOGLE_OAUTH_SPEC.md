# Backend Spec: Google OAuth Endpoints

> **For the Python backend agent** — implement these two new endpoints on the existing FastAPI (or equivalent) Python API currently deployed at `https://adcrkz336r.ap-south-1.awsapprunner.com`.

---

## Context

The Cogniv Electron desktop app uses email/password auth via `/signup` and `/login`. We are adding **Google one-click sign-in**. The Electron app cannot do a browser redirect natively, so the flow is:

1. Electron opens a `BrowserWindow` pointing to `GET /auth/google/start`
2. Backend redirects the browser to Google's OAuth consent screen
3. Google redirects back to `GET /auth/google/callback?code=...`
4. Backend exchanges the code, finds or creates the user, issues a JWT
5. Backend performs a **final redirect to the custom scheme** `cogniv://auth/callback?access_token=...&refresh_token=...&user_id=...`
6. Electron intercepts the `cogniv://` URL, extracts the tokens, and stores them

---

## New Environment Variables Required

Add these to your App Runner (or `.env`) configuration:

| Variable | Description |
|----------|-------------|
| `GOOGLE_CLIENT_ID` | From Google Cloud Console → OAuth 2.0 Client ID |
| `GOOGLE_CLIENT_SECRET` | From Google Cloud Console → OAuth 2.0 Client Secret |
| `GOOGLE_REDIRECT_URI` | Must exactly match what's registered in Google Cloud Console: `https://adcrkz336r.ap-south-1.awsapprunner.com/auth/google/callback` |
| `APP_SECRET_KEY` | A random 32+ character string used to sign state parameters (use `secrets.token_hex(32)` if one does not exist already) |

---

## Google Cloud Console Setup (done by the app developer, not this agent)

- Create an **OAuth 2.0 Client ID** (type: Web application)
- Add Authorized redirect URI: `https://adcrkz336r.ap-south-1.awsapprunner.com/auth/google/callback`
- Enable the **Google People API** (or Google+ API) for profile/email access

---

## Dependencies to Add

```txt
# requirements.txt additions
httpx           # or requests — for token exchange HTTP call
python-jose[cryptography]   # JWT signing (if not already present)
itsdangerous    # CSRF state token signing
```

---

## Endpoint 1 — `GET /auth/google/start`

**Purpose:** Redirect the browser to Google's OAuth consent screen.

### Request
No body or query params required.

### Response
HTTP `302 Redirect` → Google OAuth authorization URL

### Implementation (FastAPI)

```python
import os
import urllib.parse
from fastapi import APIRouter
from fastapi.responses import RedirectResponse
from itsdangerous import URLSafeTimedSerializer

router = APIRouter()

GOOGLE_CLIENT_ID     = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_REDIRECT_URI  = os.environ["GOOGLE_REDIRECT_URI"]
APP_SECRET_KEY       = os.environ["APP_SECRET_KEY"]

_signer = URLSafeTimedSerializer(APP_SECRET_KEY)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"

@router.get("/auth/google/start")
def google_start():
    # CSRF protection: sign a random nonce into the state param
    state = _signer.dumps("cogniv-oauth")

    params = {
        "client_id":     GOOGLE_CLIENT_ID,
        "redirect_uri":  GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope":         "openid email profile",
        "access_type":   "offline",   # request refresh_token
        "prompt":        "select_account",
        "state":         state,
    }
    url = GOOGLE_AUTH_URL + "?" + urllib.parse.urlencode(params)
    return RedirectResponse(url, status_code=302)
```

---

## Endpoint 2 — `GET /auth/google/callback`

**Purpose:** Receive the OAuth `code` from Google, exchange it for user info, find-or-create the user in the database, issue a JWT access token, and redirect to `cogniv://auth/callback`.

### Request (query params from Google)
| Param | Description |
|-------|-------------|
| `code` | OAuth authorization code |
| `state` | Signed state nonce (must be verified) |
| `error` | Present only if the user denied access |

### Final redirect (on success)
```
cogniv://auth/callback?access_token=<JWT>&refresh_token=<token>&user_id=<uuid>
```

### Final redirect (on error)
```
cogniv://auth/callback?error=<message>
```

### Implementation (FastAPI)

```python
import os, uuid, urllib.parse
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from jose import jwt
import datetime

# ── Config ────────────────────────────────────────────────────────────────────

GOOGLE_CLIENT_ID     = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
GOOGLE_REDIRECT_URI  = os.environ["GOOGLE_REDIRECT_URI"]
APP_SECRET_KEY       = os.environ["APP_SECRET_KEY"]

GOOGLE_TOKEN_URL     = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL  = "https://www.googleapis.com/oauth2/v3/userinfo"

ELECTRON_CALLBACK    = "cogniv://auth/callback"
JWT_ALGORITHM        = "HS256"
ACCESS_TOKEN_EXPIRE  = datetime.timedelta(hours=1)

_signer = URLSafeTimedSerializer(APP_SECRET_KEY)

# ── Helper: mint access token (same shape as /login) ─────────────────────────

def _mint_access_token(user_id: str, email: str) -> str:
    """
    Produce a JWT that is structurally identical to what /login returns so that
    the existing BackendApiService.extractAccessTokenFromLoginResult() picks it up.
    Adjust the secret / algorithm to match whatever /login uses today.
    """
    now = datetime.datetime.utcnow()
    payload = {
        "sub":   user_id,
        "email": email,
        "iat":   now,
        "exp":   now + ACCESS_TOKEN_EXPIRE,
    }
    return jwt.encode(payload, APP_SECRET_KEY, algorithm=JWT_ALGORITHM)

# ── Helper: find-or-create user ───────────────────────────────────────────────

def _find_or_create_user(db, email: str, google_id: str, name: str) -> dict:
    """
    Look up the user by email in the database.
    If not found, insert a new row into neon_auth."user" (or your users table).
    Returns {"id": <uuid str>, "email": <str>}.

    Adapt the DB calls to match your existing DB client (supabase-py, asyncpg, SQLAlchemy, etc.).

    IMPORTANT: The neon_auth."user" table schema is:
      id         uuid  primary key default gen_random_uuid()
      email      text  unique not null
      name       text
      created_at timestamptz default now()
      updated_at timestamptz default now()

    If you use supabase-py:
      result = supabase.table('neon_auth.user').select('*').eq('email', email).execute()
    Adjust the table path / schema prefix as your client requires.
    """
    # --- replace the block below with your actual DB logic ---
    existing = db.execute(
        'SELECT id, email FROM neon_auth."user" WHERE email = %s', (email,)
    ).fetchone()

    if existing:
        return {"id": str(existing["id"]), "email": existing["email"]}

    new_id = str(uuid.uuid4())
    db.execute(
        'INSERT INTO neon_auth."user" (id, email, name) VALUES (%s, %s, %s)',
        (new_id, email, name),
    )
    db.commit()
    return {"id": new_id, "email": email}

# ── Endpoint ──────────────────────────────────────────────────────────────────

@router.get("/auth/google/callback")
async def google_callback(request: Request, code: str = None, state: str = None, error: str = None):

    def _error_redirect(msg: str):
        params = urllib.parse.urlencode({"error": msg})
        return RedirectResponse(f"{ELECTRON_CALLBACK}?{params}", status_code=302)

    # 1. Handle user-denied
    if error:
        return _error_redirect(error)
    if not code or not state:
        return _error_redirect("missing_code_or_state")

    # 2. Verify CSRF state (max age 10 minutes)
    try:
        _signer.loads(state, max_age=600)
    except (BadSignature, SignatureExpired):
        return _error_redirect("invalid_state")

    # 3. Exchange authorization code for Google tokens
    async with httpx.AsyncClient() as client:
        token_resp = await client.post(GOOGLE_TOKEN_URL, data={
            "code":          code,
            "client_id":     GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri":  GOOGLE_REDIRECT_URI,
            "grant_type":    "authorization_code",
        })
    if token_resp.status_code != 200:
        return _error_redirect("token_exchange_failed")

    google_tokens = token_resp.json()
    google_access_token = google_tokens.get("access_token")
    if not google_access_token:
        return _error_redirect("no_access_token")

    # 4. Fetch the user's Google profile
    async with httpx.AsyncClient() as client:
        profile_resp = await client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {google_access_token}"},
        )
    if profile_resp.status_code != 200:
        return _error_redirect("profile_fetch_failed")

    profile = profile_resp.json()
    email     = profile.get("email")
    google_id = profile.get("sub")
    name      = profile.get("name", "")

    if not email:
        return _error_redirect("no_email_from_google")

    # 5. Find or create the user in your database
    #    Replace `get_db()` with however you obtain a DB connection in your app.
    db = get_db()
    user = _find_or_create_user(db, email=email, google_id=google_id, name=name)

    # 6. Mint a JWT access token (same shape as /login response)
    access_token  = _mint_access_token(user["id"], user["email"])
    #   If your /login already returns a refresh_token, create one here too.
    #   Otherwise pass an empty string — the Electron client handles missing refresh_token gracefully.
    refresh_token = google_tokens.get("refresh_token", "")

    # 7. Redirect back to the Electron app via cogniv:// deep link
    params = urllib.parse.urlencode({
        "access_token":  access_token,
        "refresh_token": refresh_token,
        "user_id":       user["id"],
    })
    return RedirectResponse(f"{ELECTRON_CALLBACK}?{params}", status_code=302)
```

---

## Important Notes for the Backend Agent

### Token format must match `/login`
The existing Electron code in `backendApiService.js` calls `extractAccessTokenFromLoginResult()`, which looks for `result.access_token`. The `cogniv://` URL param is named `access_token` to match this. Make sure the JWT you mint uses the **same signing secret and algorithm** as the one `/login` already uses — otherwise protected endpoints will return 401.

### DB client adaptation
`_find_or_create_user` is shown with raw SQL for clarity. Adapt it to whatever DB client is already in the project (supabase-py, asyncpg, SQLAlchemy, psycopg2, etc.). The table is `neon_auth."user"` — note the schema-qualified name and the double-quotes around `user` (it is a reserved keyword in SQL).

### Register the router
Add to your main `app.py` / `main.py`:
```python
from auth_google import router as google_router
app.include_router(google_router)
```

### CORS / security
`/auth/google/start` and `/auth/google/callback` are browser-facing (opened in a BrowserWindow). They do **not** need CORS headers — they respond with `302 Redirect`, not JSON.

### Testing the flow locally
1. Temporarily change `GOOGLE_REDIRECT_URI` to `http://localhost:8000/auth/google/callback`
2. Update the authorized redirect URI in Google Cloud Console to match
3. Open `http://localhost:8000/auth/google/start` in a browser
4. After consent, confirm the browser is redirected to `cogniv://auth/callback?access_token=...`

---

## Summary of Changes

| File | Change |
|------|--------|
| `requirements.txt` | Add `httpx`, `python-jose[cryptography]`, `itsdangerous` |
| `auth_google.py` (new) | Two endpoints above |
| `main.py` / `app.py` | `app.include_router(google_router)` |
| App Runner env vars | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI`, `APP_SECRET_KEY` |
