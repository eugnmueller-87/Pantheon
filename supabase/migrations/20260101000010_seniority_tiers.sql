-- ============================================================
-- Pantheon OS — Seniority redesign: tier × level + real-money track
-- Twin of infra/supabase/011_seniority_tiers.sql — keep both in sync.
--
-- Agents EARN their way up four PAPER tiers — Trainee → Junior →
-- Intermediate → Senior — each with 10 levels (10 successful trades per
-- level). Real money unlocks only at the Senior tier AND when an operator
-- arms it (ARM_REAL_MONEY). A SEPARATE real-money XP track then begins.
-- Idempotent; resets all agents to Trainee L1.
-- ============================================================

ALTER TABLE agent_seniority ALTER COLUMN level DROP DEFAULT;

ALTER TABLE agent_seniority         ALTER COLUMN level     TYPE TEXT;
ALTER TABLE agent_seniority_history ALTER COLUMN from_level TYPE TEXT;
ALTER TABLE agent_seniority_history ALTER COLUMN to_level   TYPE TEXT;

ALTER TABLE agent_seniority ALTER COLUMN level SET DEFAULT 'Trainee';

DROP VIEW IF EXISTS system_seniority;
DROP TYPE IF EXISTS seniority_level;

ALTER TABLE agent_seniority ADD COLUMN IF NOT EXISTS tier                TEXT    NOT NULL DEFAULT 'Trainee';
ALTER TABLE agent_seniority ADD COLUMN IF NOT EXISTS tier_level          INT     NOT NULL DEFAULT 1;
ALTER TABLE agent_seniority ADD COLUMN IF NOT EXISTS rank                TEXT    NOT NULL DEFAULT 'Trainee L1';
ALTER TABLE agent_seniority ADD COLUMN IF NOT EXISTS wins                INT     NOT NULL DEFAULT 0;
ALTER TABLE agent_seniority ADD COLUMN IF NOT EXISTS real_money_unlocked BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE agent_seniority ADD COLUMN IF NOT EXISTS real_money_armed    BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE agent_seniority ADD COLUMN IF NOT EXISTS progress_pct        FLOAT8  NOT NULL DEFAULT 0;

UPDATE agent_seniority SET
    level                = 'Trainee',
    level_int            = 0,
    tier                 = 'Trainee',
    tier_level           = 1,
    rank                 = 'Trainee L1',
    wins                 = 0,
    max_position_pct     = 0.01,
    real_money_unlocked  = FALSE,
    real_money_armed     = FALSE,
    live_trading_allowed = FALSE,
    progress_pct         = 0;

ALTER TABLE agent_exp ADD COLUMN IF NOT EXISTS real_money_xp     BIGINT NOT NULL DEFAULT 0;
ALTER TABLE agent_exp ADD COLUMN IF NOT EXISTS real_money_level  INT    NOT NULL DEFAULT 1;
ALTER TABLE agent_exp ADD COLUMN IF NOT EXISTS real_money_wins   INT    NOT NULL DEFAULT 0;
ALTER TABLE agent_exp ADD COLUMN IF NOT EXISTS real_money_losses INT    NOT NULL DEFAULT 0;

CREATE VIEW system_seniority AS
    SELECT
        MIN(level_int)                                              AS system_level_int,
        (ARRAY['Trainee','Junior','Intermediate','Senior'])[MIN(level_int) + 1]
                                                                    AS system_level,
        MIN(tier_level)                                             AS system_tier_level,
        MIN(max_position_pct)                                       AS max_position_pct,
        BOOL_AND(real_money_unlocked)                               AS real_money_unlocked,
        BOOL_AND(live_trading_allowed)                              AS live_trading_allowed,
        BOOL_AND(cleared)                                           AS all_cleared,
        MAX(evaluated_at)                                           AS last_evaluated_at
    FROM agent_seniority;

DROP FUNCTION IF EXISTS get_seniority_report();
CREATE OR REPLACE FUNCTION get_seniority_report()
RETURNS TABLE (
    agent_name           TEXT,
    tier                 TEXT,
    tier_level           INT,
    rank                 TEXT,
    wins                 INT,
    level_int            INT,
    cleared              BOOLEAN,
    max_position_pct     FLOAT8,
    real_money_unlocked  BOOLEAN,
    live_trading_allowed BOOLEAN,
    evaluated_at         TIMESTAMPTZ
)
LANGUAGE sql
AS $$
    SELECT
        agent_name, tier, tier_level, rank, wins, level_int, cleared,
        max_position_pct, real_money_unlocked, live_trading_allowed, evaluated_at
    FROM agent_seniority
    ORDER BY level_int DESC, tier_level DESC, agent_name;
$$;

GRANT SELECT ON system_seniority TO anon, authenticated;
GRANT EXECUTE ON FUNCTION get_seniority_report() TO anon, authenticated;
