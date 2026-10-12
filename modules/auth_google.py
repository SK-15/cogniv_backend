import asyncio
import urllib.parse

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from pydantic import BaseModel

from modules.config import settings
from modules.database import fetch_one, execute
from modules.billing import provision_free_subscription
from modules.app_tokens import mint_access_token, issue_refresh_token, require_app_secret_key
from modules.rate_limit import limiter

router = APIRouter()

GOOGLE_AUTH_URL     = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL    = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

_signer = URLSafeTimedSerializer(require_app_secret_key())

# Optional per-login value from the desktop app, echoed back on the loopback
# redirect so the app can reject a callback it did not start (login CSRF).
DESKTOP_NONCE_PATTERN = r"^[A-Za-z0-9_-]{32,128}$"


def _email_verified(value) -> bool:
    """Google sends email_verified as a bool (userinfo) or "true" (older id_tokens)."""
    return value is True or str(value).lower() == "true"


async def refresh_google_oauth_tokens(refresh_token: str) -> dict | None:
    """
    Use Google's OAuth2 refresh_token to obtain a new app access_token (HS256) and
    optionally a rotated Google refresh_token. Returns dict for JSON or None on failure.
    """
    if not refresh_token.strip():
        return None
    if not settings.google_client_id or not settings.google_client_secret:
        return None

    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
    if token_resp.status_code != 200:
        return None

    google_tokens = token_resp.json()
    google_access = google_tokens.get("access_token")
    if not google_access:
        return None

    async with httpx.AsyncClient() as client:
        profile_resp = await client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {google_access}"},
        )
    if profile_resp.status_code != 200:
        return None

    profile = profile_resp.json()
    email = profile.get("email")
    name = profile.get("name", "")
    image = profile.get("picture", "")
    if not email or not _email_verified(profile.get("email_verified")):
        return None

    user = await _find_or_create_user(email=email, name=name, image=image)
    access_token = mint_access_token(user["id"], user["email"])

    return {
        "access_token": access_token,
        "user_id": user["id"],
    }


async def _find_or_create_user(email: str, name: str, image: str = "") -> dict:
    # Callers must only pass an email Google reports as verified: the lookup
    # links to any existing account with that email.
    row = await fetch_one(
        'SELECT id, email FROM neon_auth."user" WHERE email = $1',
        email,
    )
    if row:
        user_id_str = str(row["id"])
        # Backfill name/image from the latest Google profile without overwriting
        # an existing value with a blank one.
        await execute(
            '''
            UPDATE neon_auth."user"
            SET name = COALESCE(NULLIF($2, ''), name),
                image = COALESCE(NULLIF($3, ''), image),
                "updatedAt" = now()
            WHERE id = $1::uuid
            ''',
            user_id_str, name, image,
        )
        return {"id": user_id_str, "email": row["email"]}

    # Let the DB generate the id; supply emailVerified=true since Google has verified it.
    new_row = await fetch_one(
        '''
        INSERT INTO neon_auth."user" (name, email, "emailVerified", image)
        VALUES ($1, $2, true, NULLIF($3, ''))
        RETURNING id, email
        ''',
        name, email, image,
    )
    user_id_str = str(new_row["id"])
    await provision_free_subscription(user_id_str)
    return {"id": user_id_str, "email": new_row["email"]}


@router.get("/auth/google/start/web")
def google_start_web():
    state = _signer.dumps({"nonce": "cogniv-oauth", "source": "web"})
    params = {
        "client_id":     settings.google_client_id,
        "redirect_uri":  settings.google_redirect_uri_web,
        "response_type": "code",
        "scope":         "openid email profile",
        "access_type":   "offline",
        "prompt":        "select_account",
        "state":         state,
    }
    url = GOOGLE_AUTH_URL + "?" + urllib.parse.urlencode(params)
    return RedirectResponse(url, status_code=302)


