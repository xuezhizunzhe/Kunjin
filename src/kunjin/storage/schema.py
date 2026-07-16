SCHEMA_VERSION = 14

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

SCHEMA_V4 = """
CREATE TABLE IF NOT EXISTS imported_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256 TEXT NOT NULL UNIQUE,
    original_name TEXT NOT NULL,
    managed_path TEXT,
    document_type TEXT NOT NULL CHECK(document_type IN ('alipay_payment', 'unknown')),
    imported_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'deleted')) DEFAULT 'active',
    deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS ocr_fields (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES imported_documents(id),
    field_name TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    normalized_value TEXT,
    confidence TEXT NOT NULL,
    evidence_level TEXT NOT NULL CHECK(evidence_level IN (
        'transaction_confirmed', 'user_confirmed', 'position_inferred'
    )),
    UNIQUE(document_id, field_name)
);

CREATE TABLE IF NOT EXISTS transaction_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_document_id INTEGER REFERENCES imported_documents(id),
    transaction_type TEXT NOT NULL,
    fund_code TEXT CHECK(fund_code IS NULL OR length(fund_code) = 6),
    fund_name TEXT,
    amount TEXT,
    shares TEXT,
    nav TEXT,
    fee TEXT,
    order_time TEXT,
    confirmation_time TEXT,
    evidence_level TEXT NOT NULL CHECK(evidence_level IN (
        'transaction_confirmed', 'user_confirmed', 'position_inferred'
    )),
    field_evidence_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'confirmed', 'rejected')) DEFAULT 'pending',
    created_at TEXT NOT NULL,
    confirmed_at TEXT
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_document_id INTEGER REFERENCES imported_documents(id),
    transaction_type TEXT NOT NULL,
    fund_code TEXT NOT NULL CHECK(length(fund_code) = 6),
    fund_name TEXT,
    amount TEXT,
    shares TEXT,
    nav TEXT,
    fee TEXT,
    order_time TEXT,
    confirmation_time TEXT,
    evidence_level TEXT NOT NULL CHECK(evidence_level IN (
        'transaction_confirmed', 'user_confirmed', 'position_inferred'
    )),
    field_evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS transactions_no_update
BEFORE UPDATE ON transactions
BEGIN
    SELECT RAISE(ABORT, 'transactions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS transactions_no_delete
BEFORE DELETE ON transactions
BEGIN
    SELECT RAISE(ABORT, 'transactions are immutable');
END;
"""

SCHEMA_V5 = """
CREATE TABLE IF NOT EXISTS fund_source_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fund_code TEXT NOT NULL CHECK(length(fund_code) = 6),
    document_kind TEXT NOT NULL CHECK(document_kind IN (
        'basic_profile', 'manager_history', 'fee_schedule', 'size_history',
        'benchmark', 'quarterly_holdings', 'industry_exposure', 'announcement'
    )),
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_tier INTEGER NOT NULL CHECK(source_tier BETWEEN 1 AND 3),
    publisher TEXT NOT NULL,
    published_at TEXT,
    retrieved_at TEXT NOT NULL,
    checksum TEXT NOT NULL,
    UNIQUE(fund_code, document_kind, url, checksum)
);

CREATE TABLE IF NOT EXISTS fund_identities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fund_code TEXT NOT NULL CHECK(length(fund_code) = 6),
    record_key TEXT NOT NULL,
    fund_name TEXT NOT NULL,
    status TEXT NOT NULL,
    fund_type TEXT,
    established_date TEXT,
    manager_name TEXT,
    source_document_id INTEGER NOT NULL REFERENCES fund_source_documents(id) ON DELETE RESTRICT,
    UNIQUE(fund_code, record_key, source_document_id)
);

CREATE TABLE IF NOT EXISTS fund_share_classes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fund_code TEXT NOT NULL CHECK(length(fund_code) = 6),
    record_key TEXT NOT NULL,
    related_fund_code TEXT NOT NULL CHECK(length(related_fund_code) = 6),
    share_class TEXT NOT NULL CHECK(share_class IN ('A', 'C')),
    fund_name TEXT,
    source_document_id INTEGER NOT NULL REFERENCES fund_source_documents(id) ON DELETE RESTRICT,
    UNIQUE(fund_code, record_key, source_document_id)
);

CREATE TABLE IF NOT EXISTS fund_manager_tenures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fund_code TEXT NOT NULL CHECK(length(fund_code) = 6),
    record_key TEXT NOT NULL,
    manager_name TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT,
    source_document_id INTEGER NOT NULL REFERENCES fund_source_documents(id) ON DELETE RESTRICT,
    UNIQUE(fund_code, record_key, source_document_id)
);

CREATE TABLE IF NOT EXISTS fund_fee_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fund_code TEXT NOT NULL CHECK(length(fund_code) = 6),
    record_key TEXT NOT NULL,
    fee_type TEXT NOT NULL CHECK(fee_type IN (
        'management', 'custody', 'sales_service', 'subscription', 'redemption'
    )),
    share_class TEXT CHECK(share_class IS NULL OR share_class IN ('A', 'C')),
    rate TEXT,
    fixed_amount TEXT,
    amount_min TEXT,
    amount_max TEXT,
    holding_days_min INTEGER,
    holding_days_max INTEGER,
    rule_order INTEGER NOT NULL,
    effective_from TEXT,
    effective_to TEXT,
    raw_rule_text TEXT NOT NULL,
    source_document_id INTEGER NOT NULL REFERENCES fund_source_documents(id) ON DELETE RESTRICT,
    UNIQUE(fund_code, record_key, source_document_id)
);

CREATE TABLE IF NOT EXISTS fund_sizes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fund_code TEXT NOT NULL CHECK(length(fund_code) = 6),
    record_key TEXT NOT NULL,
    report_date TEXT NOT NULL,
    net_assets TEXT,
    total_shares TEXT,
    published_at TEXT,
    source_document_id INTEGER NOT NULL REFERENCES fund_source_documents(id) ON DELETE RESTRICT,
    UNIQUE(fund_code, record_key, source_document_id)
);

CREATE TABLE IF NOT EXISTS fund_benchmarks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fund_code TEXT NOT NULL CHECK(length(fund_code) = 6),
    record_key TEXT NOT NULL,
    description TEXT NOT NULL,
    effective_from TEXT,
    effective_to TEXT,
    source_document_id INTEGER NOT NULL REFERENCES fund_source_documents(id) ON DELETE RESTRICT,
    UNIQUE(fund_code, record_key, source_document_id)
);

CREATE TABLE IF NOT EXISTS fund_holdings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fund_code TEXT NOT NULL CHECK(length(fund_code) = 6),
    record_key TEXT NOT NULL,
    report_period TEXT NOT NULL,
    published_at TEXT NOT NULL,
    rank INTEGER NOT NULL,
    security_code TEXT NOT NULL,
    security_name TEXT NOT NULL,
    asset_type TEXT NOT NULL CHECK(asset_type IN ('stock', 'bond', 'fund', 'cash', 'other')),
    weight TEXT NOT NULL,
    disclosure_scope TEXT NOT NULL,
    shares TEXT,
    market_value TEXT,
    source_document_id INTEGER NOT NULL REFERENCES fund_source_documents(id) ON DELETE RESTRICT,
    UNIQUE(fund_code, record_key, source_document_id)
);

CREATE TABLE IF NOT EXISTS fund_industry_exposure (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fund_code TEXT NOT NULL CHECK(length(fund_code) = 6),
    record_key TEXT NOT NULL,
    report_period TEXT NOT NULL,
    published_at TEXT NOT NULL,
    classification_standard TEXT NOT NULL,
    industry_name TEXT NOT NULL,
    weight TEXT NOT NULL,
    industry_code TEXT,
    market_value TEXT,
    source_document_id INTEGER NOT NULL REFERENCES fund_source_documents(id) ON DELETE RESTRICT,
    UNIQUE(fund_code, record_key, source_document_id)
);

CREATE TABLE IF NOT EXISTS fund_announcements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fund_code TEXT NOT NULL CHECK(length(fund_code) = 6),
    record_key TEXT NOT NULL,
    title TEXT NOT NULL,
    category TEXT,
    publisher TEXT NOT NULL,
    published_at TEXT NOT NULL,
    url TEXT NOT NULL,
    source_tier INTEGER NOT NULL CHECK(source_tier BETWEEN 1 AND 3),
    source_document_id INTEGER NOT NULL REFERENCES fund_source_documents(id) ON DELETE RESTRICT,
    UNIQUE(fund_code, url, source_document_id)
);

CREATE TABLE IF NOT EXISTS fund_section_syncs (
    fund_code TEXT NOT NULL CHECK(length(fund_code) = 6),
    section TEXT NOT NULL CHECK(section IN (
        'basic_profile', 'manager_history', 'fee_schedule', 'size_history',
        'benchmark', 'quarterly_holdings', 'industry_exposure', 'announcement'
    )),
    state TEXT NOT NULL CHECK(state IN ('success', 'not_disclosed', 'source_unavailable')),
    current_source_document_id INTEGER REFERENCES fund_source_documents(id) ON DELETE RESTRICT,
    last_attempted_at TEXT NOT NULL,
    last_success_at TEXT,
    warning TEXT,
    error_code TEXT,
    error_message TEXT,
    PRIMARY KEY(fund_code, section)
);
"""

SCHEMA_V6 = """
CREATE TABLE IF NOT EXISTS fund_peer_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    anchor_fund_code TEXT NOT NULL CHECK(length(anchor_fund_code) = 6),
    rule_version TEXT NOT NULL,
    rule_key TEXT NOT NULL,
    rule_description TEXT NOT NULL,
    candidate_source_url TEXT NOT NULL,
    candidate_source_tier INTEGER NOT NULL CHECK(candidate_source_tier BETWEEN 1 AND 3),
    candidate_source_checksum TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('success', 'partial')),
    warning TEXT,
    UNIQUE(anchor_fund_code, rule_version, input_fingerprint)
);

CREATE TABLE IF NOT EXISTS fund_peer_group_members (
    peer_group_id INTEGER NOT NULL REFERENCES fund_peer_groups(id) ON DELETE RESTRICT,
    fund_code TEXT NOT NULL CHECK(length(fund_code) = 6),
    membership_kind TEXT NOT NULL CHECK(membership_kind IN (
        'anchor', 'user_supplied', 'held', 'discovered'
    )),
    classification_key TEXT NOT NULL,
    acceptance_reason TEXT NOT NULL,
    warning TEXT,
    profile_source_document_id INTEGER REFERENCES fund_source_documents(id) ON DELETE RESTRICT,
    PRIMARY KEY(peer_group_id, fund_code)
);

CREATE TABLE IF NOT EXISTS fund_peer_group_syncs (
    anchor_fund_code TEXT PRIMARY KEY CHECK(length(anchor_fund_code) = 6),
    current_peer_group_id INTEGER REFERENCES fund_peer_groups(id) ON DELETE RESTRICT,
    state TEXT NOT NULL CHECK(state IN ('success', 'partial', 'source_unavailable')),
    last_attempted_at TEXT NOT NULL,
    last_success_at TEXT,
    error_code TEXT,
    warning TEXT
);

CREATE TABLE IF NOT EXISTS fund_comparison_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    comparison_kind TEXT NOT NULL CHECK(comparison_kind IN (
        'peer', 'explicit', 'portfolio_overlap'
    )),
    anchor_fund_code TEXT CHECK(anchor_fund_code IS NULL OR length(anchor_fund_code) = 6),
    peer_group_id INTEGER REFERENCES fund_peer_groups(id) ON DELETE RESTRICT,
    calculation_version TEXT NOT NULL,
    as_of TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('success', 'partial', 'insufficient_data')),
    input_fingerprint TEXT NOT NULL,
    result_json TEXT NOT NULL,
    warning TEXT,
    UNIQUE(comparison_kind, input_fingerprint, calculation_version)
);
"""

SCHEMA_V7 = """
CREATE TABLE IF NOT EXISTS financial_profile_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version INTEGER NOT NULL UNIQUE CHECK(version > 0),
    status TEXT NOT NULL CHECK(status IN (
        'draft', 'confirmed', 'superseded', 'invalidated'
    )),
    encryption_algorithm TEXT NOT NULL CHECK(encryption_algorithm = 'AES-256-GCM'),
    encryption_key_version TEXT NOT NULL,
    nonce TEXT NOT NULL,
    encrypted_payload TEXT NOT NULL,
    keyed_payload_fingerprint TEXT NOT NULL,
    confirmed_at TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    invalidated_at TEXT,
    invalidation_reason TEXT,
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS one_confirmed_financial_profile
ON financial_profile_versions(status)
WHERE status = 'confirmed';

CREATE TRIGGER IF NOT EXISTS financial_profile_payload_no_update
BEFORE UPDATE ON financial_profile_versions
WHEN OLD.version != NEW.version
  OR OLD.encryption_algorithm != NEW.encryption_algorithm
  OR OLD.encryption_key_version != NEW.encryption_key_version
  OR OLD.nonce != NEW.nonce
  OR OLD.encrypted_payload != NEW.encrypted_payload
  OR OLD.keyed_payload_fingerprint != NEW.keyed_payload_fingerprint
  OR OLD.confirmed_at != NEW.confirmed_at
  OR OLD.valid_until != NEW.valid_until
  OR OLD.created_at != NEW.created_at
BEGIN
    SELECT RAISE(ABORT, 'profile payload is immutable');
END;

CREATE TRIGGER IF NOT EXISTS financial_profile_lifecycle_on_insert
BEFORE INSERT ON financial_profile_versions
WHEN (NEW.status = 'invalidated' AND (
        NEW.invalidated_at IS NULL
        OR NEW.invalidation_reason IS NULL
        OR length(trim(NEW.invalidation_reason)) = 0
    ))
    OR (NEW.status != 'invalidated' AND (
        NEW.invalidated_at IS NOT NULL
        OR NEW.invalidation_reason IS NOT NULL
    ))
BEGIN
    SELECT RAISE(ABORT, 'invalid profile invalidation metadata');
END;

CREATE TRIGGER IF NOT EXISTS financial_profile_lifecycle_on_update
BEFORE UPDATE ON financial_profile_versions
WHEN (OLD.status = 'draft' AND NEW.status NOT IN (
        'draft', 'confirmed', 'invalidated'
    ))
    OR (OLD.status = 'confirmed' AND NEW.status NOT IN (
        'confirmed', 'superseded', 'invalidated'
    ))
    OR OLD.status IN ('superseded', 'invalidated')
BEGIN
    SELECT RAISE(ABORT, 'invalid profile lifecycle transition');
END;

CREATE TRIGGER IF NOT EXISTS financial_profile_invalidation_metadata_on_update
BEFORE UPDATE ON financial_profile_versions
WHEN (NEW.status = 'invalidated' AND (
        NEW.invalidated_at IS NULL
        OR NEW.invalidation_reason IS NULL
        OR length(trim(NEW.invalidation_reason)) = 0
    ))
    OR (NEW.status != 'invalidated' AND (
        NEW.invalidated_at IS NOT NULL
        OR NEW.invalidation_reason IS NOT NULL
    ))
BEGIN
    SELECT RAISE(ABORT, 'invalid profile invalidation metadata');
END;

CREATE TRIGGER IF NOT EXISTS financial_profile_no_delete
BEFORE DELETE ON financial_profile_versions
BEGIN
    SELECT RAISE(ABORT, 'profile versions are immutable');
END;
"""

