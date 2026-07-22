SCHEMA_VERSION = 24

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
        AND deadline_at COLLATE BINARY > started_at COLLATE BINARY
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
            AND finished_at COLLATE BINARY >= started_at COLLATE BINARY
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
        AND finished_at COLLATE BINARY >= started_at COLLATE BINARY
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
            AND data_as_of COLLATE BINARY <= finished_at COLLATE BINARY
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
            AND cooldown_until COLLATE BINARY > finished_at COLLATE BINARY
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
            AND cooldown_until COLLATE BINARY > finished_at COLLATE BINARY
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

SCHEMA_V15 = """
CREATE TABLE source_work_authorizations (
    id INTEGER PRIMARY KEY AUTOINCREMENT CHECK(typeof(id) = 'integer' AND id > 0),
    request_run_id INTEGER NOT NULL CHECK(
        typeof(request_run_id) = 'integer' AND request_run_id > 0
    ) REFERENCES request_runs(id) ON DELETE RESTRICT,
    kind TEXT NOT NULL CHECK(
        typeof(kind) = 'text' AND kind IN ('force', 'retry')
    ),
    parent_attempt_id INTEGER CHECK(
        parent_attempt_id IS NULL OR (
            typeof(parent_attempt_id) = 'integer' AND parent_attempt_id > 0
        )
    ) REFERENCES source_attempts(id) ON DELETE RESTRICT,
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
    actor TEXT CHECK(
        actor IS NULL OR (typeof(actor) = 'text' AND actor = 'local_owner')
    ),
    reason TEXT CHECK(
        reason IS NULL OR (
            typeof(reason) = 'text'
            AND reason IN (
                'owner_approved_retry', 'verify_source_recovery',
                'refresh_after_manual_supplement'
            )
        )
    ),
    reserved_at TEXT NOT NULL CHECK(
        typeof(reserved_at) = 'text'
        AND julianday(reserved_at) IS NOT NULL
        AND substr(reserved_at, -6) = '+00:00'
        AND substr(reserved_at, 11, 1) = 'T'
        AND strftime('%Y-%m-%dT%H:%M:%S', reserved_at) = substr(reserved_at, 1, 19)
        AND (
            length(reserved_at) = 25 OR (
                length(reserved_at) = 32
                AND substr(reserved_at, 20, 1) = '.'
                AND substr(reserved_at, 21, 6) NOT GLOB '*[^0-9]*'
                AND substr(reserved_at, 21, 6) != '000000'
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
        AND deadline_at COLLATE BINARY >= reserved_at COLLATE BINARY
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
    UNIQUE(request_run_id, kind, source_id, field_id, subject_key),
    CHECK(
        (
            kind = 'force'
            AND parent_attempt_id IS NULL
            AND actor = 'local_owner'
            AND reason IS NOT NULL
        ) OR (
            kind = 'retry'
            AND parent_attempt_id IS NOT NULL
            AND actor IS NULL
            AND reason IS NULL
        )
    )
);

ALTER TABLE source_attempts
ADD COLUMN authorization_id INTEGER
REFERENCES source_work_authorizations(id) ON DELETE RESTRICT;

CREATE UNIQUE INDEX source_attempts_authorization_consumed
ON source_attempts(authorization_id)
WHERE authorization_id IS NOT NULL;

CREATE INDEX source_work_authorizations_request
ON source_work_authorizations(request_run_id, id);

CREATE TRIGGER source_work_authorization_insert_guard
BEFORE INSERT ON source_work_authorizations
WHEN NOT EXISTS (
    SELECT 1
    FROM request_runs AS run
    WHERE run.id = NEW.request_run_id
      AND run.status = 'running'
      AND NEW.reserved_at COLLATE BINARY >= run.started_at COLLATE BINARY
      AND NEW.reserved_at COLLATE BINARY <= run.deadline_at COLLATE BINARY
      AND NEW.deadline_at = run.deadline_at
      AND (NEW.kind = 'retry' OR run.mode = 'deep')
      AND (
          NEW.kind = 'retry'
          OR NOT EXISTS (
              SELECT 1
              FROM source_attempts AS ordinary
              WHERE ordinary.request_run_id = NEW.request_run_id
                AND ordinary.source_id = NEW.source_id
                AND ordinary.field_id = NEW.field_id
                AND ordinary.subject_key = NEW.subject_key
                AND ordinary.attempt_number = 1
          )
      )
      AND (
          NEW.kind = 'force'
          OR EXISTS (
              SELECT 1
              FROM source_attempts AS parent
              WHERE parent.id = NEW.parent_attempt_id
                AND parent.request_run_id = NEW.request_run_id
                AND parent.source_id = NEW.source_id
                AND parent.field_id = NEW.field_id
                AND parent.subject_key = NEW.subject_key
                AND parent.attempt_number = 1
                AND parent.outcome = 'transient_failure'
                AND parent.error_code IN (
                    'dns_failure', 'transient_network_failure', 'network_timeout'
                )
                AND parent.finished_at COLLATE BINARY <= NEW.reserved_at COLLATE BINARY
                AND parent.registry_version = NEW.registry_version
                AND parent.registry_checksum = NEW.registry_checksum
          )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'source work authorization binding is invalid');
END;

CREATE TRIGGER source_work_authorization_no_replace
BEFORE INSERT ON source_work_authorizations
WHEN EXISTS (
    SELECT 1
    FROM source_work_authorizations
    WHERE id = NEW.id OR (
        request_run_id = NEW.request_run_id
        AND kind = NEW.kind
        AND source_id = NEW.source_id
        AND field_id = NEW.field_id
        AND subject_key = NEW.subject_key
    )
)
BEGIN
    SELECT RAISE(ABORT, 'source work authorizations are immutable');
END;

CREATE TRIGGER source_work_authorization_no_update
BEFORE UPDATE ON source_work_authorizations
BEGIN
    SELECT RAISE(ABORT, 'source work authorizations are immutable');
END;

CREATE TRIGGER source_work_authorization_no_delete
BEFORE DELETE ON source_work_authorizations
BEGIN
    SELECT RAISE(ABORT, 'source work authorizations are immutable');
END;

CREATE TRIGGER source_attempt_authorization_guard
BEFORE INSERT ON source_attempts
WHEN NOT (
    (
        NEW.authorization_id IS NULL
        AND NEW.attempt_number = 1
        AND NEW.force_actor IS NULL
        AND NEW.force_reason IS NULL
        AND NOT EXISTS (
            SELECT 1
            FROM source_work_authorizations AS pending_force
            LEFT JOIN source_attempts AS consumed
              ON consumed.authorization_id = pending_force.id
            WHERE pending_force.request_run_id = NEW.request_run_id
              AND pending_force.kind = 'force'
              AND pending_force.source_id = NEW.source_id
              AND pending_force.field_id = NEW.field_id
              AND pending_force.subject_key = NEW.subject_key
              AND consumed.id IS NULL
        )
    ) OR EXISTS (
        SELECT 1
        FROM source_work_authorizations AS authorization
        JOIN request_runs AS run ON run.id = authorization.request_run_id
        WHERE authorization.id = NEW.authorization_id
          AND run.status = 'running'
          AND authorization.request_run_id = NEW.request_run_id
          AND authorization.source_id = NEW.source_id
          AND authorization.field_id = NEW.field_id
          AND authorization.subject_key = NEW.subject_key
          AND authorization.registry_version = NEW.registry_version
          AND authorization.registry_checksum = NEW.registry_checksum
          AND NEW.started_at COLLATE BINARY >= authorization.reserved_at COLLATE BINARY
          AND NEW.finished_at COLLATE BINARY <= authorization.deadline_at COLLATE BINARY
          AND (
              (
                  authorization.kind = 'force'
                  AND NEW.attempt_number = 1
                  AND NEW.force_actor = authorization.actor
                  AND NEW.force_reason = authorization.reason
              ) OR (
                  authorization.kind = 'retry'
                  AND NEW.attempt_number = 2
                  AND NEW.force_actor IS NULL
                  AND NEW.force_reason IS NULL
              )
          )
    )
)
BEGIN
    SELECT RAISE(ABORT, 'source attempt authorization binding is invalid');
END;
"""

