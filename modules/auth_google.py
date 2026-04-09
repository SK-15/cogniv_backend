import urllib.parse
import datetime

import httpx
import jwt
from fastapi import APIRouter, Query
from fastapi.responses import RedirectResponse
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from modules.config import settings
from modules.database import fetch_one, execute

router = APIRouter()

GOOGLE_AUTH_URL     = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL    = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
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

    # Let the DB generate the id; supply emailVerified=true since Google has verified it.
    new_row = await fetch_one(
        '''
        INSERT INTO neon_auth."user" (name, email, "emailVerified")
        VALUES ($1, $2, true)
        RETURNING id, email
        ''',
        name, email,
    )
    return {"id": str(new_row["id"]), "email": new_row["email"]}


@router.get("/auth/google/start")
def google_start(local_port: int = Query(..., description="Loopback port the Electron app is listening on")):
    """
    Redirect the browser to Google's OAuth consent screen.
    local_port is signed into the state so the callback knows where to redirect.
    """
    state = _signer.dumps({"nonce": "cogniv-oauth", "port": local_port})
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

    def _error_redirect(port: int | None, msg: str) -> RedirectResponse:
        params = urllib.parse.urlencode({"error": msg})
        base = f"http://127.0.0.1:{port}/callback" if port else "http://127.0.0.1/callback"
        return RedirectResponse(f"{base}?{params}", status_code=302)

    # 1. Verify CSRF state and extract local_port before anything else
    local_port: int | None = None
    if state:
        try:
            payload = _signer.loads(state, max_age=600)
            local_port = int(payload["port"])
        except (BadSignature, SignatureExpired, KeyError, TypeError, ValueError):
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

    if not email:
        return _error_redirect(local_port, "no_email_from_google")

    # 5. Find or create the user in the database
    user = await _find_or_create_user(email=email, name=name)

    # 6. Mint a JWT access token compatible with require_user_id (HS256)
    access_token  = _mint_access_token(user["id"], user["email"])
    refresh_token = google_tokens.get("refresh_token", "")

    # 7. Redirect to the Electron loopback server
    params = urllib.parse.urlencode({
        "access_token":  access_token,
        "refresh_token": refresh_token,
        "user_id":       user["id"],
    })
    return RedirectResponse(f"http://127.0.0.1:{local_port}/callback?{params}", status_code=302)
