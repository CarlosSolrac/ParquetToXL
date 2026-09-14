# ParquetToXL — Partitioning specification

**Status:** design, unbuilt. This is the contract between an export configuration and the plan
it produces: how one or more Parquet files become workbooks and worksheets, how the resulting
files are named, and what makes the answer reproducible.

It is the document the web configuration editor (tool 2) will be built against. It says nothing
about staging, verification, publication or scheduling — those are in `export-pipeline-spec.md`.
It depends on `library-spec.md` for the conversion, the reader's limits and the hash.

Most of the prose here is Codex's, from `export-config-design.md`, absorbed rather than cited.
Where it has been changed the change is marked and the reason given; the largest is that the
original assumed **one source per configuration**, and this project requires a workbook to hold
worksheets drawn from several Parquet files.

Closed vocabularies are `Literal` plus `Final` constants, never `enum` —
`tools/check_declarations.py` rejects every enum member, because annotating one turns it into a
plain attribute rather than a member. Every `"enum"` in the JSON Schema becomes a `Literal` on
the Pydantic side.

## The configuration

Three documents stay separate:

- `sales.parquet.json` — the generated metadata sidecar (schema version 3).
- an export configuration — user-maintained export intent, validated by
  `export-config.schema.json`.
- a generated run manifest and receipt — the resolved configuration, planner version, ordered
  workbooks and worksheets, actual row counts, partition boundaries and the digests needed to
  validate the export. Specified in `export-pipeline-spec.md`.

Preferences do not belong in `DataframeColumnMetadata`: its dtypes and counts describe observed
data. Configuration describes how to interpret and arrange that data. Relative source and output
paths resolve against the configuration file's directory.

A configuration names **sources by alias** and contains **named profiles**, so the same sources
can repeatedly produce an annual review, a monthly view, or balanced delivery files.

```json
{
  "config_version": 1,
  "config_id": "0f7c1a94-3b52-4c8e-9a1d-6e2f0b5d7c33",
  "config_modified_utc": "2026-09-13T16:04:11Z",
  "output_directory": "az://exports/monthly",
  "sidecar_location": { "kind": "beside_source" },
  "scratch_root": null,
  "excel": { "writer": "rustpy-xlsxwriter", "options": {} },
  "sources": {
    "sales":   { "path": "../data/sales.parquet",
                 "date_columns": { "transaction_date": { "type": "date" } } },
    "returns": { "path": "../data/returns.parquet",
                 "date_columns": { "returned_on": { "type": "int", "format": "YYYYMMDD" } } }
  },
  "profiles": [
    {
      "name": "annual_review",
      "output_subdirectory": "annual_review",
      "limits": { "max_data_rows_per_worksheet": 1048575, "max_cells_per_workbook": 10000000 },
      "partitioning": {
        "algorithm": "calendar_greedy", "base_period": "year", "period_order": "ascending",
        "year_split_months": [6, 4, 3, 2, 1],
        "oversized_period": "balanced_rows", "null_dates": "separate"
      },
      "naming": {
        "workbook": "{profile}_{period_label}_{workbook_index:03d}.xlsx",
        "worksheet": "{source} {period_label}",
        "single_worksheet": "{source} Data",
        "overflow_worksheet": "{source} {period_label}_p{part_index:02d}",
        "month_format": "numeric", "worksheet_prefix": "", "sheet_collision": "error"
      },
      "sheets": [
        { "source": "sales",   "partition_column": "transaction_date",
          "sort": [{ "column": "customer_id", "direction": "ascending", "nulls": "last" }] },
        { "source": "returns", "partition_column": "returned_on", "sort": [] }
      ]
    }
  ]
}
```

### What changed from the single-source original, and why

- **`date_columns` moved per source.** Column names collide across sources, and the same name
  may be a `date` in one file and a `YYYYMM` integer in another.