LEGACY_SCHEMA_V8 = """
CREATE TABLE IF NOT EXISTS suitability_policy_versions (
    version TEXT PRIMARY KEY NOT NULL,
    canonical_policy_json TEXT NOT NULL,
    policy_checksum TEXT NOT NULL,
    effective_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS suitability_assessments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_version_id INTEGER NOT NULL
        REFERENCES financial_profile_versions(id) ON DELETE RESTRICT,
    policy_version TEXT NOT NULL
        REFERENCES suitability_policy_versions(version) ON DELETE RESTRICT,
    input_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'blocked', 'constrained', 'ready_for_allocation'
    )),
    hard_blocks_json TEXT NOT NULL,
    constraints_json TEXT NOT NULL,
    safe_summary_json TEXT NOT NULL,
    encrypted_amount_results TEXT NOT NULL,
    encryption_algorithm TEXT NOT NULL CHECK(encryption_algorithm = 'AES-256-GCM'),
    encryption_key_version TEXT NOT NULL,
    nonce TEXT NOT NULL,
    keyed_payload_fingerprint TEXT NOT NULL,
    assessed_at TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS suitability_policy_no_update
BEFORE UPDATE ON suitability_policy_versions
BEGIN
    SELECT RAISE(ABORT, 'suitability policies are immutable');
END;

CREATE TRIGGER IF NOT EXISTS suitability_policy_no_delete
BEFORE DELETE ON suitability_policy_versions
BEGIN
    SELECT RAISE(ABORT, 'suitability policies are immutable');
END;

CREATE TRIGGER IF NOT EXISTS suitability_assessment_no_update
BEFORE UPDATE ON suitability_assessments
BEGIN
    SELECT RAISE(ABORT, 'suitability assessments are immutable');
END;

CREATE TRIGGER IF NOT EXISTS suitability_assessment_no_delete
BEFORE DELETE ON suitability_assessments
BEGIN
    SELECT RAISE(ABORT, 'suitability assessments are immutable');
END;
"""

SCHEMA_V8 = """
CREATE TABLE IF NOT EXISTS suitability_policy_versions (
    version TEXT PRIMARY KEY NOT NULL CHECK(length(trim(version)) > 0),
    canonical_policy_json TEXT NOT NULL CHECK(
        json_valid(canonical_policy_json)
        AND json_type(canonical_policy_json) = 'object'
    ),
    policy_checksum TEXT NOT NULL CHECK(
        length(policy_checksum) = 64
        AND policy_checksum NOT GLOB '*[^0-9a-f]*'
    ),
    effective_at TEXT NOT NULL CHECK(
        length(trim(effective_at)) > 0
        AND julianday(effective_at) IS NOT NULL
    ),
    created_at TEXT NOT NULL CHECK(
        length(trim(created_at)) > 0
        AND julianday(created_at) IS NOT NULL
    )
);

CREATE TABLE IF NOT EXISTS suitability_assessments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_version_id INTEGER NOT NULL
        REFERENCES financial_profile_versions(id) ON DELETE RESTRICT,
    policy_version TEXT NOT NULL CHECK(length(trim(policy_version)) > 0)
        REFERENCES suitability_policy_versions(version) ON DELETE RESTRICT,
    input_fingerprint TEXT NOT NULL CHECK(
        length(input_fingerprint) = 64
        AND input_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    status TEXT NOT NULL CHECK(status IN (
        'blocked', 'constrained', 'ready_for_allocation'
    )),
    hard_blocks_json TEXT NOT NULL CHECK(
        json_valid(hard_blocks_json)
        AND json_type(hard_blocks_json) = 'array'
    ),
    constraints_json TEXT NOT NULL CHECK(
        json_valid(constraints_json)
        AND json_type(constraints_json) = 'array'
    ),
    safe_summary_json TEXT NOT NULL CHECK(
        json_valid(safe_summary_json)
        AND json_type(safe_summary_json) = 'object'
    ),
    encrypted_amount_results TEXT NOT NULL CHECK(
        length(trim(encrypted_amount_results)) > 0
    ),
    encryption_algorithm TEXT NOT NULL CHECK(encryption_algorithm = 'AES-256-GCM'),
    encryption_key_version TEXT NOT NULL CHECK(
        length(trim(encryption_key_version)) > 0
    ),
    nonce TEXT NOT NULL CHECK(length(trim(nonce)) > 0),
    keyed_payload_fingerprint TEXT NOT NULL CHECK(
        length(keyed_payload_fingerprint) = 64
        AND keyed_payload_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    assessed_at TEXT NOT NULL CHECK(
        length(trim(assessed_at)) > 0
        AND julianday(assessed_at) IS NOT NULL
    ),
    valid_until TEXT NOT NULL CHECK(
        length(trim(valid_until)) > 0
        AND julianday(valid_until) IS NOT NULL
        AND julianday(valid_until) > julianday(assessed_at)
    ),
    created_at TEXT NOT NULL CHECK(
        length(trim(created_at)) > 0
        AND julianday(created_at) IS NOT NULL
    )
);

CREATE TRIGGER IF NOT EXISTS suitability_policy_no_update
BEFORE UPDATE ON suitability_policy_versions
BEGIN
    SELECT RAISE(ABORT, 'suitability policies are immutable');
END;

CREATE TRIGGER IF NOT EXISTS suitability_policy_no_delete
BEFORE DELETE ON suitability_policy_versions
BEGIN
    SELECT RAISE(ABORT, 'suitability policies are immutable');
END;

CREATE TRIGGER IF NOT EXISTS suitability_assessment_no_update
BEFORE UPDATE ON suitability_assessments
BEGIN
    SELECT RAISE(ABORT, 'suitability assessments are immutable');
END;

CREATE TRIGGER IF NOT EXISTS suitability_assessment_no_delete
BEFORE DELETE ON suitability_assessments
BEGIN
    SELECT RAISE(ABORT, 'suitability assessments are immutable');
END;
"""