SCHEMA_V16 = """
CREATE TABLE brief_policy_versions (
    version TEXT PRIMARY KEY CHECK(
        typeof(version) = 'text' AND version = '1'
    ),
    canonical_policy_json TEXT NOT NULL CHECK(
        typeof(canonical_policy_json) = 'text'
        AND instr(canonical_policy_json, char(0)) = 0
        AND json_valid(canonical_policy_json)
        AND json_type(canonical_policy_json) = 'object'
        AND canonical_policy_json = json(canonical_policy_json)
        AND length(CAST(canonical_policy_json AS BLOB)) <= 65536
    ),
    policy_checksum TEXT NOT NULL CHECK(
        typeof(policy_checksum) = 'text'
        AND length(CAST(policy_checksum AS BLOB)) = 64
        AND policy_checksum NOT GLOB '*[^0-9a-f]*'
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
) WITHOUT ROWID;

CREATE TRIGGER brief_policy_no_replace
BEFORE INSERT ON brief_policy_versions
WHEN EXISTS (
    SELECT 1 FROM brief_policy_versions WHERE version = NEW.version
)
BEGIN
    SELECT RAISE(ABORT, 'brief policy versions cannot be replaced');
END;

CREATE TRIGGER brief_policy_no_update
BEFORE UPDATE ON brief_policy_versions
BEGIN
    SELECT RAISE(ABORT, 'brief policy versions are immutable');
END;

CREATE TRIGGER brief_policy_no_delete
BEFORE DELETE ON brief_policy_versions
BEGIN
    SELECT RAISE(ABORT, 'brief policy versions are immutable');
END;

CREATE TABLE fund_brief_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT CHECK(typeof(id) = 'integer' AND id > 0),
    request_run_id INTEGER NOT NULL UNIQUE CHECK(
        typeof(request_run_id) = 'integer' AND request_run_id > 0
    ) REFERENCES request_runs(id) ON DELETE RESTRICT,
    decision_snapshot_id INTEGER NOT NULL UNIQUE CHECK(
        typeof(decision_snapshot_id) = 'integer' AND decision_snapshot_id > 0
    ) REFERENCES decision_snapshots(id) ON DELETE RESTRICT,
    fund_code TEXT NOT NULL CHECK(
        typeof(fund_code) = 'text'
        AND length(CAST(fund_code AS BLOB)) = 6
        AND fund_code NOT GLOB '*[^0-9]*'
    ),
    action_ids_json TEXT NOT NULL CHECK(
        typeof(action_ids_json) = 'text'
        AND action_ids_json IN (
            '["fact_research","continue_holding"]',
            '["fact_research","reduce_to_cash"]',
            '["fact_research","full_exit"]',
            '["fact_research","switch_reduce","switch_buy"]'
        )
    ),
    primary_state TEXT NOT NULL CHECK(
        typeof(primary_state) = 'text'
        AND primary_state IN ('no_add', 'hold', 'watch', 'reduce_or_exit_review', 'abstain')
    ),
    action_maturity TEXT NOT NULL CHECK(
        typeof(action_maturity) = 'text'
        AND action_maturity IN ('mature', 'experimental_shadow')
    ),
    triggered_reviews_json TEXT NOT NULL CHECK(
        typeof(triggered_reviews_json) = 'text'
        AND json_valid(triggered_reviews_json)
        AND json_type(triggered_reviews_json) = 'array'
        AND triggered_reviews_json = json(triggered_reviews_json)
        AND json_array_length(triggered_reviews_json) <= 128
        AND length(CAST(triggered_reviews_json AS BLOB)) <= 16384
    ),
    affected_action_abstentions_json TEXT NOT NULL CHECK(
        typeof(affected_action_abstentions_json) = 'text'
        AND json_valid(affected_action_abstentions_json)
        AND json_type(affected_action_abstentions_json) = 'array'
        AND affected_action_abstentions_json = json(affected_action_abstentions_json)
        AND json_array_length(affected_action_abstentions_json) <= 128
        AND length(CAST(affected_action_abstentions_json AS BLOB)) <= 16384
    ),
    blocking_codes_json TEXT NOT NULL CHECK(
        typeof(blocking_codes_json) = 'text'
        AND json_valid(blocking_codes_json)
        AND json_type(blocking_codes_json) = 'array'
        AND blocking_codes_json = json(blocking_codes_json)
        AND json_array_length(blocking_codes_json) <= 128
        AND length(CAST(blocking_codes_json AS BLOB)) <= 16384
    ),
    evidence_state TEXT NOT NULL CHECK(
        typeof(evidence_state) = 'text'
        AND evidence_state IN ('complete', 'partial', 'insufficient')
    ),
    missing_fields_json TEXT NOT NULL CHECK(
        typeof(missing_fields_json) = 'text'
        AND json_valid(missing_fields_json)
        AND json_type(missing_fields_json) = 'array'
        AND missing_fields_json = json(missing_fields_json)
        AND json_array_length(missing_fields_json) <= 128
        AND length(CAST(missing_fields_json AS BLOB)) <= 16384
    ),
    conflicts_json TEXT NOT NULL CHECK(
        typeof(conflicts_json) = 'text'
        AND json_valid(conflicts_json)
        AND json_type(conflicts_json) = 'array'
        AND conflicts_json = json(conflicts_json)
        AND json_array_length(conflicts_json) <= 128
        AND length(CAST(conflicts_json AS BLOB)) <= 16384
    ),
    source_lineage_ids_json TEXT NOT NULL CHECK(
        typeof(source_lineage_ids_json) = 'text'
        AND json_valid(source_lineage_ids_json)
        AND json_type(source_lineage_ids_json) = 'array'
        AND source_lineage_ids_json = json(source_lineage_ids_json)
        AND json_array_length(source_lineage_ids_json) <= 128
        AND length(CAST(source_lineage_ids_json AS BLOB)) <= 16384
    ),
    evidence_fingerprint TEXT NOT NULL CHECK(
        typeof(evidence_fingerprint) = 'text'
        AND length(CAST(evidence_fingerprint AS BLOB)) = 64
        AND evidence_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    canonical_snapshot_json TEXT NOT NULL CHECK(
        typeof(canonical_snapshot_json) = 'text'
        AND instr(canonical_snapshot_json, char(0)) = 0
        AND json_valid(canonical_snapshot_json)
        AND json_type(canonical_snapshot_json) = 'object'
        AND canonical_snapshot_json = json(canonical_snapshot_json)
        AND length(CAST(canonical_snapshot_json AS BLOB)) <= 4194304
    ),
    result_checksum TEXT NOT NULL CHECK(
        typeof(result_checksum) = 'text'
        AND length(CAST(result_checksum AS BLOB)) = 64
        AND result_checksum NOT GLOB '*[^0-9a-f]*'
    ),
    conclusion_changed INTEGER NOT NULL CHECK(
        typeof(conclusion_changed) = 'integer' AND conclusion_changed IN (0, 1)
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

CREATE INDEX fund_brief_snapshots_history
ON fund_brief_snapshots(fund_code, created_at DESC, id DESC);

CREATE TRIGGER fund_brief_snapshot_insert_guard
BEFORE INSERT ON fund_brief_snapshots
WHEN NOT EXISTS (
    SELECT 1
    FROM request_runs
    JOIN decision_snapshots
      ON decision_snapshots.request_run_id = request_runs.id
    WHERE request_runs.id = NEW.request_run_id
      AND request_runs.status = 'running'
      AND decision_snapshots.id = NEW.decision_snapshot_id
      AND json_extract(NEW.canonical_snapshot_json, '$.request_run_id') = NEW.request_run_id
      AND json_extract(NEW.canonical_snapshot_json, '$.decision_snapshot_id')
          = NEW.decision_snapshot_id
      AND json_extract(NEW.canonical_snapshot_json, '$.fund_code') = NEW.fund_code
      AND json_extract(NEW.canonical_snapshot_json, '$.action_ids')
          = json(NEW.action_ids_json)
      AND json_extract(NEW.canonical_snapshot_json, '$.primary_state') = NEW.primary_state
      AND json_extract(NEW.canonical_snapshot_json, '$.action_maturity')
          = NEW.action_maturity
      AND json_extract(NEW.canonical_snapshot_json, '$.triggered_reviews')
          = json(NEW.triggered_reviews_json)
      AND json_extract(NEW.canonical_snapshot_json, '$.affected_action_abstentions')
          = json(NEW.affected_action_abstentions_json)
      AND json_extract(NEW.canonical_snapshot_json, '$.blocking_codes')
          = json(NEW.blocking_codes_json)
      AND json_extract(NEW.canonical_snapshot_json, '$.evidence_state') = NEW.evidence_state
      AND json_extract(NEW.canonical_snapshot_json, '$.missing_fields')
          = json(NEW.missing_fields_json)
      AND json_extract(NEW.canonical_snapshot_json, '$.conflicts')
          = json(NEW.conflicts_json)
      AND json_extract(NEW.canonical_snapshot_json, '$.source_lineage_ids')
          = json(NEW.source_lineage_ids_json)
      AND json_extract(NEW.canonical_snapshot_json, '$.evidence_fingerprint')
          = NEW.evidence_fingerprint
      AND json_extract(NEW.canonical_snapshot_json, '$.created_at') = NEW.created_at
)
BEGIN
    SELECT RAISE(ABORT, 'brief snapshot request binding failed');
END;

CREATE TRIGGER fund_brief_snapshot_array_guard
BEFORE INSERT ON fund_brief_snapshots
WHEN EXISTS (
    SELECT 1 FROM json_each(NEW.triggered_reviews_json)
    WHERE type != 'text' OR length(value) NOT BETWEEN 1 AND 64
       OR substr(value, 1, 1) NOT GLOB '[a-z]'
       OR value GLOB '*[^a-z0-9_]*'
) OR EXISTS (
    SELECT 1 FROM json_each(NEW.affected_action_abstentions_json)
    WHERE type != 'text' OR length(value) NOT BETWEEN 1 AND 64
       OR substr(value, 1, 1) NOT GLOB '[a-z]'
       OR value GLOB '*[^a-z0-9_]*'
) OR EXISTS (
    SELECT 1 FROM json_each(NEW.blocking_codes_json)
    WHERE type != 'text' OR length(value) NOT BETWEEN 1 AND 64
       OR substr(value, 1, 1) NOT GLOB '[a-z]'
       OR value GLOB '*[^a-z0-9_]*'
) OR EXISTS (
    SELECT 1 FROM json_each(NEW.missing_fields_json)
    WHERE type != 'text' OR length(value) NOT BETWEEN 1 AND 64
       OR substr(value, 1, 1) NOT GLOB '[a-z]'
       OR value GLOB '*[^a-z0-9_]*'
) OR EXISTS (
    SELECT 1 FROM json_each(NEW.conflicts_json)
    WHERE type != 'text' OR length(value) NOT BETWEEN 1 AND 64
       OR substr(value, 1, 1) NOT GLOB '[a-z]'
       OR value GLOB '*[^a-z0-9_]*'
) OR EXISTS (
    SELECT 1 FROM json_each(NEW.source_lineage_ids_json)
    WHERE type != 'text' OR length(value) NOT BETWEEN 1 AND 64
       OR substr(value, 1, 1) NOT GLOB '[a-z]'
       OR value GLOB '*[^a-z0-9_]*'
)
BEGIN
    SELECT RAISE(ABORT, 'brief snapshot arrays must contain bounded identifiers');
END;

CREATE TRIGGER fund_brief_snapshot_private_key_guard
BEFORE INSERT ON fund_brief_snapshots
WHEN EXISTS (
    SELECT 1
    FROM (
        SELECT
            lower(
                replace(replace(replace(key, '-', '_'), ' ', '_'), '.', '_')
            ) AS normalized_key,
            fullkey AS full_key,
            type AS json_value_type
        FROM json_tree(NEW.canonical_snapshot_json)
        WHERE key IS NOT NULL
    )
    WHERE
        (
            instr('_' || normalized_key || '_', '_amount_') > 0
            AND NOT (
                full_key GLOB
                    '$.interpretations[[]*[]]."exact_amount_available"'
                AND length(full_key) >
                    length('$.interpretations[')
                    + length(']."exact_amount_available"')
                AND substr(
                    full_key,
                    length('$.interpretations[') + 1,
                    length(full_key)
                    - length('$.interpretations[')
                    - length(']."exact_amount_available"')
                ) NOT GLOB '*[^0-9]*'
                AND json_value_type = 'false'
            )
        )
        OR instr('_' || normalized_key || '_', '_ciphertext_') > 0
        OR instr('_' || normalized_key || '_', '_cost_') > 0
        OR instr('_' || normalized_key || '_', '_credential_') > 0
        OR instr('_' || normalized_key || '_', '_debt_') > 0
        OR instr('_' || normalized_key || '_', '_income_') > 0
        OR instr('_' || normalized_key || '_', '_nonce_') > 0
        OR instr('_' || normalized_key || '_', '_private_') > 0
        OR instr('_' || normalized_key || '_', '_profit_') > 0
        OR instr('_' || normalized_key || '_', '_reserve_') > 0
        OR instr('_' || normalized_key || '_', '_shares_') > 0
        OR instr('_' || normalized_key || '_', '_token_') > 0
        OR (
            (
                instr('_' || normalized_key || '_', '_asset_') > 0
                OR instr('_' || normalized_key || '_', '_assets_') > 0
            )
            AND normalized_key NOT IN ('asset_class', 'candidate_asset_coverage')
        )
        OR (
            instr('_' || normalized_key || '_', '_current_') > 0
            AND instr('_' || normalized_key || '_', '_value_') > 0
        )
        OR (
            instr('_' || normalized_key || '_', '_position_') > 0
            AND instr('_' || normalized_key || '_', '_value_') > 0
        )
        OR (
            instr('_' || normalized_key || '_', '_local_') > 0
            AND instr('_' || normalized_key || '_', '_path_') > 0
        )
        OR (
            instr('_' || normalized_key || '_', '_managed_') > 0
            AND instr('_' || normalized_key || '_', '_path_') > 0
        )
        OR (
            instr('_' || normalized_key || '_', '_loss_') > 0
            AND instr('_' || normalized_key || '_', '_budget_') > 0
        )
        OR (
            instr('_' || normalized_key || '_', '_portfolio_') > 0
            AND instr('_' || normalized_key || '_', '_weight_') > 0
        )
        OR (
            instr('_' || normalized_key || '_', '_owner_') > 0
            AND instr('_' || normalized_key || '_', '_weight_') > 0
        )
        OR (
            instr('_' || normalized_key || '_', '_position_') > 0
            AND instr('_' || normalized_key || '_', '_weight_') > 0
        )
        OR (
            instr('_' || normalized_key || '_', '_purchase_') > 0
            AND instr('_' || normalized_key || '_', '_lots_') > 0
        )
        OR (
            instr('_' || normalized_key || '_', '_raw_') > 0
            AND instr('_' || normalized_key || '_', '_body_') > 0
        )
        OR (
            instr('_' || normalized_key || '_', '_response_') > 0
            AND instr('_' || normalized_key || '_', '_body_') > 0
        )
)
BEGIN
    SELECT RAISE(ABORT, 'brief snapshot contains a private key');
END;

CREATE TRIGGER fund_brief_snapshot_duplicate_guard
BEFORE INSERT ON fund_brief_snapshots
WHEN EXISTS (
    SELECT value FROM json_each(NEW.triggered_reviews_json)
    GROUP BY value HAVING count(*) > 1
) OR EXISTS (
    SELECT value FROM json_each(NEW.affected_action_abstentions_json)
    GROUP BY value HAVING count(*) > 1
) OR EXISTS (
    SELECT value FROM json_each(NEW.blocking_codes_json)
    GROUP BY value HAVING count(*) > 1
) OR EXISTS (
    SELECT value FROM json_each(NEW.missing_fields_json)
    GROUP BY value HAVING count(*) > 1
) OR EXISTS (
    SELECT value FROM json_each(NEW.conflicts_json)
    GROUP BY value HAVING count(*) > 1
) OR EXISTS (
    SELECT value FROM json_each(NEW.source_lineage_ids_json)
    GROUP BY value HAVING count(*) > 1
)
BEGIN
    SELECT RAISE(ABORT, 'brief snapshot arrays contain duplicates');
END;

CREATE TRIGGER fund_brief_snapshot_no_replace
BEFORE INSERT ON fund_brief_snapshots
WHEN EXISTS (
    SELECT 1 FROM fund_brief_snapshots
    WHERE request_run_id = NEW.request_run_id
       OR decision_snapshot_id = NEW.decision_snapshot_id
)
BEGIN
    SELECT RAISE(ABORT, 'brief snapshots cannot be replaced');
END;

CREATE TRIGGER fund_brief_snapshot_no_update
BEFORE UPDATE ON fund_brief_snapshots
BEGIN
    SELECT RAISE(ABORT, 'brief snapshots are immutable');
END;

CREATE TRIGGER fund_brief_snapshot_no_delete
BEFORE DELETE ON fund_brief_snapshots
BEGIN
    SELECT RAISE(ABORT, 'brief snapshots are immutable');
END;
"""

SCHEMA_V17 = """
ALTER TABLE fund_nav
ADD COLUMN corporate_action_state TEXT NOT NULL DEFAULT 'unknown'
CHECK(
    typeof(corporate_action_state) = 'text'
    AND corporate_action_state IN ('none', 'present', 'unknown')
);

ALTER TABLE fund_nav
ADD COLUMN source_attempt_id INTEGER
REFERENCES source_attempts(id) ON DELETE RESTRICT;

CREATE INDEX fund_nav_source_attempt
ON fund_nav(source_attempt_id, retrieved_at);
"""

SCHEMA_V18 = """
ALTER TABLE positions
ADD COLUMN sync_run_id INTEGER
REFERENCES sync_runs(id) ON DELETE RESTRICT;

CREATE INDEX positions_sync_run
ON positions(sync_run_id, account_id, fund_code);

CREATE TABLE portfolio_observation_accounts (
    sync_run_id INTEGER NOT NULL
        REFERENCES sync_runs(id) ON DELETE RESTRICT,
    account_id INTEGER NOT NULL
        REFERENCES accounts(id) ON DELETE RESTRICT,
    account_title TEXT NOT NULL CHECK(
        typeof(account_title) = 'text'
        AND instr(account_title, char(0)) = 0
        AND length(account_title) BETWEEN 1 AND 256
    ),
    observed_at TEXT NOT NULL CHECK(
        typeof(observed_at) = 'text'
        AND julianday(observed_at) IS NOT NULL
        AND substr(observed_at, -6) = '+00:00'
        AND substr(observed_at, 11, 1) = 'T'
        AND strftime('%Y-%m-%dT%H:%M:%S', observed_at) = substr(observed_at, 1, 19)
        AND (
            length(observed_at) = 25 OR (
                length(observed_at) = 32
                AND substr(observed_at, 20, 1) = '.'
                AND substr(observed_at, 21, 6) NOT GLOB '*[^0-9]*'
                AND substr(observed_at, 21, 6) != '000000'
            )
        )
    ),
    PRIMARY KEY(sync_run_id, account_id)
);

CREATE INDEX portfolio_observation_accounts_account
ON portfolio_observation_accounts(account_id, sync_run_id);

CREATE TRIGGER portfolio_observation_account_insert_guard
BEFORE INSERT ON portfolio_observation_accounts
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM sync_runs
        WHERE id = NEW.sync_run_id
          AND source = 'yangjibao'
          AND status = 'running'
    ) OR NOT EXISTS (
        SELECT 1 FROM accounts
        WHERE id = NEW.account_id AND source = 'yangjibao'
    ) OR EXISTS (
        SELECT 1 FROM portfolio_observation_snapshots
        WHERE sync_run_id = NEW.sync_run_id
    ) THEN RAISE(ABORT, 'portfolio snapshot account set is closed') END;
END;

CREATE TRIGGER portfolio_position_snapshot_insert_guard
BEFORE INSERT ON positions
WHEN NEW.sync_run_id IS NOT NULL
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM sync_runs
        WHERE id = NEW.sync_run_id
          AND source = 'yangjibao'
          AND status = 'running'
    ) OR EXISTS (
        SELECT 1 FROM portfolio_observation_snapshots
        WHERE sync_run_id = NEW.sync_run_id
    ) OR NOT EXISTS (
        SELECT 1 FROM portfolio_observation_accounts
        WHERE sync_run_id = NEW.sync_run_id
          AND account_id = NEW.account_id
          AND observed_at = NEW.observed_at
    ) THEN RAISE(ABORT, 'portfolio snapshot position set is closed') END;
END;

CREATE TRIGGER portfolio_position_snapshot_no_update
BEFORE UPDATE ON positions
WHEN OLD.sync_run_id IS NOT NULL OR NEW.sync_run_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'portfolio snapshot positions are immutable');
END;

CREATE TRIGGER portfolio_position_snapshot_no_delete
BEFORE DELETE ON positions
WHEN OLD.sync_run_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'portfolio snapshot positions are immutable');
END;

CREATE TABLE portfolio_observation_snapshots (
    sync_run_id INTEGER PRIMARY KEY
        REFERENCES sync_runs(id) ON DELETE RESTRICT,
    observed_at TEXT NOT NULL CHECK(
        typeof(observed_at) = 'text'
        AND julianday(observed_at) IS NOT NULL
        AND substr(observed_at, -6) = '+00:00'
        AND substr(observed_at, 11, 1) = 'T'
        AND strftime('%Y-%m-%dT%H:%M:%S', observed_at) = substr(observed_at, 1, 19)
        AND (
            length(observed_at) = 25 OR (
                length(observed_at) = 32
                AND substr(observed_at, 20, 1) = '.'
                AND substr(observed_at, 21, 6) NOT GLOB '*[^0-9]*'
                AND substr(observed_at, 21, 6) != '000000'
            )
        )
    ),
    account_count INTEGER NOT NULL CHECK(
        typeof(account_count) = 'integer' AND account_count >= 0
    ),
    position_count INTEGER NOT NULL CHECK(
        typeof(position_count) = 'integer' AND position_count >= 0
    )
);

CREATE TRIGGER portfolio_observation_snapshot_insert_guard
BEFORE INSERT ON portfolio_observation_snapshots
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM sync_runs
        WHERE id = NEW.sync_run_id
          AND source = 'yangjibao'
          AND status = 'running'
    ) THEN RAISE(ABORT, 'portfolio snapshot requires a running yangjibao sync') END;
    SELECT CASE WHEN (
        SELECT count(*) FROM positions WHERE sync_run_id = NEW.sync_run_id
    ) != NEW.position_count
    THEN RAISE(ABORT, 'portfolio snapshot position count mismatch') END;
    SELECT CASE WHEN (
        SELECT count(*) FROM portfolio_observation_accounts
        WHERE sync_run_id = NEW.sync_run_id
    ) != NEW.account_count
    THEN RAISE(ABORT, 'portfolio snapshot account count mismatch') END;
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM portfolio_observation_accounts
        WHERE sync_run_id = NEW.sync_run_id
          AND observed_at > NEW.observed_at
    ) THEN RAISE(ABORT, 'portfolio account observation follows snapshot') END;
END;

CREATE TRIGGER portfolio_observation_account_no_update
BEFORE UPDATE ON portfolio_observation_accounts
BEGIN
    SELECT RAISE(ABORT, 'portfolio observation accounts are immutable');
END;

CREATE TRIGGER portfolio_observation_account_no_delete
BEFORE DELETE ON portfolio_observation_accounts
BEGIN
    SELECT RAISE(ABORT, 'portfolio observation accounts are immutable');
END;

CREATE TRIGGER portfolio_observation_snapshot_no_update
BEFORE UPDATE ON portfolio_observation_snapshots
BEGIN
    SELECT RAISE(ABORT, 'portfolio observation snapshots are immutable');
END;

CREATE TRIGGER portfolio_observation_snapshot_no_delete
BEFORE DELETE ON portfolio_observation_snapshots
BEGIN
    SELECT RAISE(ABORT, 'portfolio observation snapshots are immutable');
END;
"""

