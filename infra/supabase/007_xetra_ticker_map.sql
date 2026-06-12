-- ============================================================
-- Pantheon OS — Migration 007: XETRA multi-exchange ticker map
--
-- Restructures ticker_map so each supplier can have one row per
-- exchange (NYSE/NASDAQ/OTC + XETRA).  The old single-supplier
-- PRIMARY KEY is replaced with a (supplier_name, exchange)
-- composite key.  get_ticker_for_market() in supabase_client.py
-- selects the right row based on which market is currently open.
--
-- Run in: Supabase Dashboard → SQL Editor
-- ============================================================

-- ── 1. Drop old PRIMARY KEY constraint (keeps the data) ──────
ALTER TABLE ticker_map DROP CONSTRAINT ticker_map_pkey;

-- ── 2. Add composite PRIMARY KEY ─────────────────────────────
ALTER TABLE ticker_map ADD PRIMARY KEY (supplier_name, exchange);

-- ── 3. Backfill exchange = 'OTC' where it is NULL ────────────
--    (legacy rows inserted without an exchange value)
UPDATE ticker_map SET exchange = 'OTC' WHERE exchange IS NULL;

-- ── 4. Make exchange NOT NULL now that every row has a value ──
ALTER TABLE ticker_map ALTER COLUMN exchange SET NOT NULL;

-- ── 5. Insert XETRA rows for the 14 German blue-chips ────────
--    These live alongside the existing OTC rows for the same
--    supplier.  Ares selects by exchange at order time.
INSERT INTO ticker_map (supplier_name, ticker, exchange, verified, source) VALUES
    ('Siemens',            'SIE.DE',   'XETRA', TRUE, 'default'),
    ('Volkswagen',         'VOW3.DE',  'XETRA', TRUE, 'default'),
    ('BMW',                'BMW.DE',   'XETRA', TRUE, 'default'),
    ('Mercedes-Benz',      'MBG.DE',   'XETRA', TRUE, 'default'),
    ('BASF',               'BAS.DE',   'XETRA', TRUE, 'default'),
    ('Bayer',              'BAYN.DE',  'XETRA', TRUE, 'default'),
    ('Allianz',            'ALV.DE',   'XETRA', TRUE, 'default'),
    ('Munich Re',          'MUV2.DE',  'XETRA', TRUE, 'default'),
    ('E.ON',               'EOAN.DE',  'XETRA', TRUE, 'default'),
    ('RWE',                'RWE.DE',   'XETRA', TRUE, 'default'),
    ('Infineon',           'IFX.DE',   'XETRA', TRUE, 'default'),
    ('Continental',        'CON.DE',   'XETRA', TRUE, 'default'),
    ('Deutsche Bank',      'DBK.DE',   'XETRA', TRUE, 'default'),
    ('DHL',                'DHL.DE',   'XETRA', TRUE, 'default'),
    ('Fresenius',          'FRE.DE',   'XETRA', TRUE, 'default'),
    ('Siemens Energy',     'ENR.DE',   'XETRA', TRUE, 'default'),
    ('SAP',                'SAP.DE',   'XETRA', TRUE, 'default'),
    ('Linde',              'LIN.DE',   'XETRA', TRUE, 'default')
ON CONFLICT (supplier_name, exchange) DO UPDATE
    SET ticker     = EXCLUDED.ticker,
        verified   = EXCLUDED.verified,
        source     = EXCLUDED.source,
        updated_at = NOW();

-- ── 6. Re-grant access (composite PK rebuild drops no policies,
--       but be explicit so future role audits stay consistent) ──
GRANT SELECT, INSERT, UPDATE, DELETE ON ticker_map TO service_role;
GRANT SELECT                          ON ticker_map TO anon;
