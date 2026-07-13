SCHEMA_VERSION = 9

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