- **`partitioning.column` became `sheets[].partition_column`.** Each source carries its own date
  column. `algorithm`, `base_period`, `period_order`, `year_split_months`, `oversized_period`
  and `null_dates` stay at profile level: they define one shared grid that every sheet in the
  profile is bucketed against.

  It is **`null` when, and only when, the profile's algorithm is `balanced`** — balanced modes
  slice sorted rows and consult no date at all. The field stays required so the absence is
  written down rather than left out, which would make "no date column" and "someone forgot"
  the same document. The semantic validator refuses a null under a calendar algorithm, and a
  non-null under `balanced`.
- **`sort` moved per sheet.** Sort keys name that source's columns.
- **Sources gained aliases.** Two files may share a basename, and the alias is what appears in
  worksheet names.
- **`excel` was added.** The original said writer selection stays in `ExcelWriteConfig` but left
  the configuration no field to carry it. `polars-xlsxwriter` is **refused** here: it is
  documented as not round-trip safe — `Float64` loses precision and 1900-01-01 shifts back a day
  — so selecting it would make every verification fail for reasons unrelated to the data.
- **`config_id` and `config_modified_utc` were added**, for output-directory ownership and for
  staleness. Both are consumed by `export-pipeline-spec.md`.

The partition is declared **at profile level and applies to every sheet in the profile**. That
is what keeps every source's rows totally and disjointly partitioned, which the pipeline's
verification depends on.

## Capacity and the single-sheet rule

