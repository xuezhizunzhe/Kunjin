from __future__ import annotations

import hashlib
import ipaddress
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Optional, Tuple
from urllib.parse import parse_qsl, urlsplit

from kunjin.decision.models import (
    MAX_PUBLIC_TEXT_CHARS,
    MAX_TUPLE_ITEMS,
    EvidenceCompleteness,
    EvidenceFreshness,
    RequestTerminalStatus,
    SourceTier,
    canonical_decimal,
    canonical_json_bytes,
    canonical_value,
    validate_aware_datetime,
    validate_checksum,
    validate_exact_dataclass_state,
    validate_identifier,
    validate_identifier_tuple,
    validate_public_text,
    validate_public_text_tuple,
    validate_request_id,
)
from kunjin.intelligence.policy import (
    MARKET_CROWDING_SHARE,
    NEGATIVE_BREADTH,
    NEGATIVE_CHANGE,
    POSITIVE_BREADTH,
    POSITIVE_CHANGE,
)

_FUND_CODE_PATTERN = re.compile(r"^[0-9]{6}$")
_MAPPING_PROXY_TYPE = type(MappingProxyType({}))
_MAX_PUBLIC_TREE_DEPTH = 12
_MAX_PUBLIC_MAP_ITEMS = 128
_EXCERPT_MAX_BYTES = 2_048
_EXCERPT_RETENTION_DAYS = 365
_PRIVATE_PATH_TOKENS = frozenset(
    {
        "amount",
        "authorization",
        "cookie",
        "cookies",
        "cost",
        "credential",
        "debt",
        "income",
        "local",
        "path",
        "profit",
        "raw",
        "reserve",
        "secret",
        "shares",
        "token",
        "weight",
    }
)
_PRIVATE_PATH_COMPOUNDS = frozenset(
    {
        "authorization_header",
        "authorization_headers",
        "browser_cookie",
        "browser_cookies",
        "local_path",
        "managed_path",
        "portfolio_weight",
        "raw_body",
        "response_body",
    }
)


class IntelligenceWorkflow(str, Enum):
    NEWS_RECENT = "news_recent"
    MARKET_OVERVIEW = "market_overview"
    FUND_INTELLIGENCE = "fund_intelligence"


class QueryWindow(str, Enum):
    TODAY = "today"
    RECENT = "recent"
    NEAR_TERM = "near_term"


class DimensionState(str, Enum):
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"
    RISK_FLAG = "risk_flag"
    CONFLICTED = "conflicted"
    INSUFFICIENT_DATA = "insufficient_data"


class MarketDimension(str, Enum):
    TREND_BREADTH = "trend_breadth"
    PERSISTENT_FLOW = "persistent_flow"
    CATALYSTS = "catalysts"
    CROWDING = "crowding"
    VALUATION = "valuation"
    FUNDAMENTALS_EARNINGS = "fundamentals_earnings"


class MetricId(str, Enum):
    INDUSTRY_MEDIAN_PCT_CHANGE = "industry_median_pct_change"
    INDUSTRY_AGGREGATE_BREADTH = "industry_aggregate_breadth"
    SECTOR_PCT_CHANGE = "sector_pct_change"
    SECTOR_BREADTH = "sector_breadth"
    INDUSTRY_POSITIVE_FLOW_SHARE_3D = "industry_positive_flow_share_3d"
    SECTOR_MAIN_FLOW_RATIO_3D = "sector_main_flow_ratio_3d"
    AUTHENTICATED_EVENT_DIRECTION = "authenticated_event_direction"
    INDUSTRY_OVERHEATING_SHARE = "industry_overheating_share"
    SECTOR_RETURN_TURNOVER_PERCENTILES = "sector_return_turnover_percentiles"


class EventConfidenceState(str, Enum):
    SUFFICIENT = "sufficient"
    PARTIAL = "partial"
    CONFLICTED = "conflicted"
    INSUFFICIENT = "insufficient"


class EventType(str, Enum):
    POLICY = "policy"
    FUND_OFFICIAL = "fund_official"
    FUND_MEDIA = "fund_media"
    MARKET = "market"
    SECTOR = "sector"


class EventEntityRelationship(str, Enum):
    SUBJECT = "subject"
    AFFECTS = "affects"
    POLICY_CATALYST = "policy_catalyst"
    FUND_HOLDING_EXPOSURE = "fund_holding_exposure"
    FUND_BENCHMARK_EXPOSURE = "fund_benchmark_exposure"


class MarketShadowState(str, Enum):
    OFFENSIVE_BIAS = "offensive_bias"
    NEUTRAL = "neutral"
    DEFENSIVE_BIAS = "defensive_bias"
    INSUFFICIENT_DATA = "insufficient_data"


class SectorShadowState(str, Enum):
    IMPROVING = "improving"
    NEUTRAL = "neutral"
    WEAKENING = "weakening"
    OVERHEATING_RISK = "overheating_risk"
    INSUFFICIENT_DATA = "insufficient_data"


class IntegrityState(str, Enum):
    ACTIVE = "active"
    CORRECTED = "corrected"
    RETRACTED = "retracted"
    SUPERSEDED = "superseded"
    UNKNOWN = "unknown"


class LineageKind(str, Enum):
    ORIGINAL = "original"
    DIRECT_QUOTE = "direct_quote"
    REPRINT = "reprint"
    INDEPENDENTLY_REPORTED = "independently_reported"
    CORRECTION_OF = "correction_of"
    RETRACTION_OF = "retraction_of"
    CLARIFICATION_OF = "clarification_of"
    UNKNOWN = "unknown"


def _validate_exact_record(value: object, expected_type: type, name: str) -> None:
    if type(value) is not expected_type:
        raise ValueError(f"{name} must be an exact {expected_type.__name__}")
    validate_exact_dataclass_state(value, name)


def _validate_utc_datetime(value: object, name: str) -> datetime:
    validated = validate_aware_datetime(value, name)
    if validated.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be UTC")
    return validated


def _validate_optional_utc(value: object, name: str) -> None:
    if value is not None:
        _validate_utc_datetime(value, name)


