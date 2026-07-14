# KunJin Phase D1.1-A Safe Failure Taxonomy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add allowlisted document failure stages and reasons, explicitly identify unsupported legacy OLE Word containers, and expose safe diagnostics without changing existing public error codes or persistence schemas.

**Architecture:** A new exact immutable failure model carries only a public code, a stage enum, and a reason enum. Document and parser exceptions construct this safe record at the failure source; the service propagates the allowlisted values to failed `DocumentSyncItem` results while retaining the existing public `error_code`. No raw exception parsing, failure-event persistence, converter, Schema V12, or financial classification rule is added in D1.1-A.

**Tech Stack:** Python 3.9+, frozen dataclasses, string enums, existing urllib/PDF/DOCX adapters, unittest/pytest, Ruff, SQLite regression tests, existing JSON CLI envelope.

---

## Scope And File Map

Create:

- `src/kunjin/funds/risk/failures.py`: exact failure enums, immutable safe record, public-code validation, and unknown fallback constructor.
- `tests/unit/test_risk_failures.py`: model validation, hidden-state rejection, exact public-code set, and fallback tests.

Modify:

- `src/kunjin/funds/risk/__init__.py`: export the failure types.
- `src/kunjin/funds/risk/documents.py`: require source-assigned safe reasons for document errors and recognize OLE/CFB bytes as an explicitly unsupported container.
- `src/kunjin/funds/risk/parsers.py`: require source-assigned parser reasons while preserving public parser/resource codes.
- `src/kunjin/funds/risk/service.py`: attach safe fields to failed sync items, isolate the observer, and preserve existing public errors and database values.
- `tests/unit/test_risk_documents.py`: document-source reason mapping and OLE regression tests.
- `tests/unit/test_risk_parsers.py`: parser reason mapping and privacy regressions.
- `tests/unit/test_risk_service.py`: candidate/discovery propagation, observer isolation, unknown fallback, and failed-artifact compatibility.
- `tests/integration/test_cli.py`: additive JSON fields and public-message compatibility.
- `tests/test_smoke.py`: packaged D1.1-A contract and Skill wording.
- `README.md`: explain safe failure stage/reason semantics.
- `integrations/codex/kunjin-fund/SKILL.md`: require preserving the safe diagnostics without treating them as financial evidence.
- `/Users/yanzihao/.codex/skills/kunjin-fund/SKILL.md`: synchronize only after all repository verification passes and confirm byte identity.

Do not modify the database schema, Policy V1, product-family rules,
classification reason codes, allocation code, personal profile code, or the
D1.1-B converter design.

No Git commit is created by this plan. The owner will commit only after local
and live acceptance.

### Task 1: Add Exact Safe Failure Types

**Files:**

- Create: `src/kunjin/funds/risk/failures.py`
- Create: `tests/unit/test_risk_failures.py`
- Modify: `src/kunjin/funds/risk/__init__.py`

- [ ] **Step 1: Write failing exact-model tests**

Create `tests/unit/test_risk_failures.py` with tests equivalent to:

