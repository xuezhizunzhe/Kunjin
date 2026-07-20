from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, fields
from types import MappingProxyType
from typing import Mapping, Tuple
from urllib.parse import urlparse

FUND_CODE_PATTERN = re.compile(r"^\d{6}$")
OFFICIAL_SOURCE_REGISTRY_VERSION = "1"
OFFICIAL_SOURCE_REGISTRY_V1_GOLDEN_CHECKSUM = (
    "557cac191734fbdd214ff24dabfc5afa8e3c99c1ab8ac30f230a846684c3fc9e"
)


@dataclass(frozen=True)
class OfficialSourceRegistration:
    registration_id: str
    identity: str
    source_kind: str
    accepted_hosts: Tuple[str, ...]
    document_index_url_template: str
    identity_aliases: Tuple[str, ...] = ()
    binds_fund_identity: bool = False
    requires_publication_date: bool = False

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if type(self) is not OfficialSourceRegistration:
            raise ValueError("official source registration subclasses are not accepted")
        expected_fields = {field.name for field in fields(OfficialSourceRegistration)}
        if set(vars(self)) != expected_fields:
            raise ValueError("official source registration has unexpected dataclass state")
        if type(self.registration_id) is not str or not self.registration_id:
            raise ValueError("official registration id must be non-empty ASCII")
        if not self.registration_id.isascii():
            raise ValueError("official registration id must be non-empty ASCII")
        if type(self.identity) is not str or not self.identity.strip():
            raise ValueError("official source identity is required")
        if type(self.identity_aliases) is not tuple or any(
            type(alias) is not str or not alias.strip() for alias in self.identity_aliases
        ):
            raise ValueError("official source identity aliases must be immutable text")
        canonical_aliases = tuple(sorted(set(self.identity_aliases)))
        if canonical_aliases != self.identity_aliases:
            raise ValueError("official source identity aliases must be unique and sorted")
        normalized_identity = _normalized_identity(self.identity)
        if any(
            _normalized_identity(alias) == normalized_identity for alias in self.identity_aliases
        ):
            raise ValueError("official source identity alias duplicates canonical identity")
        if type(self.binds_fund_identity) is not bool:
            raise ValueError("official source fund identity binding flag must be exact bool")
        if type(self.requires_publication_date) is not bool:
            raise ValueError("official source publication-date flag must be exact bool")
        if type(self.source_kind) is not str or self.source_kind not in {
            "fund_manager",
            "index_provider",
        }:
            raise ValueError("official source kind is invalid")
        if type(self.accepted_hosts) is not tuple or any(
            type(host) is not str for host in self.accepted_hosts
        ):
            raise ValueError("official source hosts must be an immutable string tuple")
        canonical_hosts = tuple(sorted(set(self.accepted_hosts)))
        if not self.accepted_hosts or canonical_hosts != self.accepted_hosts:
            raise ValueError("official source hosts must be unique and sorted")
        for host in self.accepted_hosts:
            if host != host.lower().rstrip(".") or ":" in host or "/" in host:
                raise ValueError("official source host must be a canonical hostname")
        if type(self.document_index_url_template) is not str:
            raise ValueError("official document index template must be exact text")
        probe = self.document_index_url_template.format(fund_code="000000", page=1)
        parsed = urlparse(probe)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or parsed.hostname.lower().rstrip(".") not in self.accepted_hosts
        ):
            raise ValueError("official document index must use a registered HTTPS host")

    def index_url(self, fund_code: str, page: int) -> str:
        self.validate()
        if type(fund_code) is not str or not FUND_CODE_PATTERN.fullmatch(fund_code):
            raise ValueError("official document index requires a six-digit fund code")
        if type(page) is not int or page <= 0:
            raise ValueError("official document index page must be positive")
        if page > 1 and "{page}" not in self.document_index_url_template:
            raise ValueError("official document index is not paginated")
        return self.document_index_url_template.format(fund_code=fund_code, page=page)

    def matches_identity(self, value: str) -> bool:
        self.validate()
        if type(value) is not str or not value.strip():
            return False
        normalized = _normalized_identity(value)
        return normalized in {
            _normalized_identity(self.identity),
            *(_normalized_identity(alias) for alias in self.identity_aliases),
        }


def _normalized_identity(value: str) -> str:
    return "".join(value.split()).casefold()


# A domain is trusted only together with one of its audited publisher names.
REGULATOR_AND_EXCHANGE_DOMAINS: Mapping[str, Tuple[str, ...]] = MappingProxyType(
    {
        "www.csrc.gov.cn": ("中国证券监督管理委员会", "中国证监会"),
        "www.sse.com.cn": ("上海证券交易所", "上交所"),
        "www.szse.cn": ("深圳证券交易所", "深交所"),
        "www.cninfo.com.cn": ("巨潮资讯网",),
    }
)


# Entries require an audited manager identity and official-link source. The
# initial entry supports the fund used by KunJin's real portfolio workflow.
FUND_COMPANY_DOMAINS: Mapping[str, str] = MappingProxyType(
    {
        "www.fund001.com": "交银施罗德基金管理有限公司",
    }
)


INDEX_PROVIDER_DOMAINS: Mapping[str, str] = MappingProxyType(
    {
        "www.csindex.com.cn": "中证指数有限公司",
    }
)


OFFICIAL_SOURCE_REGISTRATIONS: Tuple[OfficialSourceRegistration, ...] = (
    OfficialSourceRegistration(
        registration_id="fund001",
        identity="交银施罗德基金管理有限公司",
        source_kind="fund_manager",
        accepted_hosts=("www.fund001.com",),
        document_index_url_template=("https://www.fund001.com/fund/{fund_code}/sxxpl.shtml"),
        identity_aliases=("交银施罗德基金",),
        binds_fund_identity=True,
        requires_publication_date=True,
    ),
)


def _canonical_registration(value: OfficialSourceRegistration) -> dict:
    if type(value) is not OfficialSourceRegistration:
        raise ValueError("official source registry entries must be exact")
    value.validate()
    return {
        "accepted_hosts": list(value.accepted_hosts),
        "binds_fund_identity": value.binds_fund_identity,
        "document_index_url_template": value.document_index_url_template,
        "identity": value.identity,
        "identity_aliases": list(value.identity_aliases),
        "registration_id": value.registration_id,
        "requires_publication_date": value.requires_publication_date,
        "source_kind": value.source_kind,
    }


def official_source_registry_checksum(
    registrations: Tuple[OfficialSourceRegistration, ...] = OFFICIAL_SOURCE_REGISTRATIONS,
) -> str:
    if type(registrations) is not tuple:
        raise ValueError("official source registry must be an exact tuple")
    identities = tuple(item.registration_id for item in registrations)
    if identities != tuple(sorted(set(identities))):
        raise ValueError("official source registry must be in canonical unique order")
    payload = {
        "registrations": [_canonical_registration(item) for item in registrations],
        "version": OFFICIAL_SOURCE_REGISTRY_VERSION,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


if official_source_registry_checksum() != OFFICIAL_SOURCE_REGISTRY_V1_GOLDEN_CHECKSUM:
    raise RuntimeError("official source registry V1 checksum drifted")