SCHEMA_V19 = """
CREATE TABLE intelligence_policy_versions (
    version TEXT PRIMARY KEY CHECK(typeof(version) = 'text' AND length(version) BETWEEN 1 AND 64),
    canonical_policy_json TEXT NOT NULL CHECK(json_valid(canonical_policy_json)),
    policy_checksum TEXT NOT NULL CHECK(
        typeof(policy_checksum) = 'text'
        AND length(CAST(policy_checksum AS BLOB)) = 64
        AND policy_checksum NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL CHECK(julianday(created_at) IS NOT NULL)
);

CREATE TABLE market_entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_key TEXT NOT NULL UNIQUE,
    entity_type TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    active_from TEXT NOT NULL CHECK(julianday(active_from) IS NOT NULL),
    active_until TEXT CHECK(active_until IS NULL OR julianday(active_until) IS NOT NULL),
    evidence_ids_json TEXT NOT NULL CHECK(json_valid(evidence_ids_json)),
    canonical_entity_json TEXT NOT NULL CHECK(json_valid(canonical_entity_json)),
    entity_checksum TEXT NOT NULL CHECK(length(entity_checksum) = 64)
);

CREATE TABLE entity_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id INTEGER NOT NULL REFERENCES market_entities(id) ON DELETE RESTRICT,
    alias TEXT NOT NULL,
    alias_type TEXT NOT NULL,
    active_from TEXT NOT NULL CHECK(julianday(active_from) IS NOT NULL),
    active_until TEXT CHECK(active_until IS NULL OR julianday(active_until) IS NOT NULL),
    evidence_ids_json TEXT NOT NULL CHECK(json_valid(evidence_ids_json)),
    canonical_alias_json TEXT NOT NULL CHECK(json_valid(canonical_alias_json)),
    alias_checksum TEXT NOT NULL CHECK(length(alias_checksum) = 64),
    UNIQUE(entity_id, alias, alias_type, active_from)
);

CREATE TABLE intelligence_news_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_key TEXT NOT NULL UNIQUE,
    source_id TEXT NOT NULL,
    publisher TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    title TEXT NOT NULL,
    excerpt_original_bytes INTEGER NOT NULL CHECK(excerpt_original_bytes BETWEEN 1 AND 5242880),
    excerpt_sha256 TEXT NOT NULL CHECK(length(excerpt_sha256) = 64),
    published_at TEXT NOT NULL CHECK(julianday(published_at) IS NOT NULL),
    publication_precision TEXT NOT NULL CHECK(publication_precision IN ('date', 'minute')),
    publication_interval_end TEXT CHECK(
        publication_interval_end IS NULL OR julianday(publication_interval_end) IS NOT NULL
    ),
    retrieved_at TEXT NOT NULL CHECK(julianday(retrieved_at) IS NOT NULL),
    source_tier TEXT NOT NULL CHECK(source_tier IN ('tier_1', 'tier_2')),
    content_fingerprint TEXT NOT NULL CHECK(length(content_fingerprint) = 64),
    category TEXT NOT NULL,
    integrity_state TEXT NOT NULL CHECK(
        integrity_state IN ('active', 'corrected', 'retracted', 'superseded', 'unknown')
    ),
    source_attempt_id INTEGER NOT NULL
        REFERENCES source_attempts(id) ON DELETE RESTRICT
);

CREATE INDEX intelligence_news_items_attempt
ON intelligence_news_items(source_attempt_id, retrieved_at, id);

CREATE TABLE intelligence_news_excerpts (
    item_id INTEGER PRIMARY KEY REFERENCES intelligence_news_items(id) ON DELETE RESTRICT,
    excerpt_text TEXT NOT NULL CHECK(length(CAST(excerpt_text AS BLOB)) BETWEEN 1 AND 2048),
    truncated INTEGER NOT NULL CHECK(typeof(truncated) = 'integer' AND truncated IN (0, 1)),
    expires_at TEXT NOT NULL CHECK(julianday(expires_at) IS NOT NULL)
);

CREATE TABLE intelligence_snapshot_item_uses (
    request_run_id INTEGER NOT NULL
        REFERENCES request_runs(id) ON DELETE RESTRICT,
    item_id INTEGER NOT NULL
        REFERENCES intelligence_news_items(id) ON DELETE RESTRICT,
    source_attempt_id INTEGER NOT NULL
        REFERENCES source_attempts(id) ON DELETE RESTRICT,
    PRIMARY KEY(request_run_id, item_id)
);

CREATE TABLE intelligence_item_integrity_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    integrity_event_key TEXT NOT NULL UNIQUE,
    request_run_id INTEGER NOT NULL
        REFERENCES request_runs(id) ON DELETE RESTRICT,
    item_id INTEGER NOT NULL REFERENCES intelligence_news_items(id) ON DELETE RESTRICT,
    previous_state TEXT NOT NULL CHECK(
        previous_state IN ('active', 'corrected', 'retracted', 'superseded', 'unknown')
    ),
    current_state TEXT NOT NULL CHECK(
        current_state IN ('active', 'corrected', 'retracted', 'superseded', 'unknown')
    ),
    evidence_item_id INTEGER REFERENCES intelligence_news_items(id) ON DELETE RESTRICT,
    occurred_at TEXT NOT NULL CHECK(julianday(occurred_at) IS NOT NULL),
    canonical_event_json TEXT NOT NULL CHECK(json_valid(canonical_event_json)),
    event_checksum TEXT NOT NULL CHECK(length(event_checksum) = 64)
);

CREATE TABLE intelligence_lineage_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    edge_key TEXT NOT NULL UNIQUE,
    from_item_id INTEGER NOT NULL REFERENCES intelligence_news_items(id) ON DELETE RESTRICT,
    to_item_id INTEGER NOT NULL REFERENCES intelligence_news_items(id) ON DELETE RESTRICT,
    kind TEXT NOT NULL CHECK(kind IN (
        'original', 'direct_quote', 'reprint', 'independently_reported',
        'correction_of', 'retraction_of', 'clarification_of', 'unknown'
    )),
    evidence_ids_json TEXT NOT NULL CHECK(json_valid(evidence_ids_json)),
    canonical_edge_json TEXT NOT NULL CHECK(json_valid(canonical_edge_json)),
    edge_checksum TEXT NOT NULL CHECK(length(edge_checksum) = 64),
    CHECK(from_item_id != to_item_id)
);

CREATE TABLE intelligence_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL CHECK(event_type IN (
        'policy', 'fund_official', 'fund_media', 'market', 'sector'
    )),
    normalized_title TEXT NOT NULL,
    confidence_state TEXT NOT NULL CHECK(
        confidence_state IN ('sufficient', 'partial', 'conflicted', 'insufficient')
    ),
    earliest_published_at TEXT NOT NULL CHECK(julianday(earliest_published_at) IS NOT NULL),
    latest_published_at TEXT NOT NULL CHECK(julianday(latest_published_at) IS NOT NULL),
    integrity_state TEXT NOT NULL CHECK(
        integrity_state IN ('active', 'corrected', 'retracted', 'superseded', 'unknown')
    ),
    superseded_by_event_key TEXT
        REFERENCES intelligence_events(event_key) ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED,
    invalidation_conditions_json TEXT NOT NULL CHECK(json_valid(invalidation_conditions_json)),
    canonical_event_json TEXT NOT NULL CHECK(json_valid(canonical_event_json)),
    event_checksum TEXT NOT NULL CHECK(length(event_checksum) = 64),
    CHECK(superseded_by_event_key IS NULL OR superseded_by_event_key != event_key)
);

CREATE TABLE intelligence_event_items (
    event_id INTEGER NOT NULL REFERENCES intelligence_events(id) ON DELETE RESTRICT,
    item_id INTEGER NOT NULL REFERENCES intelligence_news_items(id) ON DELETE RESTRICT,
    role TEXT NOT NULL CHECK(role IN ('supporting', 'opposing', 'correction', 'retraction')),
    PRIMARY KEY(event_id, item_id),
    UNIQUE(event_id, item_id, role)
);

CREATE TABLE intelligence_event_entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    link_key TEXT NOT NULL UNIQUE,
    event_id INTEGER NOT NULL REFERENCES intelligence_events(id) ON DELETE RESTRICT,
    entity_id INTEGER NOT NULL REFERENCES market_entities(id) ON DELETE RESTRICT,
    relationship TEXT NOT NULL CHECK(relationship IN (
        'subject', 'affects', 'policy_catalyst',
        'fund_holding_exposure', 'fund_benchmark_exposure'
    )),
    evidence_ids_json TEXT NOT NULL CHECK(json_valid(evidence_ids_json)),
    canonical_link_json TEXT NOT NULL CHECK(json_valid(canonical_link_json)),
    link_checksum TEXT NOT NULL CHECK(length(link_checksum) = 64)
);

CREATE TABLE market_dimension_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_key TEXT NOT NULL UNIQUE,
    entity_id INTEGER NOT NULL REFERENCES market_entities(id) ON DELETE RESTRICT,
    source_attempt_ids_json TEXT NOT NULL CHECK(
        json_valid(source_attempt_ids_json) AND json_type(source_attempt_ids_json) = 'array'
    ),
    canonical_observation_json TEXT NOT NULL CHECK(json_valid(canonical_observation_json)),
    observation_checksum TEXT NOT NULL CHECK(length(observation_checksum) = 64),
    data_as_of TEXT NOT NULL CHECK(julianday(data_as_of) IS NOT NULL),
    retrieved_at TEXT NOT NULL CHECK(julianday(retrieved_at) IS NOT NULL)
);

CREATE TABLE market_state_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_run_id INTEGER NOT NULL UNIQUE REFERENCES request_runs(id) ON DELETE RESTRICT,
    policy_version TEXT NOT NULL
        REFERENCES intelligence_policy_versions(version) ON DELETE RESTRICT,
    observation_ids_json TEXT NOT NULL CHECK(json_valid(observation_ids_json)),
    canonical_state_json TEXT NOT NULL CHECK(json_valid(canonical_state_json)),
    state_checksum TEXT NOT NULL CHECK(length(state_checksum) = 64),
    created_at TEXT NOT NULL CHECK(julianday(created_at) IS NOT NULL)
);

CREATE TABLE intelligence_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_run_id INTEGER NOT NULL UNIQUE REFERENCES request_runs(id) ON DELETE RESTRICT,
    market_state_snapshot_id INTEGER NOT NULL UNIQUE
        REFERENCES market_state_snapshots(id) ON DELETE RESTRICT,
    policy_version TEXT NOT NULL
        REFERENCES intelligence_policy_versions(version) ON DELETE RESTRICT,
    workflow TEXT NOT NULL CHECK(
        workflow IN ('news_recent', 'market_overview', 'fund_intelligence')
    ),
    canonical_snapshot_json TEXT NOT NULL CHECK(json_valid(canonical_snapshot_json)),
    result_checksum TEXT NOT NULL CHECK(length(result_checksum) = 64),
    created_at TEXT NOT NULL CHECK(julianday(created_at) IS NOT NULL)
);

CREATE TRIGGER intelligence_policy_no_replace BEFORE INSERT ON intelligence_policy_versions
WHEN EXISTS(SELECT 1 FROM intelligence_policy_versions WHERE version=NEW.version)
BEGIN SELECT RAISE(ABORT, 'intelligence policies cannot be replaced'); END;
CREATE TRIGGER intelligence_policy_no_update BEFORE UPDATE ON intelligence_policy_versions
BEGIN SELECT RAISE(ABORT, 'intelligence policies are immutable'); END;
CREATE TRIGGER intelligence_policy_no_delete BEFORE DELETE ON intelligence_policy_versions
BEGIN SELECT RAISE(ABORT, 'intelligence policies are immutable'); END;

CREATE TRIGGER market_entity_no_replace BEFORE INSERT ON market_entities
WHEN EXISTS(SELECT 1 FROM market_entities WHERE entity_key=NEW.entity_key)
BEGIN SELECT RAISE(ABORT, 'market entities cannot be replaced'); END;
CREATE TRIGGER market_entity_no_update BEFORE UPDATE ON market_entities
BEGIN SELECT RAISE(ABORT, 'market entities are immutable'); END;
CREATE TRIGGER market_entity_no_delete BEFORE DELETE ON market_entities
BEGIN SELECT RAISE(ABORT, 'market entities are immutable'); END;
CREATE TRIGGER entity_alias_no_update BEFORE UPDATE ON entity_aliases
BEGIN SELECT RAISE(ABORT, 'entity aliases are immutable'); END;
CREATE TRIGGER entity_alias_no_delete BEFORE DELETE ON entity_aliases
BEGIN SELECT RAISE(ABORT, 'entity aliases are immutable'); END;

CREATE TRIGGER intelligence_news_item_insert_guard BEFORE INSERT ON intelligence_news_items
WHEN NOT EXISTS (
    SELECT 1 FROM source_attempts
    WHERE id=NEW.source_attempt_id
      AND source_id=NEW.source_id
      AND outcome IN ('success', 'cache_hit')
      AND finished_at <= NEW.retrieved_at
)
BEGIN SELECT RAISE(ABORT, 'intelligence item source attempt binding failed'); END;
CREATE TRIGGER intelligence_news_item_no_replace BEFORE INSERT ON intelligence_news_items
WHEN EXISTS(SELECT 1 FROM intelligence_news_items WHERE item_key=NEW.item_key)
BEGIN SELECT RAISE(ABORT, 'intelligence news items cannot be replaced'); END;
CREATE TRIGGER intelligence_news_item_no_update BEFORE UPDATE ON intelligence_news_items
BEGIN SELECT RAISE(ABORT, 'intelligence news items are immutable'); END;
CREATE TRIGGER intelligence_news_item_no_delete BEFORE DELETE ON intelligence_news_items
BEGIN SELECT RAISE(ABORT, 'intelligence news items are immutable'); END;

CREATE TRIGGER intelligence_excerpt_insert_guard BEFORE INSERT ON intelligence_news_excerpts
WHEN NOT EXISTS (
    SELECT 1 FROM intelligence_news_items AS item
    WHERE item.id=NEW.item_id
      AND item.excerpt_sha256=lower(hex(sha256(NEW.excerpt_text)))
      AND ((NEW.truncated=1 AND item.excerpt_original_bytes > 2048)
           OR (NEW.truncated=0 AND item.excerpt_original_bytes=
               length(CAST(NEW.excerpt_text AS BLOB))))
)
BEGIN SELECT RAISE(ABORT, 'intelligence excerpt authentication failed'); END;
CREATE TRIGGER intelligence_excerpt_no_update BEFORE UPDATE ON intelligence_news_excerpts
BEGIN SELECT RAISE(ABORT, 'intelligence excerpts are immutable'); END;
CREATE TRIGGER intelligence_excerpt_delete_guard BEFORE DELETE ON intelligence_news_excerpts
WHEN julianday(OLD.expires_at) > julianday(kunjin_excerpt_expiry_cutoff())
BEGIN SELECT RAISE(ABORT, 'intelligence excerpt has not expired'); END;

CREATE TRIGGER intelligence_snapshot_item_use_insert_guard
BEFORE INSERT ON intelligence_snapshot_item_uses
WHEN NOT EXISTS(
    SELECT 1
    FROM intelligence_news_items AS item
    JOIN source_attempts AS attempt ON attempt.id=NEW.source_attempt_id
    JOIN request_runs AS run ON run.id=NEW.request_run_id
    WHERE item.id=NEW.item_id
      AND attempt.request_run_id=NEW.request_run_id
      AND attempt.source_id=item.source_id
      AND attempt.outcome IN ('success', 'cache_hit')
      AND run.status='running'
)
BEGIN SELECT RAISE(ABORT, 'intelligence item use binding failed'); END;
CREATE TRIGGER intelligence_snapshot_item_use_no_replace
BEFORE INSERT ON intelligence_snapshot_item_uses
WHEN EXISTS(
    SELECT 1 FROM intelligence_snapshot_item_uses
    WHERE request_run_id=NEW.request_run_id AND item_id=NEW.item_id
)
BEGIN SELECT RAISE(ABORT, 'intelligence item uses cannot be replaced'); END;
CREATE TRIGGER intelligence_snapshot_item_use_no_update
BEFORE UPDATE ON intelligence_snapshot_item_uses
BEGIN SELECT RAISE(ABORT, 'intelligence item uses are immutable'); END;
CREATE TRIGGER intelligence_snapshot_item_use_no_delete
BEFORE DELETE ON intelligence_snapshot_item_uses
BEGIN SELECT RAISE(ABORT, 'intelligence item uses are immutable'); END;

CREATE TRIGGER intelligence_integrity_event_no_replace
BEFORE INSERT ON intelligence_item_integrity_events
WHEN EXISTS(
    SELECT 1 FROM intelligence_item_integrity_events
    WHERE integrity_event_key=NEW.integrity_event_key
)
BEGIN SELECT RAISE(ABORT, 'intelligence integrity events cannot be replaced'); END;
CREATE TRIGGER intelligence_integrity_event_insert_guard
BEFORE INSERT ON intelligence_item_integrity_events
WHEN NEW.item_id=NEW.evidence_item_id
 OR NOT (
    (NEW.previous_state='active' AND NEW.current_state IN (
        'corrected', 'retracted', 'superseded'
    ))
    OR (NEW.previous_state='corrected' AND NEW.current_state IN (
        'retracted', 'superseded'
    ))
    OR (NEW.previous_state='unknown' AND NEW.current_state IN (
        'active', 'corrected', 'retracted', 'superseded'
    ))
 )
 OR NOT EXISTS(
    SELECT 1 FROM request_runs
    WHERE id=NEW.request_run_id AND status='running'
 )
 OR NOT EXISTS(
    SELECT 1
    FROM intelligence_news_items AS evidence
    JOIN source_attempts AS attempt ON attempt.id=evidence.source_attempt_id
    WHERE evidence.id=NEW.evidence_item_id
      AND attempt.request_run_id=NEW.request_run_id
      AND attempt.outcome IN ('success', 'cache_hit')
    UNION ALL
    SELECT 1
    FROM intelligence_snapshot_item_uses AS item_use
    JOIN source_attempts AS attempt ON attempt.id=item_use.source_attempt_id
    WHERE item_use.item_id=NEW.evidence_item_id
      AND item_use.request_run_id=NEW.request_run_id
      AND attempt.outcome IN ('success', 'cache_hit')
 )
 OR EXISTS(
    SELECT 1 FROM intelligence_item_integrity_events
    WHERE item_id=NEW.item_id AND occurred_at >= NEW.occurred_at
 )
BEGIN SELECT RAISE(ABORT, 'intelligence integrity transition binding failed'); END;
CREATE TRIGGER intelligence_integrity_event_no_update
BEFORE UPDATE ON intelligence_item_integrity_events
BEGIN SELECT RAISE(ABORT, 'intelligence integrity events are immutable'); END;
CREATE TRIGGER intelligence_integrity_event_no_delete
BEFORE DELETE ON intelligence_item_integrity_events
BEGIN SELECT RAISE(ABORT, 'intelligence integrity events are immutable'); END;

CREATE TRIGGER intelligence_lineage_no_replace BEFORE INSERT ON intelligence_lineage_edges
WHEN EXISTS(SELECT 1 FROM intelligence_lineage_edges WHERE edge_key=NEW.edge_key)
BEGIN SELECT RAISE(ABORT, 'intelligence lineage cannot be replaced'); END;
CREATE TRIGGER intelligence_lineage_no_update BEFORE UPDATE ON intelligence_lineage_edges
BEGIN SELECT RAISE(ABORT, 'intelligence lineage is immutable'); END;
CREATE TRIGGER intelligence_lineage_no_delete BEFORE DELETE ON intelligence_lineage_edges
BEGIN SELECT RAISE(ABORT, 'intelligence lineage is immutable'); END;

CREATE TRIGGER intelligence_event_no_replace BEFORE INSERT ON intelligence_events
WHEN EXISTS(SELECT 1 FROM intelligence_events WHERE event_key=NEW.event_key)
BEGIN SELECT RAISE(ABORT, 'intelligence events cannot be replaced'); END;
CREATE TRIGGER intelligence_event_no_update BEFORE UPDATE ON intelligence_events
BEGIN SELECT RAISE(ABORT, 'intelligence events are immutable'); END;
CREATE TRIGGER intelligence_event_no_delete BEFORE DELETE ON intelligence_events
BEGIN SELECT RAISE(ABORT, 'intelligence events are immutable'); END;
CREATE TRIGGER intelligence_event_item_no_update BEFORE UPDATE ON intelligence_event_items
BEGIN SELECT RAISE(ABORT, 'intelligence event items are immutable'); END;
CREATE TRIGGER intelligence_event_item_no_delete BEFORE DELETE ON intelligence_event_items
BEGIN SELECT RAISE(ABORT, 'intelligence event items are immutable'); END;
CREATE TRIGGER intelligence_event_entity_no_update BEFORE UPDATE ON intelligence_event_entities
BEGIN SELECT RAISE(ABORT, 'intelligence event entities are immutable'); END;
CREATE TRIGGER intelligence_event_entity_no_delete BEFORE DELETE ON intelligence_event_entities
BEGIN SELECT RAISE(ABORT, 'intelligence event entities are immutable'); END;

CREATE TRIGGER market_dimension_observation_insert_guard
BEFORE INSERT ON market_dimension_observations
WHEN EXISTS (
    SELECT 1 FROM json_each(NEW.source_attempt_ids_json) AS attempt_id
    LEFT JOIN source_attempts AS attempt ON attempt.id=attempt_id.value
    WHERE attempt.id IS NULL OR attempt.outcome NOT IN ('success', 'cache_hit')
)
BEGIN SELECT RAISE(ABORT, 'market observation source attempt binding failed'); END;
CREATE TRIGGER market_dimension_observation_no_replace
BEFORE INSERT ON market_dimension_observations
WHEN EXISTS(SELECT 1 FROM market_dimension_observations WHERE observation_key=NEW.observation_key)
BEGIN SELECT RAISE(ABORT, 'market observations cannot be replaced'); END;
CREATE TRIGGER market_dimension_observation_no_update BEFORE UPDATE ON market_dimension_observations
BEGIN SELECT RAISE(ABORT, 'market observations are immutable'); END;
CREATE TRIGGER market_dimension_observation_no_delete BEFORE DELETE ON market_dimension_observations
BEGIN SELECT RAISE(ABORT, 'market observations are immutable'); END;

CREATE TRIGGER market_state_snapshot_insert_guard BEFORE INSERT ON market_state_snapshots
WHEN NOT EXISTS(SELECT 1 FROM request_runs WHERE id=NEW.request_run_id AND status='running')
BEGIN SELECT RAISE(ABORT, 'market state requires a running request'); END;
CREATE TRIGGER market_state_snapshot_no_update BEFORE UPDATE ON market_state_snapshots
BEGIN SELECT RAISE(ABORT, 'market state snapshots are immutable'); END;
CREATE TRIGGER market_state_snapshot_no_delete BEFORE DELETE ON market_state_snapshots
BEGIN SELECT RAISE(ABORT, 'market state snapshots are immutable'); END;

CREATE TRIGGER intelligence_snapshot_insert_guard BEFORE INSERT ON intelligence_snapshots
WHEN NOT EXISTS(SELECT 1 FROM request_runs WHERE id=NEW.request_run_id AND status='running')
 OR NOT EXISTS(
    SELECT 1 FROM market_state_snapshots
    WHERE id=NEW.market_state_snapshot_id AND request_run_id=NEW.request_run_id
 )
BEGIN SELECT RAISE(ABORT, 'intelligence snapshot request binding failed'); END;
CREATE TRIGGER intelligence_snapshot_no_replace BEFORE INSERT ON intelligence_snapshots
WHEN EXISTS(SELECT 1 FROM intelligence_snapshots WHERE request_run_id=NEW.request_run_id)
BEGIN SELECT RAISE(ABORT, 'intelligence snapshots cannot be replaced'); END;
CREATE TRIGGER intelligence_snapshot_no_update BEFORE UPDATE ON intelligence_snapshots
BEGIN SELECT RAISE(ABORT, 'intelligence snapshots are immutable'); END;
CREATE TRIGGER intelligence_snapshot_no_delete BEFORE DELETE ON intelligence_snapshots
BEGIN SELECT RAISE(ABORT, 'intelligence snapshots are immutable'); END;
"""

