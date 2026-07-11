SCHEMA_VERSION = 3

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    trigger TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK(status IN ('running', 'success', 'failed')),
    error_code TEXT,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS raw_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id),
    endpoint TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_account_id TEXT NOT NULL,
    title TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    UNIQUE(source, source_account_id)
);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    fund_code TEXT NOT NULL CHECK(length(fund_code) = 6),
    fund_name TEXT NOT NULL,
    share_class TEXT,
    shares TEXT NOT NULL,
    formal_nav TEXT,
    estimated_nav TEXT,
    observed_profit TEXT,
    observed_at TEXT NOT NULL,
    UNIQUE(account_id, fund_code, observed_at)
);
"""

SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS funds (
    fund_code TEXT PRIMARY KEY CHECK(length(fund_code) = 6),
    fund_name TEXT,
    fund_type TEXT,
    source TEXT NOT NULL,
    observed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fund_nav (
    fund_code TEXT NOT NULL CHECK(length(fund_code) = 6),
    nav_date TEXT NOT NULL,
    unit_nav TEXT NOT NULL,
    accumulated_nav TEXT,
    daily_growth TEXT,
    source TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    PRIMARY KEY(fund_code, nav_date, source)
);

CREATE TABLE IF NOT EXISTS sector_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sector_code TEXT NOT NULL,
    sector_name TEXT NOT NULL,
    sector_kind TEXT NOT NULL CHECK(sector_kind IN ('industry', 'concept')),
    pct_change TEXT,
    turnover_rate TEXT,
    advancers INTEGER,
    decliners INTEGER,
    source TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    UNIQUE(sector_code, sector_kind, retrieved_at, source)
);
"""

SCHEMA_V3 = """
CREATE TABLE IF NOT EXISTS investment_theses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fund_code TEXT NOT NULL CHECK(length(fund_code) = 6),
    rationale TEXT NOT NULL,
    horizon TEXT NOT NULL,
    invalidation TEXT NOT NULL,
    created_at TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0, 1)) DEFAULT 1
);
"""
