-- 20260101000007_equity_snapshots.sql
-- Append-only equity time-series for the dashboard equity curve.
-- Twin of infra/supabase/008_equity_snapshots.sql — keep both in sync.
--
-- WHY: portfolio_state is a singleton (UNIQUE state_id), so equity history is
-- lost. This append-only table is the dedicated source for the equity curve.

CREATE TABLE IF NOT EXISTS public.equity_snapshots (
    snapshot_id          BIGSERIAL PRIMARY KEY,
    total_equity         FLOAT8      NOT NULL,
    peak_equity          FLOAT8      NOT NULL,
    current_drawdown_pct FLOAT8      NOT NULL DEFAULT 0,
    open_positions       INT         NOT NULL DEFAULT 0,
    paper_trading        BOOLEAN     NOT NULL DEFAULT TRUE,
    recorded_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_equity_snapshots_recorded_at
    ON public.equity_snapshots (recorded_at DESC);

GRANT SELECT, INSERT, UPDATE, DELETE ON public.equity_snapshots TO service_role;
GRANT SELECT ON public.equity_snapshots TO anon;
GRANT USAGE, SELECT ON SEQUENCE public.equity_snapshots_snapshot_id_seq TO service_role;

ALTER TABLE public.equity_snapshots ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS equity_snapshots_service_all ON public.equity_snapshots;
CREATE POLICY equity_snapshots_service_all ON public.equity_snapshots
    FOR ALL TO service_role USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS equity_snapshots_anon_read ON public.equity_snapshots;
CREATE POLICY equity_snapshots_anon_read ON public.equity_snapshots
    FOR SELECT TO anon USING (true);

ALTER PUBLICATION supabase_realtime ADD TABLE public.equity_snapshots;
