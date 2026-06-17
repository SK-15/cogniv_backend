-- Self-issued refresh tokens for app-owned session management.
-- Stores only a SHA-256 hash of each opaque refresh token, never the raw value.
-- Tokens are single-use: rotated (revoked + reissued) on every successful refresh.

CREATE TABLE IF NOT EXISTS public.refresh_tokens (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES neon_auth."user"(id) ON DELETE CASCADE,
    token_hash  TEXT NOT NULL UNIQUE,
    expires_at  TIMESTAMPTZ NOT NULL,
    revoked_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_refresh_tokens_user_id
    ON public.refresh_tokens (user_id);

-- Speeds up lookups of currently-valid tokens during refresh.
CREATE INDEX IF NOT EXISTS idx_refresh_tokens_active
    ON public.refresh_tokens (token_hash)
    WHERE revoked_at IS NULL;
