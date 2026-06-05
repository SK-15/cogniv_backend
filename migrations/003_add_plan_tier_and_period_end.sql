-- Add plan_tier and current_period_end to subscriptions if not already present
-- These columns may already exist on production; IF NOT EXISTS makes this idempotent
ALTER TABLE public.subscriptions
    ADD COLUMN IF NOT EXISTS plan_tier TEXT NOT NULL DEFAULT 'free',
    ADD COLUMN IF NOT EXISTS current_period_end TIMESTAMPTZ;