def _validate_public_https_url(value: object, name: str) -> str:
    error = f"{name} must be a canonical public HTTPS URL"
    if type(value) is not str or not value or len(value) > MAX_PUBLIC_TEXT_CHARS:
        raise ValueError(error)
    if any(
        ord(character) <= 0x1F or ord(character) == 0x7F or 0xD800 <= ord(character) <= 0xDFFF
        for character in value
    ):
        raise ValueError(error)
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError(error) from None
    sensitive_query_tokens = {
        "api_key",
        "auth",
        "authorization",
        "cookie",
        "credential",
        "key",
        "password",
        "secret",
        "session",
        "token",
    }

    def sensitive_query_key(key: str) -> bool:
        tokens = _normalized_path_tokens(key)
        joined = "".join(tokens)
        return bool(
            sensitive_query_tokens.intersection(tokens)
            or any(
                marker in joined
                for marker in (
                    "authorization",
                    "cookie",
                    "credential",
                    "password",
                    "secret",
                    "session",
                    "token",
                )
            )
            or joined in {"apikey", "accesskey", "authkey"}
        )

    query_is_sensitive = any(
        sensitive_query_key(key) for key, _value in parse_qsl(parsed.query, keep_blank_values=True)
    )
    host_is_ip = False
    if hostname:
        try:
            ipaddress.ip_address(hostname)
            host_is_ip = True
        except ValueError:
            pass
    if (
        parsed.scheme != "https"
        or not hostname
        or hostname != hostname.lower()
        or not hostname.isascii()
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.netloc != hostname
        or parsed.fragment
        or not parsed.path.startswith("/")
        or "." not in hostname
        or re.fullmatch(r"[a-z0-9.-]+", hostname) is None
        or hostname.endswith(".")
        or hostname.endswith(".local")
        or hostname.endswith(".localhost")
        or host_is_ip
        or query_is_sensitive
    ):
        raise ValueError(error)
    return value


def _normalized_path_tokens(value: str) -> Tuple[str, ...]:
    return tuple(part for part in re.split(r"[^a-z0-9]+", value.casefold()) if part)


def _is_private_path(value: str) -> bool:
    tokens = _normalized_path_tokens(value)
    joined = "_".join(tokens)
    return bool(_PRIVATE_PATH_TOKENS.intersection(tokens) or joined in _PRIVATE_PATH_COMPOUNDS)


def _validate_public_identifier_tuple(
    value: object,
    name: str,
    *,
    allow_empty: bool = True,
) -> Tuple[str, ...]:
    values = validate_identifier_tuple(value, name, allow_empty=allow_empty)
    if any(_is_private_path(item) for item in values):
        raise ValueError(f"{name} contains a private field")
    return values


def _validate_positive_int_tuple(value: object, name: str, *, allow_empty: bool = True) -> None:
    if type(value) is not tuple or len(value) > MAX_TUPLE_ITEMS:
        raise ValueError(f"{name} must be a bounded exact tuple")
    if not allow_empty and not value:
        raise ValueError(f"{name} cannot be empty")
    if any(type(item) is not int or item <= 0 for item in value):
        raise ValueError(f"{name} must contain positive exact integers")
    if tuple(sorted(set(value))) != value:
        raise ValueError(f"{name} must contain unique ascending values")


def _validate_record_tuple(
    value: object,
    record_type: type,
    name: str,
    id_field: Optional[str] = None,
) -> Tuple[object, ...]:
    if type(value) is not tuple or len(value) > MAX_TUPLE_ITEMS:
        raise ValueError(f"{name} must be a bounded exact tuple")
    identifiers = []
    for item in value:
        if type(item) is not record_type:
            raise ValueError(f"{name} must contain exact {record_type.__name__} records")
        item.validate()
        if id_field is not None:
            identifiers.append(getattr(item, id_field))
    if len(identifiers) != len(set(identifiers)):
        raise ValueError(f"{name} identifiers must be unique")
    return value


def _freeze_public_tree(value: object) -> object:
    if type(value) in {dict, _MAPPING_PROXY_TYPE}:
        return MappingProxyType({key: _freeze_public_tree(item) for key, item in value.items()})
    if type(value) is tuple:
        return tuple(_freeze_public_tree(item) for item in value)
    return value