SCHEMA_V20 = """
DROP TRIGGER intelligence_news_item_insert_guard;
CREATE TRIGGER intelligence_news_item_insert_guard BEFORE INSERT ON intelligence_news_items
WHEN NOT EXISTS (
    SELECT 1 FROM source_attempts
    WHERE id=NEW.source_attempt_id
      AND source_id=NEW.source_id
      AND outcome IN ('success', 'cache_hit')
      AND julianday(NEW.retrieved_at) >= julianday(started_at, '-1 second')
      AND julianday(NEW.retrieved_at) <= julianday(finished_at, '+1 second')
)
BEGIN SELECT RAISE(ABORT, 'intelligence item source attempt binding failed'); END;
"""

SCHEMA_V21 = """
CREATE TABLE fund_official_announcement_contents (
    id INTEGER PRIMARY KEY AUTOINCREMENT CHECK(typeof(id)='integer' AND id>0),
    brief_request_run_id INTEGER NOT NULL REFERENCES request_runs(id) ON DELETE RESTRICT,
    source_attempt_id INTEGER NOT NULL REFERENCES source_attempts(id) ON DELETE RESTRICT,
    fund_code TEXT NOT NULL CHECK(
        typeof(fund_code)='text' AND length(CAST(fund_code AS BLOB))=6
        AND fund_code NOT GLOB '*[^0-9]*' AND fund_code<>'000000'
    ),
    listing_source_document_id INTEGER NOT NULL
        REFERENCES fund_source_documents(id) ON DELETE RESTRICT,
    canonical_announcement_url TEXT NOT NULL CHECK(
        typeof(canonical_announcement_url)='text'
        AND canonical_announcement_url GLOB 'https://*'
        AND instr(canonical_announcement_url, char(0))=0
    ),
    announcement_title TEXT NOT NULL CHECK(
        typeof(announcement_title)='text' AND length(announcement_title)>0
        AND instr(announcement_title, char(0))=0
    ),
    announcement_published_at TEXT NOT NULL CHECK(julianday(announcement_published_at) IS NOT NULL),
    publisher TEXT NOT NULL CHECK(
        typeof(publisher)='text' AND length(publisher)>0 AND instr(publisher, char(0))=0
    ),
    normalized_content TEXT NOT NULL CHECK(
        typeof(normalized_content)='text' AND instr(normalized_content, char(0))=0
    ),
    normalized_content_bytes INTEGER NOT NULL CHECK(
        typeof(normalized_content_bytes)='integer'
        AND normalized_content_bytes BETWEEN 1 AND 524288
    ),
    normalized_content_sha256 TEXT NOT NULL CHECK(
        typeof(normalized_content_sha256)='text'
        AND length(CAST(normalized_content_sha256 AS BLOB))=64
        AND normalized_content_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    original_source_id TEXT NOT NULL CHECK(
        original_source_id='fund_manager_official_documents'
    ),
    quoted_source_id TEXT CHECK(
        quoted_source_id IS NULL OR (
            typeof(quoted_source_id)='text' AND length(quoted_source_id) BETWEEN 1 AND 128
            AND quoted_source_id<>original_source_id
        )
    ),
    integrity_status TEXT NOT NULL CHECK(integrity_status IN ('active','corrected','retracted')),
    integrity_checked_at TEXT NOT NULL CHECK(julianday(integrity_checked_at) IS NOT NULL),
    retrieved_at TEXT NOT NULL CHECK(julianday(retrieved_at) IS NOT NULL),
    record_checksum TEXT NOT NULL CHECK(
        typeof(record_checksum)='text' AND length(CAST(record_checksum AS BLOB))=64
        AND record_checksum NOT GLOB '*[^0-9a-f]*'
    ),
    UNIQUE(listing_source_document_id, canonical_announcement_url,
           normalized_content_sha256, integrity_checked_at),
    CHECK(integrity_checked_at >= announcement_published_at),
    CHECK(retrieved_at >= announcement_published_at)
);

CREATE TABLE held_review_official_event_projections (
    id INTEGER PRIMARY KEY AUTOINCREMENT CHECK(typeof(id)='integer' AND id>0),
    brief_request_run_id INTEGER NOT NULL REFERENCES request_runs(id) ON DELETE RESTRICT,
    fund_code TEXT NOT NULL CHECK(
        typeof(fund_code)='text' AND length(CAST(fund_code AS BLOB))=6
        AND fund_code NOT GLOB '*[^0-9]*' AND fund_code<>'000000'
    ),
    announcement_row_id INTEGER NOT NULL REFERENCES fund_announcements(id) ON DELETE RESTRICT,
    announcement_content_id INTEGER NOT NULL
        REFERENCES fund_official_announcement_contents(id) ON DELETE RESTRICT,
    event_code TEXT NOT NULL CHECK(event_code IN (
        'fund_liquidation_notice','fund_termination_notice',
        'redemption_restriction_notice','manager_change_notice',
        'fee_change_notice','benchmark_change_notice'
    )),
    triggered_review_code TEXT NOT NULL CHECK(triggered_review_code IN (
        'full_exit_feasibility_review','redemption_restriction_review',
        'manager_change_review','fee_change_review','benchmark_change_review'
    )),
    policy_version TEXT NOT NULL CHECK(
        typeof(policy_version)='text' AND length(policy_version) BETWEEN 1 AND 64
    ),
    policy_checksum TEXT NOT NULL CHECK(
        typeof(policy_checksum)='text' AND length(CAST(policy_checksum AS BLOB))=64
        AND policy_checksum NOT GLOB '*[^0-9a-f]*'
    ),
    record_checksum TEXT NOT NULL CHECK(
        typeof(record_checksum)='text' AND length(CAST(record_checksum AS BLOB))=64
        AND record_checksum NOT GLOB '*[^0-9a-f]*'
    ),
    UNIQUE(brief_request_run_id, announcement_content_id, event_code),
    CHECK(
        (event_code IN ('fund_liquidation_notice','fund_termination_notice')
         AND triggered_review_code='full_exit_feasibility_review')
        OR (event_code='redemption_restriction_notice'
            AND triggered_review_code='redemption_restriction_review')
        OR (event_code='manager_change_notice'
            AND triggered_review_code='manager_change_review')
        OR (event_code='fee_change_notice' AND triggered_review_code='fee_change_review')
        OR (event_code='benchmark_change_notice'
            AND triggered_review_code='benchmark_change_review')
    )
);

CREATE TABLE thesis_match_projections (
    id INTEGER PRIMARY KEY AUTOINCREMENT CHECK(typeof(id)='integer' AND id>0),
    fund_code TEXT NOT NULL CHECK(
        typeof(fund_code)='text' AND length(CAST(fund_code AS BLOB))=6
        AND fund_code NOT GLOB '*[^0-9]*' AND fund_code<>'000000'
    ),
    thesis_id INTEGER REFERENCES investment_theses(id) ON DELETE RESTRICT,
    thesis_fingerprint TEXT CHECK(
        thesis_fingerprint IS NULL OR (
            typeof(thesis_fingerprint)='text'
            AND length(CAST(thesis_fingerprint AS BLOB))=64
            AND thesis_fingerprint NOT GLOB '*[^0-9a-f]*'
        )
    ),
    intelligence_request_run_id INTEGER NOT NULL REFERENCES request_runs(id) ON DELETE RESTRICT,
    intelligence_snapshot_id INTEGER NOT NULL
        REFERENCES intelligence_snapshots(id) ON DELETE RESTRICT,
    intelligence_snapshot_checksum TEXT NOT NULL CHECK(
        typeof(intelligence_snapshot_checksum)='text'
        AND length(CAST(intelligence_snapshot_checksum AS BLOB))=64
        AND intelligence_snapshot_checksum NOT GLOB '*[^0-9a-f]*'
    ),
    matcher_policy_version TEXT NOT NULL CHECK(
        typeof(matcher_policy_version)='text' AND length(matcher_policy_version) BETWEEN 1 AND 64
    ),
    matcher_policy_checksum TEXT NOT NULL CHECK(
        typeof(matcher_policy_checksum)='text'
        AND length(CAST(matcher_policy_checksum AS BLOB))=64
        AND matcher_policy_checksum NOT GLOB '*[^0-9a-f]*'
    ),
    projection_state TEXT NOT NULL CHECK(projection_state IN (
        'thesis_missing','no_matching_evidence','possible_invalidation_match'
    )),
    evidence_ids_json TEXT NOT NULL CHECK(
        typeof(evidence_ids_json)='text' AND json_valid(evidence_ids_json)
        AND json_type(evidence_ids_json)='array' AND evidence_ids_json=json(evidence_ids_json)
        AND json_array_length(evidence_ids_json)<=128
        AND length(CAST(evidence_ids_json AS BLOB))<=16384
    ),
    evidence_descriptors_json TEXT NOT NULL CHECK(
        typeof(evidence_descriptors_json)='text' AND json_valid(evidence_descriptors_json)
        AND json_type(evidence_descriptors_json)='array'
        AND evidence_descriptors_json=json(evidence_descriptors_json)
        AND json_array_length(evidence_descriptors_json)<=128
        AND length(CAST(evidence_descriptors_json AS BLOB))<=262144
    ),
    evidence_set_checksum TEXT NOT NULL CHECK(
        typeof(evidence_set_checksum)='text'
        AND length(CAST(evidence_set_checksum AS BLOB))=64
        AND evidence_set_checksum NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL CHECK(julianday(created_at) IS NOT NULL),
    record_checksum TEXT NOT NULL CHECK(
        typeof(record_checksum)='text' AND length(CAST(record_checksum AS BLOB))=64
        AND record_checksum NOT GLOB '*[^0-9a-f]*'
    ),
    UNIQUE(fund_code, thesis_fingerprint, intelligence_request_run_id,
           matcher_policy_checksum, evidence_set_checksum),
    CHECK((thesis_id IS NULL)=(thesis_fingerprint IS NULL)),
    CHECK(
        (projection_state='thesis_missing' AND thesis_id IS NULL
         AND json_array_length(evidence_ids_json)=0
         AND json_array_length(evidence_descriptors_json)=0)
        OR (projection_state='no_matching_evidence' AND thesis_id IS NOT NULL
            AND json_array_length(evidence_ids_json)=0
            AND json_array_length(evidence_descriptors_json)=0)
        OR (projection_state='possible_invalidation_match' AND thesis_id IS NOT NULL
            AND json_array_length(evidence_ids_json)>0
            AND json_array_length(evidence_ids_json)=json_array_length(evidence_descriptors_json))
    )
);

CREATE TABLE thesis_evidence_adjudications (
    id INTEGER PRIMARY KEY AUTOINCREMENT CHECK(typeof(id)='integer' AND id>0),
    fund_code TEXT NOT NULL CHECK(
        typeof(fund_code)='text' AND length(CAST(fund_code AS BLOB))=6
        AND fund_code NOT GLOB '*[^0-9]*' AND fund_code<>'000000'
    ),
    thesis_id INTEGER NOT NULL REFERENCES investment_theses(id) ON DELETE RESTRICT,
    thesis_fingerprint TEXT NOT NULL CHECK(
        typeof(thesis_fingerprint)='text' AND length(CAST(thesis_fingerprint AS BLOB))=64
        AND thesis_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    thesis_match_projection_id INTEGER NOT NULL
        REFERENCES thesis_match_projections(id) ON DELETE RESTRICT,
    thesis_match_projection_checksum TEXT NOT NULL CHECK(
        typeof(thesis_match_projection_checksum)='text'
        AND length(CAST(thesis_match_projection_checksum AS BLOB))=64
        AND thesis_match_projection_checksum NOT GLOB '*[^0-9a-f]*'
    ),
    intelligence_request_run_id INTEGER NOT NULL REFERENCES request_runs(id) ON DELETE RESTRICT,
    intelligence_snapshot_checksum TEXT NOT NULL CHECK(
        typeof(intelligence_snapshot_checksum)='text'
        AND length(CAST(intelligence_snapshot_checksum AS BLOB))=64
        AND intelligence_snapshot_checksum NOT GLOB '*[^0-9a-f]*'
    ),
    evidence_ids_json TEXT NOT NULL CHECK(
        typeof(evidence_ids_json)='text' AND json_valid(evidence_ids_json)
        AND json_type(evidence_ids_json)='array' AND evidence_ids_json=json(evidence_ids_json)
        AND json_array_length(evidence_ids_json) BETWEEN 1 AND 128
        AND length(CAST(evidence_ids_json AS BLOB))<=16384
    ),
    evidence_set_checksum TEXT NOT NULL CHECK(
        typeof(evidence_set_checksum)='text' AND length(CAST(evidence_set_checksum AS BLOB))=64
        AND evidence_set_checksum NOT GLOB '*[^0-9a-f]*'
    ),
    decision TEXT NOT NULL CHECK(decision IN (
        'presented_match_confirmed','presented_match_rejected','uncertain'
    )),
    superseded_adjudication_id INTEGER
        REFERENCES thesis_evidence_adjudications(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL CHECK(julianday(created_at) IS NOT NULL),
    record_checksum TEXT NOT NULL CHECK(
        typeof(record_checksum)='text' AND length(CAST(record_checksum AS BLOB))=64
        AND record_checksum NOT GLOB '*[^0-9a-f]*'
    )
);

CREATE UNIQUE INDEX thesis_evidence_adjudication_identity
ON thesis_evidence_adjudications(
    fund_code, thesis_id, thesis_fingerprint, thesis_match_projection_id,
    thesis_match_projection_checksum, intelligence_request_run_id,
    intelligence_snapshot_checksum, evidence_set_checksum, decision,
    COALESCE(superseded_adjudication_id, 0)
);

CREATE TABLE holding_review_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT CHECK(typeof(id)='integer' AND id>0),
    fund_code TEXT NOT NULL CHECK(
        typeof(fund_code)='text' AND length(CAST(fund_code AS BLOB))=6
        AND fund_code NOT GLOB '*[^0-9]*' AND fund_code<>'000000'
    ),
    action TEXT NOT NULL CHECK(action IN ('continue_holding','reduce_to_cash','full_exit')),
    brief_request_run_id INTEGER NOT NULL REFERENCES request_runs(id) ON DELETE RESTRICT,
    brief_snapshot_id INTEGER NOT NULL REFERENCES fund_brief_snapshots(id) ON DELETE RESTRICT,
    brief_snapshot_checksum TEXT NOT NULL CHECK(
        typeof(brief_snapshot_checksum)='text'
        AND length(CAST(brief_snapshot_checksum AS BLOB))=64
        AND brief_snapshot_checksum NOT GLOB '*[^0-9a-f]*'
    ),
    intelligence_request_run_id INTEGER NOT NULL REFERENCES request_runs(id) ON DELETE RESTRICT,
    intelligence_snapshot_id INTEGER NOT NULL
        REFERENCES intelligence_snapshots(id) ON DELETE RESTRICT,
    intelligence_snapshot_checksum TEXT NOT NULL CHECK(
        typeof(intelligence_snapshot_checksum)='text'
        AND length(CAST(intelligence_snapshot_checksum AS BLOB))=64
        AND intelligence_snapshot_checksum NOT GLOB '*[^0-9a-f]*'
    ),
    thesis_match_projection_id INTEGER NOT NULL
        REFERENCES thesis_match_projections(id) ON DELETE RESTRICT,
    thesis_match_projection_checksum TEXT NOT NULL CHECK(
        typeof(thesis_match_projection_checksum)='text'
        AND length(CAST(thesis_match_projection_checksum AS BLOB))=64
        AND thesis_match_projection_checksum NOT GLOB '*[^0-9a-f]*'
    ),
    active_thesis_state TEXT NOT NULL CHECK(active_thesis_state IN ('present','missing')),
    active_thesis_id INTEGER REFERENCES investment_theses(id) ON DELETE RESTRICT,
    active_thesis_fingerprint TEXT CHECK(
        active_thesis_fingerprint IS NULL OR (
            typeof(active_thesis_fingerprint)='text'
            AND length(CAST(active_thesis_fingerprint AS BLOB))=64
            AND active_thesis_fingerprint NOT GLOB '*[^0-9a-f]*'
        )
    ),
    adjudication_state TEXT NOT NULL CHECK(adjudication_state IN ('present','missing')),
    adjudication_id INTEGER REFERENCES thesis_evidence_adjudications(id) ON DELETE RESTRICT,
    adjudication_checksum TEXT CHECK(
        adjudication_checksum IS NULL OR (
            typeof(adjudication_checksum)='text'
            AND length(CAST(adjudication_checksum AS BLOB))=64
            AND adjudication_checksum NOT GLOB '*[^0-9a-f]*'
        )
    ),
    previous_review_id INTEGER REFERENCES holding_review_snapshots(id) ON DELETE RESTRICT,
    result_json TEXT NOT NULL CHECK(
        typeof(result_json)='text' AND json_valid(result_json)
        AND json_type(result_json)='object' AND result_json=json(result_json)
        AND length(CAST(result_json AS BLOB))<=4194304
    ),
    result_fingerprint TEXT NOT NULL CHECK(
        typeof(result_fingerprint)='text' AND length(CAST(result_fingerprint AS BLOB))=64
        AND result_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    policy_version TEXT NOT NULL CHECK(
        typeof(policy_version)='text' AND length(policy_version) BETWEEN 1 AND 64
    ),
    policy_checksum TEXT NOT NULL CHECK(
        typeof(policy_checksum)='text' AND length(CAST(policy_checksum AS BLOB))=64
        AND policy_checksum NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL CHECK(julianday(created_at) IS NOT NULL),
    semantic_identity_checksum TEXT NOT NULL UNIQUE CHECK(
        typeof(semantic_identity_checksum)='text'
        AND length(CAST(semantic_identity_checksum AS BLOB))=64
        AND semantic_identity_checksum NOT GLOB '*[^0-9a-f]*'
    ),
    record_checksum TEXT NOT NULL CHECK(
        typeof(record_checksum)='text' AND length(CAST(record_checksum AS BLOB))=64
        AND record_checksum NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK(
        (active_thesis_state='present' AND active_thesis_id IS NOT NULL
         AND active_thesis_fingerprint IS NOT NULL)
        OR (active_thesis_state='missing' AND active_thesis_id IS NULL
            AND active_thesis_fingerprint IS NULL)
    ),
    CHECK(
        (adjudication_state='present' AND adjudication_id IS NOT NULL
         AND adjudication_checksum IS NOT NULL AND active_thesis_state='present')
        OR (adjudication_state='missing' AND adjudication_id IS NULL
            AND adjudication_checksum IS NULL)
    )
);

CREATE INDEX holding_review_snapshots_history
ON holding_review_snapshots(fund_code, action, created_at DESC, id DESC);

CREATE UNIQUE INDEX holding_review_snapshot_semantic_fields
ON holding_review_snapshots(
    fund_code, action,
    brief_request_run_id, brief_snapshot_id, brief_snapshot_checksum,
    intelligence_request_run_id, intelligence_snapshot_id,
    intelligence_snapshot_checksum,
    thesis_match_projection_id, thesis_match_projection_checksum,
    active_thesis_state, COALESCE(active_thesis_id, 0),
    COALESCE(active_thesis_fingerprint, ''),
    adjudication_state, COALESCE(adjudication_id, 0),
    COALESCE(adjudication_checksum, ''),
    json_extract(result_json, '$.remainder_intent'),
    json_extract(result_json, '$.exit_reason'),
    json_extract(result_json, '$.use_of_proceeds'),
    policy_checksum, result_fingerprint
);

CREATE TRIGGER fund_official_announcement_content_no_replace
BEFORE INSERT ON fund_official_announcement_contents
WHEN EXISTS(
    SELECT 1 FROM fund_official_announcement_contents
    WHERE id=NEW.id OR (
        listing_source_document_id=NEW.listing_source_document_id
        AND canonical_announcement_url=NEW.canonical_announcement_url
        AND normalized_content_sha256=NEW.normalized_content_sha256
        AND integrity_checked_at=NEW.integrity_checked_at
    )
)
BEGIN SELECT RAISE(ABORT, 'official announcement contents cannot be replaced'); END;

CREATE TRIGGER held_review_official_event_projection_no_replace
BEFORE INSERT ON held_review_official_event_projections
WHEN EXISTS(
    SELECT 1 FROM held_review_official_event_projections
    WHERE id=NEW.id OR (
        brief_request_run_id=NEW.brief_request_run_id
        AND announcement_content_id=NEW.announcement_content_id
        AND event_code=NEW.event_code
    )
)
BEGIN SELECT RAISE(ABORT, 'official event projections cannot be replaced'); END;

CREATE TRIGGER fund_official_announcement_content_insert_guard
BEFORE INSERT ON fund_official_announcement_contents
WHEN NOT EXISTS(
    SELECT 1
    FROM request_runs AS run
    JOIN source_attempts AS attempt ON attempt.id=NEW.source_attempt_id
    JOIN fund_source_documents AS document ON document.id=NEW.listing_source_document_id
    JOIN fund_announcements AS announcement
      ON announcement.source_document_id=document.id
    WHERE run.id=NEW.brief_request_run_id AND run.status='running'
      AND attempt.request_run_id=run.id
      AND attempt.subject_key='fund:' || NEW.fund_code
      AND attempt.source_id=NEW.original_source_id
      AND attempt.field_id='fund_manager_product_announcement'
      AND attempt.outcome IN ('success','cache_hit')
      AND document.fund_code=NEW.fund_code
      AND document.document_kind='announcement'
      AND document.source_tier=1
      AND document.source_name=attempt.source_id
      AND document.publisher=NEW.publisher
      AND announcement.fund_code=NEW.fund_code
      AND announcement.url=NEW.canonical_announcement_url
      AND announcement.title=NEW.announcement_title
      AND announcement.published_at=NEW.announcement_published_at
      AND announcement.publisher=NEW.publisher
      AND announcement.source_tier=1
 )
 OR NEW.normalized_content_bytes<>length(CAST(NEW.normalized_content AS BLOB))
 OR NEW.normalized_content_sha256<>lower(hex(sha256(NEW.normalized_content)))
BEGIN SELECT RAISE(ABORT, 'official announcement content binding failed'); END;

CREATE TRIGGER held_review_official_event_projection_insert_guard
BEFORE INSERT ON held_review_official_event_projections
WHEN NOT EXISTS(
    SELECT 1
    FROM request_runs AS run
    JOIN fund_announcements AS announcement ON announcement.id=NEW.announcement_row_id
    JOIN fund_official_announcement_contents AS content
      ON content.id=NEW.announcement_content_id
    WHERE run.id=NEW.brief_request_run_id AND run.status='running'
      AND announcement.fund_code=NEW.fund_code
      AND content.brief_request_run_id=run.id
      AND content.fund_code=NEW.fund_code
      AND content.listing_source_document_id=announcement.source_document_id
      AND content.canonical_announcement_url=announcement.url
      AND content.announcement_title=announcement.title
      AND content.announcement_published_at=announcement.published_at
      AND content.publisher=announcement.publisher
 )
BEGIN SELECT RAISE(ABORT, 'official event projection binding failed'); END;

CREATE TRIGGER thesis_match_projection_insert_guard
BEFORE INSERT ON thesis_match_projections
WHEN NOT EXISTS(
    SELECT 1
    FROM request_runs AS run
    JOIN intelligence_snapshots AS snapshot ON snapshot.id=NEW.intelligence_snapshot_id
    WHERE run.id=NEW.intelligence_request_run_id
      AND run.status IN ('complete','partial')
      AND snapshot.request_run_id=run.id
      AND snapshot.result_checksum=NEW.intelligence_snapshot_checksum
      AND snapshot.workflow='fund_intelligence'
      AND json_extract(snapshot.canonical_snapshot_json, '$.request_run_id')=run.id
      AND json_extract(snapshot.canonical_snapshot_json, '$.subject_fund_code')=NEW.fund_code
 )
 OR (
    NEW.thesis_id IS NOT NULL AND NOT EXISTS(
        SELECT 1 FROM investment_theses
        WHERE id=NEW.thesis_id AND fund_code=NEW.fund_code AND active=1
    )
 )
 OR EXISTS(
    SELECT 1 FROM json_each(NEW.evidence_ids_json) AS evidence
    WHERE evidence.type<>'text' OR length(evidence.value) NOT BETWEEN 1 AND 128
       OR json_extract(NEW.evidence_descriptors_json, '$[' || evidence.key || '].evidence_id')
          IS NOT evidence.value
 )
 OR EXISTS(
    SELECT value FROM json_each(NEW.evidence_ids_json)
    GROUP BY value HAVING count(*)>1
 )
 OR EXISTS(
    SELECT 1 FROM json_each(NEW.evidence_descriptors_json) AS descriptor
    WHERE descriptor.type IS NOT 'object'
       OR (SELECT count(*) FROM json_each(descriptor.value))<>9
       OR EXISTS(
          SELECT 1 FROM json_each(descriptor.value) AS field
          WHERE field.key NOT IN (
              'conflicted','current','direct_subject_binding','evidence_id',
              'graph_closed','lineage_kind','original_lineage','retracted','source_tier'
          )
       )
       OR json_type(descriptor.value, '$.evidence_id') IS NOT 'text'
       OR length(json_extract(descriptor.value, '$.evidence_id')) NOT BETWEEN 1 AND 128
       OR json_type(descriptor.value, '$.source_tier') IS NOT 'integer'
       OR json_extract(descriptor.value, '$.source_tier') NOT IN (1,2)
       OR json_type(descriptor.value, '$.lineage_kind') IS NOT 'text'
       OR json_extract(descriptor.value, '$.lineage_kind') NOT IN (
          'original','direct_quote','reprint','independently_reported',
          'correction_of','retraction_of','clarification_of','unknown'
       )
       OR json_type(descriptor.value, '$.current') IS NULL
       OR json_type(descriptor.value, '$.current') NOT IN ('true','false')
       OR json_type(descriptor.value, '$.graph_closed') IS NULL
       OR json_type(descriptor.value, '$.graph_closed') NOT IN ('true','false')
       OR json_type(descriptor.value, '$.original_lineage') IS NULL
       OR json_type(descriptor.value, '$.original_lineage') NOT IN ('true','false')
       OR json_type(descriptor.value, '$.retracted') IS NULL
       OR json_type(descriptor.value, '$.retracted') NOT IN ('true','false')
       OR json_type(descriptor.value, '$.conflicted') IS NULL
       OR json_type(descriptor.value, '$.conflicted') NOT IN ('true','false')
       OR json_type(descriptor.value, '$.direct_subject_binding') IS NULL
       OR json_type(descriptor.value, '$.direct_subject_binding') NOT IN ('true','false')
       OR json_extract(descriptor.value, '$.original_lineage') IS NOT
          (json_extract(descriptor.value, '$.lineage_kind')='original')
       OR descriptor.value IS NOT json_object(
          'conflicted',json(CASE json_type(descriptor.value, '$.conflicted')
              WHEN 'true' THEN 'true' ELSE 'false' END),
          'current',json(CASE json_type(descriptor.value, '$.current')
              WHEN 'true' THEN 'true' ELSE 'false' END),
          'direct_subject_binding',json(CASE
              json_type(descriptor.value, '$.direct_subject_binding')
              WHEN 'true' THEN 'true' ELSE 'false' END),
          'evidence_id',json_extract(descriptor.value, '$.evidence_id'),
          'graph_closed',json(CASE json_type(descriptor.value, '$.graph_closed')
              WHEN 'true' THEN 'true' ELSE 'false' END),
          'lineage_kind',json_extract(descriptor.value, '$.lineage_kind'),
          'original_lineage',json(CASE
              json_type(descriptor.value, '$.original_lineage')
              WHEN 'true' THEN 'true' ELSE 'false' END),
          'retracted',json(CASE json_type(descriptor.value, '$.retracted')
              WHEN 'true' THEN 'true' ELSE 'false' END),
          'source_tier',json_extract(descriptor.value, '$.source_tier')
       )
 )
 OR EXISTS(
    SELECT 1 FROM json_each(NEW.evidence_ids_json) AS evidence
    WHERE NOT EXISTS(
        SELECT 1
        FROM intelligence_snapshots AS snapshot
        JOIN intelligence_snapshot_item_uses AS item_use
          ON item_use.request_run_id=snapshot.request_run_id
        JOIN intelligence_news_items AS item ON item.id=item_use.item_id
        JOIN source_attempts AS attempt ON attempt.id=item_use.source_attempt_id
        WHERE snapshot.id=NEW.intelligence_snapshot_id
          AND snapshot.request_run_id=NEW.intelligence_request_run_id
          AND item.item_key=evidence.value
          AND json_extract(
              NEW.evidence_descriptors_json,
              '$[' || evidence.key || '].source_tier'
          )=CASE item.source_tier WHEN 'tier_1' THEN 1 WHEN 'tier_2' THEN 2 END
          AND json_extract(
              NEW.evidence_descriptors_json,
              '$[' || evidence.key || '].retracted'
          )=(item.integrity_state='retracted')
          AND attempt.request_run_id=NEW.intelligence_request_run_id
          AND attempt.source_id=item.source_id
          AND attempt.outcome IN ('success','cache_hit')
          AND EXISTS(
              SELECT 1 FROM json_each(snapshot.canonical_snapshot_json, '$.item_ids')
              WHERE type='text' AND value=item.item_key
          )
          AND EXISTS(
              SELECT 1
              FROM json_each(snapshot.canonical_snapshot_json, '$.source_attempt_ids')
              WHERE type='integer' AND value=item_use.source_attempt_id
          )
    )
 )
 OR NEW.evidence_set_checksum<>lower(hex(sha256(json_object(
      'evidence_descriptors',json(NEW.evidence_descriptors_json),
      'evidence_ids',json(NEW.evidence_ids_json)
  ))))
BEGIN SELECT RAISE(ABORT, 'thesis match projection binding failed'); END;

CREATE TRIGGER thesis_match_projection_no_replace
BEFORE INSERT ON thesis_match_projections
WHEN EXISTS(
    SELECT 1 FROM thesis_match_projections
    WHERE id=NEW.id OR (
        fund_code=NEW.fund_code
        AND thesis_fingerprint IS NEW.thesis_fingerprint
        AND intelligence_request_run_id=NEW.intelligence_request_run_id
        AND matcher_policy_checksum=NEW.matcher_policy_checksum
        AND evidence_set_checksum=NEW.evidence_set_checksum
    )
)
BEGIN SELECT RAISE(ABORT, 'thesis match projections cannot be replaced'); END;

CREATE TRIGGER thesis_evidence_adjudication_no_replace
BEFORE INSERT ON thesis_evidence_adjudications
WHEN EXISTS(
    SELECT 1 FROM thesis_evidence_adjudications
    WHERE id=NEW.id OR (
        fund_code=NEW.fund_code
        AND thesis_id=NEW.thesis_id
        AND thesis_fingerprint=NEW.thesis_fingerprint
        AND thesis_match_projection_id=NEW.thesis_match_projection_id
        AND thesis_match_projection_checksum=NEW.thesis_match_projection_checksum
        AND intelligence_request_run_id=NEW.intelligence_request_run_id
        AND intelligence_snapshot_checksum=NEW.intelligence_snapshot_checksum
        AND evidence_set_checksum=NEW.evidence_set_checksum
        AND decision=NEW.decision
        AND COALESCE(superseded_adjudication_id,0)
            =COALESCE(NEW.superseded_adjudication_id,0)
    )
)
BEGIN SELECT RAISE(ABORT, 'thesis evidence adjudications cannot be replaced'); END;

CREATE TRIGGER thesis_evidence_adjudication_insert_guard
BEFORE INSERT ON thesis_evidence_adjudications
WHEN NOT EXISTS(
    SELECT 1
    FROM thesis_match_projections AS projection
    JOIN investment_theses AS thesis ON thesis.id=NEW.thesis_id
    JOIN intelligence_snapshots AS snapshot
      ON snapshot.id=projection.intelligence_snapshot_id
    WHERE projection.id=NEW.thesis_match_projection_id
      AND projection.record_checksum=NEW.thesis_match_projection_checksum
      AND projection.projection_state='possible_invalidation_match'
      AND projection.fund_code=NEW.fund_code
      AND projection.thesis_id=NEW.thesis_id
      AND projection.thesis_fingerprint=NEW.thesis_fingerprint
      AND projection.intelligence_request_run_id=NEW.intelligence_request_run_id
      AND projection.intelligence_snapshot_checksum=NEW.intelligence_snapshot_checksum
      AND projection.evidence_ids_json=NEW.evidence_ids_json
      AND thesis.fund_code=NEW.fund_code AND thesis.active=1
      AND snapshot.request_run_id=NEW.intelligence_request_run_id
      AND snapshot.result_checksum=NEW.intelligence_snapshot_checksum
 )
 OR NEW.evidence_set_checksum<>lower(hex(sha256(NEW.evidence_ids_json)))
 OR EXISTS(
    SELECT 1 FROM json_each(NEW.evidence_ids_json)
    WHERE type<>'text' OR length(value) NOT BETWEEN 1 AND 128
 )
 OR EXISTS(
    SELECT value FROM json_each(NEW.evidence_ids_json)
    GROUP BY value HAVING count(*)>1
 )
 OR (
    NEW.superseded_adjudication_id IS NULL AND EXISTS(
        SELECT 1 FROM thesis_evidence_adjudications AS prior
        WHERE prior.fund_code=NEW.fund_code
          AND prior.thesis_id=NEW.thesis_id
          AND prior.thesis_fingerprint=NEW.thesis_fingerprint
          AND prior.thesis_match_projection_id=NEW.thesis_match_projection_id
          AND prior.thesis_match_projection_checksum=NEW.thesis_match_projection_checksum
          AND prior.intelligence_request_run_id=NEW.intelligence_request_run_id
          AND prior.intelligence_snapshot_checksum=NEW.intelligence_snapshot_checksum
          AND prior.evidence_ids_json=NEW.evidence_ids_json
          AND prior.evidence_set_checksum=NEW.evidence_set_checksum
    )
 )
 OR (
    NEW.superseded_adjudication_id IS NOT NULL AND NOT EXISTS(
        SELECT 1 FROM thesis_evidence_adjudications AS prior
        WHERE prior.id=NEW.superseded_adjudication_id
          AND prior.fund_code=NEW.fund_code
          AND prior.thesis_id=NEW.thesis_id
          AND prior.thesis_fingerprint=NEW.thesis_fingerprint
          AND prior.thesis_match_projection_id=NEW.thesis_match_projection_id
          AND prior.thesis_match_projection_checksum=NEW.thesis_match_projection_checksum
          AND prior.intelligence_request_run_id=NEW.intelligence_request_run_id
          AND prior.intelligence_snapshot_checksum=NEW.intelligence_snapshot_checksum
          AND prior.evidence_ids_json=NEW.evidence_ids_json
          AND prior.evidence_set_checksum=NEW.evidence_set_checksum
          AND prior.decision<>NEW.decision
          AND prior.created_at<NEW.created_at
          AND NOT EXISTS(
              SELECT 1 FROM thesis_evidence_adjudications AS child
              WHERE child.superseded_adjudication_id=prior.id
          )
    )
 )
BEGIN SELECT RAISE(ABORT, 'thesis evidence adjudication binding failed'); END;

CREATE TRIGGER holding_review_snapshot_no_replace
BEFORE INSERT ON holding_review_snapshots
WHEN EXISTS(
    SELECT 1 FROM holding_review_snapshots AS prior
    WHERE prior.id=NEW.id
       OR prior.semantic_identity_checksum=NEW.semantic_identity_checksum
       OR (
          prior.fund_code=NEW.fund_code AND prior.action=NEW.action
          AND prior.brief_request_run_id=NEW.brief_request_run_id
          AND prior.brief_snapshot_id=NEW.brief_snapshot_id
          AND prior.brief_snapshot_checksum=NEW.brief_snapshot_checksum
          AND prior.intelligence_request_run_id=NEW.intelligence_request_run_id
          AND prior.intelligence_snapshot_id=NEW.intelligence_snapshot_id
          AND prior.intelligence_snapshot_checksum=NEW.intelligence_snapshot_checksum
          AND prior.thesis_match_projection_id=NEW.thesis_match_projection_id
          AND prior.thesis_match_projection_checksum=NEW.thesis_match_projection_checksum
          AND prior.active_thesis_state=NEW.active_thesis_state
          AND prior.active_thesis_id IS NEW.active_thesis_id
          AND prior.active_thesis_fingerprint IS NEW.active_thesis_fingerprint
          AND prior.adjudication_state=NEW.adjudication_state
          AND prior.adjudication_id IS NEW.adjudication_id
          AND prior.adjudication_checksum IS NEW.adjudication_checksum
          AND json_extract(prior.result_json, '$.remainder_intent')
              IS json_extract(NEW.result_json, '$.remainder_intent')
          AND json_extract(prior.result_json, '$.exit_reason')
              IS json_extract(NEW.result_json, '$.exit_reason')
          AND json_extract(prior.result_json, '$.use_of_proceeds')
              IS json_extract(NEW.result_json, '$.use_of_proceeds')
          AND prior.policy_checksum=NEW.policy_checksum
          AND prior.result_fingerprint=NEW.result_fingerprint
       )
)
BEGIN SELECT RAISE(ABORT, 'holding review snapshots cannot be replaced'); END;

CREATE TRIGGER holding_review_snapshot_insert_guard
BEFORE INSERT ON holding_review_snapshots
WHEN NOT EXISTS(
    SELECT 1
    FROM request_runs AS brief_run
    JOIN fund_brief_snapshots AS brief ON brief.id=NEW.brief_snapshot_id
    JOIN request_runs AS intelligence_run ON intelligence_run.id=NEW.intelligence_request_run_id
    JOIN intelligence_snapshots AS intelligence
      ON intelligence.id=NEW.intelligence_snapshot_id
    JOIN thesis_match_projections AS projection
      ON projection.id=NEW.thesis_match_projection_id
    WHERE brief_run.id=NEW.brief_request_run_id
      AND brief_run.status IN ('complete','partial')
      AND brief.request_run_id=brief_run.id
      AND brief.result_checksum=NEW.brief_snapshot_checksum
      AND brief.fund_code=NEW.fund_code
      AND EXISTS(SELECT 1 FROM json_each(brief.action_ids_json) WHERE value=NEW.action)
      AND intelligence_run.status IN ('complete','partial')
      AND intelligence.request_run_id=intelligence_run.id
      AND intelligence.result_checksum=NEW.intelligence_snapshot_checksum
      AND intelligence.workflow='fund_intelligence'
      AND json_extract(intelligence.canonical_snapshot_json, '$.subject_fund_code')=NEW.fund_code
      AND projection.intelligence_request_run_id=intelligence_run.id
      AND projection.intelligence_snapshot_id=intelligence.id
      AND projection.intelligence_snapshot_checksum=NEW.intelligence_snapshot_checksum
      AND projection.record_checksum=NEW.thesis_match_projection_checksum
      AND projection.fund_code=NEW.fund_code
 )
 OR (
    NEW.active_thesis_state='present' AND NOT EXISTS(
        SELECT 1
        FROM investment_theses AS thesis
        JOIN thesis_match_projections AS projection
          ON projection.id=NEW.thesis_match_projection_id
        WHERE thesis.id=NEW.active_thesis_id
          AND thesis.fund_code=NEW.fund_code AND thesis.active=1
          AND projection.thesis_id=thesis.id
          AND projection.thesis_fingerprint=NEW.active_thesis_fingerprint
          AND projection.projection_state<>'thesis_missing'
    )
 )
 OR (
    NEW.active_thesis_state='missing' AND NOT EXISTS(
        SELECT 1 FROM thesis_match_projections
        WHERE id=NEW.thesis_match_projection_id AND projection_state='thesis_missing'
    )
 )
 OR (
    NEW.adjudication_state='present' AND NOT EXISTS(
        SELECT 1 FROM thesis_evidence_adjudications
        WHERE id=NEW.adjudication_id
          AND record_checksum=NEW.adjudication_checksum
          AND fund_code=NEW.fund_code
          AND thesis_id=NEW.active_thesis_id
          AND thesis_fingerprint=NEW.active_thesis_fingerprint
          AND thesis_match_projection_id=NEW.thesis_match_projection_id
          AND thesis_match_projection_checksum=NEW.thesis_match_projection_checksum
          AND intelligence_request_run_id=NEW.intelligence_request_run_id
          AND intelligence_snapshot_checksum=NEW.intelligence_snapshot_checksum
    )
 )
 OR (
    NEW.previous_review_id IS NOT NULL AND NOT EXISTS(
        SELECT 1 FROM holding_review_snapshots AS previous
        WHERE previous.id=NEW.previous_review_id
          AND previous.fund_code=NEW.fund_code AND previous.action=NEW.action
          AND previous.created_at<=NEW.created_at
    )
 )
 OR json_type(NEW.result_json, '$.fund_code') IS NOT 'text'
 OR json_extract(NEW.result_json, '$.fund_code') IS NOT NEW.fund_code
 OR json_type(NEW.result_json, '$.action') IS NOT 'text'
 OR json_extract(NEW.result_json, '$.action') IS NOT NEW.action
 OR json_type(NEW.result_json, '$.remainder_intent') IS NOT 'text'
 OR json_extract(NEW.result_json, '$.remainder_intent') NOT IN (
      'retain_some','no_minimum_intent','unknown'
 )
 OR json_type(NEW.result_json, '$.exit_reason') IS NOT 'text'
 OR json_extract(NEW.result_json, '$.exit_reason') NOT IN (
      'owner_believes_thesis_invalidated','goal_changed','cash_need',
      'risk_reduction','other','unknown'
 )
 OR json_type(NEW.result_json, '$.use_of_proceeds') IS NOT 'text'
 OR json_extract(NEW.result_json, '$.use_of_proceeds') NOT IN (
      'cash_reserve','known_goal','reallocation_review','other','unknown'
 )
 OR (NEW.action='continue_holding' AND (
      json_extract(NEW.result_json, '$.remainder_intent') IS NOT 'unknown'
      OR json_extract(NEW.result_json, '$.exit_reason') IS NOT 'unknown'
      OR json_extract(NEW.result_json, '$.use_of_proceeds') IS NOT 'unknown'
 ))
 OR (NEW.action='reduce_to_cash' AND (
      json_extract(NEW.result_json, '$.exit_reason') IS NOT 'unknown'
      OR json_extract(NEW.result_json, '$.use_of_proceeds') IS NOT 'unknown'
 ))
 OR (NEW.action='full_exit'
     AND json_extract(NEW.result_json, '$.remainder_intent') IS NOT 'unknown')
BEGIN SELECT RAISE(ABORT, 'holding review snapshot binding failed'); END;

CREATE TRIGGER holding_review_snapshot_private_key_guard
BEFORE INSERT ON holding_review_snapshots
WHEN EXISTS(
    SELECT 1
    FROM (
        SELECT lower(replace(replace(replace(replace(replace(
            key, '-', '_'), ' ', '_'), '.', '_'), '/', '_'), char(92), '_')) AS key
        FROM json_tree(NEW.result_json) WHERE key IS NOT NULL
    )
    WHERE instr('_' || key || '_', '_ciphertext_')>0
       OR instr('_' || key || '_', '_authorization_')>0
       OR instr('_' || key || '_', '_account_number_')>0
       OR instr('_' || key || '_', '_api_key_')>0
       OR instr('_' || key || '_', '_access_key_')>0
       OR instr('_' || key || '_', '_auth_key_')>0
       OR instr('_' || key || '_', '_balance_')>0
       OR instr('_' || key || '_', '_cookie_')>0
       OR instr('_' || key || '_', '_credential_')>0
       OR instr('_' || key || '_', '_debt_')>0
       OR instr('_' || key || '_', '_header_')>0
       OR instr('_' || key || '_', '_headers_')>0
       OR instr('_' || key || '_', '_income_')>0
       OR instr('_' || key || '_', '_nonce_')>0
       OR instr('_' || key || '_', '_note_')>0
       OR instr('_' || key || '_', '_notes_')>0
       OR instr('_' || key || '_', '_private_')>0
       OR instr('_' || key || '_', '_profile_')>0
       OR instr('_' || key || '_', '_profit_')>0
       OR instr('_' || key || '_', '_reserve_')>0
       OR instr('_' || key || '_', '_secret_')>0
       OR instr('_' || key || '_', '_session_')>0
       OR instr('_' || key || '_', '_shares_')>0
       OR instr('_' || key || '_', '_token_')>0
       OR instr('_' || key || '_', '_weight_')>0
       OR key IN ('assets','current_value','holding_value','market_value')
       OR instr(key, 'accountbalance')>0
       OR instr(key, 'authorizationheader')>0
       OR instr(key, 'currentvalue')>0
       OR instr(key, 'filepath')>0
       OR instr(key, 'holdingvalue')>0
       OR instr(key, 'marketvalue')>0
       OR instr(key, 'password')>0
       OR key IN ('memo','networth')
       OR (instr('_' || key || '_', '_amount_')>0 AND key<>'exact_amount_available')
       OR (instr('_' || key || '_', '_cost_')>0 AND key<>'transaction_cost_state')
       OR (instr('_' || key || '_', '_raw_')>0 AND instr('_' || key || '_', '_body_')>0)
       OR (instr('_' || key || '_', '_response_')>0 AND instr('_' || key || '_', '_body_')>0)
       OR (instr('_' || key || '_', '_local_')>0 AND instr('_' || key || '_', '_path_')>0)
       OR (instr('_' || key || '_', '_portfolio_')>0 AND instr('_' || key || '_', '_weight_')>0)
 )
BEGIN SELECT RAISE(ABORT, 'holding review snapshot contains a private key'); END;

CREATE TRIGGER fund_official_announcement_content_no_update
BEFORE UPDATE ON fund_official_announcement_contents
BEGIN SELECT RAISE(ABORT, 'official announcement contents are immutable'); END;
CREATE TRIGGER fund_official_announcement_content_no_delete
BEFORE DELETE ON fund_official_announcement_contents
BEGIN SELECT RAISE(ABORT, 'official announcement contents are immutable'); END;
CREATE TRIGGER held_review_official_event_projection_no_update
BEFORE UPDATE ON held_review_official_event_projections
BEGIN SELECT RAISE(ABORT, 'official event projections are immutable'); END;
CREATE TRIGGER held_review_official_event_projection_no_delete
BEFORE DELETE ON held_review_official_event_projections
BEGIN SELECT RAISE(ABORT, 'official event projections are immutable'); END;
CREATE TRIGGER thesis_match_projection_no_update
BEFORE UPDATE ON thesis_match_projections
BEGIN SELECT RAISE(ABORT, 'thesis match projections are immutable'); END;
CREATE TRIGGER thesis_match_projection_no_delete
BEFORE DELETE ON thesis_match_projections
BEGIN SELECT RAISE(ABORT, 'thesis match projections are immutable'); END;
CREATE TRIGGER thesis_evidence_adjudication_no_update
BEFORE UPDATE ON thesis_evidence_adjudications
BEGIN SELECT RAISE(ABORT, 'thesis evidence adjudications are immutable'); END;
CREATE TRIGGER thesis_evidence_adjudication_no_delete
BEFORE DELETE ON thesis_evidence_adjudications
BEGIN SELECT RAISE(ABORT, 'thesis evidence adjudications are immutable'); END;
CREATE TRIGGER holding_review_snapshot_no_update
BEFORE UPDATE ON holding_review_snapshots
BEGIN SELECT RAISE(ABORT, 'holding review snapshots are immutable'); END;
CREATE TRIGGER holding_review_snapshot_no_delete
BEFORE DELETE ON holding_review_snapshots
BEGIN SELECT RAISE(ABORT, 'holding review snapshots are immutable'); END;
"""

