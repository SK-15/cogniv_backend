-- Migration: Purchase history log for one-time session pack orders
-- Run once in Neon console or via psql.

CREATE TABLE IF NOT EXISTS public.purchases (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES neon_auth."user"(id) ON DELETE CASCADE,
    plan_id     TEXT NOT NULL,
    sessions    INTEGER NOT NULL,
    amount      INTEGER NOT NULL,
    order_id    TEXT NOT NULL UNIQUE,
    payment_id  TEXT NOT NULL,
    created_at  TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS purchases_user_id_idx ON public.purchases (user_id);