SCHEMA_V9 = """
CREATE TABLE allocation_policy_versions (
    version PRIMARY KEY NOT NULL CHECK(
        typeof(version) = 'text'
        AND instr(version, char(0)) = 0
        AND length(trim(version)) > 0
    ),
    canonical_policy_json NOT NULL CHECK(
        typeof(canonical_policy_json) = 'text'
        AND instr(canonical_policy_json, char(0)) = 0
        AND json_valid(canonical_policy_json)
        AND json_type(canonical_policy_json) = 'object'
    ),
    policy_checksum NOT NULL CHECK(
        typeof(policy_checksum) = 'text'
        AND instr(policy_checksum, char(0)) = 0
        AND length(CAST(policy_checksum AS BLOB)) = 64
        AND policy_checksum NOT GLOB '*[^0-9a-f]*'
    ),
    effective_at NOT NULL CHECK(
        typeof(effective_at) = 'text'
        AND instr(effective_at, char(0)) = 0
        AND length(trim(effective_at)) > 0
        AND julianday(effective_at) IS NOT NULL
        AND substr(effective_at, -6) = '+00:00'
        AND substr(effective_at, 11, 1) = 'T'
        AND substr(effective_at, 1, 4) NOT GLOB '*[^0-9]*'
        AND substr(effective_at, 1, 4) BETWEEN '0001' AND '9999'
        AND substr(effective_at, 12, 2) NOT GLOB '*[^0-9]*'
        AND CAST(substr(effective_at, 12, 2) AS INTEGER) BETWEEN 0 AND 23
        AND substr(effective_at, 15, 2) NOT GLOB '*[^0-9]*'
        AND CAST(substr(effective_at, 15, 2) AS INTEGER) BETWEEN 0 AND 59
        AND substr(effective_at, 18, 2) NOT GLOB '*[^0-9]*'
        AND CAST(substr(effective_at, 18, 2) AS INTEGER) BETWEEN 0 AND 59
        AND strftime('%Y-%m-%dT%H:%M:%S', effective_at) = substr(effective_at, 1, 19)
        AND (
            length(effective_at) = 25
            OR (
                length(effective_at) = 32
                AND substr(effective_at, 20, 1) = '.'
                AND substr(effective_at, 21, 6) NOT GLOB '*[^0-9]*'
                AND substr(effective_at, 21, 6) != '000000'
            )
        )
    ),
    created_at NOT NULL CHECK(
        typeof(created_at) = 'text'
        AND instr(created_at, char(0)) = 0
        AND length(trim(created_at)) > 0
        AND julianday(created_at) IS NOT NULL
        AND substr(created_at, -6) = '+00:00'
        AND substr(created_at, 11, 1) = 'T'
        AND substr(created_at, 1, 4) NOT GLOB '*[^0-9]*'
        AND substr(created_at, 1, 4) BETWEEN '0001' AND '9999'
        AND substr(created_at, 12, 2) NOT GLOB '*[^0-9]*'
        AND CAST(substr(created_at, 12, 2) AS INTEGER) BETWEEN 0 AND 23
        AND substr(created_at, 15, 2) NOT GLOB '*[^0-9]*'
        AND CAST(substr(created_at, 15, 2) AS INTEGER) BETWEEN 0 AND 59
        AND substr(created_at, 18, 2) NOT GLOB '*[^0-9]*'
        AND CAST(substr(created_at, 18, 2) AS INTEGER) BETWEEN 0 AND 59
        AND strftime('%Y-%m-%dT%H:%M:%S', created_at) = substr(created_at, 1, 19)
        AND (
            length(created_at) = 25
            OR (
                length(created_at) = 32
                AND substr(created_at, 20, 1) = '.'
                AND substr(created_at, 21, 6) NOT GLOB '*[^0-9]*'
                AND substr(created_at, 21, 6) != '000000'
            )
        )
    )
);

CREATE TABLE allocation_assessments (
    id INTEGER PRIMARY KEY AUTOINCREMENT CHECK(id > 0),
    profile_version_id INTEGER NOT NULL CHECK(profile_version_id > 0)
        REFERENCES financial_profile_versions(id) ON DELETE RESTRICT,
    suitability_assessment_id INTEGER NOT NULL CHECK(suitability_assessment_id > 0)
        REFERENCES suitability_assessments(id) ON DELETE RESTRICT,
    policy_version NOT NULL CHECK(
        typeof(policy_version) = 'text'
        AND instr(policy_version, char(0)) = 0
        AND length(trim(policy_version)) > 0
    )
        REFERENCES allocation_policy_versions(version) ON DELETE RESTRICT,
    input_fingerprint NOT NULL CHECK(
        typeof(input_fingerprint) = 'text'
        AND instr(input_fingerprint, char(0)) = 0
        AND length(CAST(input_fingerprint AS BLOB)) = 64
        AND input_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    status NOT NULL CHECK(
        typeof(status) = 'text'
        AND instr(status, char(0)) = 0
        AND status = 'range_available'
    ),
    permitted_region_json NOT NULL CHECK(
        typeof(permitted_region_json) = 'text'
        AND instr(permitted_region_json, char(0)) = 0
        AND json_valid(permitted_region_json)
        AND json_type(permitted_region_json) = 'object'
    ),
    binding_constraints_json NOT NULL CHECK(
        typeof(binding_constraints_json) = 'text'
        AND instr(binding_constraints_json, char(0)) = 0
        AND json_valid(binding_constraints_json)
        AND json_type(binding_constraints_json) = 'array'
    ),
    safe_summary_json NOT NULL CHECK(
        typeof(safe_summary_json) = 'text'
        AND instr(safe_summary_json, char(0)) = 0
        AND json_valid(safe_summary_json)
        AND json_type(safe_summary_json) = 'object'
    ),
    encrypted_amount_results NOT NULL CHECK(
        typeof(encrypted_amount_results) = 'text'
        AND instr(encrypted_amount_results, char(0)) = 0
        AND length(trim(encrypted_amount_results)) > 0
    ),
    encryption_algorithm NOT NULL CHECK(
        typeof(encryption_algorithm) = 'text'
        AND instr(encryption_algorithm, char(0)) = 0
        AND encryption_algorithm = 'AES-256-GCM'
    ),
    encryption_key_version NOT NULL CHECK(
        typeof(encryption_key_version) = 'text'
        AND instr(encryption_key_version, char(0)) = 0
        AND length(trim(encryption_key_version)) > 0
    ),
    nonce NOT NULL CHECK(
        typeof(nonce) = 'text'
        AND instr(nonce, char(0)) = 0
        AND length(trim(nonce)) > 0
    ),
    keyed_payload_fingerprint NOT NULL CHECK(
        typeof(keyed_payload_fingerprint) = 'text'
        AND instr(keyed_payload_fingerprint, char(0)) = 0
        AND length(CAST(keyed_payload_fingerprint AS BLOB)) = 64
        AND keyed_payload_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    assessed_at NOT NULL CHECK(
        typeof(assessed_at) = 'text'
        AND instr(assessed_at, char(0)) = 0
        AND length(trim(assessed_at)) > 0
        AND julianday(assessed_at) IS NOT NULL
        AND substr(assessed_at, -6) = '+00:00'
        AND substr(assessed_at, 11, 1) = 'T'
        AND substr(assessed_at, 1, 4) NOT GLOB '*[^0-9]*'
        AND substr(assessed_at, 1, 4) BETWEEN '0001' AND '9999'
        AND substr(assessed_at, 12, 2) NOT GLOB '*[^0-9]*'
        AND CAST(substr(assessed_at, 12, 2) AS INTEGER) BETWEEN 0 AND 23
        AND substr(assessed_at, 15, 2) NOT GLOB '*[^0-9]*'
        AND CAST(substr(assessed_at, 15, 2) AS INTEGER) BETWEEN 0 AND 59
        AND substr(assessed_at, 18, 2) NOT GLOB '*[^0-9]*'
        AND CAST(substr(assessed_at, 18, 2) AS INTEGER) BETWEEN 0 AND 59
        AND strftime('%Y-%m-%dT%H:%M:%S', assessed_at) = substr(assessed_at, 1, 19)
        AND (
            length(assessed_at) = 25
            OR (
                length(assessed_at) = 32
                AND substr(assessed_at, 20, 1) = '.'
                AND substr(assessed_at, 21, 6) NOT GLOB '*[^0-9]*'
                AND substr(assessed_at, 21, 6) != '000000'
            )
        )
    ),
    valid_until NOT NULL CHECK(
        typeof(valid_until) = 'text'
        AND instr(valid_until, char(0)) = 0
        AND length(trim(valid_until)) > 0
        AND julianday(valid_until) IS NOT NULL
        AND substr(valid_until, -6) = '+00:00'
        AND substr(valid_until, 11, 1) = 'T'
        AND substr(valid_until, 1, 4) NOT GLOB '*[^0-9]*'
        AND substr(valid_until, 1, 4) BETWEEN '0001' AND '9999'
        AND substr(valid_until, 12, 2) NOT GLOB '*[^0-9]*'
        AND CAST(substr(valid_until, 12, 2) AS INTEGER) BETWEEN 0 AND 23
        AND substr(valid_until, 15, 2) NOT GLOB '*[^0-9]*'
        AND CAST(substr(valid_until, 15, 2) AS INTEGER) BETWEEN 0 AND 59
        AND substr(valid_until, 18, 2) NOT GLOB '*[^0-9]*'
        AND CAST(substr(valid_until, 18, 2) AS INTEGER) BETWEEN 0 AND 59
        AND strftime('%Y-%m-%dT%H:%M:%S', valid_until) = substr(valid_until, 1, 19)
        AND (
            length(valid_until) = 25
            OR (
                length(valid_until) = 32
                AND substr(valid_until, 20, 1) = '.'
                AND substr(valid_until, 21, 6) NOT GLOB '*[^0-9]*'
                AND substr(valid_until, 21, 6) != '000000'
            )
        )
        AND (valid_until COLLATE BINARY) > (assessed_at COLLATE BINARY)
    ),
    created_at NOT NULL CHECK(
        typeof(created_at) = 'text'
        AND instr(created_at, char(0)) = 0
        AND length(trim(created_at)) > 0
        AND julianday(created_at) IS NOT NULL
        AND substr(created_at, -6) = '+00:00'
        AND substr(created_at, 11, 1) = 'T'
        AND substr(created_at, 1, 4) NOT GLOB '*[^0-9]*'
        AND substr(created_at, 1, 4) BETWEEN '0001' AND '9999'
        AND substr(created_at, 12, 2) NOT GLOB '*[^0-9]*'
        AND CAST(substr(created_at, 12, 2) AS INTEGER) BETWEEN 0 AND 23
        AND substr(created_at, 15, 2) NOT GLOB '*[^0-9]*'
        AND CAST(substr(created_at, 15, 2) AS INTEGER) BETWEEN 0 AND 59
        AND substr(created_at, 18, 2) NOT GLOB '*[^0-9]*'
        AND CAST(substr(created_at, 18, 2) AS INTEGER) BETWEEN 0 AND 59
        AND strftime('%Y-%m-%dT%H:%M:%S', created_at) = substr(created_at, 1, 19)
        AND (
            length(created_at) = 25
            OR (
                length(created_at) = 32
                AND substr(created_at, 20, 1) = '.'
                AND substr(created_at, 21, 6) NOT GLOB '*[^0-9]*'
                AND substr(created_at, 21, 6) != '000000'
            )
        )
    )
);

CREATE INDEX allocation_assessments_binding_lookup
ON allocation_assessments(
    profile_version_id,
    suitability_assessment_id,
    policy_version,
    assessed_at DESC
);

CREATE INDEX allocation_assessments_history
ON allocation_assessments(assessed_at DESC, id DESC);

CREATE TRIGGER allocation_policy_no_replace
BEFORE INSERT ON allocation_policy_versions
WHEN EXISTS (
    SELECT 1 FROM allocation_policy_versions WHERE version = NEW.version
)
BEGIN
    SELECT RAISE(ABORT, 'allocation policies are immutable');
END;

CREATE TRIGGER allocation_policy_no_update
BEFORE UPDATE ON allocation_policy_versions
BEGIN
    SELECT RAISE(ABORT, 'allocation policies are immutable');
END;

CREATE TRIGGER allocation_policy_no_delete
BEFORE DELETE ON allocation_policy_versions
BEGIN
    SELECT RAISE(ABORT, 'allocation policies are immutable');
END;

CREATE TRIGGER allocation_assessment_no_replace
BEFORE INSERT ON allocation_assessments
WHEN EXISTS (
    SELECT 1 FROM allocation_assessments WHERE id = NEW.id
)
BEGIN
    SELECT RAISE(ABORT, 'allocation assessments are immutable');
END;

CREATE TRIGGER allocation_assessment_no_update
BEFORE UPDATE ON allocation_assessments
BEGIN
    SELECT RAISE(ABORT, 'allocation assessments are immutable');
END;

CREATE TRIGGER allocation_assessment_no_delete
BEFORE DELETE ON allocation_assessments
BEGIN
    SELECT RAISE(ABORT, 'allocation assessments are immutable');
END;
"""

