from __future__ import annotations

from dataclasses import replace

import pytest

from kunjin.funds.official_domains import (
    OFFICIAL_SOURCE_REGISTRATIONS,
    OFFICIAL_SOURCE_REGISTRY_VERSION,
    official_source_registry_checksum,
)


def test_official_source_registry_v1_checksum_is_frozen() -> None:
    assert OFFICIAL_SOURCE_REGISTRY_VERSION == "1"
    assert official_source_registry_checksum() == (
        "557cac191734fbdd214ff24dabfc5afa8e3c99c1ab8ac30f230a846684c3fc9e"
    )


def test_official_source_registry_requires_canonical_order() -> None:
    duplicate = (*OFFICIAL_SOURCE_REGISTRATIONS, OFFICIAL_SOURCE_REGISTRATIONS[0])
    with pytest.raises(ValueError, match="canonical"):
        official_source_registry_checksum(duplicate)


@pytest.mark.parametrize(
    "change",
    (
        {"identity": "另一基金管理人"},
        {"identity_aliases": ("另一别名",)},
        {
            "accepted_hosts": ("example.com",),
            "document_index_url_template": "https://example.com/{fund_code}",
        },
        {"binds_fund_identity": False},
        {"requires_publication_date": False},
    ),
)
def test_official_source_registry_checksum_detects_contract_drift(change) -> None:
    changed = replace(OFFICIAL_SOURCE_REGISTRATIONS[0], **change)
    assert official_source_registry_checksum((changed,)) != official_source_registry_checksum()
