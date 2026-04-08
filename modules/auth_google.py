import uuid
import urllib.parse
import datetime

import httpx
import jwt
from fastapi import APIRouter
from fastapi.responses import RedirectResponse
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from modules.config import settings
from modules.database import fetch_one, execute

router = APIRouter()

GOOGLE_AUTH_URL     = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL    = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
ELECTRON_CALLBACK   = "cogniv://auth/callback"
JWT_ALGORITHM       = "HS256"
ACCESS_TOKEN_EXPIRE = datetime.timedelta(hours=1)

_signer = URLSafeTimedSerializer(settings.app_secret_key or "fallback-key")


def _mint_access_token(user_id: str, email: str) -> str:
    now = datetime.datetime.utcnow()
    payload = {
        "sub":   user_id,
        "email": email,
        "iat":   now,
        "exp":   now + ACCESS_TOKEN_EXPIRE,
    }
    return jwt.encode(payload, settings.app_secret_key, algorithm=JWT_ALGORITHM)


async def _find_or_create_user(email: str, name: str) -> dict:
    row = await fetch_one(
        'SELECT id, email FROM neon_auth."user" WHERE email = $1',
        email,
    )
    if row:
        return {"id": str(row["id"]), "email": row["email"]}

    new_id = str(uuid.uuid4())
    await execute(
        'INSERT INTO neon_auth."user" (id, email, name) VALUES ($1, $2, $3)',
        new_id, email, name,
    )
    return {"id": new_id, "email": email}


@router.get("/auth/google/start")
def google_start():
    """Redirect the browser to Google's OAuth consent screen."""
    state = _signer.dumps("cogniv-oauth")
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
    find-or-create the user, and redirect to the Electron deep link.
    """

    def _error_redirect(msg: str) -> RedirectResponse:
        params = urllib.parse.urlencode({"error": msg})
        return RedirectResponse(f"{ELECTRON_CALLBACK}?{params}", status_code=302)

    # 1. User denied access
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
            "client_id":     settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "redirect_uri":  settings.google_redirect_uri,
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
    email = profile.get("email")
    name  = profile.get("name", "")

    if not email:
        return _error_redirect("no_email_from_google")

    # 5. Find or create the user in the database
    user = await _find_or_create_user(email=email, name=name)

    # 6. Mint a JWT access token compatible with require_user_id (HS256)
    access_token  = _mint_access_token(user["id"], user["email"])
    refresh_token = google_tokens.get("refresh_token", "")

    # 7. Redirect to Electron via cogniv:// deep link
    params = urllib.parse.urlencode({
        "access_token":  access_token,
        "refresh_token": refresh_token,
        "user_id":       user["id"],
    })
    return RedirectResponse(f"{ELECTRON_CALLBACK}?{params}", status_code=302)
