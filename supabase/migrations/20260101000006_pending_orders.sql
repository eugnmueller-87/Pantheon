-- ============================================================
-- Pantheon OS — Migration 006
-- Durable pending-order queue
--
-- When IBGateway is unreachable at the moment ZEUS approves a trade,
-- Ares writes the approval here instead of dropping it.  The next
-- cycle with a healthy IB connection drains the queue.
--
-- Safety guards baked in:
--   • signal_id UNIQUE — one queue entry per approved signal (idempotent)
--   • expires_at — trade is never placed after this timestamp
--   • status enum — PENDING → SUBMITTING → SUBMITTED | REJECTED | EXPIRED
--   • partial index on status='PENDING' — fast retry query
-- ============================================================

CREATE TABLE IF NOT EXISTS pending_orders (
    id              BIGSERIAL       PRIMARY KEY,
    signal_id       TEXT            NOT NULL UNIQUE,   -- idempotency key
    symbol          TEXT            NOT NULL,
    side            TEXT            NOT NULL,          -- 'BUY' | 'SELL'
    payload         JSONB           NOT NULL,          -- {signal_id, symbol, side, position_size_pct, confidence}
    status          TEXT            NOT NULL DEFAULT 'PENDING',  -- PENDING | SUBMITTING | SUBMITTED | REJECTED | EXPIRED
    attempts        INT             NOT NULL DEFAULT 0,
    last_error      TEXT,
    approved_at     TIMESTAMPTZ     NOT NULL,
    expires_at      TIMESTAMPTZ     NOT NULL,
    last_attempt_at TIMESTAMPTZ,
    ib_order_id     TEXT,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

-- Fast lookup: only retryable rows
CREATE INDEX IF NOT EXISTS idx_pending_orders_retryable
    ON pending_orders (approved_at ASC)
    WHERE status = 'PENDING';

-- Lookup by signal_id (used for idempotency check before placing)
CREATE INDEX IF NOT EXISTS idx_pending_orders_signal_id
    ON pending_orders (signal_id);

-- ── Row-level security (mirror migration 005 style) ────────────────────────────
-- Service role has full access; anon role has no access (queue is backend-only).
ALTER TABLE pending_orders ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE tablename = 'pending_orders' AND policyname = 'service_role_all'
    ) THEN
        CREATE POLICY service_role_all ON pending_orders
            FOR ALL TO service_role USING (true) WITH CHECK (true);
    END IF;
END
$$;

-- Grant explicit access so PostgREST sees the table (required since Oct 2026)
GRANT ALL ON TABLE pending_orders TO service_role;
GRANT USAGE, SELECT ON SEQUENCE pending_orders_id_seq TO service_role;
