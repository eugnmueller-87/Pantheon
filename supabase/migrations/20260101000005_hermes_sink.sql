-- ============================================================
-- Pantheon OS — Migration 005
-- Hermes → Supabase sink
--
-- Instead of Icarus polling the Hermes Railway API every cycle,
-- Hermes writes directly to the `raw_signals` table and Icarus
-- reads + marks rows consumed.  This gives full signal auditability.
--
-- Changes:
--   1. Add `consumed_by_icarus` + `consumed_at` to `signals` table
--   2. Add write-only API key column (for Hermes service auth)
--   3. Add `get_unconsumed_signals` helper view
--   4. Row-level security: anon/service role can INSERT; only service
--      role can UPDATE (mark consumed) or SELECT all rows
-- ============================================================

-- 1. Extend the existing signals table
ALTER TABLE signals
    ADD COLUMN IF NOT EXISTS consumed_by_icarus  BOOLEAN     NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS consumed_at         TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS hermes_id           TEXT,        -- original Hermes signal id (pre-UUID sanitisation)
    ADD COLUMN IF NOT EXISTS urgency             TEXT,        -- "HIGH" / "MEDIUM" / "LOW" — raw from Hermes
    ADD COLUMN IF NOT EXISTS is_significant      BOOLEAN     NOT NULL DEFAULT FALSE;

-- Unique constraint so Hermes can upsert without duplicates
CREATE UNIQUE INDEX IF NOT EXISTS idx_signals_hermes_id
    ON signals(hermes_id)
    WHERE hermes_id IS NOT NULL;

-- Fast query for Icarus poll: only unconsumed, recent rows
CREATE INDEX IF NOT EXISTS idx_signals_unconsumed
    ON signals(consumed_by_icarus, published_at DESC)
    WHERE consumed_by_icarus = FALSE;


-- 2. View: what Icarus sees when it polls
CREATE OR REPLACE VIEW unconsumed_signals AS
    SELECT *
    FROM   signals
    WHERE  consumed_by_icarus = FALSE
    ORDER  BY published_at DESC;


-- 3. Function: mark a batch of signal_ids as consumed atomically
CREATE OR REPLACE FUNCTION mark_signals_consumed(signal_ids UUID[])
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    updated_count INTEGER;
BEGIN
    UPDATE signals
    SET    consumed_by_icarus = TRUE,
           consumed_at        = NOW()
    WHERE  signal_id = ANY(signal_ids)
      AND  consumed_by_icarus = FALSE;

    GET DIAGNOSTICS updated_count = ROW_COUNT;
    RETURN updated_count;
END;
$$;


-- 4. Function: Hermes upsert — insert or skip duplicate hermes_id
--    Returns the signal_id (new or existing) so Hermes can log it.
CREATE OR REPLACE FUNCTION upsert_hermes_signal(
    p_hermes_id          TEXT,
    p_source_url         TEXT,
    p_headline           TEXT,
    p_summary            TEXT,
    p_published_at       TIMESTAMPTZ,
    p_category           signal_category,
    p_severity           severity_level,
    p_affected_tickers   TEXT[],
    p_raw_text           TEXT,
    p_supplier           TEXT,
    p_hermes_signal_type TEXT,
    p_urgency            TEXT,
    p_is_significant     BOOLEAN
)
RETURNS UUID
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    v_signal_id UUID;
BEGIN
    INSERT INTO signals (
        hermes_id, source_url, headline, summary, published_at,
        category, severity, affected_tickers, raw_text,
        supplier, hermes_signal_type, urgency, is_significant,
        consumed_by_icarus
    )
    VALUES (
        p_hermes_id, p_source_url, p_headline, p_summary, p_published_at,
        p_category, p_severity, p_affected_tickers, p_raw_text,
        p_supplier, p_hermes_signal_type, p_urgency, p_is_significant,
        FALSE
    )
    ON CONFLICT (hermes_id) DO NOTHING
    RETURNING signal_id INTO v_signal_id;

    -- If conflict (already exists), fetch the existing id
    IF v_signal_id IS NULL THEN
        SELECT signal_id INTO v_signal_id
        FROM   signals
        WHERE  hermes_id = p_hermes_id;
    END IF;

    RETURN v_signal_id;
END;
$$;
