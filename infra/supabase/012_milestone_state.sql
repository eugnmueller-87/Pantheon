-- 012_milestone_state.sql
-- Persisted MilestoneManager state — single source for the vault origin so
-- cumulative profit survives restarts instead of re-baselining to zero.
--
-- WHY: MilestoneManager was 100% in-memory. Every container restart re-anchored
-- the vault profit origin to whatever starting_equity was passed and reset the
-- vault balance — silently zeroing cumulative-profit tracking on each redeploy.
-- This singleton table persists the capture-once origin + vault totals + the
-- set of stages already crossed (so milestone vault alerts don't re-fire).
--
-- The STAGE/TIER band is NOT stored here — it derives from current equity each
-- cycle. Only the start-vs-now baseline and vault accounting live here.
--
-- GRANT note: tables created after 2026-10-30 need explicit GRANTs or
-- PostgREST returns 403. Mirrors the 008 pattern.

CREATE TABLE IF NOT EXISTS public.milestone_state (
    id              INT         PRIMARY KEY DEFAULT 1 CHECK (id = 1),  -- singleton
    milestone_origin FLOAT8     NOT NULL,                  -- captured once; vault profit baseline
    vault_balance   FLOAT8      NOT NULL DEFAULT 0,
    total_vaulted   FLOAT8      NOT NULL DEFAULT 0,
    crossed_stages  TEXT[]      NOT NULL DEFAULT '{}',      -- stages already crossed (no re-fire)
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── GRANTs (mandatory) ───────────────────────────────────────────────────────
GRANT SELECT, INSERT, UPDATE, DELETE ON public.milestone_state TO service_role;
GRANT SELECT ON public.milestone_state TO anon;

-- ── RLS ──────────────────────────────────────────────────────────────────────
ALTER TABLE public.milestone_state ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS milestone_state_service_all ON public.milestone_state;
CREATE POLICY milestone_state_service_all ON public.milestone_state
    FOR ALL TO service_role USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS milestone_state_anon_read ON public.milestone_state;
CREATE POLICY milestone_state_anon_read ON public.milestone_state
    FOR SELECT TO anon USING (true);
