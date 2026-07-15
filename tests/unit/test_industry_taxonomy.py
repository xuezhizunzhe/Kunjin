from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal

import pytest

import kunjin.funds.industry_taxonomy as industry_taxonomy
from kunjin.funds.industry_taxonomy import (
    PRODUCTION_TAXONOMY_MAPPINGS,
    SW_LEVEL1_2021,
    IndustryDistributionRow,
    IndustryTaxonomyMapping,
    RecognizedIndustryTaxonomy,
    ValidatedIndustryDistribution,
    validate_industry_distribution,
)


def _canonical_mapping_json(
    *,
    metadata: RecognizedIndustryTaxonomy = SW_LEVEL1_2021,
    source_url: str = "https://example.com/sw-level1-2021.json",
    published_at: date = date(2021, 12, 1),
    entries: tuple[tuple[str, str, tuple[str, ...]], ...] = (
        ("801080", "电子", ()),
        ("801780", "银行", ("银行业",)),
    ),
) -> str:
    payload = {
        "entries": [
            {"aliases": list(aliases), "code": code, "name": name}
            for code, name, aliases in entries
        ],
        "published_at": published_at.isoformat(),
        "source_url": source_url,
        "taxonomy_id": metadata.taxonomy_id,
        "version": metadata.version,
    }
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _mapping(
    *,
    metadata: RecognizedIndustryTaxonomy = SW_LEVEL1_2021,
    source_url: str = "https://example.com/sw-level1-2021.json",
    published_at: date = date(2021, 12, 1),
    entries: tuple[tuple[str, str, tuple[str, ...]], ...] = (
        ("801080", "电子", ()),
        ("801780", "银行", ("银行业",)),
    ),
) -> IndustryTaxonomyMapping:
    canonical_json = _canonical_mapping_json(
        metadata=metadata,
        source_url=source_url,
        published_at=published_at,
        entries=entries,
    )
    return IndustryTaxonomyMapping(
        metadata=metadata,
        source_url=source_url,
        published_at=published_at,
        entries=entries,
        canonical_json=canonical_json,
        checksum=hashlib.sha256(canonical_json.encode("ascii")).hexdigest(),
    )


def _row(
    rank: int,
    code: str,
    name: str,
    weight: str,
    *,
    standard: str = "申万一级行业分类（2021）",
    unit: str = "percent",
) -> IndustryDistributionRow:
    return IndustryDistributionRow(
        classification_standard=standard,
        industry_code=code,
        industry_name=name,
        rank=rank,
        weight=Decimal(weight),
        unit=unit,
    )


def _rows() -> tuple[IndustryDistributionRow, ...]:
    return (
        _row(1, "801780", "银行", "12.50"),
        _row(2, "801080", "电子", "8.35"),
    )


def test_exact_frozen_records_and_recognized_metadata() -> None:
    assert SW_LEVEL1_2021 == RecognizedIndustryTaxonomy(
        taxonomy_id="sw_level1_2021",
        version="2021",
        source_aliases=("申万一级行业分类(2021)", "申万一级行业分类（2021）"),
        expected_code_pattern=r"801[0-9]{3}",
    )
    with pytest.raises(AttributeError):
        SW_LEVEL1_2021.version = "2024"  # type: ignore[misc]


def test_production_registry_enables_no_mapping() -> None:
    assert PRODUCTION_TAXONOMY_MAPPINGS == ()
    assert validate_industry_distribution(rows=_rows(), complete_scope=True) is None


def test_complete_test_mapping_validates_one_distribution() -> None:
    mapping = _mapping()

    validated = validate_industry_distribution(
        rows=_rows(),
        complete_scope=True,
        mappings=(mapping,),
    )

    assert validated == ValidatedIndustryDistribution(
        taxonomy_id="sw_level1_2021",
        mapping_checksum=mapping.checksum,
        rows=_rows(),
    )


def test_bounded_normalization_accepts_exact_aliases_and_audited_name_aliases() -> None:
    rows = (
        _row(
            1,
            "801780",
            " 银行业 ",
            "12.50",
            standard="申万一级行业分类(2021)",
        ),
        _row(2, "801080", "电子", "8.35", standard="申万一级行业分类(2021)"),
    )

    assert (
        validate_industry_distribution(
            rows=rows,
            complete_scope=True,
            mappings=(_mapping(),),
        )
        is not None
    )