SCHEMA_V10 = """
CREATE TABLE fund_document_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT CHECK(id > 0),
    fund_code TEXT NOT NULL CHECK(
        typeof(fund_code) = 'text'
        AND length(fund_code) = 6
        AND fund_code NOT GLOB '*[^0-9]*'
    ),
    document_kind TEXT NOT NULL CHECK(
        typeof(document_kind) = 'text'
        AND instr(document_kind, char(0)) = 0
        AND document_kind IN (
            'fund_contract', 'prospectus', 'prospectus_update', 'product_summary',
            'annual_report', 'semiannual_report', 'quarterly_report',
            'index_methodology', 'classification_announcement'
        )
    ),
    url TEXT NOT NULL CHECK(
        typeof(url) = 'text'
        AND instr(url, char(0)) = 0
        AND length(trim(url)) > 0
    ),
    publisher TEXT NOT NULL CHECK(
        typeof(publisher) = 'text'
        AND instr(publisher, char(0)) = 0
        AND length(trim(publisher)) > 0
    ),
    title TEXT NOT NULL CHECK(
        typeof(title) = 'text'
        AND instr(title, char(0)) = 0
        AND length(trim(title)) > 0
    ),
    published_at TEXT CHECK(
        published_at IS NULL OR (
            typeof(published_at) = 'text'
            AND instr(published_at, char(0)) = 0
            AND julianday(published_at) IS NOT NULL
            AND substr(published_at, -6) = '+00:00'
            AND substr(published_at, 11, 1) = 'T'
            AND substr(published_at, 1, 4) NOT GLOB '*[^0-9]*'
            AND substr(published_at, 1, 4) BETWEEN '0001' AND '9999'
            AND substr(published_at, 12, 2) NOT GLOB '*[^0-9]*'
            AND CAST(substr(published_at, 12, 2) AS INTEGER) BETWEEN 0 AND 23
            AND substr(published_at, 15, 2) NOT GLOB '*[^0-9]*'
            AND CAST(substr(published_at, 15, 2) AS INTEGER) BETWEEN 0 AND 59
            AND substr(published_at, 18, 2) NOT GLOB '*[^0-9]*'
            AND CAST(substr(published_at, 18, 2) AS INTEGER) BETWEEN 0 AND 59
            AND strftime('%Y-%m-%dT%H:%M:%S', published_at) = substr(published_at, 1, 19)
            AND (
                length(published_at) = 25 OR (
                    length(published_at) = 32
                    AND substr(published_at, 20, 1) = '.'
                    AND substr(published_at, 21, 6) NOT GLOB '*[^0-9]*'
                    AND substr(published_at, 21, 6) != '000000'
                )
            )
        )
    ),
    retrieved_at TEXT NOT NULL CHECK(
        typeof(retrieved_at) = 'text'
        AND instr(retrieved_at, char(0)) = 0
        AND julianday(retrieved_at) IS NOT NULL
        AND substr(retrieved_at, -6) = '+00:00'
        AND substr(retrieved_at, 11, 1) = 'T'
        AND substr(retrieved_at, 1, 4) NOT GLOB '*[^0-9]*'
        AND substr(retrieved_at, 1, 4) BETWEEN '0001' AND '9999'
        AND substr(retrieved_at, 12, 2) NOT GLOB '*[^0-9]*'
        AND CAST(substr(retrieved_at, 12, 2) AS INTEGER) BETWEEN 0 AND 23
        AND substr(retrieved_at, 15, 2) NOT GLOB '*[^0-9]*'
        AND CAST(substr(retrieved_at, 15, 2) AS INTEGER) BETWEEN 0 AND 59
        AND substr(retrieved_at, 18, 2) NOT GLOB '*[^0-9]*'
        AND CAST(substr(retrieved_at, 18, 2) AS INTEGER) BETWEEN 0 AND 59
        AND strftime('%Y-%m-%dT%H:%M:%S', retrieved_at) = substr(retrieved_at, 1, 19)
        AND (
            length(retrieved_at) = 25 OR (
                length(retrieved_at) = 32
                AND substr(retrieved_at, 20, 1) = '.'
                AND substr(retrieved_at, 21, 6) NOT GLOB '*[^0-9]*'
                AND substr(retrieved_at, 21, 6) != '000000'
            )
        )
    ),
    content_type TEXT NOT NULL CHECK(
        typeof(content_type) = 'text'
        AND instr(content_type, char(0)) = 0
        AND length(trim(content_type)) > 0
    ),
    byte_size INTEGER NOT NULL CHECK(
        typeof(byte_size) = 'integer'
        AND byte_size > 0
        AND byte_size <= 33554432
    ),
    sha256 TEXT NOT NULL CHECK(
        typeof(sha256) = 'text'
        AND instr(sha256, char(0)) = 0
        AND length(CAST(sha256 AS BLOB)) = 64
        AND sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    managed_path TEXT NOT NULL CHECK(
        typeof(managed_path) = 'text'
        AND instr(managed_path, char(0)) = 0
        AND length(trim(managed_path)) > 0
    ),
    parse_status TEXT NOT NULL CHECK(
        typeof(parse_status) = 'text'
        AND parse_status IN ('parsed', 'failed')
    ),
    parser_version TEXT NOT NULL CHECK(
        typeof(parser_version) = 'text'
        AND instr(parser_version, char(0)) = 0
        AND length(parser_version) > 0
        AND substr(parser_version, 1, 1) GLOB '[a-z0-9]'
        AND parser_version NOT GLOB '*[^a-z0-9._-]*'
    ),
    parse_error_code TEXT CHECK(
        parse_error_code IS NULL OR (
            typeof(parse_error_code) = 'text'
            AND instr(parse_error_code, char(0)) = 0
            AND length(parse_error_code) > 0
            AND substr(parse_error_code, 1, 1) GLOB '[a-z]'
            AND parse_error_code NOT GLOB '*[^a-z0-9_]*'
        )
    ),
    CHECK(
        (parse_status = 'parsed' AND parse_error_code IS NULL)
        OR (parse_status = 'failed' AND parse_error_code IS NOT NULL)
    ),
    UNIQUE(fund_code, document_kind, url, sha256)
);

CREATE TABLE fund_mandate_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT CHECK(id > 0),
    fund_code TEXT NOT NULL CHECK(
        typeof(fund_code) = 'text'
        AND length(fund_code) = 6
        AND fund_code NOT GLOB '*[^0-9]*'
    ),
    source_document_id INTEGER NOT NULL CHECK(
        typeof(source_document_id) = 'integer' AND source_document_id > 0
    )
        REFERENCES fund_document_artifacts(id) ON DELETE RESTRICT,
    fact_kind TEXT NOT NULL CHECK(
        typeof(fact_kind) = 'text'
        AND instr(fact_kind, char(0)) = 0
        AND length(fact_kind) > 0
        AND substr(fact_kind, 1, 1) GLOB '[a-z]'
        AND fact_kind NOT GLOB '*[^a-z0-9_]*'
    ),
    normalized_value_json TEXT NOT NULL CHECK(
        typeof(normalized_value_json) = 'text'
        AND instr(normalized_value_json, char(0)) = 0
        AND json_valid(normalized_value_json)
    ),
    unit TEXT CHECK(
        unit IS NULL OR (
            typeof(unit) = 'text'
            AND instr(unit, char(0)) = 0
            AND length(trim(unit)) BETWEEN 1 AND 64
        )
    ),
    page_number INTEGER CHECK(
        page_number IS NULL OR (
            typeof(page_number) = 'integer' AND page_number > 0
        )
    ),
    section_name TEXT CHECK(
        section_name IS NULL OR (
            typeof(section_name) = 'text'
            AND instr(section_name, char(0)) = 0
            AND length(trim(section_name)) BETWEEN 1 AND 256
        )
    ),
    source_excerpt TEXT NOT NULL CHECK(
        typeof(source_excerpt) = 'text'
        AND instr(source_excerpt, char(0)) = 0
        AND length(trim(source_excerpt)) BETWEEN 1 AND 4096
    ),
    effective_from TEXT CHECK(
        effective_from IS NULL OR (
            typeof(effective_from) = 'text'
            AND instr(effective_from, char(0)) = 0
            AND length(effective_from) = 10
            AND strftime('%Y-%m-%d', effective_from) = effective_from
        )
    ),
    effective_to TEXT CHECK(
        effective_to IS NULL OR (
            typeof(effective_to) = 'text'
            AND instr(effective_to, char(0)) = 0
            AND length(effective_to) = 10
            AND strftime('%Y-%m-%d', effective_to) = effective_to
        )
    ),
    confidence_state TEXT NOT NULL CHECK(
        typeof(confidence_state) = 'text'
        AND confidence_state IN ('exact', 'bounded_range', 'present', 'absent', 'ambiguous')
    ),
    parser_version TEXT NOT NULL CHECK(
        typeof(parser_version) = 'text'
        AND instr(parser_version, char(0)) = 0
        AND length(parser_version) > 0
        AND substr(parser_version, 1, 1) GLOB '[a-z0-9]'
        AND parser_version NOT GLOB '*[^a-z0-9._-]*'
    ),
    fact_fingerprint TEXT NOT NULL CHECK(
        typeof(fact_fingerprint) = 'text'
        AND instr(fact_fingerprint, char(0)) = 0
        AND length(CAST(fact_fingerprint AS BLOB)) = 64
        AND fact_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK(
        effective_from IS NULL OR effective_to IS NULL
        OR (effective_to COLLATE BINARY) >= (effective_from COLLATE BINARY)
    ),
    UNIQUE(source_document_id, parser_version, fact_fingerprint)
);

CREATE TABLE fund_classification_policy_versions (
    version TEXT PRIMARY KEY CHECK(
        typeof(version) = 'text'
        AND instr(version, char(0)) = 0
        AND length(version) > 0
        AND substr(version, 1, 1) GLOB '[a-z0-9]'
        AND version NOT GLOB '*[^a-z0-9._-]*'
    ),
    canonical_policy_json TEXT NOT NULL CHECK(
        typeof(canonical_policy_json) = 'text'
        AND instr(canonical_policy_json, char(0)) = 0
        AND json_valid(canonical_policy_json)
        AND json_type(canonical_policy_json) = 'object'
    ),
    policy_checksum TEXT NOT NULL CHECK(
        typeof(policy_checksum) = 'text'
        AND instr(policy_checksum, char(0)) = 0
        AND length(CAST(policy_checksum AS BLOB)) = 64
        AND policy_checksum NOT GLOB '*[^0-9a-f]*'
    ),
    effective_at TEXT NOT NULL CHECK(
        typeof(effective_at) = 'text'
        AND instr(effective_at, char(0)) = 0
        AND julianday(effective_at) IS NOT NULL
        AND substr(effective_at, -6) = '+00:00'
        AND substr(effective_at, 11, 1) = 'T'
        AND substr(effective_at, 1, 4) NOT GLOB '*[^0-9]*'
        AND substr(effective_at, 1, 4) BETWEEN '0001' AND '9999'
        AND substr(effective_at, 12, 2) NOT GLOB '*[^0-9]*'
        AND CAST(substr(effective_at, 12, 2) AS INTEGER) BETWEEN 0 AND 23
        AND substr(effective_at, 15, 2) NOT GLOB '*[^0-9]*'
        AND CAST(substr(effective_at, 15, 2) AS INTEGER) BETWEEN 0 AND 59
        AND substr(effective_at, 18, 2) NOT GLOB '*[^0-9]*'
        AND CAST(substr(effective_at, 18, 2) AS INTEGER) BETWEEN 0 AND 59
        AND strftime('%Y-%m-%dT%H:%M:%S', effective_at) = substr(effective_at, 1, 19)
        AND (
            length(effective_at) = 25 OR (
                length(effective_at) = 32
                AND substr(effective_at, 20, 1) = '.'
                AND substr(effective_at, 21, 6) NOT GLOB '*[^0-9]*'
                AND substr(effective_at, 21, 6) != '000000'
            )
        )
    ),
    created_at TEXT NOT NULL CHECK(
        typeof(created_at) = 'text'
        AND instr(created_at, char(0)) = 0
        AND julianday(created_at) IS NOT NULL
        AND substr(created_at, -6) = '+00:00'
        AND substr(created_at, 11, 1) = 'T'
        AND substr(created_at, 1, 4) NOT GLOB '*[^0-9]*'
        AND substr(created_at, 1, 4) BETWEEN '0001' AND '9999'
        AND substr(created_at, 12, 2) NOT GLOB '*[^0-9]*'
        AND CAST(substr(created_at, 12, 2) AS INTEGER) BETWEEN 0 AND 23
        AND substr(created_at, 15, 2) NOT GLOB '*[^0-9]*'
        AND CAST(substr(created_at, 15, 2) AS INTEGER) BETWEEN 0 AND 59
        AND substr(created_at, 18, 2) NOT GLOB '*[^0-9]*'
        AND CAST(substr(created_at, 18, 2) AS INTEGER) BETWEEN 0 AND 59
        AND strftime('%Y-%m-%dT%H:%M:%S', created_at) = substr(created_at, 1, 19)
        AND (
            length(created_at) = 25 OR (
                length(created_at) = 32
                AND substr(created_at, 20, 1) = '.'
                AND substr(created_at, 21, 6) NOT GLOB '*[^0-9]*'
                AND substr(created_at, 21, 6) != '000000'
            )
        )
    )
);

CREATE TABLE fund_risk_classifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT CHECK(id > 0),
    fund_code TEXT NOT NULL CHECK(
        typeof(fund_code) = 'text'
        AND length(fund_code) = 6
        AND fund_code NOT GLOB '*[^0-9]*'
    ),
    policy_version TEXT NOT NULL CHECK(
        typeof(policy_version) = 'text'
        AND instr(policy_version, char(0)) = 0
        AND length(policy_version) > 0
        AND substr(policy_version, 1, 1) GLOB '[a-z0-9]'
        AND policy_version NOT GLOB '*[^a-z0-9._-]*'
    )
        REFERENCES fund_classification_policy_versions(version) ON DELETE RESTRICT,
    input_fingerprint TEXT NOT NULL CHECK(
        typeof(input_fingerprint) = 'text'
        AND instr(input_fingerprint, char(0)) = 0
        AND length(CAST(input_fingerprint AS BLOB)) = 64
        AND input_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    input_manifest_json TEXT NOT NULL CHECK(
        typeof(input_manifest_json) = 'text'
        AND instr(input_manifest_json, char(0)) = 0
        AND json_valid(input_manifest_json)
        AND json_type(input_manifest_json) = 'object'
    ),
    product_family TEXT NOT NULL CHECK(
        typeof(product_family) = 'text'
        AND product_family IN (
            'money_market', 'short_bond', 'intermediate_bond', 'ordinary_bond',
            'long_bond', 'credit_bond', 'convertible_bond', 'fixed_income_plus',
            'bond_mixed', 'broad_index', 'index_enhanced', 'sector_theme',
            'active_equity', 'equity_mixed', 'qdii_broad_equity',
            'qdii_sector_theme', 'unsupported', 'unclassified'
        )
    ),
    risk_bucket TEXT NOT NULL CHECK(
        typeof(risk_bucket) = 'text'
        AND risk_bucket IN (
            'cash_like_candidate', 'high_quality_fixed_income',
            'diversified_equity', 'concentrated_equity', 'hybrid_risk',
            'unclassified'
        )
    ),
    portfolio_role TEXT NOT NULL CHECK(
        typeof(portfolio_role) = 'text'
        AND portfolio_role IN (
            'cash_management_candidate', 'core_eligible',
            'active_diversifier_eligible', 'satellite_only', 'not_eligible'
        )
    ),
    evidence_status TEXT NOT NULL CHECK(
        typeof(evidence_status) = 'text'
        AND evidence_status IN ('verified', 'partial', 'conflicted', 'stale', 'unclassified')
    ),
    evidence_tags_json TEXT NOT NULL CHECK(
        typeof(evidence_tags_json) = 'text'
        AND instr(evidence_tags_json, char(0)) = 0
        AND json_valid(evidence_tags_json)
        AND json_type(evidence_tags_json) = 'array'
    ),
    reason_codes_json TEXT NOT NULL CHECK(
        typeof(reason_codes_json) = 'text'
        AND instr(reason_codes_json, char(0)) = 0
        AND json_valid(reason_codes_json)
        AND json_type(reason_codes_json) = 'array'
    ),
    missing_evidence_json TEXT NOT NULL CHECK(
        typeof(missing_evidence_json) = 'text'
        AND instr(missing_evidence_json, char(0)) = 0
        AND json_valid(missing_evidence_json)
        AND json_type(missing_evidence_json) = 'array'
    ),
    conflicts_json TEXT NOT NULL CHECK(
        typeof(conflicts_json) = 'text'
        AND instr(conflicts_json, char(0)) = 0
        AND json_valid(conflicts_json)
        AND json_type(conflicts_json) = 'array'
    ),
    evidence_document_ids_json TEXT NOT NULL CHECK(
        typeof(evidence_document_ids_json) = 'text'
        AND instr(evidence_document_ids_json, char(0)) = 0
        AND json_valid(evidence_document_ids_json)
        AND json_type(evidence_document_ids_json) = 'array'
    ),
    evidence_fact_ids_json TEXT NOT NULL CHECK(
        typeof(evidence_fact_ids_json) = 'text'
        AND instr(evidence_fact_ids_json, char(0)) = 0
        AND json_valid(evidence_fact_ids_json)
        AND json_type(evidence_fact_ids_json) = 'array'
    ),
    freshness_json TEXT NOT NULL CHECK(
        typeof(freshness_json) = 'text'
        AND instr(freshness_json, char(0)) = 0
        AND json_valid(freshness_json)
        AND json_type(freshness_json) = 'array'
    ),
    classified_at TEXT NOT NULL CHECK(
        typeof(classified_at) = 'text'
        AND instr(classified_at, char(0)) = 0
        AND julianday(classified_at) IS NOT NULL
        AND substr(classified_at, -6) = '+00:00'
        AND substr(classified_at, 11, 1) = 'T'
        AND substr(classified_at, 1, 4) NOT GLOB '*[^0-9]*'
        AND substr(classified_at, 1, 4) BETWEEN '0001' AND '9999'
        AND substr(classified_at, 12, 2) NOT GLOB '*[^0-9]*'
        AND CAST(substr(classified_at, 12, 2) AS INTEGER) BETWEEN 0 AND 23
        AND substr(classified_at, 15, 2) NOT GLOB '*[^0-9]*'
        AND CAST(substr(classified_at, 15, 2) AS INTEGER) BETWEEN 0 AND 59
        AND substr(classified_at, 18, 2) NOT GLOB '*[^0-9]*'
        AND CAST(substr(classified_at, 18, 2) AS INTEGER) BETWEEN 0 AND 59
        AND strftime('%Y-%m-%dT%H:%M:%S', classified_at) = substr(classified_at, 1, 19)
        AND (
            length(classified_at) = 25 OR (
                length(classified_at) = 32
                AND substr(classified_at, 20, 1) = '.'
                AND substr(classified_at, 21, 6) NOT GLOB '*[^0-9]*'
                AND substr(classified_at, 21, 6) != '000000'
            )
        )
    ),
    valid_until TEXT NOT NULL CHECK(
        typeof(valid_until) = 'text'
        AND instr(valid_until, char(0)) = 0
        AND julianday(valid_until) IS NOT NULL
        AND substr(valid_until, -6) = '+00:00'
        AND substr(valid_until, 11, 1) = 'T'
        AND substr(valid_until, 1, 4) NOT GLOB '*[^0-9]*'
        AND substr(valid_until, 1, 4) BETWEEN '0001' AND '9999'
        AND substr(valid_until, 12, 2) NOT GLOB '*[^0-9]*'
        AND CAST(substr(valid_until, 12, 2) AS INTEGER) BETWEEN 0 AND 23
        AND substr(valid_until, 15, 2) NOT GLOB '*[^0-9]*'
        AND CAST(substr(valid_until, 15, 2) AS INTEGER) BETWEEN 0 AND 59
        AND substr(valid_until, 18, 2) NOT GLOB '*[^0-9]*'
        AND CAST(substr(valid_until, 18, 2) AS INTEGER) BETWEEN 0 AND 59
        AND strftime('%Y-%m-%dT%H:%M:%S', valid_until) = substr(valid_until, 1, 19)
        AND (
            length(valid_until) = 25 OR (
                length(valid_until) = 32
                AND substr(valid_until, 20, 1) = '.'
                AND substr(valid_until, 21, 6) NOT GLOB '*[^0-9]*'
                AND substr(valid_until, 21, 6) != '000000'
            )
        )
        AND (valid_until COLLATE BINARY) > (classified_at COLLATE BINARY)
    ),
    created_at TEXT NOT NULL CHECK(
        typeof(created_at) = 'text'
        AND instr(created_at, char(0)) = 0
        AND julianday(created_at) IS NOT NULL
        AND substr(created_at, -6) = '+00:00'
        AND substr(created_at, 11, 1) = 'T'
        AND substr(created_at, 1, 4) NOT GLOB '*[^0-9]*'
        AND substr(created_at, 1, 4) BETWEEN '0001' AND '9999'
        AND substr(created_at, 12, 2) NOT GLOB '*[^0-9]*'
        AND CAST(substr(created_at, 12, 2) AS INTEGER) BETWEEN 0 AND 23
        AND substr(created_at, 15, 2) NOT GLOB '*[^0-9]*'
        AND CAST(substr(created_at, 15, 2) AS INTEGER) BETWEEN 0 AND 59
        AND substr(created_at, 18, 2) NOT GLOB '*[^0-9]*'
        AND CAST(substr(created_at, 18, 2) AS INTEGER) BETWEEN 0 AND 59
        AND strftime('%Y-%m-%dT%H:%M:%S', created_at) = substr(created_at, 1, 19)
        AND (
            length(created_at) = 25 OR (
                length(created_at) = 32
                AND substr(created_at, 20, 1) = '.'
                AND substr(created_at, 21, 6) NOT GLOB '*[^0-9]*'
                AND substr(created_at, 21, 6) != '000000'
            )
        )
    ),
    UNIQUE(fund_code, policy_version, input_fingerprint)
);

CREATE INDEX fund_document_artifacts_lookup
ON fund_document_artifacts(fund_code, document_kind, retrieved_at DESC, id DESC);

CREATE INDEX fund_mandate_facts_lookup
ON fund_mandate_facts(fund_code, fact_kind, source_document_id, id);

CREATE INDEX fund_risk_classifications_binding
ON fund_risk_classifications(fund_code, policy_version, input_fingerprint);

CREATE INDEX fund_risk_classifications_history
ON fund_risk_classifications(fund_code, classified_at DESC, id DESC);

CREATE TRIGGER fund_document_artifact_no_replace
BEFORE INSERT ON fund_document_artifacts
WHEN EXISTS (
    SELECT 1 FROM fund_document_artifacts
    WHERE id = NEW.id
       OR (
           fund_code = NEW.fund_code
           AND document_kind = NEW.document_kind
           AND url = NEW.url
           AND sha256 = NEW.sha256
       )
)
BEGIN
    SELECT RAISE(ABORT, 'fund document artifacts are immutable');
END;

CREATE TRIGGER fund_document_artifact_no_update
BEFORE UPDATE ON fund_document_artifacts
BEGIN
    SELECT RAISE(ABORT, 'fund document artifacts are immutable');
END;

CREATE TRIGGER fund_document_artifact_no_delete
BEFORE DELETE ON fund_document_artifacts
BEGIN
    SELECT RAISE(ABORT, 'fund document artifacts are immutable');
END;

CREATE TRIGGER fund_mandate_fact_no_replace
BEFORE INSERT ON fund_mandate_facts
WHEN EXISTS (
    SELECT 1 FROM fund_mandate_facts
    WHERE id = NEW.id
       OR (
           source_document_id = NEW.source_document_id
           AND parser_version = NEW.parser_version
           AND fact_fingerprint = NEW.fact_fingerprint
       )
)
BEGIN
    SELECT RAISE(ABORT, 'fund mandate facts are immutable');
END;

CREATE TRIGGER fund_mandate_fact_no_update
BEFORE UPDATE ON fund_mandate_facts
BEGIN
    SELECT RAISE(ABORT, 'fund mandate facts are immutable');
END;

CREATE TRIGGER fund_mandate_fact_no_delete
BEFORE DELETE ON fund_mandate_facts
BEGIN
    SELECT RAISE(ABORT, 'fund mandate facts are immutable');
END;

CREATE TRIGGER fund_classification_policy_no_replace
BEFORE INSERT ON fund_classification_policy_versions
WHEN EXISTS (
    SELECT 1 FROM fund_classification_policy_versions WHERE version = NEW.version
)
BEGIN
    SELECT RAISE(ABORT, 'fund classification policies are immutable');
END;

CREATE TRIGGER fund_classification_policy_no_update
BEFORE UPDATE ON fund_classification_policy_versions
BEGIN
    SELECT RAISE(ABORT, 'fund classification policies are immutable');
END;

CREATE TRIGGER fund_classification_policy_no_delete
BEFORE DELETE ON fund_classification_policy_versions
BEGIN
    SELECT RAISE(ABORT, 'fund classification policies are immutable');
END;

CREATE TRIGGER fund_risk_classification_no_replace
BEFORE INSERT ON fund_risk_classifications
WHEN EXISTS (
    SELECT 1 FROM fund_risk_classifications
    WHERE id = NEW.id
       OR (
           fund_code = NEW.fund_code
           AND policy_version = NEW.policy_version
           AND input_fingerprint = NEW.input_fingerprint
       )
)
BEGIN
    SELECT RAISE(ABORT, 'fund risk classifications are immutable');
END;

CREATE TRIGGER fund_risk_classification_no_update
BEFORE UPDATE ON fund_risk_classifications
BEGIN
    SELECT RAISE(ABORT, 'fund risk classifications are immutable');
END;

CREATE TRIGGER fund_risk_classification_no_delete
BEFORE DELETE ON fund_risk_classifications
BEGIN
    SELECT RAISE(ABORT, 'fund risk classifications are immutable');
END;
"""

