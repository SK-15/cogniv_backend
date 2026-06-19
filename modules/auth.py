import asyncio
import base64
import time
from types import SimpleNamespace
from urllib.parse import urlparse

import jwt
import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from jwt import PyJWTError

from modules.config import settings
from modules.database import get_pool


_JWKS_CACHE: dict | None = None
_JWKS_CACHE_AT: float = 0.0
_JWKS_CACHE_TTL_SECONDS = 3600


def _auth_origin() -> str:
    parsed = urlparse(settings.neon_auth_base_url)
    return f"{parsed.scheme}://{parsed.netloc}"


async def _fetch_jwks() -> dict:
    """
    Fetch and cache Neon Auth JWKS keys used for JWT verification.
    """
    global _JWKS_CACHE, _JWKS_CACHE_AT

    now = time.time()
    if _JWKS_CACHE is not None and (now - _JWKS_CACHE_AT) < _JWKS_CACHE_TTL_SECONDS:
        return _JWKS_CACHE

    jwks_url = f"{settings.neon_auth_base_url}/.well-known/jwks.json"

    def _do_fetch():
        resp = requests.get(jwks_url, timeout=10)
        resp.raise_for_status()
        return resp.json()

    _JWKS_CACHE = await asyncio.to_thread(_do_fetch)
    _JWKS_CACHE_AT = time.time()
    return _JWKS_CACHE


async def _validate_token(token: str) -> str | None:
    """
    Verify a Neon Auth JWT and return the user id (sub).
    """
    try:
        jwks = await _fetch_jwks()
        unverified_header = jwt.get_unverified_header(token)
        kid = unverified_header.get("kid")
        if not kid:
            return None

        matching_jwk = None
        for jwk in jwks.get("keys", []):
            if jwk.get("kid") == kid:
                matching_jwk = jwk
                break

        if not matching_jwk:
            return None

        # Neon Auth uses EdDSA/Ed25519, and the JWKS provides the "x" coordinate.
        x = matching_jwk.get("x")
        if not x:
            return None

        public_key_bytes = base64.urlsafe_b64decode(x + "==")
        signing_key = Ed25519PublicKey.from_public_bytes(public_key_bytes)

        origin = _auth_origin()
        payload = jwt.decode(
            token,
            key=signing_key,
            algorithms=["EdDSA"],
            issuer=origin,
            audience=origin,
        )

        # Neon Auth JWT payload typically uses "sub" and also includes user fields.
        return payload.get("sub") or payload.get("id")
    except PyJWTError:
        return None
    except Exception:
        return None


async def _post_json(url: str, body: dict, origin_header: str | None = None) -> dict:
    def _do_post():
        headers = {}
        if origin_header:
            # Neon Auth requires `Origin` for signup when callbackURL isn't absolute.
            headers["Origin"] = origin_header
        resp = requests.post(url, json=body, headers=headers, timeout=20)
        resp.raise_for_status()
        return resp.json()

    return await asyncio.to_thread(_do_post)


async def _post_json_optional(
    url: str, body: dict, origin_header: str | None = None
) -> dict | None:
    """POST JSON; return None on non-2xx or parse errors."""

    def _do_post():
        headers = {}
        if origin_header:
            headers["Origin"] = origin_header
        resp = requests.post(url, json=body, headers=headers, timeout=20)
        if resp.status_code < 200 or resp.status_code >= 300:
            return None
        try:
            return resp.json()
        except Exception:
            return None

    return await asyncio.to_thread(_do_post)


def _normalize_neon_auth_session_payload(data: dict) -> SimpleNamespace:
    """
    Map Neon / Better Auth sign-in or refresh JSON to user + session tokens.
    """
    extracted = data.get("data", data)
    user = extracted.get("user") or {}

    token_top_level = extracted.get("token")
    session = extracted.get("session") or {}

    access_token = (
        session.get("access_token")
        or session.get("accessToken")
        or token_top_level
        or session.get("token")
        or extracted.get("accessToken")
        or extracted.get("access_token")
    )
    refresh_token = (
        session.get("refresh_token")
        or session.get("refreshToken")
        or extracted.get("refreshToken")
        or extracted.get("refresh_token")
    )

    uid = user.get("id")
    return SimpleNamespace(
        user=SimpleNamespace(id=uid) if uid else None,
        session=SimpleNamespace(
            access_token=access_token,
            refresh_token=refresh_token,
        )
        if access_token
        else None,
    )


