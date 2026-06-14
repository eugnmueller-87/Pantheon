-- 009_agent_exp.sql
-- Per-agent EXP / level system — a COSMETIC motivation layer on top of the
-- seniority gate. EXP never affects position sizing or live-trading authority;
-- the real-money gate stays system_level.max_position_pct() in zeus.py.
--
-- Two objects:
--   agent_exp_ledger — append-only audit of every XP award (idempotent)
--   agent_exp        — one upserted row per agent, the dashboard read
--   system_exp       — aggregate VIEW
--
-- GRANT note: created after 2026-10-30 → explicit GRANTs required or PostgREST
-- returns 403. Mirrors the 006 pattern.

-- ── Append-only ledger (the spine) ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.agent_exp_ledger (
    id           BIGSERIAL PRIMARY KEY,
    agent_name   TEXT        NOT NULL,
    event_type   TEXT        NOT NULL,
    xp           INT         NOT NULL,
    source_ref   TEXT        NOT NULL,      -- stable id for idempotency
    metadata     JSONB       NOT NULL DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- One award per (agent, event_type, source_ref): replays/reconnects can't
    -- double-mint. Every award MUST carry a deterministic source_ref.
    UNIQUE (agent_name, event_type, source_ref)
);

CREATE INDEX IF NOT EXISTS idx_agent_exp_ledger_agent
    ON public.agent_exp_ledger (agent_name, created_at DESC);

-- ── Per-agent rollup (the dashboard read) ────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.agent_exp (
    agent_name           TEXT PRIMARY KEY,
    total_xp             BIGINT      NOT NULL DEFAULT 0,
    exp_level            INT         NOT NULL DEFAULT 1,   -- displayed (band-capped) level
    xp_into_level        INT         NOT NULL DEFAULT 0,
    xp_to_next           INT         NOT NULL DEFAULT 500,
    progress_to_next_pct FLOAT8      NOT NULL DEFAULT 0,   -- 0..100 to next seniority rung
    lifetime_wins        INT         NOT NULL DEFAULT 0,
    lifetime_losses      INT         NOT NULL DEFAULT 0,
    current_win_streak   INT         NOT NULL DEFAULT 0,
    best_win_streak      INT         NOT NULL DEFAULT 0,
    seniority_level_int  INT         NOT NULL DEFAULT 0,   -- denormalized; ordering uses agent_seniority
    last_event_at        TIMESTAMPTZ,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── Aggregate view ───────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW public.system_exp AS
SELECT
    COALESCE(SUM(total_xp), 0)        AS total_xp,
    COALESCE(AVG(exp_level), 0)       AS avg_exp_level,
    COALESCE(MIN(exp_level), 0)       AS floor_exp_level,
    MAX(updated_at)                   AS updated_at
FROM public.agent_exp;

-- ── GRANTs (mandatory) ───────────────────────────────────────────────────────
GRANT SELECT, INSERT, UPDATE, DELETE ON public.agent_exp_ledger TO service_role;
GRANT SELECT ON public.agent_exp_ledger TO anon;
GRANT USAGE, SELECT ON SEQUENCE public.agent_exp_ledger_id_seq TO service_role;

GRANT SELECT, INSERT, UPDATE, DELETE ON public.agent_exp TO service_role;
GRANT SELECT ON public.agent_exp TO anon;

GRANT SELECT ON public.system_exp TO anon;
GRANT SELECT ON public.system_exp TO service_role;

-- ── RLS ──────────────────────────────────────────────────────────────────────
ALTER TABLE public.agent_exp_ledger ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.agent_exp        ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS agent_exp_ledger_service_all ON public.agent_exp_ledger;
CREATE POLICY agent_exp_ledger_service_all ON public.agent_exp_ledger
    FOR ALL TO service_role USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS agent_exp_ledger_anon_read ON public.agent_exp_ledger;
CREATE POLICY agent_exp_ledger_anon_read ON public.agent_exp_ledger
    FOR SELECT TO anon USING (true);

DROP POLICY IF EXISTS agent_exp_service_all ON public.agent_exp;
CREATE POLICY agent_exp_service_all ON public.agent_exp
    FOR ALL TO service_role USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS agent_exp_anon_read ON public.agent_exp;
CREATE POLICY agent_exp_anon_read ON public.agent_exp
    FOR SELECT TO anon USING (true);

-- ── Realtime (agent_exp pushes; views are not publishable) ───────────────────
ALTER PUBLICATION supabase_realtime ADD TABLE public.agent_exp;