```python
import dataclasses
import unittest
from enum import Enum

from kunjin.funds.risk.failures import (
    DOCUMENT_FAILURE_PUBLIC_CODES,
    DocumentFailureReason,
    DocumentFailureStage,
    SafeDocumentFailure,
    unspecified_document_failure,
)


class DocumentFailureModelTest(unittest.TestCase):
    def test_enums_and_public_codes_are_fixed(self) -> None:
        self.assertTrue(issubclass(DocumentFailureStage, str))
        self.assertTrue(issubclass(DocumentFailureStage, Enum))
        self.assertEqual(
            {item.value for item in DocumentFailureStage},
            {
                "discovery",
                "landing_validation",
                "retrieval",
                "identity_validation",
                "container_validation",
                "conversion",
                "parser",
                "persistence",
                "unspecified",
            },
        )
        self.assertIn(
            DocumentFailureReason.LEGACY_OLE_CONTAINER_UNSUPPORTED,
            tuple(DocumentFailureReason),
        )
        self.assertEqual(
            {item.value for item in DocumentFailureReason},
            {
                "dns_unavailable",
                "network_unavailable",
                "http_unavailable",
                "source_unregistered",
                "redirect_rejected",
                "discovery_format_invalid",
                "identity_mismatch",
                "publication_date_missing",
                "landing_format_invalid",
                "landing_title_mismatch",
                "landing_date_mismatch",
                "attachment_missing",
                "attachment_ambiguous",
                "attachment_host_rejected",
                "authentication_shell",
                "empty_or_script_only_html",
                "declared_mime_unsupported",
                "detected_container_unknown",
                "declared_detected_mismatch",
                "legacy_ole_container_unsupported",
                "legacy_converter_unavailable",
                "legacy_converter_timeout",
                "legacy_converter_resource_limit",
                "legacy_converter_failed",
                "legacy_converter_output_invalid",
                "resource_limit",
                "parser_format_invalid",
                "parser_identity_mismatch",
                "parser_effective_date_invalid",
                "parser_ambiguous_fact",
                "clock_invalid",
                "managed_artifact_invalid",
                "storage_failure",
                "unspecified_failure",
            },
        )
        self.assertEqual(
            DOCUMENT_FAILURE_PUBLIC_CODES,
            frozenset(
                {
                    "official_document_unavailable",
                    "official_document_invalid",
                    "official_document_resource_limit",
                    "official_document_parse_failed",
                    "classification_storage_failed",
                }
            ),
        )

    def test_record_is_exact_frozen_and_allowlisted(self) -> None:
        failure = SafeDocumentFailure(
            public_code="official_document_invalid",
            stage=DocumentFailureStage.CONTAINER_VALIDATION,
            reason_code=DocumentFailureReason.LEGACY_OLE_CONTAINER_UNSUPPORTED,
        )
        failure.validate()
        self.assertEqual(
            {field.name for field in dataclasses.fields(SafeDocumentFailure)},
            {"public_code", "stage", "reason_code"},
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            failure.public_code = "official_document_unavailable"

    def test_subclasses_hidden_state_and_unknown_values_are_rejected(self) -> None:
        class HiddenFailure(SafeDocumentFailure):
            pass

        with self.assertRaises(ValueError):
            HiddenFailure(
                "official_document_invalid",
                DocumentFailureStage.UNSPECIFIED,
                DocumentFailureReason.UNSPECIFIED_FAILURE,
            ).validate()
        failure = unspecified_document_failure()
        object.__setattr__(failure, "private_detail", "sentinel")
        with self.assertRaises(ValueError):
            failure.validate()
        with self.assertRaises(ValueError):
            SafeDocumentFailure(
                "private_code",
                DocumentFailureStage.UNSPECIFIED,
                DocumentFailureReason.UNSPECIFIED_FAILURE,
            ).validate()

    def test_unknown_fallback_contains_no_free_form_detail(self) -> None:
        failure = unspecified_document_failure()
        self.assertEqual(failure.public_code, "official_document_invalid")
        self.assertEqual(failure.stage, DocumentFailureStage.UNSPECIFIED)
        self.assertEqual(
            failure.reason_code,
            DocumentFailureReason.UNSPECIFIED_FAILURE,
        )
        self.assertNotIn("exception", repr(failure).lower())
```

- [ ] **Step 2: Run the focused test and confirm the missing-module failure**

Run:

```bash
.venv/bin/python -m unittest tests.unit.test_risk_failures -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'kunjin.funds.risk.failures'`.

- [ ] **Step 3: Implement the exact model**

Create `failures.py` with:

```python
from __future__ import annotations

from dataclasses import dataclass, fields
from enum import Enum


class DocumentFailureStage(str, Enum):
    DISCOVERY = "discovery"
    LANDING_VALIDATION = "landing_validation"
    RETRIEVAL = "retrieval"
    IDENTITY_VALIDATION = "identity_validation"
    CONTAINER_VALIDATION = "container_validation"
    CONVERSION = "conversion"
    PARSER = "parser"
    PERSISTENCE = "persistence"
    UNSPECIFIED = "unspecified"


class DocumentFailureReason(str, Enum):
    DNS_UNAVAILABLE = "dns_unavailable"
    NETWORK_UNAVAILABLE = "network_unavailable"
    HTTP_UNAVAILABLE = "http_unavailable"
    SOURCE_UNREGISTERED = "source_unregistered"
    REDIRECT_REJECTED = "redirect_rejected"
    DISCOVERY_FORMAT_INVALID = "discovery_format_invalid"
    IDENTITY_MISMATCH = "identity_mismatch"
    PUBLICATION_DATE_MISSING = "publication_date_missing"
    LANDING_FORMAT_INVALID = "landing_format_invalid"
    LANDING_TITLE_MISMATCH = "landing_title_mismatch"
    LANDING_DATE_MISMATCH = "landing_date_mismatch"
    ATTACHMENT_MISSING = "attachment_missing"
    ATTACHMENT_AMBIGUOUS = "attachment_ambiguous"
    ATTACHMENT_HOST_REJECTED = "attachment_host_rejected"
    AUTHENTICATION_SHELL = "authentication_shell"
    EMPTY_OR_SCRIPT_ONLY_HTML = "empty_or_script_only_html"
    DECLARED_MIME_UNSUPPORTED = "declared_mime_unsupported"
    DETECTED_CONTAINER_UNKNOWN = "detected_container_unknown"
    DECLARED_DETECTED_MISMATCH = "declared_detected_mismatch"
    LEGACY_OLE_CONTAINER_UNSUPPORTED = "legacy_ole_container_unsupported"
    LEGACY_CONVERTER_UNAVAILABLE = "legacy_converter_unavailable"
    LEGACY_CONVERTER_TIMEOUT = "legacy_converter_timeout"
    LEGACY_CONVERTER_RESOURCE_LIMIT = "legacy_converter_resource_limit"
    LEGACY_CONVERTER_FAILED = "legacy_converter_failed"
    LEGACY_CONVERTER_OUTPUT_INVALID = "legacy_converter_output_invalid"
    RESOURCE_LIMIT = "resource_limit"
    PARSER_FORMAT_INVALID = "parser_format_invalid"
    PARSER_IDENTITY_MISMATCH = "parser_identity_mismatch"
    PARSER_EFFECTIVE_DATE_INVALID = "parser_effective_date_invalid"
    PARSER_AMBIGUOUS_FACT = "parser_ambiguous_fact"
    CLOCK_INVALID = "clock_invalid"
    MANAGED_ARTIFACT_INVALID = "managed_artifact_invalid"
    STORAGE_FAILURE = "storage_failure"
    UNSPECIFIED_FAILURE = "unspecified_failure"


DOCUMENT_FAILURE_PUBLIC_CODES = frozenset(
    {
        "official_document_unavailable",
        "official_document_invalid",
        "official_document_resource_limit",
        "official_document_parse_failed",
        "classification_storage_failed",
    }
)


@dataclass(frozen=True)
class SafeDocumentFailure:
    public_code: str
    stage: DocumentFailureStage
    reason_code: DocumentFailureReason

    def validate(self) -> None:
        if type(self) is not SafeDocumentFailure:
            raise ValueError("safe document failure subclasses are not accepted")
        if set(vars(self)) != {field.name for field in fields(SafeDocumentFailure)}:
            raise ValueError("safe document failure has unexpected state")
        if type(self.public_code) is not str or self.public_code not in DOCUMENT_FAILURE_PUBLIC_CODES:
            raise ValueError("safe document failure public code is invalid")
        if type(self.stage) is not DocumentFailureStage:
            raise ValueError("safe document failure stage is invalid")
        if type(self.reason_code) is not DocumentFailureReason:
            raise ValueError("safe document failure reason is invalid")


def unspecified_document_failure() -> SafeDocumentFailure:
    result = SafeDocumentFailure(
        public_code="official_document_invalid",
        stage=DocumentFailureStage.UNSPECIFIED,
        reason_code=DocumentFailureReason.UNSPECIFIED_FAILURE,
    )
    result.validate()
    return result
```

Export all four public names plus `DOCUMENT_FAILURE_PUBLIC_CODES` from
`kunjin.funds.risk.__init__`.

- [ ] **Step 4: Run the model tests**

Run:

```bash
.venv/bin/python -m unittest tests.unit.test_risk_failures -v
.venv/bin/ruff check src/kunjin/funds/risk/failures.py tests/unit/test_risk_failures.py
```

Expected: all tests pass and Ruff reports no errors.

### Task 2: Assign Safe Reasons In The Official Document Chain

**Files:**

- Modify: `src/kunjin/funds/risk/documents.py`
- Modify: `tests/unit/test_risk_documents.py`

- [ ] **Step 1: Write failing exception and OLE tests**

Add tests that require every official-document exception to own a validated
`SafeDocumentFailure`, while preserving `.code` and redacted message behavior:

```python
def test_document_errors_carry_allowlisted_failure_without_changing_public_code(self) -> None:
    error = OfficialDocumentError(
        DocumentFailureStage.CONTAINER_VALIDATION,
        DocumentFailureReason.DECLARED_DETECTED_MISMATCH,
        "private mismatch detail",
    )
    self.assertEqual(error.code, "official_document_invalid")
    self.assertEqual(error.failure.public_code, error.code)
    self.assertEqual(error.failure.stage, DocumentFailureStage.CONTAINER_VALIDATION)
    self.assertEqual(
        error.failure.reason_code,
        DocumentFailureReason.DECLARED_DETECTED_MISMATCH,
    )
    error.failure.validate()


def test_legacy_ole_word_is_recognized_but_remains_unsupported(self) -> None:
    payload = bytes.fromhex("d0cf11e0a1b11ae1") + b"synthetic legacy word"
    with self.assertRaises(OfficialDocumentError) as caught:
        self.fetch(BytesResponse(payload, content_type="application/msword"))
    self.assertEqual(caught.exception.code, "official_document_invalid")
    self.assertEqual(
        caught.exception.failure.stage,
        DocumentFailureStage.CONTAINER_VALIDATION,
    )
    self.assertEqual(
        caught.exception.failure.reason_code,
        DocumentFailureReason.LEGACY_OLE_CONTAINER_UNSUPPORTED,
    )
    self.assertEqual(tuple(self.paths.fund_documents.iterdir()), ())
```

Add representative tests for:

- DNS failure: `retrieval/dns_unavailable` plus
  `official_document_unavailable`.
- HTTP failure: `retrieval/http_unavailable` plus
  `official_document_unavailable`.
- Landing title mismatch: `landing_validation/landing_title_mismatch`.
- Attachment ambiguity: `landing_validation/attachment_ambiguous`.
- Declared/detected mismatch: `container_validation/declared_detected_mismatch`.
- Declared or streamed size limit: `retrieval/resource_limit` plus
  `official_document_resource_limit`.

- [ ] **Step 2: Run focused tests and confirm constructor/signature failures**

Run:

```bash
.venv/bin/python -m unittest tests.unit.test_risk_documents -v
```

Expected: FAIL because document exceptions do not yet require or expose safe
failure records and OLE is still treated as an unknown container.

- [ ] **Step 3: Implement source-assigned document failures**

Import the failure types and change the exception base to:

```python
class OfficialDocumentError(RuntimeError):
    code = "official_document_invalid"

    def __init__(
        self,
        stage: DocumentFailureStage,
        reason_code: DocumentFailureReason,
        message: str,
    ) -> None:
        failure = SafeDocumentFailure(self.code, stage, reason_code)
        failure.validate()
        self.failure = failure
        super().__init__(message)
```

`OfficialDocumentUnavailableError` and `OfficialDocumentResourceLimitError`
retain their current subclass codes. Update every raise site directly; do not
parse exception messages. Use this exhaustive function-level mapping:

Thread an explicit `stage: DocumentFailureStage` keyword through shared URL and
DNS helpers such as `_validate_public_dns` and `_validate_registered_url`.
Discovery callers pass `DISCOVERY`; landing attachment callers pass
`LANDING_VALIDATION`; direct document retrieval and redirect callers pass
`RETRIEVAL`. A shared helper must not guess its stage from the URL or message.

