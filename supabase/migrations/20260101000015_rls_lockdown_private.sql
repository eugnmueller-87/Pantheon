-- 20260101000015_rls_lockdown_private.sql
-- LOCK DOWN ALL PUBLIC READ ACCESS.
--
-- Supabase flagged 3 tables (llm_usage, portfolio_positions, portfolio_state)
-- with RLS disabled — "publicly accessible". But the real surface was larger:
-- 17 public tables granted SELECT to `anon`, several with an `anon_read` policy
-- of `USING (true)` — i.e. anyone with the project URL + the public anon key
-- could read trades, equity, positions, signals, LLM cost, seniority, etc.
--
-- Decision (operator): nothing should be visible to the public. The dashboard is
-- LOCAL-ONLY and authenticates with the service_role key (which bypasses RLS),
-- so revoking anon does NOT break it — it only removes public exposure. Public
-- consumers get screenshots, not live DB access.
--
-- This migration:
--   1. Enables RLS on the 3 tables that had it OFF (so no table is RLS-less).
--   2. REVOKEs SELECT (and any write) from anon + authenticated on every public
--      table, so the public roles can read nothing.
--   3. Drops the now-pointless `anon_read` policies (anon has no grant anyway;
--      dropping keeps the policy list honest). service_role policies/grants are
--      left intact — the pipeline (service_role) and the local dashboard
--      (service_role) keep full access.
--
-- Idempotent: ENABLE/REVOKE/DROP POLICY IF EXISTS are safe to re-run.

-- 1. Enable RLS on the three tables that were RLS-less (alert root cause).
ALTER TABLE public.llm_usage           ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.portfolio_positions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.portfolio_state     ENABLE ROW LEVEL SECURITY;

-- 2 + 3. Revoke all public-role access and drop anon_read policies on every
-- public table, in one pass. Done in a DO block so it covers the full set
-- without enumerating 17 names by hand (and auto-covers any future table).
DO $$
DECLARE
    t text;
    pol text;
BEGIN
    FOR t IN
        SELECT c.relname
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
        -- r=table, v=view, m=materialized view: views (system_exp,
        -- system_seniority) also granted anon SELECT and must be revoked too.
        WHERE c.relkind IN ('r', 'v', 'm')
    LOOP
        -- Strip every privilege from the public-facing roles.
        EXECUTE format('REVOKE ALL ON public.%I FROM anon', t);
        EXECUTE format('REVOKE ALL ON public.%I FROM authenticated', t);
        -- Drop any anon-facing read policy (named anon_read in the base schema).
        FOR pol IN
            SELECT policyname FROM pg_policies
            WHERE schemaname = 'public' AND tablename = t
              AND 'anon' = ANY (roles)
        LOOP
            EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', pol, t);
        END LOOP;
    END LOOP;
END $$;

-- Belt-and-suspenders: ensure future tables don't silently grant to anon.
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM anon;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM authenticated;
