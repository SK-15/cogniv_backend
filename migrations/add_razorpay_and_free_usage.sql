-- Migration: Razorpay integration + free usage quota
-- Run in Neon console or via psql. Safe to run once.

-- 1a. Rename Stripe columns to Razorpay on existing subscriptions table
ALTER TABLE public.subscriptions
    RENAME COLUMN stripe_customer_id TO razorpay_customer_id;

ALTER TABLE public.subscriptions
    RENAME COLUMN stripe_subscription_id TO razorpay_subscription_id;

-- 1b. Track free-tier session usage per user
CREATE TABLE IF NOT EXISTS public.free_usage (
    user_id       TEXT PRIMARY KEY REFERENCES neon_auth."user"(id) ON DELETE CASCADE,
    sessions_used INTEGER NOT NULL DEFAULT 0,
    created_at    TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

-- 1c. Store session duration when client calls the end endpoint
ALTER TABLE public.interview_sessions
    ADD COLUMN IF NOT EXISTS duration_seconds INTEGER;