def test_incomplete_or_unsupported_evidence_returns_none() -> None:
    assert (
        validate_industry_distribution(
            rows=_rows(), complete_scope=False, mappings=(_mapping(),)
        )
        is None
    )
    assert (
        validate_industry_distribution(rows=(), complete_scope=True, mappings=(_mapping(),))
        is None
    )
    unsupported = tuple(
        replace(row, classification_standard="中证行业分类") for row in _rows()
    )
    assert (
        validate_industry_distribution(
            rows=unsupported, complete_scope=True, mappings=(_mapping(),)
        )
        is None
    )


@pytest.mark.parametrize(
    "rows",
    (
        (_row(1, "", "银行", "12.50"),),
        (_row(1, "80178", "银行", "12.50"),),
        (_row(1, "801999", "银行", "12.50"),),
        (_row(1, "801780", "电子", "12.50"),),
        (
            _row(1, "801780", "银行", "12.50"),
            _row(2, "801780", "银行业", "8.35"),
        ),
        (
            _row(1, "801780", "银行", "12.50"),
            _row(2, "801080", "银行", "8.35"),
        ),
        (
            _row(1, "801780", "银行", "12.50"),
            _row(1, "801080", "电子", "8.35"),
        ),
        (
            _row(1, "801780", "银行", "12.50"),
            _row(3, "801080", "电子", "8.35"),
        ),
        (
            _row(1, "801780", "银行", "8.35"),
            _row(2, "801080", "电子", "12.50"),
        ),
        (
            _row(1, "801780", "银行", "12.50"),
            _row(2, "801080", "电子", "12.50"),
        ),
        (
            _row(1, "801780", "银行", "12.50", unit="percent"),
            _row(2, "801080", "电子", "8.35", unit="fraction"),
        ),
        (
            _row(1, "801780", "银行", "12.50"),
            _row(
                2,
                "801080",
                "电子",
                "8.35",
                standard="申万一级行业分类(2021)",
            ),
        ),
    ),
)
def test_invalid_or_ambiguous_distribution_returns_none(
    rows: tuple[IndustryDistributionRow, ...],
) -> None:
    assert (
        validate_industry_distribution(
            rows=rows,
            complete_scope=True,
            mappings=(_mapping(),),
        )
        is None
    )


