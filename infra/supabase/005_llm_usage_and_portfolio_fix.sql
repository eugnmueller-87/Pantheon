-- Migration 005: llm_usage table + portfolio_state singleton fix
-- Run this in the Supabase SQL Editor: https://supabase.com/dashboard/project/ehbliqdzveeflaidvprr/sql

-- 1. llm_usage table (was missing — INSERT returned 403)
CREATE TABLE IF NOT EXISTS public.llm_usage (
    id            BIGSERIAL PRIMARY KEY,
    model         TEXT        NOT NULL,
    symbol        TEXT,
    input_tokens  INTEGER     NOT NULL DEFAULT 0,
    output_tokens INTEGER     NOT NULL DEFAULT 0,
    cost_usd      NUMERIC(12,6) NOT NULL DEFAULT 0,
    recorded_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE public.llm_usage DISABLE ROW LEVEL SECURITY;

-- 2. portfolio_state singleton fix (was INSERT on every refresh → table bloat)
ALTER TABLE public.portfolio_state
    ADD COLUMN IF NOT EXISTS state_id TEXT DEFAULT 'singleton';

UPDATE public.portfolio_state
    SET state_id = 'singleton'
    WHERE state_id IS NULL;

-- Keep only the most recent row, drop historical bloat
DELETE FROM public.portfolio_state
    WHERE refreshed_at < (SELECT MAX(refreshed_at) FROM public.portfolio_state);

ALTER TABLE public.portfolio_state
    DROP CONSTRAINT IF EXISTS portfolio_state_singleton_key;

ALTER TABLE public.portfolio_state
    ADD CONSTRAINT portfolio_state_singleton_key UNIQUE (state_id);
