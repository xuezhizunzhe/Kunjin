# KunJin Real-Shape Asset Allocation Adapter Design

Date: 2026-07-15

## 1. Problem

Fresh isolated D1.1-C acceptance selected and parsed twelve current periodic
reports across `519706`, `164905`, `519718`, and `519755`, but persisted zero
`current_*` facts. This fails the D1.1-C requirement that at least one official
current asset-allocation fact complete the full selection, parsing,
classification, Manifest V3, and authenticated-readback path.

The live quarterly reports use a stable four-column table:

1. sequence;
2. project/category;
3. amount;
4. percentage of total fund assets.

Some hierarchical rows have an empty sequence cell. The current structured HTML
parser rejects an entire table when any cell is empty. Even if that rejection is
removed, the fact extractor accepts only the synthetic three-column
`indicator/unit/value` schema. The failure is therefore a two-layer schema gap,
not a selection, Docker, source, report-period, or database failure.

## 2. Scope

Implement one narrowly controlled adapter for the observed official
four-column asset-composition table. Keep all other table parsing behavior
unchanged.

The adapter may emit only:

- `current_stock_asset_allocation_percent`; and
- `current_bond_asset_allocation_percent`.

It must not infer cash from bank deposits, settlement reserves, balances,
totals, omitted categories, or `100 - disclosed values`. It must not change
industry, top-ten, holdings-completeness, duration, credit-quality, leverage, or
issuer-concentration behavior.

## 3. Structured Table Gate

The converted-HTML parser keeps its existing strict behavior by default. An
otherwise invalid table containing an empty cell may be recovered only when all
of these conditions hold:

- it has exactly four columns in every retained row;
- the first row is an exact allowlisted sequence/project/amount/total-assets
  percentage header;
- no cell uses `rowspan` or `colspan` other than an explicit value of one;
- project, amount, and percentage cells are non-empty in every retained data
  row;
- only the sequence cell may be empty; and
- all existing table, row, cell, character, and excerpt limits remain active.

The parser must not globally permit empty cells. Malformed target-shaped tables
remain absent rather than partially reconstructed.

LibreOffice may place pure HTML layout-whitespace text nodes around visible
heading content. Only the converted-HTML structured-table parser may normalize
a fragment made entirely of ASCII space, tab, CR, LF, or form-feed to one ASCII
space before section safety validation. A fragment containing visible text plus
any control character, U+0085, any other Cc, every Cf, and every default-
ignorable character remains unmodified and fails closed. The global time-
context safety predicate is not relaxed.

The recovered table is represented through the existing immutable
`ReportTable`, `ReportRow`, and `ReportCell` records. `ReportCell` carries an
exact boolean structural-placeholder flag. Only the converted-HTML recovery
path may set that flag while replacing an actually empty sequence cell; literal
source text equal to the private placeholder is rejected. Data rows containing
header cells are rejected. Four-column observations build their bounded public
excerpt only from the original project, amount, and percentage cells; the
private placeholder is never persisted as a fact value or included in a public
fact excerpt.

## 4. Fact Extraction Gate

The fact extractor recognizes the four-column schema separately from the
existing three-column indicator schema.

For each data row considered for a mapped observation it requires:

- an exact allowlisted project/category label;
- a syntactically valid amount cell;
- a syntactically valid percentage from 0 through 100; and
- the exact total-assets denominator supplied by the header.

Sequence integers are structural only and are checked after the parser's
existing NFKC normalization. They must then be canonical ASCII decimals; they
are not treated as raw-byte financial evidence.

Only explicit stock and bond subcategory labels are mapped. Broad equity,
fixed-income, deposit, reserve, other-asset, subtotal, and total labels do not
receive a semantic mapping unless they exactly name stock or bond under the
controlled vocabulary.

Equivalent duplicate observations use the existing fingerprint and deduplication
rules. Different values for the same fact remain separate authenticated facts
and are handled by the existing conflict logic; the adapter never chooses one.

Report-period authentication, effective dates, source-document identity,
parser provenance, freshness, selection binding, and Manifest V3 are unchanged.

## 5. Error And Privacy Behavior

Unsupported or malformed four-column tables emit no current fact and do not
fall back to text extraction. The public command remains technically successful
when the document itself parsed successfully, while classification preserves
the exact missing-evidence codes.

Tests and audit documents may use only a structurally faithful, content-minimized
fixture. No raw downloaded document, converted HTML, managed path, candidate
fingerprint, unselected URL, canonical selection JSON, exception text, or
database path is committed.

## 6. Verification

Add red-green tests for:

- a real-shape four-column table with empty sequence cells;
- exact stock and bond extraction with `percent_of_total_assets`;
- no cash inference from bank-deposit or settlement-reserve rows;
- wrong headers, wrong width, empty project/amount/percentage, nonnumeric amount,
  invalid percentage, and nontrivial spans;
- a later header row and literal source text equal to the private placeholder;
- pure converted-heading layout whitespace as a positive case, plus visible
  text containing a control character and non-layout Cc/Cf negative cases;
- duplicate equivalent rows and conflicting values;
- legacy converted-HTML end-to-end parsing with effective report date and
  parser v4 provenance; and
- preservation of existing three-column behavior.

Run focused parser/report-fact tests, the complete suite, Ruff, compileall,
dependency checks, and diff checks. Then run a new four-fund isolated live
acceptance with the reviewed Docker image. Acceptance requires at least one
authenticated current stock or bond asset-allocation fact, while retaining:

- at most one periodic attempt per kind;
- no older-report fallback;
- Schema V13 and Manifest V3 authentication;
- industry-observation coverage of zero;
- `research_only` output; and
- no direction, amount, or 90 percent beginner-help claim.

## 7. Non-Goals

- Generic rowspan/colspan grid reconstruction.
- Free-text percentage extraction from converted legacy documents.
- Cash, industry, top-ten, or full-holdings inference.
- D2 portfolio construction, D3 product selection, or Phase E monitoring.
- Any purchase recommendation, target weight, or transaction amount.
