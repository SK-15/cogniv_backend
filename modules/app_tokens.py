"""
App-owned session tokens.

This module is the single source of truth for issuing and validating the
session credentials this backend hands out, regardless of how the user
authenticated (email/password or Google OAuth):

  * Access token  - short-lived HS256 JWT signed with APP_SECRET_KEY.
                    Verified by modules.auth.get_user (HS256 path).
  * Refresh token - long-lived opaque random string. Only its SHA-256 hash
                    is stored (public.refresh_tokens). Single-use: every
                    successful refresh revokes the old token and issues a new
                    one (rotation).
"""

import datetime
import hashlib
import secrets

import jwt

from modules.config import settings
from modules.database import execute, fetch_one

JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_TTL = datetime.timedelta(hours=1)
REFRESH_TOKEN_TTL = datetime.timedelta(days=30)


def mint_access_token(user_id: str, email: str = "") -> str:
    """Create a short-lived HS256 access token for the given user."""
    now = datetime.datetime.now(datetime.timezone.utc)
    payload = {
        "sub": user_id,
        "email": email,
        "iat": now,
        "exp": now + ACCESS_TOKEN_TTL,
    }
    return jwt.encode(payload, settings.app_secret_key, algorithm=JWT_ALGORITHM)


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


async def issue_refresh_token(user_id: str) -> str:
    """Create a new opaque refresh token, store its hash, and return the raw token."""
    raw_token = secrets.token_urlsafe(48)
    expires_at = datetime.datetime.now(datetime.timezone.utc) + REFRESH_TOKEN_TTL
    await execute(
        """
        INSERT INTO public.refresh_tokens (user_id, token_hash, expires_at)
        VALUES ($1::uuid, $2, $3)
        """,
        user_id,
        _hash_token(raw_token),
        expires_at,
    )
    return raw_token


async def rotate_refresh_token(raw_token: str) -> tuple[str, str] | None:
    """
    Validate a refresh token and rotate it.

    Returns (user_id, new_raw_refresh_token) on success, or None if the token is
    unknown, already revoked, or expired.
    """
    if not raw_token.strip():
        return None

    row = await fetch_one(
        """
        SELECT id, user_id
        FROM public.refresh_tokens
        WHERE token_hash = $1
          AND revoked_at IS NULL
          AND expires_at > now()
        """,
        _hash_token(raw_token),
    )
    if not row:
        return None

    user_id = str(row["user_id"])
    await execute(
        "UPDATE public.refresh_tokens SET revoked_at = now() WHERE id = $1",
        row["id"],
    )
    new_raw_token = await issue_refresh_token(user_id)
    return user_id, new_raw_token


async def revoke_refresh_token(raw_token: str) -> None:
    """Revoke a single refresh token (e.g. on logout). No-op if unknown."""
    if not raw_token.strip():
        return
    await execute(
        """
        UPDATE public.refresh_tokens
        SET revoked_at = now()
        WHERE token_hash = $1 AND revoked_at IS NULL
        """,
        _hash_token(raw_token),
    )
