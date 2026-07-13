from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import date
from typing import Optional, Sequence, Tuple

from kunjin.funds.models import DisclosureBundle, FundBenchmark
from kunjin.funds.peers.models import (
    DirectoryCandidate,
    MembershipKind,
    PeerClassification,
)

PEER_MEMBER_LIMIT = 20
DISCOVERY_VALIDATION_LIMIT = 40
PEER_RULE_VERSION = "1"

EQUITY_TOKENS = (
    "沪深300",
    "中证500",
    "中证800",
    "中证1000",
    "中证A500",
    "上证指数",
    "深证成指",
    "创业板",
    "科创50",
    "股票指数",
)
BOND_TOKENS = (
    "中债",
    "中证全债",
    "中证综合债",
    "上证国债",
    "债券指数",
    "国债指数",
    "信用债指数",
)

_FUND_CODE_PATTERN = re.compile(r"^\d{6}$")
_BACK_END_MARKERS = ("（后端）", "(后端)")


def _normalize_text(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).split())


def _normalize_fund_type(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    normalized = _normalize_text(value)
    return normalized or None


def benchmark_family(text: str) -> Optional[str]:
    normalized = _normalize_text(text)
    has_equity = any(token in normalized for token in EQUITY_TOKENS)
    has_bond = any(token in normalized for token in BOND_TOKENS)
    if has_equity and has_bond:
        return "equity_bond"
    if has_equity:
        return "equity"
    if has_bond:
        return "bond"
    return None


def ordered_candidates(
    anchor_code: str,
    directory: Sequence[DirectoryCandidate],
    user_supplied: Sequence[str],
    held_codes: Sequence[str],
) -> Tuple[Tuple[str, MembershipKind], ...]:
    _validate_code(anchor_code)
    for candidate in directory:
        candidate.validate()
    for code in tuple(user_supplied) + tuple(held_codes):
        _validate_code(code)

    selected = {anchor_code: MembershipKind.ANCHOR}
    ordered = [(anchor_code, MembershipKind.ANCHOR)]

    def add(code: str, kind: MembershipKind) -> None:
        if code not in selected:
            selected[code] = kind
            ordered.append((code, kind))

    for code in user_supplied:
        add(code, MembershipKind.USER_SUPPLIED)
    for code in sorted(set(held_codes)):
        add(code, MembershipKind.HELD)

    anchor_entry = next(
        (candidate for candidate in directory if candidate.fund_code == anchor_code),
        None,
    )
    if anchor_entry is None:
        return tuple(ordered)

    discovered_by_code = {}
    for candidate in directory:
        if candidate.directory_type != anchor_entry.directory_type:
            continue
        if any(marker in candidate.fund_name for marker in _BACK_END_MARKERS):
            continue
        if candidate.fund_code in selected:
            continue
        discovered_by_code.setdefault(candidate.fund_code, candidate)

    discovered_codes = sorted(
        discovered_by_code,
        key=lambda code: (
            hashlib.sha256(f"{anchor_code}:{code}".encode("ascii")).hexdigest(),
            code,
        ),
    )[:DISCOVERY_VALIDATION_LIMIT]
    ordered.extend((code, MembershipKind.DISCOVERED) for code in discovered_codes)
    return tuple(ordered)


def classify_peer(
    anchor: DisclosureBundle,
    candidate: DisclosureBundle,
    as_of: date,
) -> PeerClassification:
    candidate_issue = _identity_issue(candidate)
    if candidate_issue is not None:
        reason, warnings = candidate_issue
        return _rejected(candidate.fund_code, reason, warnings=warnings)

    candidate_type = _normalize_fund_type(candidate.identity.fund_type)
    if candidate_type is None:
        return _rejected(
            candidate.fund_code,
            "peer_classification_ambiguous",
            warnings=("missing_fund_type",),
        )
    candidate_style = _management_style(candidate_type)
    candidate_benchmark = _current_benchmark_family(candidate.benchmarks, as_of)
    if candidate_benchmark is None:
        return _rejected(
            candidate.fund_code,
            "peer_classification_ambiguous",
            fund_type_family=candidate_type,
            management_style=candidate_style,
            warnings=("missing_benchmark_family",),
        )

    candidate_key = _classification_key(
        candidate_type, candidate_style, candidate_benchmark
    )
    anchor_issue = _identity_issue(anchor)
    if anchor_issue is not None:
        reason, warnings = anchor_issue
        return _rejected(
            candidate.fund_code,
            "peer_classification_ambiguous",
            classification_key=candidate_key,
            fund_type_family=candidate_type,
            management_style=candidate_style,
            benchmark_family_value=candidate_benchmark,
            warnings=tuple(f"anchor_{warning}" for warning in warnings)
            or (f"anchor_{reason}",),
        )

    anchor_type = _normalize_fund_type(anchor.identity.fund_type)
    if anchor_type is None:
        return _rejected(
            candidate.fund_code,
            "peer_classification_ambiguous",
            classification_key=candidate_key,
            fund_type_family=candidate_type,
            management_style=candidate_style,
            benchmark_family_value=candidate_benchmark,
            warnings=("anchor_missing_fund_type",),
        )
    anchor_style = _management_style(anchor_type)
    anchor_benchmark = _current_benchmark_family(anchor.benchmarks, as_of)
    if anchor_benchmark is None:
        return _rejected(
            candidate.fund_code,
            "peer_classification_ambiguous",
            classification_key=candidate_key,
            fund_type_family=candidate_type,
            management_style=candidate_style,
            benchmark_family_value=candidate_benchmark,
            warnings=("anchor_missing_benchmark_family",),
        )

    if candidate_type != anchor_type:
        return _rejected(
            candidate.fund_code,
            "type_mismatch",
            classification_key=candidate_key,
            fund_type_family=candidate_type,
            management_style=candidate_style,
            benchmark_family_value=candidate_benchmark,
        )
    if candidate_style != anchor_style:
        return _rejected(
            candidate.fund_code,
            "management_style_mismatch",
            classification_key=candidate_key,
            fund_type_family=candidate_type,
            management_style=candidate_style,
            benchmark_family_value=candidate_benchmark,
        )
    if candidate_benchmark != anchor_benchmark:
        return _rejected(
            candidate.fund_code,
            "benchmark_mismatch",
            classification_key=candidate_key,
            fund_type_family=candidate_type,
            management_style=candidate_style,
            benchmark_family_value=candidate_benchmark,
        )

    warnings = (
        ("share_class_sibling",)
        if _share_class_siblings(anchor, candidate)
        else ()
    )
    result = PeerClassification(
        fund_code=candidate.fund_code,
        accepted=True,
        classification_key=candidate_key,
        fund_type_family=candidate_type,
        management_style=candidate_style,
        benchmark_family=candidate_benchmark,
        reason="classification_match",
        warnings=warnings,
    )
    result.validate()
    return result


def _validate_code(code: str) -> None:
    if not isinstance(code, str) or _FUND_CODE_PATTERN.fullmatch(code) is None:
        raise ValueError(f"invalid fund code: {code}")


def _identity_issue(
    bundle: DisclosureBundle,
) -> Optional[Tuple[str, Tuple[str, ...]]]:
    section_has_conflict = any(
        status.get("error_code") == "identity_conflict"
        for status in bundle.section_statuses.values()
    )
    if section_has_conflict or any(
        "identity_conflict" in conflict for conflict in bundle.conflicts
    ):
        return "identity_conflict", ()
    if bundle.identity is None:
        return "missing_identity", ()
    if bundle.identity.fund_code != bundle.fund_code:
        return "identity_conflict", ()
    if bundle.identity.status != "active":
        return "inactive_fund", ()
    return None


def _management_style(fund_type: str) -> str:
    return "passive" if "指数型" in fund_type else "active_or_unspecified"


def _current_benchmark_family(
    benchmarks: Sequence[FundBenchmark], as_of: date
) -> Optional[str]:
    active = tuple(
        item
        for item in benchmarks
        if (item.effective_from is None or item.effective_from <= as_of)
        and (item.effective_to is None or item.effective_to >= as_of)
    )
    families = tuple(benchmark_family(item.description) for item in active)
    if not families or any(family is None for family in families):
        return None
    unique = set(families)
    return next(iter(unique)) if len(unique) == 1 else None


def _classification_key(
    fund_type: str, management_style: str, benchmark: str
) -> str:
    return f"{fund_type}|{management_style}|{benchmark}"


def _share_class_siblings(
    anchor: DisclosureBundle, candidate: DisclosureBundle
) -> bool:
    if anchor.fund_code == candidate.fund_code:
        return False
    anchor_related = {item.related_fund_code for item in anchor.share_classes}
    candidate_related = {item.related_fund_code for item in candidate.share_classes}
    return (
        candidate.fund_code in anchor_related
        or anchor.fund_code in candidate_related
    )


def _rejected(
    fund_code: str,
    reason: str,
    *,
    classification_key: Optional[str] = None,
    fund_type_family: Optional[str] = None,
    management_style: Optional[str] = None,
    benchmark_family_value: Optional[str] = None,
    warnings: Tuple[str, ...] = (),
) -> PeerClassification:
    result = PeerClassification(
        fund_code=fund_code,
        accepted=False,
        classification_key=classification_key,
        fund_type_family=fund_type_family,
        management_style=management_style,
        benchmark_family=benchmark_family_value,
        reason=reason,
        warnings=warnings,
    )
    result.validate()
    return result
