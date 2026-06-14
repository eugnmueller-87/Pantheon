-- 010_dashboard_analytics.sql
-- Analytics RPCs + seniority columns for the Phase 4 dashboard panels.
-- Run AFTER 001/003. All functions STABLE, anon-executable.

-- ── agent_seniority: persist progress + raw metrics the evaluator computes ────
ALTER TABLE public.agent_seniority
    ADD COLUMN IF NOT EXISTS progress_pct FLOAT8 DEFAULT 0,
    ADD COLUMN IF NOT EXISTS metrics      JSONB  DEFAULT '{}';

-- New columns need GRANTs visible to anon (table already granted in 006, but
-- re-grant is idempotent and covers fresh installs).
GRANT SELECT ON public.agent_seniority TO anon;

-- ── R-multiple distribution ──────────────────────────────────────────────────
-- R = pnl_pct / risk_pct, risk_pct from the stop distance (NULLIF guards 0),
-- falling back to position_pct when no stop. Bucketed for a histogram.
CREATE OR REPLACE FUNCTION public.get_r_multiple_distribution()
RETURNS TABLE (r_bucket TEXT, cnt BIGINT)
LANGUAGE sql STABLE AS $$
    WITH r AS (
        SELECT
            pnl_pct / NULLIF(
                CASE
                    WHEN fill_price IS NOT NULL AND stop_loss IS NOT NULL AND fill_price > 0
                        THEN ABS(fill_price - stop_loss) / fill_price
                    ELSE NULLIF(position_pct, 0)
                END, 0) AS r
        FROM public.trades
        WHERE hit IS NOT NULL AND pnl_pct IS NOT NULL
    )
    SELECT
        CASE
            WHEN r <= -3 THEN '<= -3R'
            WHEN r <  -2 THEN '-3R..-2R'
            WHEN r <  -1 THEN '-2R..-1R'
            WHEN r <   0 THEN '-1R..0'
            WHEN r <   1 THEN '0..1R'
            WHEN r <   2 THEN '1R..2R'
            WHEN r <   3 THEN '2R..3R'
            ELSE '>= 3R'
        END AS r_bucket,
        COUNT(*) AS cnt
    FROM r
    WHERE r IS NOT NULL
    GROUP BY 1
    ORDER BY MIN(r);
$$;

-- ── Performance stats (one row) ──────────────────────────────────────────────
-- Sharpe here is PER-TRADE (avg/stddev of pnl_pct), explicitly NOT annualized.
CREATE OR REPLACE FUNCTION public.get_performance_stats()
RETURNS TABLE (
    total_trades     BIGINT,
    win_rate         FLOAT8,
    avg_win_pct      FLOAT8,
    avg_loss_pct     FLOAT8,
    profit_factor    FLOAT8,
    expectancy       FLOAT8,
    per_trade_sharpe FLOAT8
)
LANGUAGE sql STABLE AS $$
    WITH closed AS (
        SELECT pnl_pct FROM public.trades WHERE hit IS NOT NULL AND pnl_pct IS NOT NULL
    )
    SELECT
        COUNT(*)                                                              AS total_trades,
        COALESCE(AVG(CASE WHEN pnl_pct > 0 THEN 1.0 ELSE 0.0 END), 0)         AS win_rate,
        COALESCE(AVG(pnl_pct) FILTER (WHERE pnl_pct > 0), 0)                  AS avg_win_pct,
        COALESCE(AVG(pnl_pct) FILTER (WHERE pnl_pct <= 0), 0)                 AS avg_loss_pct,
        COALESCE(
            SUM(pnl_pct) FILTER (WHERE pnl_pct > 0)
            / NULLIF(ABS(SUM(pnl_pct) FILTER (WHERE pnl_pct <= 0)), 0), 0)    AS profit_factor,
        COALESCE(AVG(pnl_pct), 0)                                             AS expectancy,
        COALESCE(AVG(pnl_pct) / NULLIF(STDDEV_SAMP(pnl_pct), 0), 0)           AS per_trade_sharpe
    FROM closed;
$$;

GRANT EXECUTE ON FUNCTION public.get_r_multiple_distribution() TO anon;
GRANT EXECUTE ON FUNCTION public.get_performance_stats() TO anon;