SCHEMA_V22 = """
CREATE TABLE held_review_official_check_closures (
    id INTEGER PRIMARY KEY AUTOINCREMENT CHECK(typeof(id)='integer' AND id>0),
    brief_request_run_id INTEGER NOT NULL CHECK(
        typeof(brief_request_run_id)='integer' AND brief_request_run_id>0
    ) REFERENCES request_runs(id) ON DELETE RESTRICT,
    fund_code TEXT NOT NULL CHECK(
        typeof(fund_code)='text' AND length(CAST(fund_code AS BLOB))=6
        AND fund_code NOT GLOB '*[^0-9]*' AND fund_code<>'000000'
    ),
    listing_source_attempt_id INTEGER NOT NULL CHECK(
        typeof(listing_source_attempt_id)='integer' AND listing_source_attempt_id>0
    ) REFERENCES source_attempts(id) ON DELETE RESTRICT,
    official_registry_version TEXT NOT NULL CHECK(official_registry_version='1'),
    official_registry_checksum TEXT NOT NULL CHECK(
        official_registry_checksum=
        '557cac191734fbdd214ff24dabfc5afa8e3c99c1ab8ac30f230a846684c3fc9e'
    ),
    source_registration_ids_json TEXT NOT NULL CHECK(
        typeof(source_registration_ids_json)='text'
        AND json_valid(source_registration_ids_json)
        AND json_type(source_registration_ids_json)='array'
        AND source_registration_ids_json=json(source_registration_ids_json)
        AND json_array_length(source_registration_ids_json)<=10
        AND length(CAST(source_registration_ids_json AS BLOB))<=4096
    ),
    manager_identity_state TEXT NOT NULL CHECK(
        manager_identity_state IN ('present','missing','stale','conflicted')
    ),
    manager_identity_row_id INTEGER REFERENCES fund_identities(id) ON DELETE RESTRICT,
    manager_identity_source_document_id INTEGER
        REFERENCES fund_source_documents(id) ON DELETE RESTRICT,
    manager_identity_source_document_checksum TEXT CHECK(
        manager_identity_source_document_checksum IS NULL OR (
            typeof(manager_identity_source_document_checksum)='text'
            AND length(CAST(manager_identity_source_document_checksum AS BLOB))=64
            AND manager_identity_source_document_checksum NOT GLOB '*[^0-9a-f]*'
        )
    ),
    manager_identity_normalized_name TEXT CHECK(
        manager_identity_normalized_name IS NULL OR (
            typeof(manager_identity_normalized_name)='text'
            AND length(manager_identity_normalized_name) BETWEEN 1 AND 256
            AND instr(manager_identity_normalized_name, char(0))=0
        )
    ),
    manager_identity_fingerprint TEXT CHECK(
        manager_identity_fingerprint IS NULL OR (
            typeof(manager_identity_fingerprint)='text'
            AND length(CAST(manager_identity_fingerprint AS BLOB))=64
            AND manager_identity_fingerprint NOT GLOB '*[^0-9a-f]*'
        )
    ),
    listing_page_evidence_json TEXT NOT NULL CHECK(
        typeof(listing_page_evidence_json)='text'
        AND json_valid(listing_page_evidence_json)
        AND json_type(listing_page_evidence_json)='array'
        AND listing_page_evidence_json=json(listing_page_evidence_json)
        AND json_array_length(listing_page_evidence_json)<=10
        AND length(CAST(listing_page_evidence_json AS BLOB))<=131072
    ),
    window_start TEXT NOT NULL CHECK(
        typeof(window_start)='text' AND julianday(window_start) IS NOT NULL
        AND substr(window_start,-6)='+00:00' AND substr(window_start,11,1)='T'
    ),
    window_end TEXT NOT NULL CHECK(
        typeof(window_end)='text' AND julianday(window_end) IS NOT NULL
        AND substr(window_end,-6)='+00:00' AND substr(window_end,11,1)='T'
        AND window_end COLLATE BINARY>window_start COLLATE BINARY
    ),
    listing_count INTEGER NOT NULL CHECK(
        typeof(listing_count)='integer' AND listing_count BETWEEN 0 AND 1000
    ),
    candidate_count INTEGER NOT NULL CHECK(
        typeof(candidate_count)='integer' AND candidate_count BETWEEN 0 AND 20
    ),
    authenticated_body_count INTEGER NOT NULL CHECK(
        typeof(authenticated_body_count)='integer'
        AND authenticated_body_count BETWEEN 0 AND 20
    ),
    projected_event_count INTEGER NOT NULL CHECK(
        typeof(projected_event_count)='integer' AND projected_event_count BETWEEN 0 AND 20
    ),
    listing_truncated INTEGER NOT NULL CHECK(listing_truncated IN (0,1)),
    candidate_cap_reached INTEGER NOT NULL CHECK(candidate_cap_reached IN (0,1)),
    body_cap_reached INTEGER NOT NULL CHECK(body_cap_reached IN (0,1)),
    gap_codes_json TEXT NOT NULL CHECK(
        typeof(gap_codes_json)='text' AND json_valid(gap_codes_json)
        AND json_type(gap_codes_json)='array' AND gap_codes_json=json(gap_codes_json)
        AND json_array_length(gap_codes_json)<=64
        AND length(CAST(gap_codes_json AS BLOB))<=8192
    ),
    official_negative_check_complete INTEGER NOT NULL CHECK(
        official_negative_check_complete IN (0,1)
    ),
    policy_version TEXT NOT NULL CHECK(policy_version='1'),
    policy_checksum TEXT NOT NULL CHECK(
        policy_checksum='a78f01681f5b45dcbc9a264cfbdb2ee9805c30c7dab8583903ee60a83956fc46'
    ),
    official_check_policy_version TEXT NOT NULL CHECK(official_check_policy_version='1'),
    official_check_policy_checksum TEXT NOT NULL CHECK(
        typeof(official_check_policy_checksum)='text'
        AND length(CAST(official_check_policy_checksum AS BLOB))=64
        AND official_check_policy_checksum NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL CHECK(
        typeof(created_at)='text' AND julianday(created_at) IS NOT NULL
        AND substr(created_at,-6)='+00:00' AND substr(created_at,11,1)='T'
        AND created_at COLLATE BINARY>=window_end COLLATE BINARY
    ),
    record_checksum TEXT NOT NULL CHECK(
        typeof(record_checksum)='text' AND length(CAST(record_checksum AS BLOB))=64
        AND record_checksum NOT GLOB '*[^0-9a-f]*'
    ),
    UNIQUE(brief_request_run_id, fund_code),
    CHECK(projected_event_count<=authenticated_body_count),
    CHECK(authenticated_body_count<=candidate_count),
    CHECK(candidate_count<=listing_count),
    CHECK(abs((julianday(window_end)-julianday(window_start))-180.0)<0.00000001),
    CHECK(
        (manager_identity_state='present'
         AND manager_identity_row_id IS NOT NULL
         AND manager_identity_source_document_id IS NOT NULL
         AND manager_identity_source_document_checksum IS NOT NULL
         AND manager_identity_normalized_name IS NOT NULL
         AND manager_identity_fingerprint IS NOT NULL)
        OR (manager_identity_state<>'present'
            AND manager_identity_row_id IS NULL
            AND manager_identity_source_document_id IS NULL
            AND manager_identity_source_document_checksum IS NULL
            AND manager_identity_normalized_name IS NULL
            AND manager_identity_fingerprint IS NULL)
    ),
    CHECK(
        official_negative_check_complete=0 OR (
            manager_identity_state='present'
            AND source_registration_ids_json<>'[]'
            AND listing_page_evidence_json<>'[]'
            AND listing_truncated=0 AND candidate_cap_reached=0 AND body_cap_reached=0
            AND gap_codes_json='[]'
            AND projected_event_count=authenticated_body_count
            AND authenticated_body_count=candidate_count
        )
    )
);

CREATE TRIGGER held_review_official_check_closure_no_replace
BEFORE INSERT ON held_review_official_check_closures
WHEN EXISTS(
    SELECT 1 FROM held_review_official_check_closures
    WHERE id=NEW.id OR (
        brief_request_run_id=NEW.brief_request_run_id AND fund_code=NEW.fund_code
    )
)
BEGIN SELECT RAISE(ABORT, 'official check closures cannot be replaced'); END;

CREATE TRIGGER held_review_official_check_closure_insert_guard
BEFORE INSERT ON held_review_official_check_closures
WHEN NOT EXISTS(
    SELECT 1
    FROM request_runs AS run
    JOIN source_attempts AS attempt ON attempt.id=NEW.listing_source_attempt_id
    WHERE run.id=NEW.brief_request_run_id AND run.mode='deep' AND run.status='running'
      AND attempt.request_run_id=run.id
      AND attempt.subject_key='fund:' || NEW.fund_code
      AND attempt.source_id='fund_manager_official_documents'
      AND attempt.field_id='fund_manager_product_announcement'
      AND attempt.attempt_number=1
      AND attempt.force_actor IS NULL AND attempt.force_reason IS NULL
      AND attempt.authorization_id IS NULL
      AND attempt.registry_version='1'
      AND attempt.registry_checksum=
          'c876085a132026afab288a0a7022b7b29389fe36de4bcf9dba85a204c986953e'
      AND attempt.started_at COLLATE BINARY>=run.started_at COLLATE BINARY
      AND attempt.finished_at COLLATE BINARY<=run.deadline_at COLLATE BINARY
      AND NEW.created_at COLLATE BINARY<=run.deadline_at COLLATE BINARY
      AND attempt.response_byte_count=COALESCE((
          SELECT sum(CAST(json_extract(page.value,'$.raw_byte_count') AS INTEGER))
          FROM json_each(NEW.listing_page_evidence_json) AS page
      ),0)
      AND (
          NEW.official_negative_check_complete=0
          OR attempt.outcome IN ('success','cache_hit')
      )
      AND (SELECT count(*) FROM source_attempts AS exact_attempt
           WHERE exact_attempt.request_run_id=run.id
             AND exact_attempt.source_id=attempt.source_id
             AND exact_attempt.field_id=attempt.field_id
             AND exact_attempt.subject_key=attempt.subject_key)=1
)
OR NEW.official_check_policy_checksum<>
   '93722946c100518229531c79cabf606c23cf169536bd5c1d3213e3bf5836cb1b'
OR EXISTS(
    SELECT 1 FROM json_each(NEW.source_registration_ids_json)
    WHERE type<>'text' OR value<>'fund001'
)
OR EXISTS(
    SELECT 1 FROM json_each(NEW.source_registration_ids_json) AS current
    JOIN json_each(NEW.source_registration_ids_json) AS prior
      ON CAST(prior.key AS INTEGER)<CAST(current.key AS INTEGER)
    WHERE prior.value>=current.value
)
OR (
    NEW.manager_identity_state='present' AND NOT EXISTS(
        SELECT 1
        FROM fund_identities AS identity
        JOIN fund_source_documents AS document
          ON document.id=NEW.manager_identity_source_document_id
        JOIN fund_section_syncs AS sync
          ON sync.fund_code=NEW.fund_code AND sync.section='basic_profile'
        WHERE identity.id=NEW.manager_identity_row_id
          AND identity.fund_code=NEW.fund_code AND identity.status='active'
          AND identity.source_document_id=document.id
          AND identity.manager_name=NEW.manager_identity_normalized_name
          AND document.fund_code=NEW.fund_code AND document.document_kind='basic_profile'
          AND document.source_tier IN (1,2)
          AND document.checksum=NEW.manager_identity_source_document_checksum
          AND sync.state='success' AND sync.current_source_document_id=document.id
          AND julianday(document.retrieved_at)<=julianday(NEW.created_at)
          AND julianday(document.retrieved_at)>=julianday(NEW.created_at,'-30 days')
          AND (
              NEW.source_registration_ids_json='[]'
              OR NEW.manager_identity_normalized_name IN (
                  '交银施罗德基金管理有限公司','交银施罗德基金'
              )
          )
    )
)
OR EXISTS(
    SELECT 1 FROM json_each(NEW.listing_page_evidence_json) AS page
    WHERE page.type<>'object'
       OR (SELECT count(*) FROM json_each(page.value))<>11
       OR EXISTS(
          SELECT 1 FROM json_each(page.value) AS field
          WHERE field.key NOT IN (
              'registration_id','page_number','reported_total_pages',
              'canonical_page_url','raw_byte_count','raw_sha256','retrieved_at',
              'parsed_item_count','parsed_items_sha256','terminal_state',
              'source_document_id'
          )
       )
       OR json_type(page.value,'$.registration_id')<>'text'
       OR json_extract(page.value,'$.registration_id')<>'fund001'
       OR json_type(page.value,'$.page_number')<>'integer'
       OR json_extract(page.value,'$.page_number') NOT BETWEEN 1 AND 10
       OR json_type(page.value,'$.reported_total_pages')<>'integer'
       OR json_extract(page.value,'$.reported_total_pages')<
          json_extract(page.value,'$.page_number')
       OR json_type(page.value,'$.canonical_page_url')<>'text'
       OR json_extract(page.value,'$.canonical_page_url') NOT GLOB 'https://www.fund001.com/*'
       OR json_type(page.value,'$.raw_byte_count')<>'integer'
       OR json_extract(page.value,'$.raw_byte_count') NOT BETWEEN 1 AND 2097152
       OR json_type(page.value,'$.raw_sha256')<>'text'
       OR length(CAST(json_extract(page.value,'$.raw_sha256') AS BLOB))<>64
       OR json_extract(page.value,'$.raw_sha256') GLOB '*[^0-9a-f]*'
       OR json_type(page.value,'$.retrieved_at')<>'text'
       OR julianday(json_extract(page.value,'$.retrieved_at')) IS NULL
       OR substr(json_extract(page.value,'$.retrieved_at'),-6)<>'+00:00'
       OR json_type(page.value,'$.parsed_item_count')<>'integer'
       OR json_extract(page.value,'$.parsed_item_count') NOT BETWEEN 0 AND 1000
       OR json_type(page.value,'$.parsed_items_sha256')<>'text'
       OR length(CAST(json_extract(page.value,'$.parsed_items_sha256') AS BLOB))<>64
       OR json_extract(page.value,'$.parsed_items_sha256') GLOB '*[^0-9a-f]*'
       OR json_type(page.value,'$.terminal_state') NOT IN ('null','text')
       OR COALESCE(json_extract(page.value,'$.terminal_state'),'') NOT IN (
          '','source_final_page','window_boundary_reached'
       )
       OR json_type(page.value,'$.source_document_id')<>'integer'
       OR json_extract(page.value,'$.source_document_id')<=0
       OR page.value<>json_object(
          'canonical_page_url',json_extract(page.value,'$.canonical_page_url'),
          'page_number',json_extract(page.value,'$.page_number'),
          'parsed_item_count',json_extract(page.value,'$.parsed_item_count'),
          'parsed_items_sha256',json_extract(page.value,'$.parsed_items_sha256'),
          'raw_byte_count',json_extract(page.value,'$.raw_byte_count'),
          'raw_sha256',json_extract(page.value,'$.raw_sha256'),
          'registration_id',json_extract(page.value,'$.registration_id'),
          'reported_total_pages',json_extract(page.value,'$.reported_total_pages'),
          'retrieved_at',json_extract(page.value,'$.retrieved_at'),
          'source_document_id',json_extract(page.value,'$.source_document_id'),
          'terminal_state',json_extract(page.value,'$.terminal_state')
       )
)
OR EXISTS(
    SELECT 1 FROM json_each(NEW.listing_page_evidence_json) AS page
    WHERE NOT EXISTS(
        SELECT 1 FROM json_each(NEW.source_registration_ids_json) AS registration
        WHERE registration.value=json_extract(page.value,'$.registration_id')
    )
       OR NOT EXISTS(
          SELECT 1 FROM fund_source_documents AS document
          WHERE document.id=json_extract(page.value,'$.source_document_id')
            AND document.fund_code=NEW.fund_code
            AND document.document_kind='announcement' AND document.source_tier=1
            AND document.source_name='fund_manager_official_documents'
            AND document.url=json_extract(page.value,'$.canonical_page_url')
            AND document.checksum=json_extract(page.value,'$.raw_sha256')
            AND document.publisher IN (
                '交银施罗德基金管理有限公司','交银施罗德基金'
            )
            AND EXISTS(
                SELECT 1 FROM source_attempts AS capture
                WHERE capture.id=NEW.listing_source_attempt_id
                  AND julianday(json_extract(page.value,'$.retrieved_at'))
                      >=julianday(capture.started_at)
                  AND julianday(json_extract(page.value,'$.retrieved_at'))
                      <=julianday(capture.finished_at)
            )
       )
)
OR EXISTS(
    SELECT 1 FROM json_each(NEW.listing_page_evidence_json) AS current
    JOIN json_each(NEW.listing_page_evidence_json) AS prior
      ON CAST(prior.key AS INTEGER)<CAST(current.key AS INTEGER)
    WHERE json_extract(prior.value,'$.registration_id')>
          json_extract(current.value,'$.registration_id')
       OR (
          json_extract(prior.value,'$.registration_id')=
          json_extract(current.value,'$.registration_id')
          AND json_extract(prior.value,'$.page_number')>=
              json_extract(current.value,'$.page_number')
       )
       OR json_extract(prior.value,'$.canonical_page_url')=
          json_extract(current.value,'$.canonical_page_url')
       OR json_extract(prior.value,'$.raw_sha256')=
          json_extract(current.value,'$.raw_sha256')
       OR json_extract(prior.value,'$.source_document_id')=
          json_extract(current.value,'$.source_document_id')
)
OR (
    NEW.official_negative_check_complete=1 AND (
        EXISTS(
            SELECT 1 FROM json_each(NEW.source_registration_ids_json) AS registration
            WHERE NOT EXISTS(
                SELECT 1 FROM json_each(NEW.listing_page_evidence_json) AS first_page
                WHERE json_extract(first_page.value,'$.registration_id')=registration.value
                  AND json_extract(first_page.value,'$.page_number')=1
            )
               OR NOT EXISTS(
                SELECT 1 FROM json_each(NEW.listing_page_evidence_json) AS terminal
                WHERE json_extract(terminal.value,'$.registration_id')=registration.value
                  AND json_extract(terminal.value,'$.terminal_state') IS NOT NULL
                  AND json_extract(terminal.value,'$.page_number')=(
                      SELECT max(json_extract(candidate.value,'$.page_number'))
                      FROM json_each(NEW.listing_page_evidence_json) AS candidate
                      WHERE json_extract(candidate.value,'$.registration_id')=registration.value
                  )
            )
        )
        OR EXISTS(
            SELECT 1 FROM json_each(NEW.listing_page_evidence_json) AS page
            WHERE json_extract(page.value,'$.page_number')>1
              AND NOT EXISTS(
                  SELECT 1 FROM json_each(NEW.listing_page_evidence_json) AS previous
                  WHERE json_extract(previous.value,'$.registration_id')=
                        json_extract(page.value,'$.registration_id')
                    AND json_extract(previous.value,'$.page_number')=
                        json_extract(page.value,'$.page_number')-1
              )
        )
        OR EXISTS(
            SELECT 1 FROM json_each(NEW.listing_page_evidence_json) AS page
            WHERE json_extract(page.value,'$.terminal_state') IS NOT NULL
              AND json_extract(page.value,'$.page_number')<>(
                  SELECT max(json_extract(candidate.value,'$.page_number'))
                  FROM json_each(NEW.listing_page_evidence_json) AS candidate
                  WHERE json_extract(candidate.value,'$.registration_id')=
                        json_extract(page.value,'$.registration_id')
              )
        )
        OR EXISTS(
            SELECT 1 FROM json_each(NEW.listing_page_evidence_json) AS page
            WHERE json_extract(page.value,'$.terminal_state')='source_final_page'
              AND json_extract(page.value,'$.page_number')<>
                  json_extract(page.value,'$.reported_total_pages')
        )
        OR EXISTS(
            SELECT 1 FROM json_each(NEW.listing_page_evidence_json) AS page
            WHERE EXISTS(
                SELECT 1 FROM json_each(NEW.listing_page_evidence_json) AS other
                WHERE json_extract(other.value,'$.registration_id')=
                      json_extract(page.value,'$.registration_id')
                  AND json_extract(other.value,'$.reported_total_pages')<>
                      json_extract(page.value,'$.reported_total_pages')
            )
        )
        OR (SELECT count(*) FROM fund_official_announcement_contents
            WHERE brief_request_run_id=NEW.brief_request_run_id
              AND fund_code=NEW.fund_code AND integrity_status='active')
           <>NEW.authenticated_body_count
        OR (SELECT count(*) FROM held_review_official_event_projections
            WHERE brief_request_run_id=NEW.brief_request_run_id
              AND fund_code=NEW.fund_code)<>NEW.projected_event_count
    )
)
BEGIN SELECT RAISE(ABORT, 'official check closure binding failed'); END;

CREATE TRIGGER held_review_official_check_closure_no_update
BEFORE UPDATE ON held_review_official_check_closures
BEGIN SELECT RAISE(ABORT, 'official check closures are immutable'); END;

CREATE TRIGGER held_review_official_check_closure_no_delete
BEFORE DELETE ON held_review_official_check_closures
BEGIN SELECT RAISE(ABORT, 'official check closures are immutable'); END;
"""

