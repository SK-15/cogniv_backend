-- Migration: Add sessions_purchased for one-time session pack purchases
-- Run once in Neon console or via psql.

ALTER TABLE public.subscriptions
    ADD COLUMN IF NOT EXISTS sessions_purchased INTEGER NOT NULL DEFAULT 0;
