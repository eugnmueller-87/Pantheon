-- ============================================================
-- Pantheon OS — Agent Seniority Tables
-- Migration 004 — Seniority tracking + Grafana panels 9-12
-- ============================================================

CREATE TYPE seniority_level AS ENUM ('Senior', 'Principal', 'Managing Director', 'Director');


-- ── Current seniority per agent (one row per agent, upserted) ─────────────────

CREATE TABLE agent_seniority (
    agent_name      TEXT PRIMARY KEY,
    level           seniority_level NOT NULL DEFAULT 'Senior',
    level_int       INT NOT NULL DEFAULT 0,
    cleared         BOOLEAN NOT NULL DEFAULT FALSE,
    criteria        JSONB NOT NULL DEFAULT '{}',   -- {criterion: bool}
    notes           TEXT[] NOT NULL DEFAULT '{}',  -- failed criterion reasons
    max_position_pct FLOAT8 NOT NULL DEFAULT 0.03,
    live_trading_allowed BOOLEAN NOT NULL DEFAULT FALSE,
    evaluated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_agent_seniority_level     ON agent_seniority(level);
CREATE INDEX idx_agent_seniority_evaluated ON agent_seniority(evaluated_at DESC);


-- ── Promotion history (append-only time-series) ───────────────────────────────

CREATE TABLE agent_seniority_history (
    history_id      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    agent_name      TEXT NOT NULL,
    from_level      seniority_level,               -- NULL = first record
    to_level        seniority_level NOT NULL,
    level_int       INT NOT NULL,
    criteria        JSONB NOT NULL DEFAULT '{}',
    promoted_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_seniority_history_agent  ON agent_seniority_history(agent_name);
CREATE INDEX idx_seniority_history_time   ON agent_seniority_history(promoted_at DESC);


-- ── System seniority view (min of all agents) ─────────────────────────────────

CREATE VIEW system_seniority AS
    SELECT
        MIN(level_int)                  AS system_level_int,
        (ARRAY['Senior','Principal','Managing Director','Director'])[MIN(level_int) + 1]
                                        AS system_level,
        MIN(max_position_pct)           AS max_position_pct,
        BOOL_AND(live_trading_allowed)  AS live_trading_allowed,
        BOOL_AND(cleared)               AS all_cleared,
        MAX(evaluated_at)               AS last_evaluated_at
    FROM agent_seniority;


-- ── RLS ───────────────────────────────────────────────────────────────────────

ALTER TABLE agent_seniority         ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_seniority_history ENABLE ROW LEVEL SECURITY;

CREATE POLICY "service_role_all" ON agent_seniority         FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY "service_role_all" ON agent_seniority_history FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY "anon_read"        ON agent_seniority         FOR SELECT TO anon USING (true);
CREATE POLICY "anon_read"        ON agent_seniority_history FOR SELECT TO anon USING (true);


-- ── Realtime ──────────────────────────────────────────────────────────────────

ALTER PUBLICATION supabase_realtime ADD TABLE agent_seniority;


-- ── RPC: get_seniority_report() — Grafana panels 9-12 ────────────────────────

CREATE OR REPLACE FUNCTION get_seniority_report()
RETURNS TABLE (
    agent_name          TEXT,
    level               TEXT,
    level_int           INT,
    cleared             BOOLEAN,
    max_position_pct    FLOAT8,
    live_trading_allowed BOOLEAN,
    evaluated_at        TIMESTAMPTZ
)
LANGUAGE sql
AS $$
    SELECT
        agent_name,
        level::TEXT,
        level_int,
        cleared,
        max_position_pct,
        live_trading_allowed,
        evaluated_at
    FROM agent_seniority
    ORDER BY level_int DESC, agent_name;
$$;


-- ── RPC: get_promotion_history(days) — promotion timeline panel ──────────────

CREATE OR REPLACE FUNCTION get_promotion_history(days_back INT DEFAULT 90)
RETURNS TABLE (
    agent_name  TEXT,
    from_level  TEXT,
    to_level    TEXT,
    level_int   INT,
    promoted_at TIMESTAMPTZ
)
LANGUAGE sql
AS $$
    SELECT
        agent_name,
        from_level::TEXT,
        to_level::TEXT,
        level_int,
        promoted_at
    FROM agent_seniority_history
    WHERE promoted_at >= NOW() - (days_back || ' days')::INTERVAL
    ORDER BY promoted_at DESC;
$$;