@router.get("/auth/google/callback/web")
async def google_callback_web(
    code: str = None,
    state: str = None,
    error: str = None,
):
    WEB_FRONTEND_CALLBACK = "https://cogniv.co.in/auth/callback"

    def _error_redirect(msg: str) -> RedirectResponse:
        params = urllib.parse.urlencode({"error": msg})
        return RedirectResponse(f"{WEB_FRONTEND_CALLBACK}?{params}", status_code=302)

    if state:
        try:
            _signer.loads(state, max_age=600)
        except (BadSignature, SignatureExpired):
            return _error_redirect("invalid_state")

    if error:
        return _error_redirect(error)
    if not code or not state:
        return _error_redirect("missing_code_or_state")

    async with httpx.AsyncClient() as client:
        token_resp = await client.post(GOOGLE_TOKEN_URL, data={
            "code":          code,
            "client_id":     settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "redirect_uri":  settings.google_redirect_uri_web,
            "grant_type":    "authorization_code",
        })
    if token_resp.status_code != 200:
        return _error_redirect("token_exchange_failed")

    google_tokens = token_resp.json()
    google_access_token = google_tokens.get("access_token")
    if not google_access_token:
        return _error_redirect("no_access_token")

    async with httpx.AsyncClient() as client:
        profile_resp = await client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {google_access_token}"},
        )
    if profile_resp.status_code != 200:
        return _error_redirect("profile_fetch_failed")

    profile = profile_resp.json()
    email = profile.get("email")
    name  = profile.get("name", "")
    image = profile.get("picture", "")
    if not email:
        return _error_redirect("no_email_from_google")
    if not _email_verified(profile.get("email_verified")):
        return _error_redirect("email_not_verified")

    user = await _find_or_create_user(email=email, name=name, image=image)
    access_token  = mint_access_token(user["id"], user["email"])
    refresh_token = await issue_refresh_token(user["id"])

    fragment = urllib.parse.urlencode({
        "access_token":  access_token,
        "refresh_token": refresh_token,
        "user_id":       user["id"],
    })
    return RedirectResponse(f"{WEB_FRONTEND_CALLBACK}#{fragment}", status_code=302)


@router.get("/auth/google/start")
def google_start(
    local_port: int = Query(..., description="Loopback port the Electron app is listening on"),
    nonce: str | None = Query(
        None,
        pattern=DESKTOP_NONCE_PATTERN,
        description="Optional random value (32-128 URL-safe chars) echoed back on the loopback redirect.",
    ),
):
    """
    Redirect the browser to Google's OAuth consent screen.
    local_port (and the app's nonce, if sent) is signed into the state so the
    callback knows where to redirect.
    """
    payload = {"nonce": "cogniv-oauth", "port": local_port}
    if nonce:
        payload["app_nonce"] = nonce
    state = _signer.dumps(payload)
    params = {
        "client_id":     settings.google_client_id,
        "redirect_uri":  settings.google_redirect_uri,
        "response_type": "code",
        "scope":         "openid email profile",
        "access_type":   "offline",
        "prompt":        "select_account",
        "state":         state,
    }
    url = GOOGLE_AUTH_URL + "?" + urllib.parse.urlencode(params)
    return RedirectResponse(url, status_code=302)


