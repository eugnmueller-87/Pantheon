-- ============================================================
-- Pantheon OS — Seed: Default Ticker Map
-- Migration 002 — Pre-mapped suppliers (40+ from Apollo defaults)
--
-- Each supplier can have multiple rows — one per exchange.
-- Primary key is (supplier_name, exchange).
-- Ares selects the right row based on which market is open:
--   NYSE open  → prefer NYSE/NASDAQ/OTC ticker (USD, SMART routing)
--   XETRA open → prefer XETRA ticker (EUR, XETRA routing)
-- ============================================================

INSERT INTO ticker_map (supplier_name, ticker, exchange, verified, source) VALUES
    -- ── US-only names (NYSE / NASDAQ) ────────────────────────
    ('NVIDIA',             'NVDA',   'NASDAQ', TRUE, 'default'),
    ('ASML',               'ASML',   'NASDAQ', TRUE, 'default'),
    ('Intel',              'INTC',   'NASDAQ', TRUE, 'default'),
    ('TSMC',               'TSM',    'NYSE',   TRUE, 'default'),
    ('Qualcomm',           'QCOM',   'NASDAQ', TRUE, 'default'),
    ('Broadcom',           'AVGO',   'NASDAQ', TRUE, 'default'),
    ('Texas Instruments',  'TXN',    'NASDAQ', TRUE, 'default'),
    ('Micron',             'MU',     'NASDAQ', TRUE, 'default'),
    ('Applied Materials',  'AMAT',   'NASDAQ', TRUE, 'default'),
    ('Shell',              'SHEL',   'NYSE',   TRUE, 'default'),
    ('TotalEnergies',      'TTE',    'NYSE',   TRUE, 'default'),
    ('ABB',                'ABB',    'NYSE',   TRUE, 'default'),
    ('Stellantis',         'STLA',   'NYSE',   TRUE, 'default'),
    ('Novartis',           'NVS',    'NYSE',   TRUE, 'default'),
    ('AstraZeneca',        'AZN',    'NASDAQ', TRUE, 'default'),

    -- ── OTC ADRs — traded on US hours only ───────────────────
    ('Bosch',              'BSWQY',  'OTC',    TRUE, 'default'),  -- not publicly listed on XETRA
    ('ZF Friedrichshafen', 'ZFSVF',  'OTC',    TRUE, 'default'),  -- not publicly listed on XETRA
    ('Schneider Electric', 'SBGSY',  'OTC',    TRUE, 'default'),  -- listed on Euronext Paris, not XETRA
    ('Vestas',             'VWDRY',  'OTC',    TRUE, 'default'),  -- listed on Copenhagen, not XETRA
    ('DB Schenker',        'DBOEY',  'OTC',    TRUE, 'default'),
    ('Kuehne+Nagel',       'KHNGY',  'OTC',    TRUE, 'default'),
    ('Maersk',             'AMKBY',  'OTC',    TRUE, 'default'),
    ('Hapag-Lloyd',        'HPGLY',  'OTC',    TRUE, 'default'),
    ('Roche',              'RHHBY',  'OTC',    TRUE, 'default'),

    -- ── Dual-listed: OTC ADR (US hours) + XETRA (EU hours) ───
    ('SAP',                'SAP',    'NYSE',   TRUE, 'default'),
    ('SAP',                'SAP.DE', 'XETRA',  TRUE, 'default'),
    ('Infineon',           'IFNNY',  'OTC',    TRUE, 'default'),
    ('Infineon',           'IFX.DE', 'XETRA',  TRUE, 'default'),
    ('Siemens',            'SIEGY',  'OTC',    TRUE, 'default'),
    ('Siemens',            'SIE.DE', 'XETRA',  TRUE, 'default'),
    ('BASF',               'BASFY',  'OTC',    TRUE, 'default'),
    ('BASF',               'BAS.DE', 'XETRA',  TRUE, 'default'),
    ('Linde',              'LIN',    'NYSE',   TRUE, 'default'),
    ('Linde',              'LIN.DE', 'XETRA',  TRUE, 'default'),
    ('RWE',                'RWEOY',  'OTC',    TRUE, 'default'),
    ('RWE',                'RWE.DE', 'XETRA',  TRUE, 'default'),
    ('E.ON',               'EONGY',  'OTC',    TRUE, 'default'),
    ('E.ON',               'EOAN.DE','XETRA',  TRUE, 'default'),
    ('Siemens Energy',     'SMEGF',  'OTC',    TRUE, 'default'),
    ('Siemens Energy',     'ENR.DE', 'XETRA',  TRUE, 'default'),
    ('Volkswagen',         'VWAGY',  'OTC',    TRUE, 'default'),
    ('Volkswagen',         'VOW3.DE','XETRA',  TRUE, 'default'),
    ('BMW',                'BMWYY',  'OTC',    TRUE, 'default'),
    ('BMW',                'BMW.DE', 'XETRA',  TRUE, 'default'),
    ('Mercedes-Benz',      'MBGYY',  'OTC',    TRUE, 'default'),
    ('Mercedes-Benz',      'MBG.DE', 'XETRA',  TRUE, 'default'),
    ('Continental',        'CTTAY',  'OTC',    TRUE, 'default'),
    ('Continental',        'CON.DE', 'XETRA',  TRUE, 'default'),
    ('DHL',                'DPSGY',  'OTC',    TRUE, 'default'),
    ('DHL',                'DHL.DE', 'XETRA',  TRUE, 'default'),
    ('Bayer',              'BAYRY',  'OTC',    TRUE, 'default'),
    ('Bayer',              'BAYN.DE','XETRA',  TRUE, 'default'),
    ('Fresenius',          'FSNUY',  'OTC',    TRUE, 'default'),
    ('Fresenius',          'FRE.DE', 'XETRA',  TRUE, 'default'),
    ('Deutsche Bank',      'DB',     'NYSE',   TRUE, 'default'),
    ('Deutsche Bank',      'DBK.DE', 'XETRA',  TRUE, 'default'),
    ('Allianz',            'ALIZY',  'OTC',    TRUE, 'default'),
    ('Allianz',            'ALV.DE', 'XETRA',  TRUE, 'default'),
    ('Munich Re',          'MURGY',  'OTC',    TRUE, 'default'),
    ('Munich Re',          'MUV2.DE','XETRA',  TRUE, 'default')

ON CONFLICT (supplier_name, exchange) DO UPDATE
    SET ticker     = EXCLUDED.ticker,
        verified   = EXCLUDED.verified,
        source     = EXCLUDED.source,
        updated_at = NOW();