| Source boundary | Stage | Reason |
| --- | --- | --- |
| index URL or pagination validation | `discovery` | `discovery_format_invalid` |
| index fund/product identity mismatch | `identity_validation` | `identity_mismatch` |
| missing required index publication date | `discovery` | `publication_date_missing` |
| index DNS | `discovery` | `dns_unavailable` |
| index HTTP | `discovery` | `http_unavailable` |
| index timeout/socket/URL failure | `discovery` | `network_unavailable` |
| redirect or non-registered redirect host | `retrieval` | `redirect_rejected` |
| document DNS | `retrieval` | `dns_unavailable` |
| document HTTP | `retrieval` | `http_unavailable` |
| document timeout/socket/URL failure | `retrieval` | `network_unavailable` |
| unregistered publisher, source URL, or direct host | `identity_validation` | `source_unregistered` |
| declared/streamed index or document size limit | the active `discovery` or `retrieval` stage | `resource_limit` |
| login/password shell | `landing_validation` | `authentication_shell` |
| empty/script-only HTML | `landing_validation` | `empty_or_script_only_html` |
| invalid landing/index HTML or encoding | the active `discovery` or `landing_validation` stage | `discovery_format_invalid` or `landing_format_invalid` |
| landing title mismatch | `landing_validation` | `landing_title_mismatch` |
| landing date mismatch/invalid date | `landing_validation` | `landing_date_mismatch` |
| no landing attachment | `landing_validation` | `attachment_missing` |
| multiple landing attachments | `landing_validation` | `attachment_ambiguous` |
| invalid or non-registered attachment host | `landing_validation` | `attachment_host_rejected` |
| unsupported declared MIME | `container_validation` | `declared_mime_unsupported` |
| unknown detected bytes | `container_validation` | `detected_container_unknown` |
| declared/detected mismatch | `container_validation` | `declared_detected_mismatch` |
| legacy OLE/CFB with `application/msword` | `container_validation` | `legacy_ole_container_unsupported` |
| invalid timezone-aware retrieval clock | `retrieval` | `clock_invalid` |
| managed artifact integrity mismatch | `persistence` | `managed_artifact_invalid` |

Add exact OLE detection before generic unknown detection:

```python
_OLE_COMPOUND_FILE_SIGNATURE = bytes.fromhex("d0cf11e0a1b11ae1")


def _detected_family(payload: bytes) -> Optional[str]:
    if payload.startswith(_OLE_COMPOUND_FILE_SIGNATURE):
        return "legacy_ole_doc"
    # existing PDF, HTML, and DOCX checks remain unchanged
```

Immediately after detection and declared-MIME classification, handle OLE before
the generic `_families_match` branch:

```python
if detected == "legacy_ole_doc":
    if declared != "docx_or_legacy_doc":
        raise OfficialDocumentError(
            DocumentFailureStage.CONTAINER_VALIDATION,
            DocumentFailureReason.DECLARED_DETECTED_MISMATCH,
            "official document content type does not match payload",
        )
    raise OfficialDocumentError(
        DocumentFailureStage.CONTAINER_VALIDATION,
        DocumentFailureReason.LEGACY_OLE_CONTAINER_UNSUPPORTED,
        "official legacy Word container is not supported",
    )
```

This branch must run for both the initial response and a resolved attachment,
before suffix selection or managed artifact publication. Do not accept, store,
convert, or parse OLE in D1.1-A.

- [ ] **Step 4: Run document tests and lint**

Run:

```bash
.venv/bin/python -m unittest tests.unit.test_risk_documents -v
.venv/bin/ruff check src/kunjin/funds/risk/documents.py tests/unit/test_risk_documents.py
```

Expected: all tests pass; all existing public `.code` assertions remain
unchanged.

### Task 3: Assign Safe Reasons In The Fact Parser

**Files:**

- Modify: `src/kunjin/funds/risk/parsers.py`
- Modify: `tests/unit/test_risk_parsers.py`
- Modify: `tests/unit/test_risk_service.py` constructor call sites that create `RiskDocumentParseError` directly

- [ ] **Step 1: Write failing parser-reason tests**

Add tests for exact failure records:

```python
def test_parser_errors_keep_public_codes_and_allowlisted_reasons(self) -> None:
    unsupported = replace(
        artifact_for(FIXTURES / "pure-bond-prospectus.html", content_type="text/html"),
        content_type="image/png",
    )
    with self.assertRaises(RiskDocumentParseError) as caught:
        parse_artifact(unsupported)
    self.assertEqual(caught.exception.code, "official_document_parse_failed")
    self.assertEqual(caught.exception.failure.stage, DocumentFailureStage.PARSER)
    self.assertEqual(
        caught.exception.failure.reason_code,
        DocumentFailureReason.PARSER_FORMAT_INVALID,
    )


def test_parser_identity_effective_date_ambiguity_and_resource_reasons(self) -> None:
    # Reuse the existing mismatched DOCX, conflicting-date, conflicting-clause,
    # and resource-limit fixtures. Assert respectively:
    # parser_identity_mismatch, parser_effective_date_invalid,
    # parser_ambiguous_fact, and resource_limit.
```

Privacy assertions must confirm that the existing private exception messages,
fixture paths, and raw text sentinels do not appear in `repr(error.failure)`.

- [ ] **Step 2: Run parser and service tests and confirm signature failures**

Run:

```bash
.venv/bin/python -m unittest tests.unit.test_risk_parsers tests.unit.test_risk_service -v
```

Expected: FAIL because `RiskDocumentParseError` does not yet carry a safe
failure and direct test constructors still use the old signature.

- [ ] **Step 3: Implement parser-safe failures**

Use this constructor:

```python
class RiskDocumentParseError(RuntimeError):
    def __init__(
        self,
        code: str,
        reason_code: DocumentFailureReason,
        message: str,
    ) -> None:
        if code not in {
            "official_document_parse_failed",
            "official_document_resource_limit",
        }:
            raise ValueError("parser error code is invalid")
        failure = SafeDocumentFailure(code, DocumentFailureStage.PARSER, reason_code)
        failure.validate()
        self.code = code
        self.failure = failure
        super().__init__(message)
```

Replace `_fail` with an explicit reason parameter and make the resource helper
fixed:

```python
def _fail(
    reason_code: DocumentFailureReason,
    message: str = "official fund document parsing failed",
) -> RiskDocumentParseError:
    return RiskDocumentParseError(
        "official_document_parse_failed",
        reason_code,
        message,
    )


def _resource_limit() -> RiskDocumentParseError:
    return RiskDocumentParseError(
        "official_document_resource_limit",
        DocumentFailureReason.RESOURCE_LIMIT,
        "official fund document exceeded a parser resource limit",
    )
```

Update every parser failure at its source with this mapping:

| Parser boundary | Reason |
| --- | --- |
| fund code or legal identity mismatch | `parser_identity_mismatch` |
| conflicting/invalid effective date | `parser_effective_date_invalid` |
| conflicting clause, ambiguous field, duplicate incompatible fact | `parser_ambiguous_fact` |
| size/page/entry/character/fact/excerpt resource boundary | `resource_limit` |
| unsupported MIME, unsafe/malformed PDF/DOCX/XML/HTML, checksum/path/state failure, decoding failure, or unknown parser exception | `parser_format_invalid` |

Update direct `RiskDocumentParseError` constructors in tests and service
wrappers to pass `DocumentFailureReason.PARSER_FORMAT_INVALID` unless a more
specific tested source applies. Update direct
`OfficialDocumentUnavailableError` constructors in service test fakes to pass
`DocumentFailureStage.RETRIEVAL` and
`DocumentFailureReason.NETWORK_UNAVAILABLE`. Do not infer a reason from an
exception string.

- [ ] **Step 4: Run parser/service focused tests and lint**

Run:

```bash
.venv/bin/python -m unittest tests.unit.test_risk_parsers tests.unit.test_risk_service -v
.venv/bin/ruff check src/kunjin/funds/risk/parsers.py tests/unit/test_risk_parsers.py tests/unit/test_risk_service.py
```

Expected: all tests pass and existing parser public-code assertions remain
unchanged.

### Task 4: Propagate Safe Diagnostics Through The Service

**Files:**

- Modify: `src/kunjin/funds/risk/service.py`
- Modify: `tests/unit/test_risk_service.py`

- [ ] **Step 1: Write failing service propagation and privacy tests**

Add `failure_stage` and `failure_reason` assertions to the existing partial-sync
and parse-failure tests. Add tests equivalent to:

```python
def test_sync_propagates_safe_diagnostics_and_observes_each_failure(self) -> None:
    observed = []
    failure = SafeDocumentFailure(
        "official_document_invalid",
        DocumentFailureStage.CONTAINER_VALIDATION,
        DocumentFailureReason.LEGACY_OLE_CONTAINER_UNSUPPORTED,
    )
    client = FakeDocumentClientThatRaises(
        OfficialDocumentError(
            failure.stage,
            failure.reason_code,
            "private URL path and response detail",
        )
    )
    service = FundRiskService(
        # existing fakes
        failure_observer=observed.append,
    )
    result = service.sync_documents("519755")
    item = result.documents[0]
    self.assertEqual(item.error_code, "official_document_invalid")
    self.assertEqual(item.failure_stage, "container_validation")
    self.assertEqual(item.failure_reason, "legacy_ole_container_unsupported")
    self.assertEqual(observed, [failure])
    self.assertNotIn("private URL", repr(result))


def test_unknown_and_observer_failures_remain_fail_closed(self) -> None:
    def broken_observer(_: SafeDocumentFailure) -> None:
        raise RuntimeError("private observer sentinel")

    # Make the client raise an unknown RuntimeError and assert the result keeps:
    # official_document_invalid / unspecified / unspecified_failure.
    # The observer exception and both private sentinels must not escape.
```

Also test discovery-level observer notification, parser failures, resource
failures, and storage override to `classification_storage_failed / persistence /
storage_failure`.

- [ ] **Step 2: Run service tests and confirm missing-field failures**

Run:

```bash
.venv/bin/python -m unittest tests.unit.test_risk_service -v
```

Expected: FAIL because `DocumentSyncItem` lacks safe fields and the service
does not classify or observe safe failures.

- [ ] **Step 3: Implement deterministic service mapping**

Append optional fields to preserve existing keyword constructors:

```python
@dataclass(frozen=True)
class DocumentSyncItem:
    # existing fields unchanged
    error_code: Optional[str]
    failure_stage: Optional[str] = None
    failure_reason: Optional[str] = None
```

Add to `FundRiskService.__init__`:

```python
failure_observer: Callable[[SafeDocumentFailure], None] = lambda _failure: None
```

Store it as `_failure_observer`. Add:

```python
def _safe_failure(error: Exception) -> SafeDocumentFailure:
    if isinstance(error, (OfficialDocumentError, RiskDocumentParseError)):
        error.failure.validate()
        return error.failure
    if isinstance(error, RiskStoreError):
        return SafeDocumentFailure(
            "classification_storage_failed",
            DocumentFailureStage.PERSISTENCE,
            DocumentFailureReason.STORAGE_FAILURE,
        )
    return unspecified_document_failure()
```

`_service_error` must first return an exact existing `RiskServiceError`
unchanged, preserving classification policy/calculation/storage errors. For all
other errors it returns `RiskServiceError(_safe_failure(error).public_code)` and
must not inspect exception messages. Add an instance helper that calls the
observer in a `try/except Exception` and discards observer errors.

On discovery failure:

1. Build the safe failure.
2. Notify the observer.
3. Raise `RiskServiceError(failure.public_code)` with `reason=None`.

On candidate failure:

1. Build the safe failure.
2. If failed-artifact persistence fails, replace it with the storage failure.
3. Notify the observer once with the final failure.
4. Return the existing `error_code` plus `failure.stage.value` and
   `failure.reason_code.value`.

Successful items leave both safe fields null. Classification behavior,
artifact persistence, and reason/missing-evidence codes remain unchanged.

- [ ] **Step 4: Run service, store, and research regression tests**

Run:

```bash
.venv/bin/python -m unittest \
  tests.unit.test_risk_service \
  tests.unit.test_risk_store \
  tests.unit.test_risk_research -v
.venv/bin/ruff check src/kunjin/funds/risk/service.py tests/unit/test_risk_service.py
```

Expected: all tests pass; database failed-artifact rows still contain only the
existing public parse error code.

### Task 5: Expose The Additive CLI Contract And Verify D1.1-A

**Files:**