@pytest.mark.parametrize(
    "row",
    (
        replace(_rows()[0], classification_standard="申万一级\u200b行业分类（2021）"),
        replace(_rows()[0], industry_code="801\u200b780"),
        replace(_rows()[0], industry_name="银\u200b行"),
        replace(_rows()[0], industry_name="银行\ufe0f"),
        replace(_rows()[0], industry_name="银行\ufff0"),
        replace(_rows()[0], unit="per\u0007cent"),
    ),
)
def test_unsafe_unicode_in_caller_records_is_rejected(
    row: IndustryDistributionRow,
) -> None:
    with pytest.raises(ValueError, match="unsafe Unicode"):
        validate_industry_distribution(
            rows=(row,), complete_scope=True, mappings=(_mapping(),)
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("classification_standard", b"standard"),
        ("industry_code", 801780),
        ("industry_name", None),
        ("rank", True),
        ("rank", 0),
        ("weight", "12.50"),
        ("weight", Decimal("NaN")),
        ("weight", Decimal("-0.01")),
        ("weight", Decimal("100.01")),
        ("unit", None),
    ),
)
def test_malformed_caller_row_raises_value_error(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        validate_industry_distribution(
            rows=(replace(_rows()[0], **{field: value}),),
            complete_scope=True,
            mappings=(_mapping(),),
        )


def test_mutable_or_subclassed_caller_records_are_rejected() -> None:
    class RowSubclass(IndustryDistributionRow):
        pass

    subclassed = RowSubclass(**_rows()[0].__dict__)
    for rows in (list(_rows()), (subclassed,)):
        with pytest.raises(ValueError):
            validate_industry_distribution(
                rows=rows,  # type: ignore[arg-type]
                complete_scope=True,
                mappings=(_mapping(),),
            )
    with pytest.raises(ValueError):
        validate_industry_distribution(
            rows=_rows(), complete_scope=1, mappings=(_mapping(),)  # type: ignore[arg-type]
        )


def test_caller_row_rejects_injected_dataclass_state() -> None:
    row = replace(_rows()[0])
    object.__setattr__(row, "unexpected_mutable_state", [])

    with pytest.raises(ValueError, match="exact record state"):
        validate_industry_distribution(
            rows=(row,), complete_scope=True, mappings=(_mapping(),)
        )


def test_registry_rejects_injected_metadata_dataclass_state() -> None:
    metadata = replace(SW_LEVEL1_2021)
    object.__setattr__(metadata, "unexpected_mutable_state", [])

    with pytest.raises(ValueError, match="exact record state"):
        validate_industry_distribution(
            rows=_rows(),
            complete_scope=True,
            mappings=(_mapping(metadata=metadata),),
        )


def test_registry_rejects_injected_mapping_dataclass_state() -> None:
    mapping = _mapping()
    object.__setattr__(mapping, "unexpected_mutable_state", [])

    with pytest.raises(ValueError, match="exact record state"):
        validate_industry_distribution(
            rows=_rows(), complete_scope=True, mappings=(mapping,)
        )


@pytest.mark.parametrize("record_kind", ("row", "mapping"))
def test_records_reject_custom_dict_hiding_injected_state(record_kind: str) -> None:
    class HidingState(dict[str, object]):
        def __iter__(self):  # type: ignore[no-untyped-def]
            return (
                key
                for key in super().__iter__()
                if key != "unexpected_mutable_state"
            )

    record = replace(_rows()[0]) if record_kind == "row" else _mapping()
    hidden_state = HidingState(vars(record))
    hidden_state["unexpected_mutable_state"] = []
    object.__setattr__(record, "__dict__", hidden_state)

    with pytest.raises(ValueError, match="exact record state"):
        if record_kind == "row":
            validate_industry_distribution(
                rows=(record,),  # type: ignore[arg-type]
                complete_scope=True,
                mappings=(_mapping(),),
            )
        else:
            validate_industry_distribution(
                rows=_rows(),
                complete_scope=True,
                mappings=(record,),  # type: ignore[arg-type]
            )


def test_validated_distribution_rejects_injected_dataclass_state() -> None:
    validated = validate_industry_distribution(
        rows=_rows(), complete_scope=True, mappings=(_mapping(),)
    )
    assert validated is not None
    object.__setattr__(validated, "unexpected_mutable_state", [])

    with pytest.raises(ValueError, match="exact record state"):
        industry_taxonomy._validate_validated_distribution(validated)


@pytest.mark.parametrize(
    "mapping",
    (
        replace(_mapping(), checksum="0" * 64),
        replace(_mapping(), checksum="A" * 64),
        replace(_mapping(), canonical_json=_mapping().canonical_json + "\n"),
        replace(_mapping(), entries=tuple(reversed(_mapping().entries))),
        replace(
            _mapping(),
            entries=(("801080", "电子", ()), ("801780", "银行", ("银行业", "银 行"))),
        ),
        replace(_mapping(), source_url="http://example.com/taxonomy.json"),
        replace(_mapping(), published_at=datetime(2021, 12, 1)),
    ),
)
def test_invalid_registry_mapping_raises_value_error(
    mapping: IndustryTaxonomyMapping,
) -> None:
    with pytest.raises(ValueError):
        validate_industry_distribution(
            rows=_rows(), complete_scope=True, mappings=(mapping,)
        )


@pytest.mark.parametrize(
    "source_url",
    (
        " https://example.com/sw-level1-2021.json",
        "https://example.com/sw-level1-2021.json ",
        "https://example.com/ＳＷ-level1-2021.json",
        "https://example.com:invalid/sw-level1-2021.json",
        "https://exa mple.com/sw.json",
        "https://example.com\\evil/sw.json",
        "https://user@example.com/sw.json",
        "https://example.com/行业.json",
        "https://example.com/\u0007sw.json",
        "https://example.com /sw.json",
        "https://[example.com]/sw.json",
        "https://[1.2.3.4]/sw.json",
        "https://[v1.fe80]/sw.json",
        "https://[2001:db8::1]suffix/sw.json",
        "https://192.0.2.1/sw.json",
        "https://[2001:db8::1]/sw.json",
        "https://example.com:443/sw.json",
        "https://example.com/sw.json?version=1",
        "https://example.com/sw.json?",
        "https://example.com/sw.json#official",
        "https://example.com/sw.json#",
        "https://example.com",
        "https://Example.com/sw.json",
        "HTTPS://example.com/sw.json",
        "HtTpS://example.com/sw.json",
        "https://0x7f000001/sw.json",
        "https://0x7f.0.0.1/sw.json",
    ),
)
def test_registry_rejects_noncanonical_source_url(
    source_url: str,
) -> None:
    with pytest.raises(ValueError, match="source URL"):
        validate_industry_distribution(
            rows=_rows(),
            complete_scope=True,
            mappings=(_mapping(source_url=source_url),),
        )


@pytest.mark.parametrize(
    "source_url",
    (
        "https://example.com/sw.json",
        "https://example.com/",
        "https://xn--fsqu00a.xn--0zwm56d/sw.json",
    ),
)
def test_registry_accepts_canonical_dns_source_url(
    source_url: str,
) -> None:
    assert (
        validate_industry_distribution(
            rows=_rows(),
            complete_scope=True,
            mappings=(_mapping(source_url=source_url),),
        )
        is not None
    )


def test_noncanonical_mapping_json_is_rejected_even_with_matching_checksum() -> None:
    canonical = _mapping().canonical_json
    noncanonical = json.dumps(json.loads(canonical), ensure_ascii=True, indent=2)
    mapping = replace(
        _mapping(),
        canonical_json=noncanonical,
        checksum=hashlib.sha256(noncanonical.encode("ascii")).hexdigest(),
    )

    with pytest.raises(ValueError, match="canonical"):
        validate_industry_distribution(
            rows=_rows(), complete_scope=True, mappings=(mapping,)
        )


def test_registry_rejects_mutable_subclassed_and_conflicting_records() -> None:
    class MetadataSubclass(RecognizedIndustryTaxonomy):
        pass

    class MappingSubclass(IndustryTaxonomyMapping):
        pass

    subclassed_metadata = MetadataSubclass(**SW_LEVEL1_2021.__dict__)
    metadata_mapping = replace(_mapping(), metadata=subclassed_metadata)
    subclassed = MappingSubclass(**_mapping().__dict__)
    conflicting = replace(
        _mapping(),
        source_url="https://example.com/other.json",
    )
    conflicting_json = _canonical_mapping_json(source_url=conflicting.source_url)
    conflicting = replace(
        conflicting,
        canonical_json=conflicting_json,
        checksum=hashlib.sha256(conflicting_json.encode("ascii")).hexdigest(),
    )

    mutable_entries = replace(_mapping(), entries=list(_mapping().entries))
    mutable_aliases = replace(
        _mapping(),
        entries=(("801080", "电子", []), ("801780", "银行", ("银行业",))),
    )
    for mappings in (
        list((_mapping(),)),
        (metadata_mapping,),
        (subclassed,),
        (mutable_entries,),
        (mutable_aliases,),
        (_mapping(), conflicting),
    ):
        with pytest.raises(ValueError):
            validate_industry_distribution(
                rows=_rows(),
                complete_scope=True,
                mappings=mappings,  # type: ignore[arg-type]
            )


def test_mapping_rejects_duplicate_codes_names_aliases_and_unsafe_unicode() -> None:
    invalid_entries = (
        (
            ("801080", "电子", ()),
            ("801080", "银行", ()),
        ),
        (
            ("801080", "电子", ()),
            ("801780", "电子", ()),
        ),
        (
            ("801080", "电子", ("银行业",)),
            ("801780", "银行", ("银行业",)),
        ),
        (
            ("801080", "电\u200b子", ()),
            ("801780", "银行", ()),
        ),
    )
    for entries in invalid_entries:
        canonical_json = _canonical_mapping_json(entries=entries)
        mapping = replace(
            _mapping(),
            entries=entries,
            canonical_json=canonical_json,
            checksum=hashlib.sha256(canonical_json.encode("ascii")).hexdigest(),
        )
        with pytest.raises(ValueError):
            validate_industry_distribution(
                rows=_rows(), complete_scope=True, mappings=(mapping,)
            )
