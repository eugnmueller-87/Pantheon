-- ============================================================
-- Pantheon OS — Supabase PostgreSQL Schema
-- Migration 001 — Full initial schema
--
-- Run in: Supabase Dashboard → SQL Editor
-- Or via: supabase db push
--
-- Extensions required: pgvector, uuid-ossp
-- ============================================================

-- Extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS vector;

-- ============================================================
-- ENUMS
-- ============================================================

CREATE TYPE signal_category AS ENUM (
    'supplier_disruption',
    'positive_news',
    'earnings_surprise',
    'regulatory_action',
    'macro_shift',
    'neutral'
);

CREATE TYPE severity_level AS ENUM ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL');

CREATE TYPE market_regime AS ENUM ('bull', 'bear', 'sideways', 'unknown');

CREATE TYPE pipeline_status AS ENUM ('running', 'halted', 'paused', 'shutdown');

CREATE TYPE agent_health_status AS ENUM ('healthy', 'degraded', 'failed');

CREATE TYPE vix_band AS ENUM ('low', 'medium', 'high', 'extreme');

CREATE TYPE trade_side AS ENUM ('BUY', 'SELL');

CREATE TYPE position_side AS ENUM ('LONG', 'SHORT');

CREATE TYPE cb_state AS ENUM ('closed', 'open', 'half_open');


-- ============================================================
-- 1. SIGNALS  (Icarus output — raw market signals)
-- ============================================================