def _canonical_public_tree(value: object) -> object:
    if type(value) is _MAPPING_PROXY_TYPE:
        return {key: _canonical_public_tree(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_canonical_public_tree(item) for item in value]
    return canonical_value(value)


def _validate_public_tree(value: object, path: str, *, depth: int = 0) -> None:
    if depth > _MAX_PUBLIC_TREE_DEPTH:
        raise ValueError(f"{path} exceeds the public tree depth limit")
    if type(value) is Decimal:
        raise ValueError(f"{path} contains Decimal, which public report trees forbid")
    if type(value) is float:
        raise ValueError(f"{path} contains an unsupported float")
    if isinstance(value, Enum):
        raise ValueError(f"{path} contains an unsupported Enum")
    if type(value) is datetime:
        _validate_utc_datetime(value, path)
        return
    if type(value) is date:
        return
    if type(value) is _MAPPING_PROXY_TYPE:
        if len(value) > _MAX_PUBLIC_MAP_ITEMS:
            raise ValueError(f"{path} has too many mapping items")
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError(f"{path} mapping keys must be exact strings")
            validate_identifier(key, f"{path} key")
            if _is_private_path(key):
                raise ValueError(f"{path}.{key} is a private field")
            _validate_public_tree(item, f"{path}.{key}", depth=depth + 1)
        return
    if type(value) is tuple:
        if len(value) > MAX_TUPLE_ITEMS:
            raise ValueError(f"{path} has too many tuple items")
        for index, item in enumerate(value):
            _validate_public_tree(item, f"{path}[{index}]", depth=depth + 1)
        return
    if type(value) is str:
        validate_public_text(value, path)
        return
    if type(value) is bool or value is None:
        return
    if type(value) is int:
        raise ValueError(f"{path} contains an unsupported int")
    raise ValueError(f"{path} contains unsupported {type(value).__name__}")


def truncate_excerpt_utf8(value: str, maximum_bytes: int = _EXCERPT_MAX_BYTES) -> Tuple[str, bool]:
    validate_public_text(value, "excerpt source")
    if type(maximum_bytes) is not int or maximum_bytes <= 0:
        raise ValueError("excerpt maximum bytes must be a positive exact integer")
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return value, False
    truncated = encoded[:maximum_bytes]
    while True:
        try:
            return truncated.decode("utf-8"), True
        except UnicodeDecodeError as exc:
            truncated = truncated[: exc.start]


@dataclass(frozen=True)
class NewsItem:
    item_id: str
    source_id: str
    publisher: str
    canonical_url: str
    title: str
    excerpt: Optional[str]
    excerpt_truncated: bool
    excerpt_original_bytes: int
    excerpt_expires_at: datetime
    excerpt_expired_at: Optional[datetime]
    published_at: datetime
    publication_precision: str
    publication_interval_end: Optional[datetime]
    retrieved_at: datetime
    source_tier: SourceTier
    content_fingerprint: str
    category: str
    integrity_state: IntegrityState
    source_attempt_id: int

    def validate(self) -> None:
        _validate_exact_record(self, NewsItem, "news item")
        validate_identifier(self.item_id, "news item id")
        validate_identifier(self.source_id, "news source id")
        validate_public_text(self.publisher, "news publisher")
        _validate_public_https_url(self.canonical_url, "news canonical URL")
        validate_public_text(self.title, "news title")
        if type(self.excerpt_truncated) is not bool:
            raise ValueError("excerpt truncated must be an exact boolean")
        if (
            type(self.excerpt_original_bytes) is not int
            or not 1 <= self.excerpt_original_bytes <= 5 * 1024 * 1024
        ):
            raise ValueError("excerpt original bytes must be a bounded positive exact integer")
        _validate_utc_datetime(self.excerpt_expires_at, "excerpt expiry")
        _validate_optional_utc(self.excerpt_expired_at, "excerpt expired time")
        _validate_utc_datetime(self.published_at, "news publication time")
        _validate_optional_utc(self.publication_interval_end, "publication interval end")
        _validate_utc_datetime(self.retrieved_at, "news retrieval time")
        if self.retrieved_at < self.published_at:
            raise ValueError("news retrieval time cannot precede publication evidence")
        if self.excerpt_expires_at != self.retrieved_at + timedelta(days=_EXCERPT_RETENTION_DAYS):
            raise ValueError("excerpt expiry must use the exact 365-day retention")
        if self.excerpt is None:
            if self.excerpt_expired_at is None or self.excerpt_expired_at < self.excerpt_expires_at:
                raise ValueError("expired excerpt must record expiry at or after its deadline")
            if self.excerpt_truncated != (self.excerpt_original_bytes > _EXCERPT_MAX_BYTES):
                raise ValueError("expired excerpt truncated flag contradicts original bytes")
        else:
            validate_public_text(self.excerpt, "news excerpt")
            excerpt_bytes = len(self.excerpt.encode("utf-8"))
            if excerpt_bytes > _EXCERPT_MAX_BYTES:
                raise ValueError("news excerpt exceeds 2048 UTF-8 bytes")
            if self.excerpt_expired_at is not None:
                raise ValueError("active excerpt cannot have an expired timestamp")
            if self.excerpt_truncated:
                if (
                    self.excerpt_original_bytes <= _EXCERPT_MAX_BYTES
                    or not _EXCERPT_MAX_BYTES - 3 <= excerpt_bytes <= _EXCERPT_MAX_BYTES
                ):
                    raise ValueError(
                        "excerpt truncated flag requires a full 2048-byte boundary truncation"
                    )
            elif self.excerpt_original_bytes != excerpt_bytes:
                raise ValueError("untruncated excerpt must preserve its exact original byte count")
        if self.publication_precision == "date":
            if (
                self.published_at.hour,
                self.published_at.minute,
                self.published_at.second,
                self.published_at.microsecond,
            ) != (16, 0, 0, 0):
                raise ValueError(
                    "date publication must be the Asia/Shanghai local-day start in UTC"
                )
            if self.publication_interval_end != self.published_at + timedelta(days=1):
                raise ValueError("date publication requires the exclusive next local midnight")
        elif self.publication_precision == "minute":
            if self.published_at.second != 0 or self.published_at.microsecond != 0:
                raise ValueError("minute publication must be stored at exact minute precision")
            if self.publication_interval_end is not None:
                raise ValueError("minute publication cannot declare an interval end")
        else:
            raise ValueError("publication precision must be exactly date or minute")
        if type(self.source_tier) is not SourceTier or self.source_tier not in {
            SourceTier.TIER_1,
            SourceTier.TIER_2,
        }:
            raise ValueError("news source tier must be exact public Tier 1 or Tier 2")
        validate_checksum(self.content_fingerprint, "news content fingerprint")
        validate_identifier(self.category, "news category")
        if type(self.integrity_state) is not IntegrityState:
            raise ValueError("news integrity state must be exact")
        if type(self.source_attempt_id) is not int or self.source_attempt_id <= 0:
            raise ValueError("source attempt id must be a positive exact integer")

    def to_canonical_dict(self) -> dict:
        self.validate()
        return {
            "canonical_url": self.canonical_url,
            "category": self.category,
            "content_fingerprint": self.content_fingerprint,
            "excerpt": self.excerpt,
            "excerpt_expired_at": (
                None
                if self.excerpt_expired_at is None
                else canonical_value(self.excerpt_expired_at)
            ),
            "excerpt_expires_at": canonical_value(self.excerpt_expires_at),
            "excerpt_original_bytes": self.excerpt_original_bytes,
            "excerpt_truncated": self.excerpt_truncated,
            "integrity_state": self.integrity_state.value,
            "item_id": self.item_id,
            "publication_interval_end": (
                None
                if self.publication_interval_end is None
                else canonical_value(self.publication_interval_end)
            ),
            "publication_precision": self.publication_precision,
            "published_at": canonical_value(self.published_at),
            "publisher": self.publisher,
            "retrieved_at": canonical_value(self.retrieved_at),
            "source_attempt_id": self.source_attempt_id,
            "source_id": self.source_id,
            "source_tier": self.source_tier.value,
            "title": self.title,
        }


@dataclass(frozen=True)
class DimensionObservation:
    observation_id: str
    entity_id: str
    dimension: MarketDimension
    metric_id: Optional[MetricId]
    state: DimensionState
    value: Optional[Decimal]
    unit: Optional[str]
    data_as_of: datetime
    retrieved_at: datetime
    source_tier: SourceTier
    source_attempt_ids: Tuple[int, ...]
    evidence_ids: Tuple[str, ...]
    freshness: EvidenceFreshness
    completeness: EvidenceCompleteness
    conflict_ids: Tuple[str, ...]

    def validate(self) -> None:
        _validate_exact_record(self, DimensionObservation, "dimension observation")
        for value, name in (
            (self.observation_id, "observation id"),
            (self.entity_id, "observation entity id"),
        ):
            validate_identifier(value, name)
        if type(self.dimension) is not MarketDimension:
            raise ValueError("dimension must be an exact MarketDimension")
        if self.metric_id is not None and type(self.metric_id) is not MetricId:
            raise ValueError("metric id must be an exact MetricId or None")
        if type(self.state) is not DimensionState:
            raise ValueError("dimension state must be exact")
        metric_dimensions = {
            MetricId.INDUSTRY_MEDIAN_PCT_CHANGE: MarketDimension.TREND_BREADTH,
            MetricId.INDUSTRY_AGGREGATE_BREADTH: MarketDimension.TREND_BREADTH,
            MetricId.SECTOR_PCT_CHANGE: MarketDimension.TREND_BREADTH,
            MetricId.SECTOR_BREADTH: MarketDimension.TREND_BREADTH,
            MetricId.INDUSTRY_POSITIVE_FLOW_SHARE_3D: MarketDimension.PERSISTENT_FLOW,
            MetricId.SECTOR_MAIN_FLOW_RATIO_3D: MarketDimension.PERSISTENT_FLOW,
            MetricId.AUTHENTICATED_EVENT_DIRECTION: MarketDimension.CATALYSTS,
            MetricId.INDUSTRY_OVERHEATING_SHARE: MarketDimension.CROWDING,
            MetricId.SECTOR_RETURN_TURNOVER_PERCENTILES: MarketDimension.CROWDING,
        }
        unsupported = {
            MarketDimension.VALUATION,
            MarketDimension.FUNDAMENTALS_EARNINGS,
        }
        if self.metric_id is None:
            if self.dimension not in unsupported:
                raise ValueError("metric id may be absent only for unsupported dimensions")
        elif metric_dimensions[self.metric_id] is not self.dimension:
            raise ValueError("metric id does not belong to its market dimension")
        if self.value is None:
            if self.unit is not None:
                raise ValueError("dimension unit must be absent when value is absent")
        else:
            if type(self.value) is not Decimal or not self.value.is_finite():
                raise ValueError("dimension value must be a finite exact Decimal or None")
            if self.unit == "percentage_points":
                if not Decimal("-100") <= self.value <= Decimal("100"):
                    raise ValueError("dimension value is outside the public market ratio bound")
            elif self.unit == "decimal_fraction":
                if not Decimal("-1") <= self.value <= Decimal("1"):
                    raise ValueError("dimension value is outside the public market ratio bound")
            else:
                raise ValueError("dimension unit must be an exact supported public ratio unit")
        if self.state is DimensionState.INSUFFICIENT_DATA and self.value is not None:
            raise ValueError("insufficient dimension cannot claim a measured value")
        if self.metric_id is None and (
            self.state is not DimensionState.INSUFFICIENT_DATA
            or self.value is not None
            or self.unit is not None
        ):
            raise ValueError("unsupported dimension must remain insufficient without a value")
        numeric_contracts = {
            MetricId.INDUSTRY_MEDIAN_PCT_CHANGE: (
                "percentage_points",
                POSITIVE_CHANGE,
                NEGATIVE_CHANGE,
            ),
            MetricId.SECTOR_PCT_CHANGE: (
                "percentage_points",
                POSITIVE_CHANGE,
                NEGATIVE_CHANGE,
            ),
            MetricId.INDUSTRY_AGGREGATE_BREADTH: (
                "decimal_fraction",
                POSITIVE_BREADTH,
                NEGATIVE_BREADTH,
            ),
            MetricId.SECTOR_BREADTH: (
                "decimal_fraction",
                POSITIVE_BREADTH,
                NEGATIVE_BREADTH,
            ),
        }
        if self.metric_id in numeric_contracts and self.state not in {
            DimensionState.INSUFFICIENT_DATA,
            DimensionState.CONFLICTED,
        }:
            expected_unit, positive_at, negative_at = numeric_contracts[self.metric_id]
            if self.value is None or self.unit != expected_unit:
                raise ValueError("metric unit and value do not match Policy V1")
            expected_state = (
                DimensionState.POSITIVE
                if self.value >= positive_at
                else DimensionState.NEGATIVE
                if self.value <= negative_at
                else DimensionState.NEUTRAL
            )
            if self.state is not expected_state:
                raise ValueError("metric state does not match Policy V1 thresholds")
        if self.metric_id is MetricId.INDUSTRY_OVERHEATING_SHARE and self.state not in {
            DimensionState.INSUFFICIENT_DATA,
            DimensionState.CONFLICTED,
        }:
            if self.value is None or self.unit != "decimal_fraction":
                raise ValueError("crowding metric unit and value do not match Policy V1")
            expected_state = (
                DimensionState.RISK_FLAG
                if self.value >= MARKET_CROWDING_SHARE
                else DimensionState.NEUTRAL
            )
            if self.state is not expected_state:
                raise ValueError("crowding metric state does not match Policy V1 thresholds")
        qualitative_metrics = {
            MetricId.INDUSTRY_POSITIVE_FLOW_SHARE_3D,
            MetricId.SECTOR_MAIN_FLOW_RATIO_3D,
            MetricId.AUTHENTICATED_EVENT_DIRECTION,
            MetricId.SECTOR_RETURN_TURNOVER_PERCENTILES,
        }
        if self.metric_id in qualitative_metrics and (
            self.value is not None or self.unit is not None
        ):
            raise ValueError("derived qualitative metric cannot claim a scalar value")
        if self.metric_id in {
            MetricId.INDUSTRY_POSITIVE_FLOW_SHARE_3D,
            MetricId.SECTOR_MAIN_FLOW_RATIO_3D,
            MetricId.AUTHENTICATED_EVENT_DIRECTION,
        } and self.state not in {
            DimensionState.POSITIVE,
            DimensionState.NEUTRAL,
            DimensionState.NEGATIVE,
            DimensionState.CONFLICTED,
            DimensionState.INSUFFICIENT_DATA,
        }:
            raise ValueError("derived direction metric state is not allowed by Policy V1")
        if self.metric_id is MetricId.SECTOR_RETURN_TURNOVER_PERCENTILES and self.state not in {
            DimensionState.RISK_FLAG,
            DimensionState.NEUTRAL,
            DimensionState.CONFLICTED,
            DimensionState.INSUFFICIENT_DATA,
        }:
            raise ValueError("sector crowding state is not allowed by Policy V1")
        _validate_utc_datetime(self.data_as_of, "dimension data time")
        _validate_utc_datetime(self.retrieved_at, "dimension retrieval time")
        if self.retrieved_at < self.data_as_of:
            raise ValueError("dimension retrieval time cannot precede data time")
        if type(self.source_tier) is not SourceTier or self.source_tier not in {
            SourceTier.TIER_1,
            SourceTier.TIER_2,
        }:
            raise ValueError("dimension source tier must be exact public Tier 1 or Tier 2")
        _validate_positive_int_tuple(
            self.source_attempt_ids,
            "dimension source attempt ids",
            allow_empty=False,
        )
        _validate_public_identifier_tuple(self.evidence_ids, "dimension evidence ids")
        if type(self.freshness) is not EvidenceFreshness:
            raise ValueError("dimension freshness must be exact EvidenceFreshness")
        if type(self.completeness) is not EvidenceCompleteness:
            raise ValueError("dimension completeness must be exact EvidenceCompleteness")
        _validate_public_identifier_tuple(self.conflict_ids, "dimension conflict ids")
        if bool(self.conflict_ids) != (self.state is DimensionState.CONFLICTED):
            raise ValueError("dimension conflict ids and state are inconsistent")
        if self.state is DimensionState.CONFLICTED and (
            self.completeness is not EvidenceCompleteness.PARTIAL
        ):
            raise ValueError("conflicted dimension completeness must be partial")
        if (self.completeness is EvidenceCompleteness.INSUFFICIENT) != (
            self.state is DimensionState.INSUFFICIENT_DATA
        ):
            raise ValueError("dimension completeness and insufficient state are inconsistent")

    def to_canonical_dict(self) -> dict:
        self.validate()
        return {
            "data_as_of": canonical_value(self.data_as_of),
            "dimension": self.dimension.value,
            "entity_id": self.entity_id,
            "evidence_ids": list(self.evidence_ids),
            "metric_id": None if self.metric_id is None else self.metric_id.value,
            "observation_id": self.observation_id,
            "retrieved_at": canonical_value(self.retrieved_at),
            "source_tier": self.source_tier.value,
            "source_attempt_ids": list(self.source_attempt_ids),
            "state": self.state.value,
            "unit": self.unit,
            "value": None if self.value is None else canonical_decimal(self.value),
            "freshness": self.freshness.value,
            "completeness": self.completeness.value,
            "conflict_ids": list(self.conflict_ids),
        }


@dataclass(frozen=True)
class QueryInterval:
    start_at: datetime
    end_at: datetime
    timezone_name: str

    def validate(self) -> None:
        _validate_exact_record(self, QueryInterval, "query interval")
        _validate_utc_datetime(self.start_at, "query interval start")
        _validate_utc_datetime(self.end_at, "query interval end")
        if self.end_at <= self.start_at:
            raise ValueError("query interval end must follow its start")
        if type(self.timezone_name) is not str or self.timezone_name != "Asia/Shanghai":
            raise ValueError("query interval timezone must be exactly Asia/Shanghai")

    def to_canonical_dict(self) -> dict:
        self.validate()
        return {
            "end_at": canonical_value(self.end_at),
            "start_at": canonical_value(self.start_at),
            "timezone_name": self.timezone_name,
        }


@dataclass(frozen=True)
class MarketEntity:
    entity_id: str
    entity_type: str
    canonical_name: str
    active_from: datetime
    active_until: Optional[datetime]
    evidence_ids: Tuple[str, ...]

    def validate(self) -> None:
        _validate_exact_record(self, MarketEntity, "market entity")
        validate_identifier(self.entity_id, "market entity id")
        validate_identifier(self.entity_type, "market entity type")
        validate_public_text(self.canonical_name, "market entity canonical name")
        _validate_utc_datetime(self.active_from, "market entity active from")
        _validate_optional_utc(self.active_until, "market entity active until")
        if self.active_until is not None and self.active_until <= self.active_from:
            raise ValueError("market entity active interval is invalid")
        _validate_public_identifier_tuple(self.evidence_ids, "market entity evidence ids")

    def to_canonical_dict(self) -> dict:
        self.validate()
        return {
            "active_from": canonical_value(self.active_from),
            "active_until": (
                None if self.active_until is None else canonical_value(self.active_until)
            ),
            "canonical_name": self.canonical_name,
            "entity_id": self.entity_id,
            "entity_type": self.entity_type,
            "evidence_ids": list(self.evidence_ids),
        }


@dataclass(frozen=True)
class EntityAlias:
    entity_id: str
    alias: str
    alias_type: str
    active_from: datetime
    active_until: Optional[datetime]
    evidence_ids: Tuple[str, ...]

    def validate(self) -> None:
        _validate_exact_record(self, EntityAlias, "entity alias")
        validate_identifier(self.entity_id, "alias entity id")
        validate_public_text(self.alias, "entity alias")
        validate_identifier(self.alias_type, "entity alias type")
        _validate_utc_datetime(self.active_from, "entity alias active from")
        _validate_optional_utc(self.active_until, "entity alias active until")
        if self.active_until is not None and self.active_until <= self.active_from:
            raise ValueError("entity alias active interval is invalid")
        _validate_public_identifier_tuple(self.evidence_ids, "entity alias evidence ids")

    def to_canonical_dict(self) -> dict:
        self.validate()
        return {
            "active_from": canonical_value(self.active_from),
            "active_until": (
                None if self.active_until is None else canonical_value(self.active_until)
            ),
            "alias": self.alias,
            "alias_type": self.alias_type,
            "entity_id": self.entity_id,
            "evidence_ids": list(self.evidence_ids),
        }


@dataclass(frozen=True)
class LineageEdge:
    edge_id: str
    from_item_id: str
    to_item_id: str
    kind: LineageKind
    evidence_ids: Tuple[str, ...]

    def validate(self) -> None:
        _validate_exact_record(self, LineageEdge, "lineage edge")
        for value, name in (
            (self.edge_id, "lineage edge id"),
            (self.from_item_id, "lineage source item id"),
            (self.to_item_id, "lineage target item id"),
        ):
            validate_identifier(value, name)
        if self.from_item_id == self.to_item_id:
            raise ValueError("lineage edge cannot reference one item twice")
        if type(self.kind) is not LineageKind:
            raise ValueError("lineage kind must be exact")
        _validate_public_identifier_tuple(self.evidence_ids, "lineage evidence ids")

    def to_canonical_dict(self) -> dict:
        self.validate()
        return {
            "edge_id": self.edge_id,
            "evidence_ids": list(self.evidence_ids),
            "from_item_id": self.from_item_id,
            "kind": self.kind.value,
            "to_item_id": self.to_item_id,
        }


@dataclass(frozen=True)
class NewsEvent:
    event_id: str
    event_type: EventType
    normalized_title: str
    supporting_item_ids: Tuple[str, ...]
    opposing_item_ids: Tuple[str, ...]
    correction_item_ids: Tuple[str, ...]
    retraction_item_ids: Tuple[str, ...]
    confidence_state: EventConfidenceState
    earliest_published_at: datetime
    latest_published_at: datetime
    integrity_state: IntegrityState
    superseded_by_event_id: Optional[str]
    invalidation_conditions: Tuple[str, ...]

    def validate(self) -> None:
        _validate_exact_record(self, NewsEvent, "news event")
        validate_identifier(self.event_id, "news event id")
        if type(self.event_type) is not EventType:
            raise ValueError("news event type must be an exact EventType")
        validate_public_text(self.normalized_title, "news event normalized title")
        for values, name in (
            (self.supporting_item_ids, "supporting item ids"),
            (self.opposing_item_ids, "opposing item ids"),
            (self.correction_item_ids, "correction item ids"),
            (self.retraction_item_ids, "retraction item ids"),
        ):
            _validate_public_identifier_tuple(values, name)
        all_item_ids = (
            self.supporting_item_ids
            + self.opposing_item_ids
            + self.correction_item_ids
            + self.retraction_item_ids
        )
        if not all_item_ids:
            raise ValueError("news event requires at least one item")
        if len(all_item_ids) != len(set(all_item_ids)):
            raise ValueError("news event item roles must not overlap")
        if type(self.confidence_state) is not EventConfidenceState:
            raise ValueError("news event confidence must be exact EventConfidenceState")
        if self.confidence_state is EventConfidenceState.SUFFICIENT and (
            not self.supporting_item_ids
            or self.opposing_item_ids
            or self.correction_item_ids
            or self.retraction_item_ids
        ):
            raise ValueError("sufficient event requires unopposed, uncorrected support")
        if self.confidence_state is EventConfidenceState.PARTIAL and (
            not self.supporting_item_ids or self.opposing_item_ids or self.retraction_item_ids
        ):
            raise ValueError("partial event requires support without opposition or retraction")
        if self.confidence_state is EventConfidenceState.CONFLICTED and (
            not self.supporting_item_ids or not self.opposing_item_ids or self.retraction_item_ids
        ):
            raise ValueError("conflicted event requires both support and opposition")
        if self.confidence_state is EventConfidenceState.INSUFFICIENT and (
            self.supporting_item_ids
            and not self.retraction_item_ids
            and self.integrity_state is not IntegrityState.SUPERSEDED
        ):
            raise ValueError("insufficient event cannot retain unretracted supporting evidence")
        _validate_utc_datetime(self.earliest_published_at, "event earliest publication")
        _validate_utc_datetime(self.latest_published_at, "event latest publication")
        if self.latest_published_at < self.earliest_published_at:
            raise ValueError("event publication interval is invalid")
        if type(self.integrity_state) is not IntegrityState:
            raise ValueError("event integrity state must be exact IntegrityState")
        if self.integrity_state is IntegrityState.ACTIVE and (
            self.correction_item_ids or self.retraction_item_ids
        ):
            raise ValueError("active event cannot contain correction or retraction roles")
        if self.integrity_state is IntegrityState.CORRECTED and (
            not self.correction_item_ids or self.retraction_item_ids
        ):
            raise ValueError("corrected event requires correction without retraction")
        if self.integrity_state is IntegrityState.RETRACTED and (
            not self.retraction_item_ids
            or self.confidence_state is not EventConfidenceState.INSUFFICIENT
        ):
            raise ValueError("retracted event requires retraction and insufficient confidence")
        if self.integrity_state is IntegrityState.UNKNOWN and (
            self.confidence_state is not EventConfidenceState.INSUFFICIENT
        ):
            raise ValueError("unknown event integrity requires insufficient confidence")
        if self.integrity_state is IntegrityState.SUPERSEDED:
            if (
                self.confidence_state is not EventConfidenceState.INSUFFICIENT
                or self.correction_item_ids
                or self.retraction_item_ids
            ):
                raise ValueError("superseded event requires insufficient historical support only")
            validate_identifier(
                self.superseded_by_event_id,
                "superseded event replacement id",
            )
            if self.superseded_by_event_id == self.event_id:
                raise ValueError("superseded event replacement must not reference itself")
        elif self.superseded_by_event_id is not None:
            raise ValueError("non-superseded event cannot declare a replacement event id")
        validate_public_text_tuple(self.invalidation_conditions, "event invalidation conditions")

    def to_canonical_dict(self) -> dict:
        self.validate()
        return {
            "confidence_state": self.confidence_state.value,
            "correction_item_ids": list(self.correction_item_ids),
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "earliest_published_at": canonical_value(self.earliest_published_at),
            "latest_published_at": canonical_value(self.latest_published_at),
            "integrity_state": self.integrity_state.value,
            "invalidation_conditions": list(self.invalidation_conditions),
            "normalized_title": self.normalized_title,
            "opposing_item_ids": list(self.opposing_item_ids),
            "retraction_item_ids": list(self.retraction_item_ids),
            "supporting_item_ids": list(self.supporting_item_ids),
            "superseded_by_event_id": self.superseded_by_event_id,
        }


@dataclass(frozen=True)
class EventEntityLink:
    link_id: str
    event_id: str
    entity_id: str
    relationship: EventEntityRelationship
    evidence_ids: Tuple[str, ...]

    def validate(self) -> None:
        _validate_exact_record(self, EventEntityLink, "event entity link")
        validate_identifier(self.link_id, "event entity link id")
        validate_identifier(self.event_id, "linked event id")
        validate_identifier(self.entity_id, "linked entity id")
        if type(self.relationship) is not EventEntityRelationship:
            raise ValueError("event entity relationship must be exact")
        _validate_public_identifier_tuple(self.evidence_ids, "event entity evidence ids")

    def to_canonical_dict(self) -> dict:
        self.validate()
        return {
            "entity_id": self.entity_id,
            "event_id": self.event_id,
            "link_id": self.link_id,
            "evidence_ids": list(self.evidence_ids),
            "relationship": self.relationship.value,
        }


@dataclass(frozen=True)
class MarketStateSnapshot:
    market_state: MarketShadowState
    sector_states: Tuple[Tuple[str, SectorShadowState], ...]
    dimensions: Tuple[DimensionObservation, ...]
    supporting_observation_ids: Tuple[str, ...]
    opposing_observation_ids: Tuple[str, ...]
    unknown_dimensions: Tuple[MarketDimension, ...]
    invalidation_conditions: Tuple[str, ...]
    next_review_at: datetime
    policy_checksum: str

    def validate(self) -> None:
        _validate_exact_record(self, MarketStateSnapshot, "market state snapshot")
        if type(self.market_state) is not MarketShadowState:
            raise ValueError("market shadow state must be exact")
        if type(self.sector_states) is not tuple or len(self.sector_states) > MAX_TUPLE_ITEMS:
            raise ValueError("sector states must be a bounded exact tuple")
        sector_ids = []
        for entry in self.sector_states:
            if type(entry) is not tuple or len(entry) != 2:
                raise ValueError("each sector state must be an exact pair")
            sector_id, state = entry
            validate_identifier(sector_id, "sector state id")
            if type(state) is not SectorShadowState:
                raise ValueError("sector shadow state must be exact")
            sector_ids.append(sector_id)
        if sector_ids != sorted(sector_ids) or len(sector_ids) != len(set(sector_ids)):
            raise ValueError("sector states must use unique ascending sector ids")
        _validate_record_tuple(
            self.dimensions,
            DimensionObservation,
            "market dimensions",
            "observation_id",
        )
        _validate_public_identifier_tuple(
            self.supporting_observation_ids, "market supporting observation ids"
        )
        _validate_public_identifier_tuple(
            self.opposing_observation_ids, "market opposing observation ids"
        )
        if set(self.supporting_observation_ids).intersection(self.opposing_observation_ids):
            raise ValueError("market supporting and opposing observations must not overlap")
        observation_ids = {item.observation_id for item in self.dimensions}
        if not set(self.supporting_observation_ids + self.opposing_observation_ids).issubset(
            observation_ids
        ):
            raise ValueError("market observation references must resolve to dimensions")
        if type(self.unknown_dimensions) is not tuple:
            raise ValueError("unknown dimensions must be an exact tuple")
        if any(type(item) is not MarketDimension for item in self.unknown_dimensions):
            raise ValueError("unknown dimensions must contain exact MarketDimension values")
        if len(self.unknown_dimensions) != len(set(self.unknown_dimensions)):
            raise ValueError("unknown dimensions must not contain duplicates")
        validate_public_text_tuple(self.invalidation_conditions, "market invalidation conditions")
        _validate_utc_datetime(self.next_review_at, "market next review time")
        validate_checksum(self.policy_checksum, "market policy checksum")

    def to_canonical_dict(self) -> dict:
        self.validate()
        return {
            "dimensions": [item.to_canonical_dict() for item in self.dimensions],
            "invalidation_conditions": list(self.invalidation_conditions),
            "market_state": self.market_state.value,
            "next_review_at": canonical_value(self.next_review_at),
            "opposing_observation_ids": list(self.opposing_observation_ids),
            "policy_checksum": self.policy_checksum,
            "sector_states": [
                {"sector_id": sector_id, "state": state.value}
                for sector_id, state in self.sector_states
            ],
            "supporting_observation_ids": list(self.supporting_observation_ids),
            "unknown_dimensions": [item.value for item in self.unknown_dimensions],
        }


@dataclass(frozen=True)
class IntelligenceSnapshot:
    workflow: IntelligenceWorkflow
    request_id: str
    request_run_id: int
    interval: QueryInterval
    subject_fund_code: Optional[str]
    entities: Tuple[MarketEntity, ...]
    item_ids: Tuple[str, ...]
    source_attempt_ids: Tuple[int, ...]
    lineage_edge_ids: Tuple[str, ...]
    event_ids: Tuple[str, ...]
    event_entity_links: Tuple[EventEntityLink, ...]
    market_state: MarketStateSnapshot
    fund_relevance_link_ids: Tuple[str, ...]
    conflicts: Tuple[str, ...]
    missing_evidence: Tuple[str, ...]
    created_at: datetime
    exact_amount_available: bool

    def validate(self) -> None:
        _validate_exact_record(self, IntelligenceSnapshot, "intelligence snapshot")
        if type(self.workflow) is not IntelligenceWorkflow:
            raise ValueError("intelligence workflow must be exact")
        validate_request_id(self.request_id)
        if type(self.request_run_id) is not int or self.request_run_id <= 0:
            raise ValueError("request run id must be a positive exact integer")
        if type(self.interval) is not QueryInterval:
            raise ValueError("snapshot interval must be exact")
        self.interval.validate()
        if self.subject_fund_code is not None and (
            type(self.subject_fund_code) is not str
            or _FUND_CODE_PATTERN.fullmatch(self.subject_fund_code) is None
        ):
            raise ValueError("subject fund code must be exactly six ASCII digits or None")
        if self.workflow is IntelligenceWorkflow.FUND_INTELLIGENCE:
            if self.subject_fund_code is None:
                raise ValueError("fund intelligence requires a subject fund code")
        elif self.subject_fund_code is not None:
            raise ValueError("only fund intelligence may declare a subject fund code")
        _validate_record_tuple(self.entities, MarketEntity, "snapshot entities", "entity_id")
        _validate_public_identifier_tuple(self.item_ids, "snapshot item ids")
        _validate_positive_int_tuple(
            self.source_attempt_ids,
            "snapshot source attempt ids",
            allow_empty=False,
        )
        _validate_public_identifier_tuple(self.lineage_edge_ids, "snapshot lineage edge ids")
        _validate_public_identifier_tuple(self.event_ids, "snapshot event ids")
        _validate_record_tuple(
            self.event_entity_links,
            EventEntityLink,
            "snapshot event entity links",
            "link_id",
        )
        if type(self.market_state) is not MarketStateSnapshot:
            raise ValueError("snapshot market state must be exact")
        self.market_state.validate()
        _validate_public_identifier_tuple(
            self.fund_relevance_link_ids,
            "snapshot fund relevance link ids",
        )
        _validate_public_identifier_tuple(self.conflicts, "snapshot conflicts")
        _validate_public_identifier_tuple(self.missing_evidence, "snapshot missing evidence")
        _validate_utc_datetime(self.created_at, "snapshot creation time")
        if self.created_at < self.interval.end_at:
            raise ValueError("snapshot creation cannot precede its query interval end")
        if type(self.exact_amount_available) is not bool or self.exact_amount_available:
            raise ValueError("intelligence snapshot exact amount availability must be false")

        item_ids = set(self.item_ids)
        entity_ids = {item.entity_id for item in self.entities}
        event_ids = set(self.event_ids)
        referenced_items = set()
        source_attempt_ids = set(self.source_attempt_ids)
        for entity in self.entities:
            referenced_items.update(entity.evidence_ids)
        for observation in self.market_state.dimensions:
            referenced_items.update(observation.evidence_ids)
            if observation.entity_id not in entity_ids:
                raise ValueError("dimension entity id must resolve to a snapshot entity")
            if not set(observation.source_attempt_ids).issubset(source_attempt_ids):
                raise ValueError("dimension source attempt ids must resolve to snapshot attempts")
        link_ids = {item.link_id for item in self.event_entity_links}
        for link in self.event_entity_links:
            referenced_items.update(link.evidence_ids)
            if link.entity_id not in entity_ids or link.event_id not in event_ids:
                raise ValueError("event entity links must resolve to snapshot entities and events")
        if not set(self.fund_relevance_link_ids).issubset(link_ids):
            raise ValueError("fund relevance link ids must be a subset of event entity links")
        if self.workflow is not IntelligenceWorkflow.FUND_INTELLIGENCE and (
            self.fund_relevance_link_ids
        ):
            raise ValueError("fund relevance is allowed only for fund intelligence")
        if not referenced_items.issubset(item_ids):
            raise ValueError("snapshot evidence references must resolve to item ids")

    def to_canonical_dict(self) -> dict:
        self.validate()
        return {
            "conflicts": list(self.conflicts),
            "created_at": canonical_value(self.created_at),
            "entities": [item.to_canonical_dict() for item in self.entities],
            "event_entity_links": [item.to_canonical_dict() for item in self.event_entity_links],
            "event_ids": list(self.event_ids),
            "exact_amount_available": self.exact_amount_available,
            "fund_relevance_link_ids": list(self.fund_relevance_link_ids),
            "interval": self.interval.to_canonical_dict(),
            "item_ids": list(self.item_ids),
            "source_attempt_ids": list(self.source_attempt_ids),
            "lineage_edge_ids": list(self.lineage_edge_ids),
            "market_state": self.market_state.to_canonical_dict(),
            "missing_evidence": list(self.missing_evidence),
            "request_id": self.request_id,
            "request_run_id": self.request_run_id,
            "subject_fund_code": self.subject_fund_code,
            "workflow": self.workflow.value,
        }

    def canonical_json(self) -> bytes:
        return canonical_json_bytes(self)

    def checksum(self) -> str:
        return hashlib.sha256(self.canonical_json()).hexdigest()


@dataclass(frozen=True)
class IntelligenceReport:
    snapshot: IntelligenceSnapshot
    terminal_status: RequestTerminalStatus
    omitted_work: Tuple[str, ...]
    beginner_explanation_zh: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "beginner_explanation_zh",
            _freeze_public_tree(self.beginner_explanation_zh),
        )

    def validate(self) -> None:
        _validate_exact_record(self, IntelligenceReport, "intelligence report")
        if type(self.snapshot) is not IntelligenceSnapshot:
            raise ValueError("report snapshot must be exact")
        self.snapshot.validate()
        if type(self.terminal_status) is not RequestTerminalStatus or self.terminal_status not in {
            RequestTerminalStatus.COMPLETE,
            RequestTerminalStatus.PARTIAL,
        }:
            raise ValueError("report terminal status must be complete or partial")
        _validate_public_identifier_tuple(self.omitted_work, "report omitted work")
        if (self.terminal_status is RequestTerminalStatus.COMPLETE and self.omitted_work) or (
            self.terminal_status is RequestTerminalStatus.PARTIAL and not self.omitted_work
        ):
            raise ValueError("report terminal status and omitted work are inconsistent")
        if type(self.beginner_explanation_zh) is not _MAPPING_PROXY_TYPE:
            raise ValueError("beginner explanation must be an immutable exact mapping")
        _validate_public_tree(self.beginner_explanation_zh, "beginner_explanation_zh")

    def to_canonical_dict(self) -> dict:
        self.validate()
        return {
            "beginner_explanation_zh": _canonical_public_tree(self.beginner_explanation_zh),
            "omitted_work": list(self.omitted_work),
            "snapshot": self.snapshot.to_canonical_dict(),
            "terminal_status": self.terminal_status.value,
        }

    def canonical_json(self) -> bytes:
        return canonical_json_bytes(self)