@router.get("/auth/google/callback")
async def google_callback(
    code: str = None,
    state: str = None,
    error: str = None,
):
    """
    Receive the OAuth code from Google, exchange it for user info,
    find-or-create the user, and redirect to http://127.0.0.1:{port}/callback
    (the loopback server the Electron app started before opening the browser).
    """

    app_nonce: str | None = None

    def _loopback_redirect(port: int | None, params: dict) -> RedirectResponse:
        if app_nonce:
            params = {**params, "nonce": app_nonce}
        base = f"http://127.0.0.1:{port}/callback" if port else "http://127.0.0.1/callback"
        return RedirectResponse(f"{base}?{urllib.parse.urlencode(params)}", status_code=302)

    def _error_redirect(port: int | None, msg: str) -> RedirectResponse:
        return _loopback_redirect(port, {"error": msg})

    # 1. Verify CSRF state and extract local_port before anything else
    local_port: int | None = None
    if state:
        try:
            payload = _signer.loads(state, max_age=600)
            local_port = int(payload["port"])
            app_nonce = payload.get("app_nonce")
        except (BadSignature, SignatureExpired, KeyError, TypeError, ValueError, AttributeError):
            return _error_redirect(None, "invalid_state")

    # 2. Handle user-denied or missing params (port now known for error redirect)
    if error:
        return _error_redirect(local_port, error)
    if not code or not state:
        return _error_redirect(local_port, "missing_code_or_state")
    if not local_port:
        return _error_redirect(None, "missing_port_in_state")

    # 3. Exchange authorization code for Google tokens
    async with httpx.AsyncClient() as client:
        token_resp = await client.post(GOOGLE_TOKEN_URL, data={
            "code":          code,
            "client_id":     settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "redirect_uri":  settings.google_redirect_uri,
            "grant_type":    "authorization_code",
        })
    if token_resp.status_code != 200:
        return _error_redirect(local_port, "token_exchange_failed")

    google_tokens = token_resp.json()
    google_access_token = google_tokens.get("access_token")
    if not google_access_token:
        return _error_redirect(local_port, "no_access_token")

    # 4. Fetch the user's Google profile
    async with httpx.AsyncClient() as client:
        profile_resp = await client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {google_access_token}"},
        )
    if profile_resp.status_code != 200:
        return _error_redirect(local_port, "profile_fetch_failed")

    profile = profile_resp.json()
    email = profile.get("email")
    name  = profile.get("name", "")
    image = profile.get("picture", "")

    if not email:
        return _error_redirect(local_port, "no_email_from_google")
    if not _email_verified(profile.get("email_verified")):
        return _error_redirect(local_port, "email_not_verified")

    # 5. Find or create the user in the database
    user = await _find_or_create_user(email=email, name=name, image=image)

    # 6. Mint a JWT access token compatible with require_user_id (HS256)
    access_token  = mint_access_token(user["id"], user["email"])
    refresh_token = await issue_refresh_token(user["id"])

    # 7. Redirect to the Electron loopback server
    return _loopback_redirect(local_port, {
        "access_token":  access_token,
        "refresh_token": refresh_token,
        "user_id":       user["id"],
    })


def _accepted_mobile_audiences() -> list[str]:
    """Client IDs accepted as the `aud` of a native Google Sign-In id_token."""
    candidates = [
        settings.google_android_client_id,
        settings.google_ios_client_id,
        settings.google_client_id,
    ]
    return [c for c in candidates if c]


def _verify_google_id_token(token: str) -> dict:
    """
    Verify a Google-issued id_token from the native Sign-In SDK.

    Validates the signature against Google's public certs and checks `iss`,
    `aud` (against the configured native client IDs) and expiry. Returns the
    decoded claims, or raises ValueError on any failure.
    """
    audiences = _accepted_mobile_audiences()
    if not audiences:
        raise ValueError("no_mobile_client_id_configured")

    claims = google_id_token.verify_oauth2_token(
        token,
        google_requests.Request(),
        audience=None,
    )

    if claims.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
        raise ValueError("invalid_issuer")
    if claims.get("aud") not in audiences:
        raise ValueError("invalid_audience")
    if not claims.get("email"):
        raise ValueError("no_email")
    if not _email_verified(claims.get("email_verified")):
        raise ValueError("email_not_verified")
    return claims


class GoogleMobileRequest(BaseModel):
    id_token: str


@router.post("/auth/google/mobile")
@limiter.limit("10/minute")
async def google_mobile(request: Request, body: GoogleMobileRequest):
    """
    Native mobile Google Sign-In.

    The app obtains an `id_token` on-device via the Google Sign-In SDK and posts
    it here. The backend verifies the token, find-or-creates the user, and
    returns the same unified session tokens as the other auth flows.
    """
    token = (body.id_token or "").strip()
    if not token:
        raise HTTPException(status_code=422, detail="id_token is required")

    try:
        claims = await asyncio.to_thread(_verify_google_id_token, token)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    except Exception:
        raise HTTPException(status_code=401, detail="invalid_id_token")

    email = claims.get("email")
    name = claims.get("name", "")
    image = claims.get("picture", "")

    user = await _find_or_create_user(email=email, name=name, image=image)
    access_token = mint_access_token(user["id"], user["email"])
    refresh_token = await issue_refresh_token(user["id"])

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "user_id": user["id"],
    }
