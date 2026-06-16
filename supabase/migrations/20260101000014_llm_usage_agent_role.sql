-- 20260101000014_llm_usage_agent_role.sql
-- Per-agent / per-role token attribution on the dashboard. Until now the role
-- of each LLM call was encoded only in the `symbol` string (e.g. 'NVDA:bull',
-- 'NVDA:bear', or a bare 'NVDA' for the Director). That string-suffix parse is
-- fragile to read from the dashboard, so promote it to explicit columns.
--
--   agent : which pipeline agent made the call (today always 'zeus' — it is the
--           only LLM caller; column lets other agents attribute spend later).
--   role  : the sub-role within the agent — 'director' | 'bull' | 'bear'.
--
-- Backfill the existing rows from the symbol suffix so historical spend is
-- attributed too. llm_usage already has GRANT SELECT TO anon, and new columns
-- inherit it, so the dashboard (anon) can read these with no extra grant.

ALTER TABLE public.llm_usage
    ADD COLUMN IF NOT EXISTS agent TEXT NOT NULL DEFAULT 'zeus',
    ADD COLUMN IF NOT EXISTS role  TEXT NOT NULL DEFAULT 'director';

-- Backfill role from the historical symbol-suffix convention.
UPDATE public.llm_usage
   SET role = CASE
                WHEN symbol LIKE '%:bull' THEN 'bull'
                WHEN symbol LIKE '%:bear' THEN 'bear'
                ELSE 'director'
              END
 WHERE role = 'director';   -- only touch un-backfilled rows (idempotent-ish)

CREATE INDEX IF NOT EXISTS idx_llm_usage_agent_role
    ON public.llm_usage (agent, role);