SCHEMA_V11 = """
DROP TRIGGER fund_document_artifact_no_update;

ALTER TABLE fund_document_artifacts ADD COLUMN landing_url TEXT CHECK(
    landing_url IS NULL OR (
        typeof(landing_url) = 'text'
        AND instr(landing_url, char(0)) = 0
        AND length(trim(landing_url)) > 0
    )
);

UPDATE fund_document_artifacts SET landing_url = url;

CREATE TRIGGER fund_document_artifact_landing_url_required
BEFORE INSERT ON fund_document_artifacts
WHEN NEW.landing_url IS NULL
     OR typeof(NEW.landing_url) != 'text'
     OR instr(NEW.landing_url, char(0)) != 0
     OR length(trim(NEW.landing_url)) = 0
BEGIN
    SELECT RAISE(ABORT, 'fund document artifact landing URL is required');
END;

CREATE TRIGGER fund_document_artifact_no_update
BEFORE UPDATE ON fund_document_artifacts
BEGIN
    SELECT RAISE(ABORT, 'fund document artifacts are immutable');
END;
"""

SCHEMA_V12 = """
CREATE TABLE fund_document_parser_provenance (
    id INTEGER PRIMARY KEY AUTOINCREMENT CHECK(typeof(id) = 'integer' AND id > 0),
    parser_version TEXT NOT NULL CHECK(
        typeof(parser_version) = 'text'
        AND length(parser_version) > 0
        AND substr(parser_version, 1, 1) GLOB '[a-z0-9]'
        AND parser_version NOT GLOB '*[^a-z0-9._-]*'
    ),
    converter_kind TEXT NOT NULL CHECK(
        typeof(converter_kind) = 'text'
        AND converter_kind IN ('none', 'docker_libreoffice')
    ),
    canonical_json TEXT NOT NULL UNIQUE CHECK(
        typeof(canonical_json) = 'text'
        AND instr(canonical_json, char(0)) = 0
        AND json_valid(canonical_json)
        AND json_type(canonical_json) = 'object'
    ),
    provenance_checksum TEXT NOT NULL UNIQUE CHECK(
        typeof(provenance_checksum) = 'text'
        AND length(CAST(provenance_checksum AS BLOB)) = 64
        AND provenance_checksum NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL CHECK(
        typeof(created_at) = 'text'
        AND julianday(created_at) IS NOT NULL
        AND substr(created_at, -6) = '+00:00'
        AND substr(created_at, 11, 1) = 'T'
    )
);

CREATE TABLE fund_document_parse_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT CHECK(typeof(id) = 'integer' AND id > 0),
    source_document_id INTEGER NOT NULL CHECK(
        typeof(source_document_id) = 'integer' AND source_document_id > 0
    ) REFERENCES fund_document_artifacts(id) ON DELETE RESTRICT,
    provenance_id INTEGER NOT NULL CHECK(
        typeof(provenance_id) = 'integer' AND provenance_id > 0
    ) REFERENCES fund_document_parser_provenance(id) ON DELETE RESTRICT,
    parser_input_sha256 TEXT NOT NULL CHECK(
        typeof(parser_input_sha256) = 'text'
        AND length(CAST(parser_input_sha256 AS BLOB)) = 64
        AND parser_input_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    fact_set_fingerprint TEXT NOT NULL CHECK(
        typeof(fact_set_fingerprint) = 'text'
        AND length(CAST(fact_set_fingerprint AS BLOB)) = 64
        AND fact_set_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL CHECK(
        typeof(created_at) = 'text'
        AND julianday(created_at) IS NOT NULL
        AND substr(created_at, -6) = '+00:00'
        AND substr(created_at, 11, 1) = 'T'
    ),
    UNIQUE(source_document_id, provenance_id)
);

CREATE TABLE __kunjin_fund_mandate_facts_v12_sequence (
    seq INTEGER
);
INSERT INTO __kunjin_fund_mandate_facts_v12_sequence(seq)
SELECT seq FROM sqlite_sequence WHERE name = 'fund_mandate_facts';

DROP INDEX fund_mandate_facts_lookup;
DROP TRIGGER fund_mandate_fact_no_replace;
DROP TRIGGER fund_mandate_fact_no_update;
DROP TRIGGER fund_mandate_fact_no_delete;
ALTER TABLE fund_mandate_facts RENAME TO __kunjin_v11_fund_mandate_facts;

CREATE TABLE fund_mandate_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT CHECK(id > 0),
    fund_code TEXT NOT NULL CHECK(
        typeof(fund_code) = 'text'
        AND length(fund_code) = 6
        AND fund_code NOT GLOB '*[^0-9]*'
    ),
    source_document_id INTEGER NOT NULL CHECK(
        typeof(source_document_id) = 'integer' AND source_document_id > 0
    ) REFERENCES fund_document_artifacts(id) ON DELETE RESTRICT,
    fact_kind TEXT NOT NULL CHECK(
        typeof(fact_kind) = 'text'
        AND instr(fact_kind, char(0)) = 0
        AND length(fact_kind) > 0
        AND substr(fact_kind, 1, 1) GLOB '[a-z]'
        AND fact_kind NOT GLOB '*[^a-z0-9_]*'
    ),
    normalized_value_json TEXT NOT NULL CHECK(
        typeof(normalized_value_json) = 'text'
        AND instr(normalized_value_json, char(0)) = 0
        AND json_valid(normalized_value_json)
    ),
    unit TEXT CHECK(
        unit IS NULL OR (
            typeof(unit) = 'text'
            AND instr(unit, char(0)) = 0
            AND length(trim(unit)) BETWEEN 1 AND 64
        )
    ),
    page_number INTEGER CHECK(
        page_number IS NULL OR (
            typeof(page_number) = 'integer' AND page_number > 0
        )
    ),
    section_name TEXT CHECK(
        section_name IS NULL OR (
            typeof(section_name) = 'text'
            AND instr(section_name, char(0)) = 0
            AND length(trim(section_name)) BETWEEN 1 AND 256
        )
    ),
    source_excerpt TEXT NOT NULL CHECK(
        typeof(source_excerpt) = 'text'
        AND instr(source_excerpt, char(0)) = 0
        AND length(trim(source_excerpt)) BETWEEN 1 AND 4096
    ),
    effective_from TEXT CHECK(
        effective_from IS NULL OR (
            typeof(effective_from) = 'text'
            AND instr(effective_from, char(0)) = 0
            AND length(effective_from) = 10
            AND strftime('%Y-%m-%d', effective_from) = effective_from
        )
    ),
    effective_to TEXT CHECK(
        effective_to IS NULL OR (
            typeof(effective_to) = 'text'
            AND instr(effective_to, char(0)) = 0
            AND length(effective_to) = 10
            AND strftime('%Y-%m-%d', effective_to) = effective_to
        )
    ),
    confidence_state TEXT NOT NULL CHECK(
        typeof(confidence_state) = 'text'
        AND confidence_state IN ('exact', 'bounded_range', 'present', 'absent', 'ambiguous')
    ),
    parser_version TEXT NOT NULL CHECK(
        typeof(parser_version) = 'text'
        AND instr(parser_version, char(0)) = 0
        AND length(parser_version) > 0
        AND substr(parser_version, 1, 1) GLOB '[a-z0-9]'
        AND parser_version NOT GLOB '*[^a-z0-9._-]*'
    ),
    fact_fingerprint TEXT NOT NULL CHECK(
        typeof(fact_fingerprint) = 'text'
        AND instr(fact_fingerprint, char(0)) = 0
        AND length(CAST(fact_fingerprint AS BLOB)) = 64
        AND fact_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    parse_result_id INTEGER
        REFERENCES fund_document_parse_results(id) ON DELETE RESTRICT,
    CHECK(
        effective_from IS NULL OR effective_to IS NULL
        OR (effective_to COLLATE BINARY) >= (effective_from COLLATE BINARY)
    ),
    UNIQUE(parse_result_id, fact_fingerprint)
);

INSERT INTO fund_mandate_facts(
    id, fund_code, source_document_id, parse_result_id, fact_kind,
    normalized_value_json, unit, page_number, section_name, source_excerpt,
    effective_from, effective_to, confidence_state, parser_version,
    fact_fingerprint
)
SELECT
    id, fund_code, source_document_id, NULL, fact_kind,
    normalized_value_json, unit, page_number, section_name, source_excerpt,
    effective_from, effective_to, confidence_state, parser_version,
    fact_fingerprint
FROM __kunjin_v11_fund_mandate_facts
ORDER BY id;

UPDATE sqlite_sequence
SET seq = (SELECT seq FROM __kunjin_fund_mandate_facts_v12_sequence)
WHERE name = 'fund_mandate_facts'
  AND EXISTS (SELECT 1 FROM __kunjin_fund_mandate_facts_v12_sequence);

DROP TABLE __kunjin_v11_fund_mandate_facts;
DROP TABLE __kunjin_fund_mandate_facts_v12_sequence;

CREATE INDEX fund_mandate_facts_lookup
ON fund_mandate_facts(fund_code, fact_kind, source_document_id, id);

CREATE TRIGGER fund_mandate_fact_no_replace
BEFORE INSERT ON fund_mandate_facts
WHEN EXISTS (
    SELECT 1 FROM fund_mandate_facts
    WHERE id = NEW.id
       OR (
           parse_result_id = NEW.parse_result_id
           AND fact_fingerprint = NEW.fact_fingerprint
       )
)
BEGIN
    SELECT RAISE(ABORT, 'fund mandate facts are immutable');
END;

CREATE TRIGGER fund_mandate_fact_no_update
BEFORE UPDATE ON fund_mandate_facts
BEGIN
    SELECT RAISE(ABORT, 'fund mandate facts are immutable');
END;

CREATE TRIGGER fund_mandate_fact_no_delete
BEFORE DELETE ON fund_mandate_facts
BEGIN
    SELECT RAISE(ABORT, 'fund mandate facts are immutable');
END;

CREATE TABLE fund_document_parse_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT CHECK(typeof(id) = 'integer' AND id > 0),
    source_document_id INTEGER NOT NULL CHECK(
        typeof(source_document_id) = 'integer' AND source_document_id > 0
    ) REFERENCES fund_document_artifacts(id) ON DELETE RESTRICT,
    provenance_id INTEGER NOT NULL CHECK(
        typeof(provenance_id) = 'integer' AND provenance_id > 0
    ) REFERENCES fund_document_parser_provenance(id) ON DELETE RESTRICT,
    run_kind TEXT NOT NULL CHECK(run_kind IN ('live', 'legacy_backfill')),
    outcome TEXT NOT NULL CHECK(outcome IN ('success', 'failed')),
    parse_result_id INTEGER REFERENCES fund_document_parse_results(id) ON DELETE RESTRICT,
    public_error_code TEXT CHECK(
        public_error_code IS NULL OR public_error_code IN (
            'official_document_unavailable', 'official_document_invalid',
            'official_document_resource_limit', 'official_document_parse_failed',
            'classification_storage_failed'
        )
    ),
    failure_stage TEXT CHECK(
        failure_stage IS NULL OR failure_stage IN (
            'discovery', 'landing_validation', 'retrieval', 'identity_validation',
            'container_validation', 'conversion', 'parser', 'persistence', 'unspecified'
        )
    ),
    failure_reason TEXT CHECK(
        failure_reason IS NULL OR failure_reason IN (
            'dns_unavailable', 'network_unavailable', 'http_unavailable',
            'source_unregistered', 'redirect_rejected', 'discovery_format_invalid',
            'identity_mismatch', 'publication_date_missing', 'landing_format_invalid',
            'landing_title_mismatch', 'landing_date_mismatch', 'attachment_missing',
            'attachment_ambiguous', 'attachment_host_rejected', 'authentication_shell',
            'empty_or_script_only_html', 'declared_mime_unsupported',
            'detected_container_unknown', 'declared_detected_mismatch',
            'legacy_ole_container_unsupported', 'legacy_converter_unavailable',
            'legacy_converter_timeout', 'legacy_converter_resource_limit',
            'legacy_converter_failed', 'legacy_converter_output_invalid',
            'resource_limit', 'parser_format_invalid', 'parser_identity_mismatch',
            'parser_effective_date_invalid', 'parser_ambiguous_fact', 'clock_invalid',
            'managed_artifact_invalid', 'storage_failure', 'unspecified_failure'
        )
    ),
    attempted_at TEXT NOT NULL CHECK(
        typeof(attempted_at) = 'text'
        AND julianday(attempted_at) IS NOT NULL
        AND substr(attempted_at, -6) = '+00:00'
        AND substr(attempted_at, 11, 1) = 'T'
    ),
    CHECK(
        (outcome = 'success' AND parse_result_id IS NOT NULL
         AND public_error_code IS NULL AND failure_stage IS NULL AND failure_reason IS NULL)
        OR
        (outcome = 'failed' AND parse_result_id IS NULL AND public_error_code IS NOT NULL
         AND (
             (run_kind = 'live' AND failure_stage IS NOT NULL AND failure_reason IS NOT NULL)
             OR
             (run_kind = 'legacy_backfill' AND failure_stage IS NULL AND failure_reason IS NULL
              AND public_error_code IN (
                  'official_document_parse_failed', 'official_document_resource_limit'
              ))
         ))
    )
);

CREATE TABLE fund_document_refresh_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT CHECK(typeof(id) = 'integer' AND id > 0),
    fund_code TEXT NOT NULL CHECK(
        typeof(fund_code) = 'text'
        AND length(fund_code) = 6
        AND fund_code NOT GLOB '*[^0-9]*'
    ),
    started_at TEXT NOT NULL CHECK(
        typeof(started_at) = 'text'
        AND julianday(started_at) IS NOT NULL
        AND substr(started_at, -6) = '+00:00'
        AND substr(started_at, 11, 1) = 'T'
    )
);

CREATE TABLE fund_document_refresh_completions (
    refresh_run_id INTEGER PRIMARY KEY CHECK(
        typeof(refresh_run_id) = 'integer' AND refresh_run_id > 0
    ) REFERENCES fund_document_refresh_runs(id) ON DELETE RESTRICT,
    outcome TEXT NOT NULL CHECK(outcome IN ('success', 'partial', 'failed', 'empty')),
    public_error_code TEXT CHECK(
        public_error_code IS NULL OR public_error_code IN (
            'official_document_unavailable', 'official_document_invalid',
            'official_document_resource_limit', 'official_document_parse_failed',
            'classification_storage_failed'
        )
    ),
    failure_stage TEXT CHECK(
        failure_stage IS NULL OR failure_stage IN (
            'discovery', 'landing_validation', 'retrieval', 'identity_validation',
            'container_validation', 'conversion', 'parser', 'persistence', 'unspecified'
        )
    ),
    failure_reason TEXT CHECK(
        failure_reason IS NULL OR failure_reason IN (
            'dns_unavailable', 'network_unavailable', 'http_unavailable',
            'source_unregistered', 'redirect_rejected', 'discovery_format_invalid',
            'identity_mismatch', 'publication_date_missing', 'landing_format_invalid',
            'landing_title_mismatch', 'landing_date_mismatch', 'attachment_missing',
            'attachment_ambiguous', 'attachment_host_rejected', 'authentication_shell',
            'empty_or_script_only_html', 'declared_mime_unsupported',
            'detected_container_unknown', 'declared_detected_mismatch',
            'legacy_ole_container_unsupported', 'legacy_converter_unavailable',
            'legacy_converter_timeout', 'legacy_converter_resource_limit',
            'legacy_converter_failed', 'legacy_converter_output_invalid',
            'resource_limit', 'parser_format_invalid', 'parser_identity_mismatch',
            'parser_effective_date_invalid', 'parser_ambiguous_fact', 'clock_invalid',
            'managed_artifact_invalid', 'storage_failure', 'unspecified_failure'
        )
    ),
    completed_at TEXT NOT NULL CHECK(
        typeof(completed_at) = 'text'
        AND julianday(completed_at) IS NOT NULL
        AND substr(completed_at, -6) = '+00:00'
        AND substr(completed_at, 11, 1) = 'T'
    ),
    CHECK(
        (outcome IN ('success', 'partial', 'empty') AND public_error_code IS NULL
         AND failure_stage IS NULL AND failure_reason IS NULL)
        OR
        (outcome = 'failed' AND public_error_code IS NOT NULL
         AND failure_stage IS NOT NULL AND failure_reason IS NOT NULL)
    )
);

CREATE TABLE fund_document_candidate_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT CHECK(typeof(id) = 'integer' AND id > 0),
    refresh_run_id INTEGER NOT NULL CHECK(
        typeof(refresh_run_id) = 'integer' AND refresh_run_id > 0
    ) REFERENCES fund_document_refresh_runs(id) ON DELETE RESTRICT,
    candidate_fingerprint TEXT NOT NULL CHECK(
        typeof(candidate_fingerprint) = 'text'
        AND length(CAST(candidate_fingerprint AS BLOB)) = 64
        AND candidate_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    fund_code TEXT NOT NULL CHECK(
        typeof(fund_code) = 'text'
        AND length(fund_code) = 6
        AND fund_code NOT GLOB '*[^0-9]*'
    ),
    document_kind TEXT NOT NULL CHECK(document_kind IN (
        'fund_contract', 'prospectus', 'prospectus_update', 'product_summary',
        'annual_report', 'semiannual_report', 'quarterly_report',
        'index_methodology', 'classification_announcement'
    )),
    url TEXT NOT NULL CHECK(
        typeof(url) = 'text' AND instr(url, char(0)) = 0 AND length(trim(url)) > 0
    ),
    published_at TEXT CHECK(
        published_at IS NULL OR (
            typeof(published_at) = 'text'
            AND julianday(published_at) IS NOT NULL
            AND substr(published_at, -6) = '+00:00'
            AND substr(published_at, 11, 1) = 'T'
        )
    ),
    outcome TEXT NOT NULL CHECK(outcome IN ('success', 'failed')),
    source_document_id INTEGER REFERENCES fund_document_artifacts(id) ON DELETE RESTRICT,
    parse_run_id INTEGER REFERENCES fund_document_parse_runs(id) ON DELETE RESTRICT,
    public_error_code TEXT CHECK(
        public_error_code IS NULL OR public_error_code IN (
            'official_document_unavailable', 'official_document_invalid',
            'official_document_resource_limit', 'official_document_parse_failed',
            'classification_storage_failed'
        )
    ),
    failure_stage TEXT CHECK(
        failure_stage IS NULL OR failure_stage IN (
            'discovery', 'landing_validation', 'retrieval', 'identity_validation',
            'container_validation', 'conversion', 'parser', 'persistence', 'unspecified'
        )
    ),
    failure_reason TEXT CHECK(
        failure_reason IS NULL OR failure_reason IN (
            'dns_unavailable', 'network_unavailable', 'http_unavailable',
            'source_unregistered', 'redirect_rejected', 'discovery_format_invalid',
            'identity_mismatch', 'publication_date_missing', 'landing_format_invalid',
            'landing_title_mismatch', 'landing_date_mismatch', 'attachment_missing',
            'attachment_ambiguous', 'attachment_host_rejected', 'authentication_shell',
            'empty_or_script_only_html', 'declared_mime_unsupported',
            'detected_container_unknown', 'declared_detected_mismatch',
            'legacy_ole_container_unsupported', 'legacy_converter_unavailable',
            'legacy_converter_timeout', 'legacy_converter_resource_limit',
            'legacy_converter_failed', 'legacy_converter_output_invalid',
            'resource_limit', 'parser_format_invalid', 'parser_identity_mismatch',
            'parser_effective_date_invalid', 'parser_ambiguous_fact', 'clock_invalid',
            'managed_artifact_invalid', 'storage_failure', 'unspecified_failure'
        )
    ),
    created_at TEXT NOT NULL CHECK(
        typeof(created_at) = 'text'
        AND julianday(created_at) IS NOT NULL
        AND substr(created_at, -6) = '+00:00'
        AND substr(created_at, 11, 1) = 'T'
    ),
    CHECK(
        (outcome = 'success' AND source_document_id IS NOT NULL AND parse_run_id IS NOT NULL
         AND public_error_code IS NULL AND failure_stage IS NULL AND failure_reason IS NULL)
        OR
        (outcome = 'failed' AND public_error_code IS NOT NULL
         AND failure_stage IS NOT NULL AND failure_reason IS NOT NULL
         AND ((source_document_id IS NULL AND parse_run_id IS NULL)
              OR (source_document_id IS NOT NULL AND parse_run_id IS NOT NULL)))
    ),
    UNIQUE(refresh_run_id, candidate_fingerprint)
);

CREATE INDEX fund_document_refresh_runs_fund
ON fund_document_refresh_runs(fund_code, started_at DESC, id DESC);
CREATE INDEX fund_document_candidate_runs_refresh
ON fund_document_candidate_runs(refresh_run_id, id);
CREATE INDEX fund_document_parse_results_source
ON fund_document_parse_results(source_document_id, provenance_id, id);
CREATE INDEX fund_document_parse_runs_source
ON fund_document_parse_runs(source_document_id, provenance_id, attempted_at DESC, id DESC);

CREATE TRIGGER fund_document_parse_result_binding
BEFORE INSERT ON fund_document_parse_results
WHEN NOT EXISTS (
    SELECT 1 FROM fund_document_artifacts AS artifact
    JOIN fund_document_parser_provenance AS provenance ON provenance.id = NEW.provenance_id
    WHERE artifact.id = NEW.source_document_id
)
BEGIN
    SELECT RAISE(ABORT, 'fund document parse result binding is invalid');
END;

CREATE TRIGGER fund_document_parse_run_binding
BEFORE INSERT ON fund_document_parse_runs
WHEN NEW.parse_result_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM fund_document_parse_results AS result
    WHERE result.id = NEW.parse_result_id
      AND result.source_document_id = NEW.source_document_id
      AND result.provenance_id = NEW.provenance_id
)
BEGIN
    SELECT RAISE(ABORT, 'fund document parse run binding is invalid');
END;

CREATE TRIGGER fund_document_candidate_run_binding
BEFORE INSERT ON fund_document_candidate_runs
WHEN NOT EXISTS (
    SELECT 1 FROM fund_document_refresh_runs AS refresh
    WHERE refresh.id = NEW.refresh_run_id AND refresh.fund_code = NEW.fund_code
)
OR (
    NEW.source_document_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM fund_document_artifacts AS artifact
        WHERE artifact.id = NEW.source_document_id
          AND artifact.fund_code = NEW.fund_code
          AND artifact.document_kind = NEW.document_kind
          AND artifact.landing_url = NEW.url
          AND artifact.published_at IS NEW.published_at
    )
)
OR (
    NEW.parse_run_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM fund_document_parse_runs AS run
        WHERE run.id = NEW.parse_run_id
          AND run.source_document_id = NEW.source_document_id
          AND run.outcome = NEW.outcome
    )
)
BEGIN
    SELECT RAISE(ABORT, 'fund document candidate run binding is invalid');
END;

CREATE TRIGGER fund_document_fact_result_required
BEFORE INSERT ON fund_mandate_facts
WHEN NEW.parse_result_id IS NULL
BEGIN
    SELECT RAISE(ABORT, 'fund mandate fact parse result is required');
END;

CREATE TRIGGER fund_document_fact_result_binding_insert
BEFORE INSERT ON fund_mandate_facts
WHEN NEW.parse_result_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM fund_document_parse_results AS result
    JOIN fund_document_parser_provenance AS provenance
      ON provenance.id = result.provenance_id
    JOIN fund_document_artifacts AS artifact
      ON artifact.id = result.source_document_id
    WHERE result.id = NEW.parse_result_id
      AND result.source_document_id = NEW.source_document_id
      AND provenance.parser_version = NEW.parser_version
      AND artifact.fund_code = NEW.fund_code
)
BEGIN
    SELECT RAISE(ABORT, 'fund mandate fact parse result binding is invalid');
END;

CREATE TRIGGER fund_document_fact_result_binding_update
BEFORE UPDATE OF parse_result_id ON fund_mandate_facts
WHEN NEW.parse_result_id IS NULL OR NOT EXISTS (
    SELECT 1 FROM fund_document_parse_results AS result
    JOIN fund_document_parser_provenance AS provenance
      ON provenance.id = result.provenance_id
    JOIN fund_document_artifacts AS artifact
      ON artifact.id = result.source_document_id
    WHERE result.id = NEW.parse_result_id
      AND result.source_document_id = NEW.source_document_id
      AND provenance.parser_version = NEW.parser_version
      AND artifact.fund_code = NEW.fund_code
)
BEGIN
    SELECT RAISE(ABORT, 'fund mandate fact parse result binding is invalid');
END;

CREATE TRIGGER fund_document_parser_provenance_no_replace
BEFORE INSERT ON fund_document_parser_provenance
WHEN EXISTS (
    SELECT 1 FROM fund_document_parser_provenance
    WHERE id = NEW.id OR provenance_checksum = NEW.provenance_checksum
       OR canonical_json = NEW.canonical_json
)
BEGIN
    SELECT RAISE(ABORT, 'fund document parser provenance is immutable');
END;
CREATE TRIGGER fund_document_parser_provenance_no_update
BEFORE UPDATE ON fund_document_parser_provenance BEGIN
    SELECT RAISE(ABORT, 'fund document parser provenance is immutable');
END;
CREATE TRIGGER fund_document_parser_provenance_no_delete
BEFORE DELETE ON fund_document_parser_provenance BEGIN
    SELECT RAISE(ABORT, 'fund document parser provenance is immutable');
END;
CREATE TRIGGER fund_document_parse_result_no_replace
BEFORE INSERT ON fund_document_parse_results
WHEN EXISTS (
    SELECT 1 FROM fund_document_parse_results
    WHERE id = NEW.id OR (
        source_document_id = NEW.source_document_id AND provenance_id = NEW.provenance_id
    )
)
BEGIN
    SELECT RAISE(ABORT, 'fund document parse results are immutable');
END;
CREATE TRIGGER fund_document_parse_result_no_update
BEFORE UPDATE ON fund_document_parse_results BEGIN
    SELECT RAISE(ABORT, 'fund document parse results are immutable');
END;
CREATE TRIGGER fund_document_parse_result_no_delete
BEFORE DELETE ON fund_document_parse_results BEGIN
    SELECT RAISE(ABORT, 'fund document parse results are immutable');
END;
CREATE TRIGGER fund_document_parse_run_no_replace
BEFORE INSERT ON fund_document_parse_runs
WHEN EXISTS (SELECT 1 FROM fund_document_parse_runs WHERE id = NEW.id)
BEGIN
    SELECT RAISE(ABORT, 'fund document parse runs are immutable');
END;
CREATE TRIGGER fund_document_parse_run_no_update
BEFORE UPDATE ON fund_document_parse_runs BEGIN
    SELECT RAISE(ABORT, 'fund document parse runs are immutable');
END;
CREATE TRIGGER fund_document_parse_run_no_delete
BEFORE DELETE ON fund_document_parse_runs BEGIN
    SELECT RAISE(ABORT, 'fund document parse runs are immutable');
END;
CREATE TRIGGER fund_document_refresh_run_no_replace
BEFORE INSERT ON fund_document_refresh_runs
WHEN EXISTS (SELECT 1 FROM fund_document_refresh_runs WHERE id = NEW.id)
BEGIN
    SELECT RAISE(ABORT, 'fund document refresh runs are immutable');
END;
CREATE TRIGGER fund_document_refresh_run_no_update
BEFORE UPDATE ON fund_document_refresh_runs BEGIN
    SELECT RAISE(ABORT, 'fund document refresh runs are immutable');
END;
CREATE TRIGGER fund_document_refresh_run_no_delete
BEFORE DELETE ON fund_document_refresh_runs BEGIN
    SELECT RAISE(ABORT, 'fund document refresh runs are immutable');
END;
CREATE TRIGGER fund_document_refresh_completion_no_replace
BEFORE INSERT ON fund_document_refresh_completions
WHEN EXISTS (
    SELECT 1 FROM fund_document_refresh_completions
    WHERE refresh_run_id = NEW.refresh_run_id
)
BEGIN
    SELECT RAISE(ABORT, 'fund document refresh completions are immutable');
END;
CREATE TRIGGER fund_document_refresh_completion_no_update
BEFORE UPDATE ON fund_document_refresh_completions BEGIN
    SELECT RAISE(ABORT, 'fund document refresh completions are immutable');
END;
CREATE TRIGGER fund_document_refresh_completion_no_delete
BEFORE DELETE ON fund_document_refresh_completions BEGIN
    SELECT RAISE(ABORT, 'fund document refresh completions are immutable');
END;
CREATE TRIGGER fund_document_candidate_run_no_replace
BEFORE INSERT ON fund_document_candidate_runs
WHEN EXISTS (
    SELECT 1 FROM fund_document_candidate_runs
    WHERE id = NEW.id OR (
        refresh_run_id = NEW.refresh_run_id
        AND candidate_fingerprint = NEW.candidate_fingerprint
    )
)
BEGIN
    SELECT RAISE(ABORT, 'fund document candidate runs are immutable');
END;
CREATE TRIGGER fund_document_candidate_run_no_update
BEFORE UPDATE ON fund_document_candidate_runs BEGIN
    SELECT RAISE(ABORT, 'fund document candidate runs are immutable');
END;
CREATE TRIGGER fund_document_candidate_run_no_delete
BEFORE DELETE ON fund_document_candidate_runs BEGIN
    SELECT RAISE(ABORT, 'fund document candidate runs are immutable');
END;
"""