CREATE TABLE signals (
    signal_id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source_url          TEXT NOT NULL,
    headline            TEXT NOT NULL,
    summary             TEXT,
    published_at        TIMESTAMPTZ NOT NULL,
    category            signal_category NOT NULL,
    severity            severity_level NOT NULL,
    affected_tickers    TEXT[] NOT NULL DEFAULT '{}',
    raw_text            TEXT,
    supplier            TEXT,
    hermes_signal_type  TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_signals_published_at    ON signals(published_at DESC);
CREATE INDEX idx_signals_category        ON signals(category);
CREATE INDEX idx_signals_severity        ON signals(severity);
CREATE INDEX idx_signals_supplier        ON signals(supplier);
CREATE INDEX idx_signals_tickers         ON signals USING GIN(affected_tickers);


-- ============================================================
-- 2. FILTERED_SIGNALS  (Hades output — compliance results)
-- ============================================================

CREATE TABLE filtered_signals (
    filtered_signal_id  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    signal_id           UUID NOT NULL REFERENCES signals(signal_id) ON DELETE CASCADE,
    supplier            TEXT,
    compliance_score    FLOAT8 NOT NULL CHECK (compliance_score BETWEEN 0.0 AND 1.0),
    esg_flag            BOOLEAN NOT NULL DEFAULT FALSE,
    ofac_flag           BOOLEAN NOT NULL DEFAULT FALSE,
    downgraded          BOOLEAN NOT NULL DEFAULT FALSE,
    notes               TEXT[] NOT NULL DEFAULT '{}',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_filtered_signals_signal_id ON filtered_signals(signal_id);
CREATE INDEX idx_filtered_signals_supplier  ON filtered_signals(supplier);
CREATE INDEX idx_filtered_signals_ofac      ON filtered_signals(ofac_flag) WHERE ofac_flag = TRUE;
CREATE INDEX idx_filtered_signals_esg       ON filtered_signals(esg_flag) WHERE esg_flag = TRUE;


-- ============================================================
-- 3. MACRO_CONTEXT  (Artemis output — market regime snapshots)
-- ============================================================

CREATE TABLE macro_context (
    macro_id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    fetched_at          TIMESTAMPTZ NOT NULL,
    regime              market_regime NOT NULL,
    vix                 FLOAT8 NOT NULL,
    sp500_1m_return     FLOAT8 NOT NULL,
    sector_momentum     JSONB NOT NULL DEFAULT '{}',
    suppress            BOOLEAN NOT NULL DEFAULT FALSE,
    suppress_reason     TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_macro_context_fetched_at ON macro_context(fetched_at DESC);
CREATE INDEX idx_macro_context_regime     ON macro_context(regime);
CREATE INDEX idx_macro_context_vix        ON macro_context(vix);


-- ============================================================
-- 4. TRADES  (Pythia + Ares — replaces SQLite trade_log.db)
-- ============================================================

CREATE TABLE trades (
    trade_id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    signal_id           UUID REFERENCES signals(signal_id),
    context_key         TEXT NOT NULL,          -- "{category}|{regime}|{vix_band}"
    category            signal_category NOT NULL,
    regime              market_regime NOT NULL,
    vix_band            vix_band NOT NULL,
    confidence          FLOAT8 NOT NULL CHECK (confidence BETWEEN 0.0 AND 1.0),
    position_pct        FLOAT8 NOT NULL CHECK (position_pct >= 0.0),
    symbol              TEXT NOT NULL,
    side                trade_side NOT NULL,
    order_id            TEXT,
    fill_price          FLOAT8,
    stop_loss           FLOAT8,
    take_profit         FLOAT8,
    qty                 FLOAT8,
    pnl_pct             FLOAT8,                 -- NULL while open, backfilled by Argus
    hit                 BOOLEAN,                -- NULL=open, TRUE=TP hit, FALSE=SL hit
    paper_trading       BOOLEAN NOT NULL DEFAULT TRUE,
    recorded_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at           TIMESTAMPTZ
);

CREATE INDEX idx_trades_context_key   ON trades(context_key);
CREATE INDEX idx_trades_symbol        ON trades(symbol);
CREATE INDEX idx_trades_recorded_at   ON trades(recorded_at DESC);
CREATE INDEX idx_trades_hit           ON trades(hit) WHERE hit IS NOT NULL;
CREATE INDEX idx_trades_signal_id     ON trades(signal_id);
CREATE INDEX idx_trades_open          ON trades(hit) WHERE hit IS NULL;

-- Materialized view: hit rates by context key (Pythia's primary lookup)
CREATE MATERIALIZED VIEW trade_hit_rates AS
    SELECT
        context_key,
        category,
        regime,
        vix_band,
        COUNT(*)                                        AS total_trades,
        COUNT(*) FILTER (WHERE hit IS NOT NULL)         AS closed_trades,
        AVG(CASE WHEN hit THEN 1.0 ELSE 0.0 END)
            FILTER (WHERE hit IS NOT NULL)              AS hit_rate,
        AVG(pnl_pct) FILTER (WHERE pnl_pct IS NOT NULL) AS avg_pnl_pct
    FROM trades
    GROUP BY context_key, category, regime, vix_band;

CREATE UNIQUE INDEX ON trade_hit_rates(context_key);

-- Refresh function (call after each trade update)
CREATE OR REPLACE FUNCTION refresh_hit_rates()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    REFRESH MATERIALIZED VIEW CONCURRENTLY trade_hit_rates;
    RETURN NULL;
END;
$$;

CREATE TRIGGER trg_refresh_hit_rates
    AFTER INSERT OR UPDATE OF hit, pnl_pct ON trades
    FOR EACH STATEMENT EXECUTE FUNCTION refresh_hit_rates();


-- ============================================================
-- 5. DECISION_TRACES  (Full pipeline audit trail — replaces ChromaDB decisions)
-- ============================================================

CREATE TABLE decision_traces (
    trace_id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    signal_id               UUID REFERENCES signals(signal_id),
    timestamp               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    headline                TEXT NOT NULL,
    supplier                TEXT,
    category                signal_category NOT NULL,
    severity                severity_level NOT NULL,

    -- Hades
    hades_passed            BOOLEAN NOT NULL DEFAULT FALSE,
    hades_notes             TEXT[] NOT NULL DEFAULT '{}',

    -- Artemis
    trend_suppressed        BOOLEAN NOT NULL DEFAULT FALSE,
    trend_regime            market_regime,
    trend_vix               FLOAT8 NOT NULL DEFAULT 0.0,

    -- Pythia
    pattern_confidence      FLOAT8 NOT NULL DEFAULT 0.0,
    pattern_size_pct        FLOAT8 NOT NULL DEFAULT 0.0,

    -- ZEUS
    zeus_reasoning          TEXT,
    zeus_approved           BOOLEAN NOT NULL DEFAULT FALSE,
    zeus_override           BOOLEAN NOT NULL DEFAULT FALSE,
    zeus_override_reason    TEXT,

    -- Ares outcome
    trade_placed            BOOLEAN NOT NULL DEFAULT FALSE,
    symbol                  TEXT,
    side                    trade_side,
    fill_price              FLOAT8,
    pnl_pct                 FLOAT8,             -- backfilled by Argus

    -- Kill info
    killed_at_stage         TEXT,
    kill_reason             TEXT,

    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_decision_traces_timestamp  ON decision_traces(timestamp DESC);
CREATE INDEX idx_decision_traces_signal_id  ON decision_traces(signal_id);
CREATE INDEX idx_decision_traces_category   ON decision_traces(category);
CREATE INDEX idx_decision_traces_approved   ON decision_traces(zeus_approved);
CREATE INDEX idx_decision_traces_supplier   ON decision_traces(supplier);
CREATE INDEX idx_decision_traces_symbol     ON decision_traces(symbol);

-- Auto-update updated_at
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = NOW(); RETURN NEW; END;
$$;

CREATE TRIGGER trg_decision_traces_updated_at
    BEFORE UPDATE ON decision_traces
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();


-- ============================================================
-- 6. PORTFOLIO_STATE  (Argus — equity snapshots, time-series)
-- ============================================================

CREATE TABLE portfolio_state (
    state_id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    total_equity            NUMERIC(19,4) NOT NULL,
    peak_equity             NUMERIC(19,4) NOT NULL,
    current_drawdown_pct    FLOAT8 NOT NULL CHECK (current_drawdown_pct >= 0.0),
    open_positions          INT NOT NULL DEFAULT 0,
    paper_trading           BOOLEAN NOT NULL DEFAULT TRUE,
    refreshed_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_portfolio_state_refreshed_at ON portfolio_state(refreshed_at DESC);

-- Latest state view (dashboard reads this)
CREATE VIEW portfolio_state_latest AS
    SELECT * FROM portfolio_state ORDER BY refreshed_at DESC LIMIT 1;


-- ============================================================
-- 7. PORTFOLIO_POSITIONS  (Argus — open positions)
-- ============================================================

CREATE TABLE portfolio_positions (
    position_id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    symbol              TEXT NOT NULL,
    side                position_side NOT NULL,
    qty                 NUMERIC(18,8) NOT NULL,
    avg_cost            NUMERIC(19,4) NOT NULL,
    current_price       NUMERIC(19,4),
    unrealized_pnl      NUMERIC(19,4),
    unrealized_pnl_pct  FLOAT8,
    stop_loss           NUMERIC(19,4),
    take_profit         NUMERIC(19,4),
    opened_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    refreshed_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at           TIMESTAMPTZ             -- NULL = still open
);

CREATE INDEX idx_portfolio_positions_symbol       ON portfolio_positions(symbol);
CREATE INDEX idx_portfolio_positions_open         ON portfolio_positions(closed_at) WHERE closed_at IS NULL;
CREATE INDEX idx_portfolio_positions_refreshed_at ON portfolio_positions(refreshed_at DESC);


-- ============================================================
-- 8. AGENT_HEALTH  (Watchdog reports)
-- ============================================================

CREATE TABLE agent_health (
    health_id       UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    agent_name      TEXT NOT NULL,
    status          agent_health_status NOT NULL,
    message         TEXT,
    error_count     INT NOT NULL DEFAULT 0,
    checked_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_agent_health_agent_name  ON agent_health(agent_name);
CREATE INDEX idx_agent_health_checked_at  ON agent_health(checked_at DESC);
CREATE INDEX idx_agent_health_status      ON agent_health(status);

-- Latest health per agent view
CREATE VIEW agent_health_latest AS
    SELECT DISTINCT ON (agent_name)
        agent_name, status, message, error_count, checked_at
    FROM agent_health
    ORDER BY agent_name, checked_at DESC;


-- ============================================================
-- 9. CIRCUIT_BREAKER_STATE  (per-agent CB snapshots)
-- ============================================================

CREATE TABLE circuit_breakers (
    cb_id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    agent_name      TEXT NOT NULL,
    state           cb_state NOT NULL DEFAULT 'closed',
    failure_count   INT NOT NULL DEFAULT 0,
    last_failure_at TIMESTAMPTZ,
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_cb_agent_name  ON circuit_breakers(agent_name);
CREATE INDEX idx_cb_recorded_at ON circuit_breakers(recorded_at DESC);


-- ============================================================
-- 10. KNOWLEDGE_BASE  (replaces local ChromaDB — pgvector)
-- ============================================================

CREATE TABLE knowledge_documents (
    doc_id          TEXT PRIMARY KEY,               -- "curated:{name}:chunk{i}" | "lit:{uuid}:chunk{i}" | "decision:{trace_id}"
    collection      TEXT NOT NULL,                  -- "knowledge" | "decisions" | "literature"
    document_text   TEXT NOT NULL,
    metadata        JSONB NOT NULL DEFAULT '{}',
    embedding       vector(1536),                   -- OpenAI ada-002 OR local embeddings
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_knowledge_collection    ON knowledge_documents(collection);
CREATE INDEX idx_knowledge_metadata      ON knowledge_documents USING GIN(metadata);
CREATE INDEX idx_knowledge_created_at    ON knowledge_documents(created_at DESC);

-- HNSW index for fast vector similarity search (better than IVFFlat at <100k vectors)
CREATE INDEX idx_knowledge_embedding_hnsw
    ON knowledge_documents
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE TRIGGER trg_knowledge_updated_at
    BEFORE UPDATE ON knowledge_documents
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();


-- ============================================================
-- 11. TICKER_MAP  (replaces data/ticker_map.json — Apollo maintains this)
-- ============================================================

CREATE TABLE ticker_map (
    supplier_name   TEXT PRIMARY KEY,
    ticker          TEXT NOT NULL,
    exchange        TEXT,                           -- XETRA | NYSE | NASDAQ etc.
    verified        BOOLEAN NOT NULL DEFAULT FALSE,
    last_verified   TIMESTAMPTZ,
    source          TEXT,                           -- "default" | "yfinance" | "hermes"
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_ticker_map_ticker ON ticker_map(ticker);


-- ============================================================
-- REALTIME  (enable Supabase Realtime on key tables)
-- Dashboard subscribes to these for live updates
-- ============================================================

ALTER PUBLICATION supabase_realtime ADD TABLE trades;
ALTER PUBLICATION supabase_realtime ADD TABLE decision_traces;
ALTER PUBLICATION supabase_realtime ADD TABLE portfolio_state;
ALTER PUBLICATION supabase_realtime ADD TABLE agent_health;


-- ============================================================
-- ROW LEVEL SECURITY  (lock down all tables — service role only writes)
-- ============================================================

ALTER TABLE signals             ENABLE ROW LEVEL SECURITY;
ALTER TABLE filtered_signals    ENABLE ROW LEVEL SECURITY;
ALTER TABLE macro_context       ENABLE ROW LEVEL SECURITY;
ALTER TABLE trades              ENABLE ROW LEVEL SECURITY;
ALTER TABLE decision_traces     ENABLE ROW LEVEL SECURITY;
ALTER TABLE portfolio_state     ENABLE ROW LEVEL SECURITY;
ALTER TABLE portfolio_positions ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_health        ENABLE ROW LEVEL SECURITY;
ALTER TABLE circuit_breakers ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge_documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE ticker_map          ENABLE ROW LEVEL SECURITY;

-- Service role (backend) has full access — anon key is read-only for dashboard
CREATE POLICY "service_role_all" ON signals             FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY "service_role_all" ON filtered_signals    FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY "service_role_all" ON macro_context       FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY "service_role_all" ON trades              FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY "service_role_all" ON decision_traces     FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY "service_role_all" ON portfolio_state     FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY "service_role_all" ON portfolio_positions FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY "service_role_all" ON agent_health        FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY "service_role_all" ON circuit_breakers FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY "service_role_all" ON knowledge_documents FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY "service_role_all" ON ticker_map          FOR ALL TO service_role USING (true) WITH CHECK (true);

-- Anon key (React dashboard) — read-only on dashboard tables
CREATE POLICY "anon_read" ON trades             FOR SELECT TO anon USING (true);
CREATE POLICY "anon_read" ON decision_traces    FOR SELECT TO anon USING (true);
CREATE POLICY "anon_read" ON portfolio_state    FOR SELECT TO anon USING (true);
CREATE POLICY "anon_read" ON portfolio_positions FOR SELECT TO anon USING (true);
CREATE POLICY "anon_read" ON agent_health       FOR SELECT TO anon USING (true);
CREATE POLICY "anon_read" ON macro_context      FOR SELECT TO anon USING (true);
CREATE POLICY "anon_read" ON signals            FOR SELECT TO anon USING (true);
CREATE POLICY "anon_read" ON ticker_map         FOR SELECT TO anon USING (true);