async def sign_up_user(email: str, password: str):
    """
    Create a new user via Neon Auth (Better Auth) email/password signup.
    Returns an object shaped like Supabase's response (expects `.user.id`).
    """
    # With base URL shaped like ".../neondb/auth", the signup path is ".../neondb/auth/sign-up/email".
    url = f"{settings.neon_auth_base_url}/sign-up/email"
    # Neon Auth's email/password signup expects a user `name` as well.
    name = (email.split("@")[0] or "User").strip()[:128]
    payload = {"email": email, "password": password, "name": name}
    data = await _post_json(url, payload, origin_header=_auth_origin())

    extracted = data.get("data", data)
    user = extracted.get("user") or {}
    user_id = user.get("id")

    return SimpleNamespace(
        user=SimpleNamespace(id=user_id) if user_id else None,
        session=None,
    )


async def login_user(email: str, password: str):
    """
    Sign in a user via Neon Auth email/password and return tokens.
    Returns an object shaped like Supabase's response (expects `.session.access_token`).
    """
    # With base URL shaped like ".../neondb/auth", the login path is ".../neondb/auth/sign-in/email".
    url = f"{settings.neon_auth_base_url}/sign-in/email"
    payload = {"email": email, "password": password}
    data = await _post_json(url, payload, origin_header=_auth_origin())
    return _normalize_neon_auth_session_payload(data)


async def refresh_neon_auth_session(refresh_token: str) -> SimpleNamespace | None:
    """
    Exchange a Neon Auth refresh token for new session tokens.
    Tries configured or default Better Auth-style /refresh URLs and body shapes.
    """
    if not refresh_token.strip():
        return None

    base = settings.neon_auth_base_url.rstrip("/")
    origin = _auth_origin()
    configured = (settings.neon_auth_refresh_url or "").strip()
    urls = []
    if configured:
        urls.append(configured)
    else:
        urls.extend(
            [
                f"{base}/refresh",
                f"{base}/refresh-token",
            ]
        )

    bodies = (
        {"refreshToken": refresh_token},
        {"refresh_token": refresh_token},
    )

    for url in urls:
        for body in bodies:
            data = await _post_json_optional(url, body, origin_header=origin)
            if not data:
                continue
            normalized = _normalize_neon_auth_session_payload(data)
            if normalized.session:
                return normalized
    return None


async def get_user(token: str):
    """
    Validate the JWT and return an object shaped like Supabase auth.get_user().

    Validation order:
    1. Opaque Neon Auth session token  (neon_auth.session table lookup)
    2. EdDSA JWT issued by Neon Auth   (JWKS verification)
    3. HS256 JWT issued by this app    (Google OAuth flow, signed with APP_SECRET_KEY)
    """
    # 1. Opaque session token lookup
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT "userId"
                FROM neon_auth.session
                WHERE token = $1
                  AND ("expiresAt" IS NULL OR "expiresAt" > now())
                LIMIT 1
                """,
                token,
            )
        if row and row.get("userId"):
            return SimpleNamespace(user=SimpleNamespace(id=str(row["userId"])))
    except Exception:
        pass

    # 2. EdDSA JWT from Neon Auth JWKS
    user_id = await _validate_token(token)
    if user_id:
        return SimpleNamespace(user=SimpleNamespace(id=user_id))

    # 3. HS256 JWT minted by this app's Google OAuth flow
    try:
        app_secret = settings.app_secret_key
        if app_secret:
            payload = jwt.decode(token, app_secret, algorithms=["HS256"])
            sub = payload.get("sub")
            if sub:
                return SimpleNamespace(user=SimpleNamespace(id=sub))
    except PyJWTError:
        pass

    return SimpleNamespace(user=None)


async def get_auth_user_row(user_id: str) -> dict | None:
    """
    Load neon_auth user row for dashboard (id, email, name).
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, email, name
                FROM neon_auth."user"
                WHERE id = $1::uuid
                """,
                user_id,
            )
        return dict(row) if row else None
    except Exception:
        return None


async def get_user_role(user_id: str) -> str | None:
    """
    Return the neon_auth user's role (e.g. 'admin'), or None if missing/error.
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT role FROM neon_auth."user" WHERE id = $1::uuid',
                user_id,
            )
        return row["role"] if row else None
    except Exception:
        return None