Excel permits 1,048,576 rows and 16,384 columns per worksheet. One header row leaves 1,048,575
data rows. Worksheet count depends on available memory.
[Microsoft: Excel specifications and limits](https://support.microsoft.com/en-us/excel/excel-specifications-and-limits)

Use a configurable **cell budget**, with 10,000,000 as an initial value to benchmark on the
target 16 GB computers. Treat 20,000,000 and 30,000,000 as larger candidate budgets to test, not
known-safe limits. Target 64-bit desktop Excel and measure representative numeric data, repeated
text, long unique text, and the actual styles. Record peak Excel process memory, open and save
times, and sort/filter responsiveness, leaving headroom for the OS and other applications.
Opening several output workbooks at once still consumes their combined memory.

Ten to thirty million numeric values contain 80-240 MB of raw eight-byte payload; this is **not**
an estimate of Excel process memory. Cell structures, strings, formatting and other workbook
features add costs. Compressed `.xlsx` size is also not a RAM bound. Microsoft does not provide
a universal cells-to-memory conversion.

`max_cells_per_workbook` is an enforced planning limit. Count every exported position, including
nulls, plus headers. This deliberately matches this project's `value_count` convention, which
includes nulls. V1 exports all source columns in source order and writes exactly one header row
per sheet, with no index or title rows.

### Per-source capacity

**Changed for fan-in.** The original derived one column count `C` for the whole export. With
several sources in one workbook each has its own width, so let `C_s` be source `s`'s exported
column count, `L` the configured data-row limit, and `B` the cell budget. Reject `C_s = 0` or
`C_s > 16,384`. For nonempty data the effective sheet capacity for that source is:

```text
R_s            = min(L, floor(B / C_s) - 1)
sheet_cells    = C_s * (data_rows + 1)
workbook_cells = sum over the sheets in the workbook of C_s * (rows_s + 1)
```

A workbook's cost is therefore a **sum across its sheets**, not a single product. Every capacity
check below is that sum.

Reject a budget too small for a header and one data row of the widest source. For empty data,
emit one header-only worksheet when its header fits the budget. Never silently drop rows or
columns to meet limits.

If all of a profile's rows fit one sheet per source and those sheets fit one workbook, emit
exactly one workbook, each sheet named by `single_worksheet`, regardless of the selected
partitioning algorithm. Sorting still applies. Otherwise use the algorithm. **The workbook budget
takes priority over the single-sheet shortcut.** 500,000 rows by 100 columns fits Excel's row
limit but contains over 50 million cells with headers, and must split under a 10-million budget.
Conversely 2 million rows by 3 columns needs several sheets yet fits one workbook.

**`R_s` is the per-sheet cap for every mode except balanced-workbooks.** That exception is stated
where it arises, below. The original presented `R` as universal and then used `L` in the
balanced-workbook cost formula, which contradicted itself.

## Date interpretation

**A source may register several date columns; a sheet selects exactly one.** `date_columns`
declares what is *interpretable* as a date in that file — several columns often are — and
`partition_column` names the single one this sheet partitions on. Nothing combines them.

A composite grouping such as `(invoice year, payment month)` is **out of scope for v1**, and must
be refused rather than approximated. It needs an ordered list of keys and a separate rule for
subdividing each group, neither of which exists here. In particular it must never be silently
reinterpreted as several independent exports, or as a fallback where one column is consulted when
another is null: both would produce a plausible-looking export that answers a different question
than the one asked.

`date_columns` is keyed by exact source column name, within one source. Declared types must match
Parquet types: `date`, `datetime`, or any signed or unsigned integer width for `int`; reject
floats, booleans, strings and implicit coercions.

- **`date`** — use its calendar date.
- **`datetime`** — `{"mode": "source_wall_clock"}` uses the source's local calendar date,
  consistent with the existing ToExcel policy. `{"mode": "zone", "zone": "..."}` first converts
  timezone-aware input to that IANA zone for partition keys. A naive input requires
  `source_wall_clock` in v1; assigning a zone to it would need an explicit DST ambiguity policy.
  **Changed:** the original put the sentinel `"source_wall_clock"` in the same string field as
  real zone names. A discriminated union matches how `ColumnDtype` and `HashedDataframe` are
  already modelled here. The naive-input rule is data-dependent and cannot be checked by schema
  alone.
- **`int`** — interpret only the declared `YYMM`, `YYYYMM`, `YYYYMMDD` or `YYMMDD` format.
  Two-digit years require a fixed 100-year window. With a window starting in 1970, `69` means
  2069 and `70` means 1970. Never use the current year or a platform default. Nonnegative
  integers are zero-padded to their format's width before parsing, so `101` in `YYMM` means
  January 2001 for that window. Reject excess digits, negatives, year zero, invalid months and
  impossible dates. V1 supports Gregorian years 1-9999.

**One model, and one schema branch.** The original expressed integer dates as two `oneOf` branches
both carrying `"type": "int"` — one for the four-digit formats, one for the two-digit formats plus
the window. Pydantic's `Field(discriminator="type")` requires unique discriminator values per
member, so that shape cannot be expressed in this project's house pattern at all.

The window field therefore **stays optional** and the rule becomes a validator. The schema does the
same thing, with `if`/`then`/`else`, so both sides have one shape per kind and one statement of the
rule. Use a single `IntDateColumn`:

```python
class IntDateColumn(DateColumnBase, frozen=True):
    type: Literal["int"]
    format: Literal["YYMM", "YYYYMM", "YYMMDD", "YYYYMMDD"]
    two_digit_year_window_start: int | None = None
```

with a `model_validator` requiring the window if and only if the format begins with `YY`. The
window is **forbidden** on the four-digit formats, not merely unused: a setting that silently does
nothing is the kind of configuration bug that survives for years.

**The schema mirrors that structure rather than diverging from it.** `dateColumn` selects on `type`
with `if`/`then`, the same discriminator the Pydantic union uses, and closes the object with
`unevaluatedProperties: false`. Branch-specific fields live inside the `then` blocks rather than in
the outer `properties`, which is what makes that closure reject a field belonging to another kind —
`{"type": "date", "format": "YYMM"}` fails.

This is written with `if`/`then` rather than `oneOf` for error quality, which is the schema's main
job here: a `oneOf` failure reports only that nothing matched, while this names the offending field.
Measured against the current file, a `YYMM` entry with no window reports
`'two_digit_year_window_start' is a required property`, and a `datetime` with no zone reports
`'calendar_timezone' is a required property`. The one message that stays awkward is the forbidden
case — supplying a window alongside `YYYYMM` — which surfaces as a `not` violation, because that is
how JSON Schema spells "this must be absent".

Because both sides now express the same rule in the same shape, the corpus check in
`export-pipeline-spec.md` is not papering over a deliberate divergence. It still earns its place for
the rules JSON Schema cannot state at all: that a sheet's `source` names a declared alias, that
`partition_column` is registered in *that* source's `date_columns`, the `{source}` token rule, and
per-source capacity.

Month-only encodings carry month precision: reject `base_period: year-month-day` for them. Do not
invent a day. Date, datetime and day encodings support all three base periods.

Registered integer dates sort by **decoded chronology**, not by numeric value, and registered
datetimes sort by their full timestamp, not a truncated date. For `YYMM` these differ only across
the century window boundary — under a 1970 window `6912` (December 2069) sorts *after* `7001`
(January 1970) despite being the smaller number — which is exactly the case a reader will not
expect, so the manifest records the decoded key beside the raw value.

Partition keys never overwrite the exported source values, and are derived **before** ToExcel
removes timezone information or converts integer dtypes. Temporary partition keys and source
ordinals must never reach exported data: an extra column would break both the pipeline's digest
comparison and the additive identity it relies on.

Invalid non-null dates are errors naming the column, the source row ordinal and the offending
value. For calendar partitioning, null dates either error or form an `Undated` bucket, placed
last regardless of `period_order` and split by rows if needed. No date policy may discard rows.
With the single-sheet shortcut no partition date values need decoding unless that column is a
sort key; schema, referenced columns, declared dtypes and precision are still validated.

## Sorting and deterministic output

Sort keys are applied in array order with explicit direction and null placement. Preserve source
row ordinal as the final tie-breaker. For the same sources, resolved configuration and planner
version, assignments and names must repeat; byte-identical ZIP files are not promised. Persist
the resolved configuration and the planner version.

Balanced exports sort the whole dataset before contiguous slicing. Calendar exports order buckets
by `period_order`, then apply the requested sort within each final bucket before any row-based
overflow split. A calendar export sorted by customer is therefore sorted by customer inside each
sheet, not globally across all years. An empty sort list preserves source order inside each
bucket. No hidden date sort is injected into the requested row sort.

Sorting does not affect verification. The binary-aggregate hash is order-independent, so every
arrangement of the same rows produces the same digest.

## Partitioning algorithms

| Configuration | Behaviour when the single-sheet shortcut does not apply |
| --- | --- |
| `balanced`, `balance_across: worksheets` | Globally sort each source, create the minimum number of sheets of capacity `R_s`, with data-row counts differing by at most one, then pack sheets into workbooks. |
| `balanced`, `balance_across: workbooks` | Globally sort, find the minimum feasible number of workbooks, distribute rows evenly across them, then distribute each workbook's rows evenly across its minimum number of sheets. |
| `calendar_greedy` | Merge successive whole base periods while they fit; split any oversized base period. With a year base, this is greedy-years. |
| `calendar_periods` | Keep each base period separate, subdividing an oversized period. With a year base, one year per sheet. |

Balanced division of `N` rows into `K` outputs gives `N mod K` outputs of size
`floor(N/K) + 1`, followed by the smaller outputs. Never shuffle rows to equalise them.

For balanced workbooks the cell cost of one source contributing `n > 0` rows is
`C_s * (n + ceil(n/L))`; find the smallest `K` for which the summed larger balanced share across
the profile's sources fits `B`. This includes repeated headers, and feasibility is monotonic in
`K`, so a linear scan or a binary search both terminate. The resulting workbook sizes differ by
at most one data row per source; sheet sizes are balanced within each workbook.

**This mode uses `L`, not `R_s`, as the per-sheet cap** — the documented exception. It is cheaper,
because fewer sheets means fewer repeated headers, and it stays safe because the workbook budget
still binds through the cost formula. Calendar boundaries are not preserved by either balanced
mode.

Greedy merging uses ordered, nonempty base buckets. Before adding a bucket that would exceed
capacity, emit the accumulated sheet. Flush an accumulated sheet before processing an oversized
bucket. Do not backfill older sheets or merge fragments of an oversized year into a neighbouring
year. Missing periods do not create empty sheets. A merged range label describes coverage, not a
guarantee that every intervening period has rows.

For an oversized **year**, try the calendar grids specified by `year_split_months`:

| Months per bucket | Meaning | Numeric labels | Abbreviated labels |
| --- | --- | --- | --- |
| 6 | Two semesters | `2025-01~06`, `2025-07~12` | `2025-Jan~Jun`, `2025-Jul~Dec` |
| 4 | Three four-month periods | `2025-01~04` through `2025-09~12` | `2025-Jan~Apr` through `2025-Sep~Dec` |
| 3 | Four quarters | `2025-01~03` through `2025-10~12` | `2025-Jan~Mar` through `2025-Oct~Dec` |
| 2 | Six bimonthly periods | `2025-01~02` through `2025-11~12` | `2025-Jan~Feb` through `2025-Nov~Dec` |
| 1 | Twelve months | `2025-01` through `2025-12` | `2025-Jan` through `2025-Dec` |

All grids start in January. Use the first grid for which **every nonempty bucket in that year**
fits. These are alternatives evaluated against the entire year, not recursive subdivisions:
four-month periods do not nest inside semesters. A three-month trimester duplicates a quarter;
the distinct four-month option is named by its size so the terminology cannot change behaviour.

If even monthly buckets are too large, emit the fitting months and apply `oversized_period` to
each oversized month: split its sorted rows evenly at capacity, or fail. An oversized base month
or base day uses the same overflow rule directly. No implicit daily subdivision is added in v1;
`year-month-day` selects daily base buckets explicitly. This also handles a single date holding
more than a sheet's capacity. Every source row belongs to exactly one final sheet per profile.

`year_split_months` must be strictly descending and end in `1`; a profile may omit unwanted
intermediate grids, for example `[6, 3, 1]`. It is unused for month and day bases. The JSON
Schema constrains the member values and uniqueness; ordering is checked by the semantic
validator. The set of valid arrays is exactly the descending subsets of `{6,4,3,2}` followed by
`1` — sixteen in all — so enumerating them in the schema is possible if a purely structural check
is wanted.

## Workbook allocation

Except for balanced-workbook mode, consume planned sheets in their final order. Append the next
whole sheet if it fits the remaining cell budget; otherwise open the next workbook. A sheet
already fits an empty workbook because planning used `R_s`.

Do not split a fitting sheet merely to fill a workbook's remaining capacity. This may leave unused
capacity, preserving meaningful calendar boundaries. It is deterministic ordered packing, not a
claim of optimal packing. Year boundaries alone do not force a new workbook, and an oversized year
may span several workbooks. The budget is what triggers another workbook.

**Fan-in consequence.** A calendar bucket whose *combined* cost across the profile's sources
exceeds `B` can occur even when each source's own sheet fits its `R_s`. Ordered allocation
resolves it — the period spans workbooks — but the consequence must be stated rather than
inferred: **a workbook may hold only some of the profile's sources for a period, and "one workbook
per period" is not a guarantee.** A source with no rows in a period simply contributes no sheet
there.

## Naming

Templates use a small allowlist of tokens, with literal text and optional integer zero-padding
such as `{workbook_index:03d}`. Do not evaluate Python expressions, attributes, arbitrary format
directives or environment variables.

| Token | Scope and definition |
| --- | --- |
| `source` | The source alias. **New**, and required — see below |
| `source_stem` | That source's basename without its final `.parquet` extension |
| `profile` | The profile name |
| `workbook_index` | One-based workbook index within the profile |
| `sheet_index` | One-based worksheet index within its workbook; sheet templates only |
| `period_label` | Coverage label using `month_format`; `Data` for balanced or unsplit output, `Undated` for null dates |
| `part_index` | One-based row-fragment index within the original calendar bucket; overflow template only |

**`{source}` is required in `worksheet` and `overflow_worksheet` whenever a profile has more than
one sheet.** Without it every source in a workbook renders the same name — `2025` twice — and
`sheet_collision: suffix` would resolve that to `2025` and `2025_2`, which is worse than failing,
because the names then carry no indication of which source they hold. The semantic validator
refuses such a profile.

Use the literal `~` character for ranges. `naming.month_format` selects `numeric` or
`abbreviated`; both use `~` between range endpoints. No backslash is stored before `~` in JSON or
in an output name.

| Period | `numeric` | `abbreviated` |
| --- | --- | --- |
| One year | `2025` | `2025` |
| Multiple years | `2022~2024` | `2022~2024` |
| One month | `2025-01` | `2025-Jan` |
| Month range within one year | `2025-01~06` | `2025-Jan~Jun` |
| Month range crossing a year boundary | `2025-11~2026-02` | `2025-Nov~2026-Feb` |
| One day | `2025-01-31` | `2025-01-31` |
| Day range | `2025-01-31~2025-02-02` | `2025-01-31~2025-02-02` |

Month abbreviations are the fixed English `Jan`, `Feb`, `Mar`, `Apr`, `May`, `Jun`, `Jul`, `Aug`,
`Sep`, `Oct`, `Nov`, `Dec`, independent of the computer's locale — spelled out because `Sep`
against `Sept` is exactly the kind of difference that changes a filename.
Numeric months always use two digits. Ranges show the earlier endpoint first, even when sheets
are ordered descending. A single period has no redundant range endpoint. Semester and other
subdivisions use their calendar month boundaries as shown, even if some months have no rows. The
choice changes labels only, never partition boundaries.

On a workbook template, `period_label` summarises the earliest through latest calendar coverage
represented in that workbook, with `_Undated` appended when needed; all-undated workbooks use
`Undated`. Exact coverage belongs in the manifest. Use workbook indices in filenames even for a
single workbook, to avoid a filename rule that changes when the dataset grows. Include profile
names to distinguish variants.

`single_worksheet` is used only when the entire export uses the shortcut, not for every workbook
that happens to contain one sheet. `overflow_worksheet` handles fragments such as `2025-07_p01`.
Balanced sheets use `worksheet`.

`worksheet_prefix` is literal text, not another template. A nonempty value is prepended to every
rendered worksheet name with one space, including single-sheet, balanced, undated and overflow
names. An empty string disables the prefix. It does not change workbook filenames or
`period_label`. With `worksheet_prefix: "debits"`, `month_format: "numeric"` and
`worksheet: "{period_label}"`, the result is `debits 2025-01~06`; switching to `abbreviated`
gives `debits 2025-Jan~Jun`. For a custom separator, leave `worksheet_prefix` empty and put
literal text in each template, such as `debits_{period_label}`.

### Name validation

Validate all expanded names after adding the prefix and before writing. The prefix and its
separator count toward the sheet-name length limit.

**Worksheet names** must fit 31 characters, be nonempty, exclude `/ \ ? * : [ ]`, avoid leading
and trailing apostrophes and the reserved name `History`, and be unique case-insensitively within
a workbook.
[Microsoft: Rename a worksheet](https://support.microsoft.com/en-us/excel/rename-a-worksheet)

`rustpy-xlsxwriter` ships `validate_sheet_name`, which covers the length and the character set but
**not** `History`, apostrophes or uniqueness. Use it, and check the rest here.

Invalid base names are errors. For a duplicate valid sheet name, `suffix` appends `_2`, `_3` and
so on, reserving space by truncating the stem as needed and checking the new name against every
name already allocated. Indices and suffixes follow final output order. `error` rejects collisions
instead.

**Workbook names** are the part the original left as "valid `.xlsx` basenames for the destination",
which is the hard half. The destination may be a Windows or SMB share, Azure Blob or ADLS, or a
POSIX mount, and the host generating the names is Linux — which will object to none of it. So
enforce the union explicitly, in code, regardless of host:

- no `< > : " / \ | ? *`, and no control characters;
- no trailing dot or space;
- no reserved device names: `CON`, `PRN`, `AUX`, `NUL`, `COM1`-`COM9`, `LPT1`-`LPT9`;
- unique **case-insensitively**, because `Sales.xlsx` and `sales.xlsx` collide on SMB;
- no directory traversal;
- full path within 260 characters for SMB, and within 1024 for an Azure blob name.

Workbook collisions are errors. Source aliases take the pattern `^[A-Za-z0-9][A-Za-z0-9_-]*$`, the
same one profile names use, since both are interpolated into names.

Do not silently overwrite existing files. Publication, replacement and the removal of superseded
files are specified in `export-pipeline-spec.md`.

## Validation and the implementation boundary

The schema uses [JSON Schema Draft 2020-12](https://json-schema.org/draft/2020-12). Unknown fields
are rejected. Explicit fields avoid hidden policy defaults; schema annotations do not insert
values. `config_version` is independent of the sidecar, conversion and hash versions.

JSON Schema validates structure. A semantic validator must additionally check: unique profile
names; unique source aliases; existing source, sort and partition columns; duplicate sort keys;
partition references into that source's `date_columns`; dtype compatibility; integer-date
precision; valid timezone names; the descending grid list; template tokens including the
`{source}` rule; per-source capacity; two profiles resolving to the same output directory; and
expanded output names. Data scans check actual date values and bucket counts. Schema validation
alone does not prove an export is possible.

The planner produces a reviewable manifest before writing: interpret keys, count buckets, choose
boundaries, allocate workbooks, resolve names. Keep temporary partition keys and source ordinals
out of exported data. Scan, count and materialise only what writing needs; external sorting and
bounded-memory writing need measurement against the installed libraries. A cell budget limits
output size, not exporter RAM.

`ExcelWriterBase.write(df, path, options)` creates a single-sheet workbook, and repeated calls
would overwrite the file. A multi-sheet interface is needed. `rustpy-xlsxwriter` 0.6.1 already
provides one — `write_worksheets(...)`, and a `FastExcel(target).sheet(name, data).save()` builder
— on the default round-trip-safe writer. Two traps: `FastExcel` defaults `autofit=True`, the
opposite of what `RustpyExcelWriter.write` deliberately passes, and `dedupe_strings` is per-sheet.
Whether `.sheet()` streams a generator or buffers every sheet until `.save()` is unmeasured, and
decides the memory story. Writer selection stays in `ExcelWriteConfig`; the planner handles layout
rather than duplicating writer options.

### Acceptance cases

Exactly-at and one-over row and cell limits; the single-sheet bypass; wide data exceeding the
budget below Excel's row limit; year merging; each calendar grid; one huge month and one huge day;
balanced-workbook header accounting; `YY` century boundaries and leading zeros; invalid and null
dates; timezone year boundaries; numeric and abbreviated month ranges including cross-year ranges;
literal worksheet prefixes across all sheet templates; name collisions and length limits after
prefixing; stable tie order; header-only output.

And, new with fan-in: a period whose combined cost across sources exceeds the budget while each
sheet fits its own `R_s`; a source absent from a period; a profile whose worksheet template omits
`{source}`; two profiles resolving to one output directory.

Do not assume a fragment's hash equals the whole source hash. Record the assignments, and validate
the union as `export-pipeline-spec.md` specifies.
