-- ============================================================
-- Pantheon OS — Supabase RPC Functions
-- Migration 003 — Vector search + analytics helpers
--
-- Run AFTER 001_schema.sql in: Supabase Dashboard → SQL Editor
-- ============================================================


-- ── Vector similarity search for knowledge documents ──────────────────────────
-- Called by core/supabase_client.py → search_knowledge()

CREATE OR REPLACE FUNCTION match_knowledge_documents(
    query_embedding   vector(1536),
    match_collection  TEXT,
    match_count       INT     DEFAULT 5,
    min_similarity    FLOAT8  DEFAULT 0.7
)
RETURNS TABLE (
    doc_id          TEXT,
    document_text   TEXT,
    metadata        JSONB,
    similarity      FLOAT8
)
LANGUAGE plpgsql
AS $$
BEGIN
    RETURN QUERY
    SELECT
        kd.doc_id,
        kd.document_text,
        kd.metadata,
        1 - (kd.embedding <=> query_embedding) AS similarity
    FROM knowledge_documents kd
    WHERE
        kd.collection = match_collection
        AND kd.embedding IS NOT NULL
        AND 1 - (kd.embedding <=> query_embedding) >= min_similarity
    ORDER BY kd.embedding <=> query_embedding
    LIMIT match_count;
END;
$$;


-- ── Win rate by signal category (Grafana panel: "Best Signal Types") ──────────

CREATE OR REPLACE FUNCTION get_win_rates_by_category()
RETURNS TABLE (
    category        TEXT,
    total_trades    BIGINT,
    win_rate        FLOAT8,
    avg_pnl_pct     FLOAT8
)
LANGUAGE plpgsql
AS $$
BEGIN
    RETURN QUERY
    SELECT
        t.category::TEXT,
        COUNT(*)                                                    AS total_trades,
        AVG(CASE WHEN t.hit THEN 1.0 ELSE 0.0 END)
            FILTER (WHERE t.hit IS NOT NULL)                        AS win_rate,
        AVG(t.pnl_pct) FILTER (WHERE t.pnl_pct IS NOT NULL)        AS avg_pnl_pct
    FROM trades t
    GROUP BY t.category
    ORDER BY win_rate DESC NULLS LAST;
END;
$$;


-- ── Monthly P&L summary (Grafana panel: "Monthly Returns") ───────────────────

CREATE OR REPLACE FUNCTION get_monthly_returns()
RETURNS TABLE (
    month           TEXT,
    total_trades    BIGINT,
    winning_trades  BIGINT,
    win_rate        FLOAT8,
    avg_pnl_pct     FLOAT8,
    total_pnl_pct   FLOAT8
)
LANGUAGE plpgsql
AS $$
BEGIN
    RETURN QUERY
    SELECT
        TO_CHAR(DATE_TRUNC('month', t.recorded_at), 'YYYY-MM')      AS month,
        COUNT(*)                                                     AS total_trades,
        COUNT(*) FILTER (WHERE t.hit = TRUE)                        AS winning_trades,
        AVG(CASE WHEN t.hit THEN 1.0 ELSE 0.0 END)
            FILTER (WHERE t.hit IS NOT NULL)                         AS win_rate,
        AVG(t.pnl_pct) FILTER (WHERE t.pnl_pct IS NOT NULL)         AS avg_pnl_pct,
        SUM(t.pnl_pct) FILTER (WHERE t.pnl_pct IS NOT NULL)         AS total_pnl_pct
    FROM trades t
    GROUP BY DATE_TRUNC('month', t.recorded_at)
    ORDER BY DATE_TRUNC('month', t.recorded_at) DESC;
END;
$$;


-- ── Kill stage distribution (Grafana panel: "Where do signals die?") ─────────

CREATE OR REPLACE FUNCTION get_kill_stage_stats()
RETURNS TABLE (
    killed_at_stage TEXT,
    count           BIGINT,
    pct             FLOAT8
)
LANGUAGE plpgsql
AS $$
BEGIN
    RETURN QUERY
    WITH totals AS (SELECT COUNT(*) AS total FROM decision_traces)
    SELECT
        COALESCE(dt.killed_at_stage, 'executed')    AS killed_at_stage,
        COUNT(*)                                     AS count,
        ROUND(COUNT(*) * 100.0 / totals.total, 1)   AS pct
    FROM decision_traces dt, totals
    GROUP BY dt.killed_at_stage, totals.total
    ORDER BY count DESC;
END;
$$;


-- ── Equity drawdown stats (Grafana panel: "Drawdown History") ─────────────────

CREATE OR REPLACE FUNCTION get_drawdown_history(hours_back INT DEFAULT 168)
RETURNS TABLE (
    refreshed_at        TIMESTAMPTZ,
    total_equity        NUMERIC,
    peak_equity         NUMERIC,
    current_drawdown_pct FLOAT8
)
LANGUAGE plpgsql
AS $$
BEGIN
    RETURN QUERY
    SELECT
        ps.refreshed_at,
        ps.total_equity,
        ps.peak_equity,
        ps.current_drawdown_pct
    FROM portfolio_state ps
    WHERE ps.refreshed_at >= NOW() - (hours_back || ' hours')::INTERVAL
    ORDER BY ps.refreshed_at ASC;
END;
$$;


-- ── Agent health summary (Grafana panel: "Agent Status") ─────────────────────

CREATE OR REPLACE FUNCTION get_agent_health_summary()
RETURNS TABLE (
    agent_name  TEXT,
    status      TEXT,
    message     TEXT,
    checked_at  TIMESTAMPTZ
)
LANGUAGE plpgsql
AS $$
BEGIN
    RETURN QUERY
    SELECT DISTINCT ON (ah.agent_name)
        ah.agent_name,
        ah.status::TEXT,
        ah.message,
        ah.checked_at
    FROM agent_health ah
    ORDER BY ah.agent_name, ah.checked_at DESC;
END;
$$;
