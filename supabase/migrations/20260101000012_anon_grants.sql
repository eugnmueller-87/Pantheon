-- 20260101000012_anon_grants.sql
-- Fix dashboard 403s: the base schema (…0001) created "anon_read" RLS POLICIES
-- for these tables but never issued the matching table-level GRANT SELECT TO
-- anon. PostgREST enforces BOTH layers — a policy without a grant still returns
-- 403 ("permission denied for table …"). equity_snapshots only worked because
-- its own migration (…0007) added the grant. This backfills the missing grants
-- so the frontend (anon role) can read what its policies already allow.
--
-- Pattern mirrors GRANT SELECT ON public.equity_snapshots TO anon; exactly.
-- Idempotent: GRANT is safe to re-run.

GRANT SELECT ON public.trades              TO anon;
GRANT SELECT ON public.decision_traces     TO anon;
GRANT SELECT ON public.portfolio_state     TO anon;
GRANT SELECT ON public.portfolio_positions TO anon;
GRANT SELECT ON public.agent_health        TO anon;
GRANT SELECT ON public.macro_context       TO anon;
GRANT SELECT ON public.signals             TO anon;
GRANT SELECT ON public.ticker_map          TO anon;