SCHEMA_V23 = """
CREATE TABLE public_research_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain_id TEXT NOT NULL CHECK(length(domain_id) BETWEEN 1 AND 64),
    source_name TEXT NOT NULL CHECK(length(source_name) BETWEEN 1 AND 256),
    publisher TEXT NOT NULL CHECK(length(publisher) BETWEEN 1 AND 256),
    source_kind TEXT NOT NULL CHECK(source_kind IN (
        'official', 'platform_data', 'industry_data', 'media', 'community'
    )),
    source_tier TEXT NOT NULL CHECK(source_tier IN ('tier_1', 'tier_2')),
    title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 1000),
    original_url TEXT NOT NULL CHECK(original_url GLOB 'https://*'),
    published_at TEXT NOT NULL,
    statistics_period TEXT NOT NULL CHECK(length(statistics_period) BETWEEN 1 AND 64),
    indicator_name TEXT NOT NULL CHECK(length(indicator_name) BETWEEN 1 AND 256),
    indicator_value TEXT NOT NULL CHECK(length(indicator_value) BETWEEN 1 AND 256),
    unit TEXT NOT NULL CHECK(length(unit) BETWEEN 1 AND 128),
    methodology TEXT,
    short_excerpt TEXT CHECK(short_excerpt IS NULL OR length(short_excerpt) <= 1000),
    excerpt_sha256 TEXT NOT NULL CHECK(
        length(excerpt_sha256) = 64 AND excerpt_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    verification_state TEXT NOT NULL CHECK(verification_state = 'outer_page_verified'),
    revision_of_evidence_id INTEGER REFERENCES public_research_evidence(id) ON DELETE RESTRICT,
    retrieved_at TEXT NOT NULL,
    record_sha256 TEXT NOT NULL UNIQUE CHECK(
        length(record_sha256) = 64 AND record_sha256 NOT GLOB '*[^0-9a-f]*'
    )
);

CREATE INDEX public_research_evidence_timeline_idx
ON public_research_evidence(domain_id, indicator_name, unit, statistics_period);

CREATE INDEX public_research_evidence_source_idx
ON public_research_evidence(original_url, published_at);
"""