- Modify: `tests/integration/test_cli.py`
- Modify: `tests/test_smoke.py`
- Modify: `README.md`
- Modify: `integrations/codex/kunjin-fund/SKILL.md`
- Modify after verification: `/Users/yanzihao/.codex/skills/kunjin-fund/SKILL.md`

- [ ] **Step 1: Write failing CLI and packaged-contract tests**

Update every `DocumentSyncItem` fixture to include or accept the default safe
fields. For a failed result, require this additive JSON shape:

```python
self.assertEqual(
    payload["data"]["documents"][1],
    {
        # existing keys and values remain unchanged
        "error_code": "official_document_parse_failed",
        "failure_stage": "parser",
        "failure_reason": "parser_format_invalid",
    },
)
```

Keep existing top-level technical-error messages exact. Scan serialized JSON
for private exception sentinels, managed paths, raw URLs not already present as
the public candidate URL, response bodies, and personal-field names.

Update the smoke contract to require the repository Skill to mention
`failure_stage`, `failure_reason`, exact-code preservation, and that diagnostics
are technical evidence rather than a classification or purchase signal.

- [ ] **Step 2: Run CLI and smoke tests and confirm contract failures**

Run:

```bash
.venv/bin/python -m unittest tests.integration.test_cli tests.test_smoke -v
```

Expected: FAIL until the fake results, README, and Skill document the additive
safe fields.

- [ ] **Step 3: Update documentation and Skill boundaries**

Add concise README text:

```text
Failed `sync fund-documents` items retain the existing `error_code` and may add
allowlisted `failure_stage` and `failure_reason`. These values explain the
technical boundary only. They do not prove a product family, risk bucket,
portfolio role, suitability result, or purchase direction.
```

Add to the repository Skill workflow:

```text
Preserve `failure_stage` and `failure_reason` exactly when present. Explain
them separately from D1 classification reason and missing-evidence codes. Never
reconstruct omitted exception text, paths, response details, or document
content from a diagnostic code.
```

Do not add D1.1-B converter wording as an implemented capability.

- [ ] **Step 4: Run the complete verification gate**

Run:

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check \
  src/kunjin/funds/risk/failures.py \
  src/kunjin/funds/risk/__init__.py \
  src/kunjin/funds/risk/documents.py \
  src/kunjin/funds/risk/parsers.py \
  src/kunjin/funds/risk/service.py \
  tests/unit/test_risk_failures.py \
  tests/unit/test_risk_documents.py \
  tests/unit/test_risk_parsers.py \
  tests/unit/test_risk_service.py \
  tests/integration/test_cli.py \
  tests/test_smoke.py
PYTHONPYCACHEPREFIX=/private/tmp/kunjin-pycache \
  .venv/bin/python -m compileall -q src tests
.venv/bin/python -m pip check
git diff --check
```

Expected: full tests, lint, touched-file formatting, compilation, dependency
check, and diff check all pass.

- [ ] **Step 5: Synchronize the installed Skill and verify byte identity**

Only after Step 4 passes:

```bash
cp integrations/codex/kunjin-fund/SKILL.md \
  /Users/yanzihao/.codex/skills/kunjin-fund/SKILL.md
cmp -s integrations/codex/kunjin-fund/SKILL.md \
  /Users/yanzihao/.codex/skills/kunjin-fund/SKILL.md
```

Expected: `cmp` exits 0. Do not modify any other installed Skill file.

- [ ] **Step 6: Prepare the isolated live acceptance command**

Prepare, but do not claim completion before running, a fresh isolated v6 script
for the four predeclared public codes. It must capture profile, holdings,
documents, classification, evidence, and history exactly as v5 did. The D1.1-A
acceptance analysis must additionally report counts by:

```text
document_kind
error_code
failure_stage
failure_reason
```

It must confirm that all failed candidate items have allowlisted safe fields,
that the representative legacy cohort reports
`legacy_ole_container_unsupported`, and that no classification changed merely
because diagnostics were added.

After live acceptance, run the independent financial review checkpoint. The
review may award zero additional points because D1.1-A is diagnostic-only. It
must not award D2, D3, or Phase E credit and must not claim 90% beginner help.