SCHEMA_V13 = """
CREATE TABLE fund_document_selection_manifests (
    refresh_run_id INTEGER PRIMARY KEY
        REFERENCES fund_document_refresh_runs(id) ON DELETE RESTRICT,
    fund_code TEXT NOT NULL CHECK(
        typeof(fund_code) = 'text' AND length(fund_code) = 6
        AND fund_code NOT GLOB '*[^0-9]*'
    ),
    manifest_version INTEGER NOT NULL CHECK(manifest_version = 1),
    selection_policy_checksum TEXT NOT NULL CHECK(
        typeof(selection_policy_checksum) = 'text'
        AND length(CAST(selection_policy_checksum AS BLOB)) = 64
        AND selection_policy_checksum NOT GLOB '*[^0-9a-f]*'
    ),
    canonical_json TEXT NOT NULL CHECK(
        typeof(canonical_json) = 'text' AND instr(canonical_json, char(0)) = 0
        AND json_valid(canonical_json) AND json_type(canonical_json) = 'object'
    ),
    selection_checksum TEXT NOT NULL UNIQUE CHECK(
        typeof(selection_checksum) = 'text'
        AND length(CAST(selection_checksum AS BLOB)) = 64
        AND selection_checksum NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL CHECK(
        julianday(created_at) IS NOT NULL AND substr(created_at, -6) = '+00:00'
        AND substr(created_at, 11, 1) = 'T'
    )
);

CREATE TRIGGER fund_document_selection_manifest_refresh_binding
BEFORE INSERT ON fund_document_selection_manifests
WHEN NOT EXISTS (
    SELECT 1 FROM fund_document_refresh_runs
    WHERE id = NEW.refresh_run_id AND fund_code = NEW.fund_code
)
BEGIN
    SELECT RAISE(ABORT, 'fund document selection refresh binding is invalid');
END;

CREATE TRIGGER fund_document_selection_manifest_no_replace
BEFORE INSERT ON fund_document_selection_manifests
WHEN EXISTS (
    SELECT 1 FROM fund_document_selection_manifests
    WHERE refresh_run_id = NEW.refresh_run_id
       OR selection_checksum = NEW.selection_checksum
)
BEGIN
    SELECT RAISE(ABORT, 'fund document selection manifests are immutable');
END;

CREATE TRIGGER fund_document_selection_manifest_no_update
BEFORE UPDATE ON fund_document_selection_manifests
BEGIN
    SELECT RAISE(ABORT, 'fund document selection manifests are immutable');
END;

CREATE TRIGGER fund_document_selection_manifest_no_delete
BEFORE DELETE ON fund_document_selection_manifests
BEGIN
    SELECT RAISE(ABORT, 'fund document selection manifests are immutable');
END;
"""