SCHEMA_V24 = """
CREATE TABLE public_research_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL CHECK(length(event_key) BETWEEN 16 AND 64),
    domain_id TEXT NOT NULL CHECK(length(domain_id) BETWEEN 1 AND 64),
    source_name TEXT NOT NULL CHECK(length(source_name) BETWEEN 1 AND 256),
    publisher TEXT NOT NULL CHECK(length(publisher) BETWEEN 1 AND 256),
    source_kind TEXT NOT NULL CHECK(source_kind IN (
        'official', 'platform_data', 'industry_data', 'media', 'community'
    )),
    source_tier TEXT NOT NULL CHECK(source_tier IN ('tier_1', 'tier_2', 'lead')),
    title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 1000),
    original_url TEXT NOT NULL CHECK(original_url GLOB 'https://*'),
    event_occurred_at TEXT,
    published_at TEXT NOT NULL,
    fact_summary TEXT NOT NULL CHECK(length(fact_summary) BETWEEN 1 AND 1000),
    claim_boundary TEXT NOT NULL CHECK(length(claim_boundary) BETWEEN 1 AND 1000),
    event_fact_key TEXT CHECK(event_fact_key IS NULL OR length(event_fact_key) <= 256),
    event_fact_value TEXT CHECK(event_fact_value IS NULL OR length(event_fact_value) <= 256),
    event_fact_unit TEXT CHECK(event_fact_unit IS NULL OR length(event_fact_unit) <= 128),
    short_excerpt TEXT CHECK(short_excerpt IS NULL OR length(short_excerpt) <= 1000),
    excerpt_sha256 TEXT NOT NULL CHECK(
        length(excerpt_sha256) = 64 AND excerpt_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    verification_state TEXT NOT NULL CHECK(verification_state = 'outer_page_verified'),
    retrieved_at TEXT NOT NULL,
    record_sha256 TEXT NOT NULL UNIQUE CHECK(
        length(record_sha256) = 64 AND record_sha256 NOT GLOB '*[^0-9a-f]*'
    )
);

CREATE INDEX public_research_events_cluster_idx
ON public_research_events(domain_id, event_key, published_at);
"""
