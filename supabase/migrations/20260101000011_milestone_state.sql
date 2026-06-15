-- Persisted MilestoneManager state — twin of infra/supabase/012_milestone_state.sql.
-- Keep both in sync. Singleton table: the capture-once vault origin + vault
-- accounting survive restarts instead of re-baselining. The stage/tier band is
-- NOT stored — it derives from current equity each cycle.

CREATE TABLE IF NOT EXISTS public.milestone_state (
    id              INT         PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    milestone_origin FLOAT8     NOT NULL,
    vault_balance   FLOAT8      NOT NULL DEFAULT 0,
    total_vaulted   FLOAT8      NOT NULL DEFAULT 0,
    crossed_stages  TEXT[]      NOT NULL DEFAULT '{}',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT, UPDATE, DELETE ON public.milestone_state TO service_role;
GRANT SELECT ON public.milestone_state TO anon;

ALTER TABLE public.milestone_state ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS milestone_state_service_all ON public.milestone_state;
CREATE POLICY milestone_state_service_all ON public.milestone_state
    FOR ALL TO service_role USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS milestone_state_anon_read ON public.milestone_state;
CREATE POLICY milestone_state_anon_read ON public.milestone_state
    FOR SELECT TO anon USING (true);