SCHEMA_V14 = """
CREATE TABLE request_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT CHECK(typeof(id) = 'integer' AND id > 0),
    request_id TEXT NOT NULL UNIQUE CHECK(
        typeof(request_id) = 'text'
        AND length(CAST(request_id AS BLOB)) = 32
        AND request_id NOT GLOB '*[^0-9a-f]*'
    ),
    mode TEXT NOT NULL CHECK(
        typeof(mode) = 'text' AND mode IN ('rapid', 'deep')
    ),
    status TEXT NOT NULL CHECK(
        typeof(status) = 'text'
        AND status IN ('running', 'complete', 'partial', 'failed', 'cancelled', 'expired')
    ),
    started_at TEXT NOT NULL CHECK(
        typeof(started_at) = 'text'
        AND julianday(started_at) IS NOT NULL
        AND substr(started_at, -6) = '+00:00'
        AND substr(started_at, 11, 1) = 'T'
        AND strftime('%Y-%m-%dT%H:%M:%S', started_at) = substr(started_at, 1, 19)
        AND (
            length(started_at) = 25 OR (
                length(started_at) = 32
                AND substr(started_at, 20, 1) = '.'
                AND substr(started_at, 21, 6) NOT GLOB '*[^0-9]*'
                AND substr(started_at, 21, 6) != '000000'
            )
        )
    ),
    deadline_at TEXT NOT NULL CHECK(
        typeof(deadline_at) = 'text'
        AND julianday(deadline_at) IS NOT NULL
        AND substr(deadline_at, -6) = '+00:00'
        AND substr(deadline_at, 11, 1) = 'T'
        AND strftime('%Y-%m-%dT%H:%M:%S', deadline_at) = substr(deadline_at, 1, 19)
        AND (
            length(deadline_at) = 25 OR (
                length(deadline_at) = 32
                AND substr(deadline_at, 20, 1) = '.'
                AND substr(deadline_at, 21, 6) NOT GLOB '*[^0-9]*'
                AND substr(deadline_at, 21, 6) != '000000'
            )
        )
        AND julianday(deadline_at) > julianday(started_at)
    ),
    finished_at TEXT CHECK(
        finished_at IS NULL OR (
            typeof(finished_at) = 'text'
            AND julianday(finished_at) IS NOT NULL
            AND substr(finished_at, -6) = '+00:00'
            AND substr(finished_at, 11, 1) = 'T'
            AND strftime('%Y-%m-%dT%H:%M:%S', finished_at) = substr(finished_at, 1, 19)
            AND (
                length(finished_at) = 25 OR (
                    length(finished_at) = 32
                    AND substr(finished_at, 20, 1) = '.'
                    AND substr(finished_at, 21, 6) NOT GLOB '*[^0-9]*'
                    AND substr(finished_at, 21, 6) != '000000'
                )
            )
            AND julianday(finished_at) >= julianday(started_at)
        )
    ),
    omitted_work_json TEXT NOT NULL CHECK(
        typeof(omitted_work_json) = 'text'
        AND instr(omitted_work_json, char(0)) = 0
        AND json_valid(omitted_work_json)
        AND json_type(omitted_work_json) = 'array'
    ),
    CHECK(
        (status = 'running' AND finished_at IS NULL)
        OR (status != 'running' AND finished_at IS NOT NULL)
    )
);

CREATE TABLE source_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT CHECK(typeof(id) = 'integer' AND id > 0),
    request_run_id INTEGER NOT NULL CHECK(
        typeof(request_run_id) = 'integer' AND request_run_id > 0
    ) REFERENCES request_runs(id) ON DELETE RESTRICT,
    source_id TEXT NOT NULL CHECK(
        typeof(source_id) = 'text'
        AND length(source_id) BETWEEN 1 AND 128
        AND substr(source_id, 1, 1) GLOB '[a-z]'
        AND source_id NOT GLOB '*[^a-z0-9_]*'
    ),
    field_id TEXT NOT NULL CHECK(
        typeof(field_id) = 'text'
        AND length(field_id) BETWEEN 1 AND 128
        AND substr(field_id, 1, 1) GLOB '[a-z]'
        AND field_id NOT GLOB '*[^a-z0-9_]*'
    ),
    subject_key TEXT NOT NULL CHECK(
        typeof(subject_key) = 'text'
        AND length(subject_key) = 11
        AND substr(subject_key, 1, 5) = 'fund:'
        AND substr(subject_key, 6) NOT GLOB '*[^0-9]*'
    ),
    attempt_number INTEGER NOT NULL CHECK(
        typeof(attempt_number) = 'integer' AND attempt_number IN (1, 2)
    ),
    outcome TEXT NOT NULL CHECK(
        typeof(outcome) = 'text'
        AND outcome IN (
            'success', 'transient_failure', 'unavailable', 'unsupported',
            'cancelled', 'expired', 'cache_hit', 'skipped_cooldown'
        )
    ),
    started_at TEXT NOT NULL CHECK(
        typeof(started_at) = 'text'
        AND julianday(started_at) IS NOT NULL
        AND substr(started_at, -6) = '+00:00'
        AND substr(started_at, 11, 1) = 'T'
        AND strftime('%Y-%m-%dT%H:%M:%S', started_at) = substr(started_at, 1, 19)
        AND (
            length(started_at) = 25 OR (
                length(started_at) = 32
                AND substr(started_at, 20, 1) = '.'
                AND substr(started_at, 21, 6) NOT GLOB '*[^0-9]*'
                AND substr(started_at, 21, 6) != '000000'
            )
        )
    ),
    finished_at TEXT NOT NULL CHECK(
        typeof(finished_at) = 'text'
        AND julianday(finished_at) IS NOT NULL
        AND substr(finished_at, -6) = '+00:00'
        AND substr(finished_at, 11, 1) = 'T'
        AND strftime('%Y-%m-%dT%H:%M:%S', finished_at) = substr(finished_at, 1, 19)
        AND (
            length(finished_at) = 25 OR (
                length(finished_at) = 32
                AND substr(finished_at, 20, 1) = '.'
                AND substr(finished_at, 21, 6) NOT GLOB '*[^0-9]*'
                AND substr(finished_at, 21, 6) != '000000'
            )
        )
        AND julianday(finished_at) >= julianday(started_at)
    ),
    data_as_of TEXT CHECK(
        data_as_of IS NULL OR (
            typeof(data_as_of) = 'text'
            AND julianday(data_as_of) IS NOT NULL
            AND substr(data_as_of, -6) = '+00:00'
            AND substr(data_as_of, 11, 1) = 'T'
            AND strftime('%Y-%m-%dT%H:%M:%S', data_as_of) = substr(data_as_of, 1, 19)
            AND (
                length(data_as_of) = 25 OR (
                    length(data_as_of) = 32
                    AND substr(data_as_of, 20, 1) = '.'
                    AND substr(data_as_of, 21, 6) NOT GLOB '*[^0-9]*'
                    AND substr(data_as_of, 21, 6) != '000000'
                )
            )
            AND julianday(data_as_of) <= julianday(finished_at)
        )
    ),
    error_code TEXT CHECK(
        error_code IS NULL OR (
            typeof(error_code) = 'text'
            AND error_code IN (
                'dns_failure', 'transient_network_failure', 'network_timeout',
                'source_unavailable', 'http_4xx', 'unsafe_url', 'unsafe_redirect',
                'oversized_response', 'decode_failure', 'validation_failure',
                'parse_failure', 'identity_conflict', 'paywall_or_auth_required',
                'field_unsupported', 'source_contract_unsupported', 'http_not_found',
                'http_gone', 'request_cancelled', 'request_expired', 'cooldown_active'
            )
        )
    ),
    cooldown_until TEXT CHECK(
        cooldown_until IS NULL OR (
            typeof(cooldown_until) = 'text'
            AND julianday(cooldown_until) IS NOT NULL
            AND substr(cooldown_until, -6) = '+00:00'
            AND substr(cooldown_until, 11, 1) = 'T'
            AND strftime('%Y-%m-%dT%H:%M:%S', cooldown_until) = substr(cooldown_until, 1, 19)
            AND (
                length(cooldown_until) = 25 OR (
                    length(cooldown_until) = 32
                    AND substr(cooldown_until, 20, 1) = '.'
                    AND substr(cooldown_until, 21, 6) NOT GLOB '*[^0-9]*'
                    AND substr(cooldown_until, 21, 6) != '000000'
                )
            )
        )
    ),
    force_actor TEXT CHECK(
        force_actor IS NULL OR (
            typeof(force_actor) = 'text' AND force_actor = 'local_owner'
        )
    ),
    force_reason TEXT CHECK(
        force_reason IS NULL OR (
            typeof(force_reason) = 'text'
            AND force_reason IN (
                'owner_approved_retry', 'verify_source_recovery',
                'refresh_after_manual_supplement'
            )
        )
    ),
    registry_version TEXT NOT NULL CHECK(
        typeof(registry_version) = 'text'
        AND length(registry_version) BETWEEN 1 AND 64
        AND substr(registry_version, 1, 1) GLOB '[a-z0-9]'
        AND registry_version NOT GLOB '*[^a-z0-9._-]*'
    ),
    registry_checksum TEXT NOT NULL CHECK(
        typeof(registry_checksum) = 'text'
        AND length(CAST(registry_checksum AS BLOB)) = 64
        AND registry_checksum NOT GLOB '*[^0-9a-f]*'
    ),
    response_byte_count INTEGER NOT NULL CHECK(
        typeof(response_byte_count) = 'integer' AND response_byte_count >= 0
    ),
    UNIQUE(request_run_id, source_id, field_id, subject_key, attempt_number),
    CHECK(
        (force_actor IS NULL AND force_reason IS NULL)
        OR (
            force_actor = 'local_owner'
            AND force_reason IS NOT NULL
            AND outcome NOT IN ('cache_hit', 'skipped_cooldown')
        )
    ),
    CHECK(
        (
            outcome IN ('success', 'cache_hit')
            AND data_as_of IS NOT NULL
            AND error_code IS NULL
            AND cooldown_until IS NULL
        ) OR (
            outcome = 'transient_failure'
            AND data_as_of IS NULL
            AND error_code IN ('dns_failure', 'transient_network_failure', 'network_timeout')
            AND cooldown_until IS NOT NULL
            AND julianday(cooldown_until) > julianday(finished_at)
        ) OR (
            outcome = 'unavailable'
            AND data_as_of IS NULL
            AND error_code IN (
                'source_unavailable', 'http_4xx', 'unsafe_url', 'unsafe_redirect',
                'oversized_response', 'decode_failure', 'validation_failure',
                'parse_failure', 'identity_conflict', 'paywall_or_auth_required'
            )
            AND cooldown_until IS NULL
        ) OR (
            outcome = 'unsupported'
            AND data_as_of IS NULL
            AND error_code IN (
                'field_unsupported', 'source_contract_unsupported',
                'http_not_found', 'http_gone'
            )
            AND cooldown_until IS NULL
        ) OR (
            outcome = 'cancelled'
            AND data_as_of IS NULL
            AND error_code = 'request_cancelled'
            AND cooldown_until IS NULL
        ) OR (
            outcome = 'expired'
            AND data_as_of IS NULL
            AND error_code = 'request_expired'
            AND cooldown_until IS NULL
        ) OR (
            outcome = 'skipped_cooldown'
            AND data_as_of IS NULL
            AND error_code = 'cooldown_active'
            AND cooldown_until IS NOT NULL
            AND julianday(cooldown_until) > julianday(finished_at)
        )
    )
);

CREATE INDEX source_attempts_request
ON source_attempts(request_run_id, id);

CREATE INDEX source_attempts_history
ON source_attempts(source_id, field_id, subject_key, finished_at DESC, id DESC);

CREATE TABLE decision_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT CHECK(typeof(id) = 'integer' AND id > 0),
    request_run_id INTEGER NOT NULL UNIQUE CHECK(
        typeof(request_run_id) = 'integer' AND request_run_id > 0
    ) REFERENCES request_runs(id) ON DELETE RESTRICT,
    evidence_policy_version TEXT NOT NULL CHECK(
        typeof(evidence_policy_version) = 'text'
        AND length(evidence_policy_version) BETWEEN 1 AND 64
        AND substr(evidence_policy_version, 1, 1) GLOB '[a-z0-9]'
        AND evidence_policy_version NOT GLOB '*[^a-z0-9._-]*'
    ),
    evidence_policy_json TEXT NOT NULL CHECK(
        typeof(evidence_policy_json) = 'text'
        AND instr(evidence_policy_json, char(0)) = 0
        AND json_valid(evidence_policy_json)
        AND json_type(evidence_policy_json) = 'object'
    ),
    evidence_policy_checksum TEXT NOT NULL CHECK(
        typeof(evidence_policy_checksum) = 'text'
        AND length(CAST(evidence_policy_checksum AS BLOB)) = 64
        AND evidence_policy_checksum NOT GLOB '*[^0-9a-f]*'
    ),
    source_registry_version TEXT NOT NULL CHECK(
        typeof(source_registry_version) = 'text'
        AND length(source_registry_version) BETWEEN 1 AND 64
        AND substr(source_registry_version, 1, 1) GLOB '[a-z0-9]'
        AND source_registry_version NOT GLOB '*[^a-z0-9._-]*'
    ),
    source_registry_json TEXT NOT NULL CHECK(
        typeof(source_registry_json) = 'text'
        AND instr(source_registry_json, char(0)) = 0
        AND json_valid(source_registry_json)
        AND json_type(source_registry_json) = 'object'
    ),
    source_registry_checksum TEXT NOT NULL CHECK(
        typeof(source_registry_checksum) = 'text'
        AND length(CAST(source_registry_checksum AS BLOB)) = 64
        AND source_registry_checksum NOT GLOB '*[^0-9a-f]*'
    ),
    canonical_route_json TEXT NOT NULL CHECK(
        typeof(canonical_route_json) = 'text'
        AND instr(canonical_route_json, char(0)) = 0
        AND json_valid(canonical_route_json)
        AND json_type(canonical_route_json) = 'object'
    ),
    result_checksum TEXT NOT NULL CHECK(
        typeof(result_checksum) = 'text'
        AND length(CAST(result_checksum AS BLOB)) = 64
        AND result_checksum NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL CHECK(
        typeof(created_at) = 'text'
        AND julianday(created_at) IS NOT NULL
        AND substr(created_at, -6) = '+00:00'
        AND substr(created_at, 11, 1) = 'T'
        AND strftime('%Y-%m-%dT%H:%M:%S', created_at) = substr(created_at, 1, 19)
        AND (
            length(created_at) = 25 OR (
                length(created_at) = 32
                AND substr(created_at, 20, 1) = '.'
                AND substr(created_at, 21, 6) NOT GLOB '*[^0-9]*'
                AND substr(created_at, 21, 6) != '000000'
            )
        )
    )
);

CREATE TRIGGER request_run_insert_guard
BEFORE INSERT ON request_runs
WHEN NEW.status != 'running'
     OR NEW.finished_at IS NOT NULL
     OR NEW.omitted_work_json != '[]'
BEGIN
    SELECT RAISE(ABORT, 'request runs must begin in running state');
END;

CREATE TRIGGER request_run_no_replace
BEFORE INSERT ON request_runs
WHEN EXISTS (
    SELECT 1 FROM request_runs
    WHERE id = NEW.id OR request_id = NEW.request_id
)
BEGIN
    SELECT RAISE(ABORT, 'request runs cannot be replaced');
END;

CREATE TRIGGER request_run_update_guard
BEFORE UPDATE ON request_runs
WHEN NOT (
    OLD.status = 'running'
    AND NEW.status IN ('complete', 'partial', 'failed', 'cancelled', 'expired')
    AND NEW.id = OLD.id
    AND NEW.request_id = OLD.request_id
    AND NEW.mode = OLD.mode
    AND NEW.started_at = OLD.started_at
    AND NEW.deadline_at = OLD.deadline_at
    AND OLD.finished_at IS NULL
    AND NEW.finished_at IS NOT NULL
)
BEGIN
    SELECT RAISE(ABORT, 'request run transition is invalid');
END;

CREATE TRIGGER request_run_no_delete
BEFORE DELETE ON request_runs
BEGIN
    SELECT RAISE(ABORT, 'request runs cannot be deleted');
END;

CREATE TRIGGER source_attempt_no_replace
BEFORE INSERT ON source_attempts
WHEN EXISTS (
    SELECT 1 FROM source_attempts
    WHERE id = NEW.id OR (
        request_run_id = NEW.request_run_id
        AND source_id = NEW.source_id
        AND field_id = NEW.field_id
        AND subject_key = NEW.subject_key
        AND attempt_number = NEW.attempt_number
    )
)
BEGIN
    SELECT RAISE(ABORT, 'source attempts are immutable');
END;

CREATE TRIGGER source_attempt_no_update
BEFORE UPDATE ON source_attempts
BEGIN
    SELECT RAISE(ABORT, 'source attempts are immutable');
END;

CREATE TRIGGER source_attempt_no_delete
BEFORE DELETE ON source_attempts
BEGIN
    SELECT RAISE(ABORT, 'source attempts are immutable');
END;

CREATE TRIGGER decision_snapshot_no_replace
BEFORE INSERT ON decision_snapshots
WHEN EXISTS (
    SELECT 1 FROM decision_snapshots
    WHERE id = NEW.id OR request_run_id = NEW.request_run_id
)
BEGIN
    SELECT RAISE(ABORT, 'decision snapshots are immutable');
END;

CREATE TRIGGER decision_snapshot_no_update
BEFORE UPDATE ON decision_snapshots
BEGIN
    SELECT RAISE(ABORT, 'decision snapshots are immutable');
END;

CREATE TRIGGER decision_snapshot_no_delete
BEFORE DELETE ON decision_snapshots
BEGIN
    SELECT RAISE(ABORT, 'decision snapshots are immutable');
END;
"""
