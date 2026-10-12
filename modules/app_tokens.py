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
                    one (rotation). Presenting an already-rotated token again
                    is treated as theft and revokes all of the user's tokens.
"""

import datetime
import hashlib
import secrets

import logging

import jwt

from modules.config import settings
from modules.database import execute, get_pool

logger = logging.getLogger(__name__)

JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_TTL = datetime.timedelta(hours=1)
REFRESH_TOKEN_TTL = datetime.timedelta(days=30)
# A rotated token replayed within this window is treated as a retry, not reuse.
REUSE_GRACE_SECONDS = 30.0


def require_app_secret_key() -> str:
    """APP_SECRET_KEY signs every access token and OAuth state; refuse to run without it."""
    if not (settings.app_secret_key or "").strip():
        raise RuntimeError("APP_SECRET_KEY is not set")
    return settings.app_secret_key


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
    unknown, already revoked, or expired. The revoke is a single conditional
    UPDATE, so two concurrent refreshes with the same token cannot both win.
    Reusing a token that was already revoked revokes every token of its user.
    """
    if not raw_token.strip():
        return None

    token_hash = _hash_token(raw_token)
    new_raw_token = secrets.token_urlsafe(48)
    expires_at = datetime.datetime.now(datetime.timezone.utc) + REFRESH_TOKEN_TTL
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE public.refresh_tokens
                SET revoked_at = now()
                WHERE token_hash = $1
                  AND revoked_at IS NULL
                  AND expires_at > now()
                RETURNING user_id
                """,
                token_hash,
            )
            if row:
                await conn.execute(
                    """
                    INSERT INTO public.refresh_tokens (user_id, token_hash, expires_at)
                    VALUES ($1, $2, $3)
                    """,
                    row["user_id"],
                    _hash_token(new_raw_token),
                    expires_at,
                )
                return str(row["user_id"]), new_raw_token

        # A token revoked moments ago is most likely a client retry after a lost
        # response, not theft — fail it without logging the user out everywhere.
        stale = await conn.fetchrow(
            """
            SELECT user_id
            FROM public.refresh_tokens
            WHERE token_hash = $1
              AND revoked_at < now() - make_interval(secs => $2)
            """,
            token_hash,
            REUSE_GRACE_SECONDS,
        )
    if stale:
        logger.warning("Refresh token reuse for user %s; revoking all sessions", stale["user_id"])
        await revoke_all_refresh_tokens(str(stale["user_id"]))
    return None


async def revoke_all_refresh_tokens(user_id: str) -> None:
    """Revoke every live refresh token of a user (reuse detected, password reset, deletion)."""
    await execute(
        """
        UPDATE public.refresh_tokens
        SET revoked_at = now()
        WHERE user_id = $1::uuid AND revoked_at IS NULL
        """,
        user_id,
    )


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
