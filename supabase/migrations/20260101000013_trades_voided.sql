-- 20260101000013_trades_voided.sql
-- A trade can be VOIDED: it was recorded but never represented a real outcome
-- (e.g. qty=0 / fill_price NULL — an order that never actually filled). Voided
-- trades must be excluded from every outcome calculation: XP, KB win-rate, and
-- the dashboard. A real boolean column (not a sentinel pnl_pct/hit value) so the
-- exclusion is one explicit `WHERE NOT voided` clause everywhere, instead of
-- three independent convention guesses about what a magic value means.
--
-- Voided rows keep pnl_pct/hit NULL (no outcome) and get closed_at set (the
-- books are closed) — they are neither wins nor losses, they are non-events.
--
-- Column on an existing table inherits the table's grants, so no new GRANT is
-- needed (trades already has GRANT SELECT TO anon from …0012). Idempotent.

ALTER TABLE public.trades
    ADD COLUMN IF NOT EXISTS voided BOOLEAN NOT NULL DEFAULT FALSE;

-- Fast filter for the "real, resolved or open" trade set the readers care about.
CREATE INDEX IF NOT EXISTS idx_trades_not_voided
    ON public.trades (voided) WHERE NOT voided;
