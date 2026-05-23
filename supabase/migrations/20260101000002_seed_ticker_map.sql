-- ============================================================
-- Pantheon OS — Seed: Default Ticker Map
-- Migration 002 — Pre-mapped suppliers (40+ from Apollo defaults)
-- ============================================================

INSERT INTO ticker_map (supplier_name, ticker, exchange, verified, source) VALUES
    -- Tech / Semiconductors
    ('NVIDIA',             'NVDA',   'NASDAQ', TRUE, 'default'),
    ('SAP',                'SAP',    'NYSE',   TRUE, 'default'),
    ('Infineon',           'IFNNY',  'OTC',    TRUE, 'default'),
    ('ASML',               'ASML',   'NASDAQ', TRUE, 'default'),
    ('Siemens',            'SIEGY',  'OTC',    TRUE, 'default'),
    ('Bosch',              'BSWQY',  'OTC',    TRUE, 'default'),
    ('Intel',              'INTC',   'NASDAQ', TRUE, 'default'),
    ('TSMC',               'TSM',    'NYSE',   TRUE, 'default'),
    ('Qualcomm',           'QCOM',   'NASDAQ', TRUE, 'default'),
    ('Broadcom',           'AVGO',   'NASDAQ', TRUE, 'default'),
    ('Texas Instruments',  'TXN',    'NASDAQ', TRUE, 'default'),
    ('Micron',             'MU',     'NASDAQ', TRUE, 'default'),
    ('Applied Materials',  'AMAT',   'NASDAQ', TRUE, 'default'),
    ('BASF',               'BASFY',  'OTC',    TRUE, 'default'),
    ('Linde',              'LIN',    'NYSE',   TRUE, 'default'),

    -- Energy / Industrials
    ('Shell',              'SHEL',   'NYSE',   TRUE, 'default'),
    ('TotalEnergies',      'TTE',    'NYSE',   TRUE, 'default'),
    ('RWE',                'RWEOY',  'OTC',    TRUE, 'default'),
    ('E.ON',               'EONGY',  'OTC',    TRUE, 'default'),
    ('Siemens Energy',     'SMEGF',  'OTC',    TRUE, 'default'),
    ('Vestas',             'VWDRY',  'OTC',    TRUE, 'default'),
    ('Schneider Electric', 'SBGSY',  'OTC',    TRUE, 'default'),
    ('ABB',                'ABB',    'NYSE',   TRUE, 'default'),

    -- Automotive
    ('Volkswagen',         'VWAGY',  'OTC',    TRUE, 'default'),
    ('BMW',                'BMWYY',  'OTC',    TRUE, 'default'),
    ('Mercedes-Benz',      'MBGYY',  'OTC',    TRUE, 'default'),
    ('Stellantis',         'STLA',   'NYSE',   TRUE, 'default'),
    ('Continental',        'CTTAY',  'OTC',    TRUE, 'default'),
    ('ZF Friedrichshafen', 'ZFSVF',  'OTC',    TRUE, 'default'),

    -- Logistics / Procurement
    ('DHL',                'DPSGY',  'OTC',    TRUE, 'default'),
    ('DB Schenker',        'DBOEY',  'OTC',    TRUE, 'default'),
    ('Kuehne+Nagel',       'KHNGY',  'OTC',    TRUE, 'default'),
    ('Maersk',             'AMKBY',  'OTC',    TRUE, 'default'),
    ('Hapag-Lloyd',        'HPGLY',  'OTC',    TRUE, 'default'),

    -- Pharma / Healthcare
    ('Bayer',              'BAYRY',  'OTC',    TRUE, 'default'),
    ('Fresenius',          'FSNUY',  'OTC',    TRUE, 'default'),
    ('Roche',              'RHHBY',  'OTC',    TRUE, 'default'),
    ('Novartis',           'NVS',    'NYSE',   TRUE, 'default'),
    ('AstraZeneca',        'AZN',    'NASDAQ', TRUE, 'default'),

    -- Finance
    ('Deutsche Bank',      'DB',     'NYSE',   TRUE, 'default'),
    ('Allianz',            'ALIZY',  'OTC',    TRUE, 'default'),
    ('Munich Re',          'MURGY',  'OTC',    TRUE, 'default')

ON CONFLICT (supplier_name) DO UPDATE
    SET ticker       = EXCLUDED.ticker,
        exchange     = EXCLUDED.exchange,
        verified     = EXCLUDED.verified,
        source       = EXCLUDED.source,
        updated_at   = NOW();
