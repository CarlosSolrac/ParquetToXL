> **Superseded by `partitioning-spec.md`, and kept only for your review.**
>
> Its prose was absorbed there rather than cited, with the changes marked and argued in place.
> Six things were amended rather than carried over:
>
> - the configuration was **single-source**; a workbook must be able to hold worksheets drawn
>   from several Parquet files, so `date_columns`, `partition_column` and `sort` moved and
>   sources gained aliases;
> - capacity derived one column count `C` for the whole export, so `R` became per-source `R_s`
>   and a workbook's cost became a sum across its sheets;
> - `R` was presented as universal but balanced-workbook mode sizes sheets with `L`, which the
>   text contradicted; the exception is now stated;
> - writer selection was said to live in `ExcelWriteConfig` while the schema had no field for it;
> - integer dates were two `oneOf` branches both keyed `"type": "int"`, which Pydantic's
>   discriminated union cannot express; the window field is now optional with the rule carried
>   by `if`/`then`/`else` in the schema and a validator in the model;
> - `calendar_timezone` held the sentinel `"source_wall_clock"` in the same string field as real
>   IANA zone names; it is now a discriminated union, matching how this project models every
>   other closed vocabulary.
>
> Delete this file once you have checked nothing was lost.

# Repeatable Parquet-to-Excel exports — proposed configuration v1

Status: architecture proposal. The accompanying schema and example describe a future
planner; the current application does not consume this configuration.

Keep three documents separate:

- `parquet_a.parquet.json`: the existing, generated metadata sidecar (schema version 2).
- `parquet_a.parquet.export.json`: user-maintained export intent, validated by
  [export-config.schema.json](export-config.schema.json).
- A generated export manifest: the resolved configuration, source identity, planner
  version, ordered workbooks and worksheets, actual row/cell counts, date coverage,
  partition boundaries, and hashes needed to validate the combined export.

The configuration contains named profiles so the same source can repeatedly produce an
annual review, a monthly view, or balanced delivery files. Do not put preferences in
`DataframeColumnMetadata`: its dtype and counts describe observed data. Configuration
describes how to interpret and arrange that data. Relative source and output paths resolve
against the configuration file's directory. The example's column names are illustrative.

**Proposed v1 assumption:** multiple date columns may be declared, but each calendar
profile selects one. A composite grouping such as `(invoice year, payment month)` needs
an ordered list of keys and a separate rule for subdividing each group. It should not be
silently interpreted as several independent exports or as a fallback date column.

## Capacity and the single-sheet rule

Excel permits 1,048,576 rows and 16,384 columns per worksheet. One header row leaves
1,048,575 data rows. Worksheet count depends on available memory.
[Microsoft: Excel specifications and limits](https://support.microsoft.com/en-us/excel/excel-specifications-and-limits)

Use a configurable **cell budget**, with 10,000,000 as an initial value to benchmark on
the target 16 GB computers. Treat 20,000,000 and 30,000,000 as larger candidate budgets
to test, not known-safe limits. Target 64-bit desktop Excel and measure representative
numeric data, repeated text, long unique text, and the actual styles/formulas. Record
peak Excel process memory, open/save times, and sort/filter responsiveness, leaving
headroom for the OS and other applications. Opening multiple output workbooks at once
still consumes their combined memory.

Ten to thirty million numeric values contain 80–240 MB of raw eight-byte payload; this
is **not** an estimate of Excel process memory. Cell structures, strings, formatting,
and other workbook features add costs. Compressed `.xlsx` size is also not a RAM bound.
Microsoft does not provide a universal cells-to-memory conversion.

Define `max_cells_per_workbook` as an enforced planning limit. Count every exported
position, including nulls, plus headers. This deliberately matches this project's
`value_count` convention, which includes nulls. V1 exports all source columns in source
order and writes exactly one header row per sheet, with no index or title rows.

Let `C` be the exported column count, `L` the configured data-row limit, and `B` the cell
budget. Reject `C = 0` or `C > 16,384`. For nonempty data the effective sheet capacity is:

```text
R = min(L, floor(B / C) - 1)
sheet_cells = C * (data_rows + 1)
workbook_cells = sum(sheet_cells)
```

Reject a budget too small for a header and one data row. For empty data, emit one
header-only worksheet when its header fits the budget. Never silently drop rows or
columns to meet limits.

If all rows fit `R`, emit exactly one workbook and one sheet named by
`single_worksheet`, regardless of the selected partitioning algorithm. Sorting still
applies. Otherwise use the algorithm. **The workbook budget takes priority over the
single-sheet shortcut.** For example, 500,000 rows × 100 columns fits Excel's row limit,
but contains over 50 million cells with headers and must split under a 10-million budget.
Conversely, 2 million rows × 3 columns needs multiple sheets but can fit one workbook.

## Date interpretation

`date_columns` is keyed by exact source column name. Declared types must match Parquet
types: `date`, `datetime`, or any signed/unsigned integer width for `int`; reject floats,
booleans, strings, and implicit coercions.

- `date`: use its calendar date.
- `datetime`: `source_wall_clock` uses the source's local calendar date, consistent
  with the existing ToExcel policy. An explicit IANA zone first converts timezone-aware
  input to that zone for partition keys. A naive input requires `source_wall_clock` in
  v1; assigning a zone to it would need an explicit DST ambiguity policy.
- `int`: interpret only the declared `YYMM`, `YYYYMM`, `YYYYMMDD`, or `YYMMDD` format.
  Two-digit years require a fixed 100-year window. With a window starting in 1970,
  `69` means 2069 and `70` means 1970. Never use the current year or a platform default.
  Nonnegative integers are zero-padded to their format's width before parsing, so `101`
  in `YYMM` means January 2001 for that window. Reject excess digits, negatives, year
  zero, invalid months, and impossible dates. V1 supports Gregorian years 1–9999.

Month-only encodings carry month precision: reject `base_period: year-month-day` for
them. Do not invent a day. Date/datetime/day encodings support all three base periods.
Registered integer dates sort by decoded chronology, rather than their numeric value;
registered datetimes sort by their full timestamp, not a truncated date. Partition keys
never overwrite the exported source values. Derive keys before ToExcel removes timezone
information or converts integer dtypes.

Invalid non-null dates are errors with column, source row ordinal, and offending value.
For calendar partitioning, null dates either error or form an `Undated` bucket, placed
last and split by rows if needed. No date policy may discard rows. With the single-sheet
shortcut, no partition date values need decoding unless that column is a sort key;
schema, referenced columns, declared dtypes, and precision are still validated.

## Sorting and deterministic output

Sort keys are applied in array order with explicit direction and null placement.
Preserve source row ordinal as the final tie-breaker. For the same source, resolved
configuration, and planner version, assignments and names must repeat; byte-identical
ZIP files are not promised. Persist the resolved configuration and planner version.

Balanced exports sort the whole dataset before contiguous slicing. Calendar exports
order buckets by `period_order`, then apply the requested sort within each final bucket
before any row-based overflow split. Therefore a calendar export sorted by customer is
sorted by customer inside each sheet, not globally across all years. An empty sort list
preserves source order inside each bucket. No hidden date sort is injected into the
requested row sort.

## Partitioning algorithms

| Configuration | Behavior when the single-sheet shortcut does not apply |
| --- | --- |
| `balanced`, `balance_across: worksheets` | Globally sort, create the minimum number of sheets of capacity `R`, with data-row counts differing by at most one, then pack sheets into workbooks. |
| `balanced`, `balance_across: workbooks` | Globally sort, find the minimum feasible number of workbooks, distribute rows evenly across them, then distribute each workbook's rows evenly across its minimum number of sheets. |
| `calendar_greedy` | Merge successive whole base periods while they fit a sheet; split any oversized base period. With a year base, this is the requested greedy-years algorithm. |
| `calendar_periods` | Keep each base period separate, subdividing an oversized period. With a year base, this is the requested one-year-per-sheet algorithm. |

Balanced division of `N` rows into `K` outputs gives `N mod K` outputs of size
`floor(N/K) + 1`, followed by the smaller outputs. Never shuffle rows to equalize them.
For balanced workbooks, the cell cost of a workbook with `n > 0` rows is
`C * (n + ceil(n/L))`; find the smallest `K` for which the larger balanced share fits
`B`. This includes repeated headers. The resulting workbook sizes differ by at most
one data row; sheet sizes are balanced within each workbook. Calendar boundaries are
not preserved by either balanced mode.

Greedy merging uses ordered, nonempty base buckets. Before adding a bucket that would
exceed `R`, emit the accumulated sheet. Flush an accumulated sheet before processing
an oversized bucket. Do not backfill older sheets or merge fragments of an oversized
year into a neighboring year. Missing periods do not create empty sheets. A merged
range label describes coverage, not a guarantee that every intervening period has rows.

For an oversized **year**, try the calendar grids specified by `year_split_months`:

| Months per bucket | Meaning | Numeric labels | Abbreviated labels |
| --- | --- | --- | --- |
| 6 | Two semesters | `2025-01~06`, `2025-07~12` | `2025-Jan~Jun`, `2025-Jul~Dec` |
| 4 | Three four-month periods | `2025-01~04` through `2025-09~12` | `2025-Jan~Apr` through `2025-Sep~Dec` |
| 3 | Four quarters | `2025-01~03` through `2025-10~12` | `2025-Jan~Mar` through `2025-Oct~Dec` |
| 2 | Six bimonthly periods | `2025-01~02` through `2025-11~12` | `2025-Jan~Feb` through `2025-Nov~Dec` |
| 1 | Twelve months | `2025-01` through `2025-12` | `2025-Jan` through `2025-Dec` |

All grids start in January. Use the first grid for which **every nonempty bucket in
that year** fits `R`. These are alternatives evaluated against the entire year, not
recursive subdivisions: four-month periods do not nest inside semesters. A three-month
trimester duplicates a quarter; the distinct four-month option above is explicitly
named by its size so the terminology cannot change behavior.

If even monthly buckets are too large, emit the fitting months and apply
`oversized_period` to each oversized month: split its sorted rows evenly at capacity
`R`, or fail. An oversized base month or base day uses the same overflow rule directly.
No implicit daily subdivision is added in v1; `year-month-day` selects daily base
buckets explicitly. This also handles a single date containing more than a sheet's
capacity. Every source row belongs to exactly one final sheet per profile.

`year_split_months` must be strictly descending and end in `1`; a profile may omit
unwanted intermediate grids, for example `[6, 3, 1]`. It is unused for month/day bases.

## Workbook allocation

Except for balanced-workbook mode, consume planned sheets in their final order. Append
the next whole sheet if it fits the remaining cell budget; otherwise open the next
workbook. A sheet must already fit an empty workbook because planning used `R`.

Do not split a fitting sheet merely to fill a workbook's remaining capacity. This may
leave unused capacity, preserving meaningful calendar boundaries. It is deterministic
ordered packing, not a claim of optimal packing. Year boundaries alone do not force a
new workbook, and an oversized year may span several workbooks. The budget is what
triggers another workbook.

## Naming

Templates use a small allowlist of tokens, with literal text and optional integer
zero-padding such as `{workbook_index:03d}`. Do not evaluate Python expressions,
attributes, arbitrary format directives, or environment variables.

| Token | Scope and definition |
| --- | --- |
| `source_stem` | Source basename without its final `.parquet` extension |
| `profile` | The profile name |
| `workbook_index` | One-based workbook index within the profile |
| `sheet_index` | One-based worksheet index within its workbook; sheet templates only |
| `period_label` | Coverage label using `month_format`; `Data` for balanced/unsplit output, `Undated` for null dates |
| `part_index` | One-based row-fragment index within the original calendar bucket; overflow template only |

Use the literal `~` character for ranges. `naming.month_format` selects `numeric` or
`abbreviated`; both formats use `~` between range endpoints. No backslash is stored
before `~` in JSON or in an output name.

| Period | `numeric` | `abbreviated` |
| --- | --- | --- |
| One year | `2025` | `2025` |
| Multiple years | `2022~2024` | `2022~2024` |
| One month | `2025-01` | `2025-Jan` |
| Month range within one year | `2025-01~06` | `2025-Jan~Jun` |
| Month range crossing a year boundary | `2025-11~2026-02` | `2025-Nov~2026-Feb` |
| One day | `2025-01-31` | `2025-01-31` |
| Day range | `2025-01-31~2025-02-02` | `2025-01-31~2025-02-02` |

Month abbreviations are fixed English `Jan`, `Feb`, `Mar`, `Apr`, `May`, `Jun`, `Jul`,
`Aug`, `Sep`, `Oct`, `Nov`, `Dec`, independent of the computer's locale. Numeric months
always use two digits. Ranges show the earlier endpoint first, even when sheets are
ordered descending. A single period has no redundant range endpoint. Semester and
other subdivisions use their calendar month boundaries as shown above, even if some
months have no rows. The choice changes labels only, not partition boundaries.

On a workbook template, `period_label` uses the same formatting and summarizes the earliest
through latest calendar coverage represented in that workbook, with `_Undated` appended
when needed; all-undated workbooks use `Undated`. Exact coverage belongs in the manifest.
Use workbook indices in filenames even for a single workbook to avoid a filename rule
that changes when the dataset grows. Include profile names to distinguish variants.

`single_worksheet` is used only when the entire export uses the shortcut, not for every
workbook that happens to contain one sheet. `overflow_worksheet` handles fragments such
as `2025-07_p01`. Balanced sheets use `worksheet`, typically `Data_{sheet_index:03d}`.

`worksheet_prefix` is literal text, not another template. A nonempty value is prepended
to every rendered worksheet name with one space, including single-sheet, balanced,
undated, and overflow names. An empty string disables the prefix. It does not change
workbook filenames or `period_label`. For example:

```json
{
  "month_format": "numeric",
  "worksheet_prefix": "debits",
  "single_worksheet": "Data",
  "worksheet": "{period_label}",
  "overflow_worksheet": "{period_label}_p{part_index:02d}"
}
```

This naming excerpt produces `debits Data`, `debits 2025-01~06`, or
`debits 2025-07_p01`, as appropriate. Changing `month_format` to `abbreviated` produces
`debits 2025-Jan~Jun` and `debits 2025-Jul_p01`. For a custom separator, leave
`worksheet_prefix` empty and include literal text in each template, such as
`debits_{period_label}`.

Validate all expanded names after adding the prefix and before writing. The prefix and
its separator count toward the sheet-name length limit. Sheet names must fit 31 characters, be
nonempty, exclude `/ \\ ? * : [ ]`, avoid leading/trailing apostrophes and reserved
`History`, and be unique case-insensitively within a workbook.
[Microsoft: Rename a worksheet](https://support.microsoft.com/en-us/excel/rename-a-worksheet)

Invalid base names are errors. For a duplicate valid sheet name, `suffix` appends `_2`,
`_3`, etc., reserving space by truncating the stem as needed and checking the new name
against all names already allocated. Indices and suffixes follow final output order.
`error` rejects collisions instead. Workbook names must be valid `.xlsx` basenames for
the destination, contain no directory traversal, and be unique across all requested
profiles. Workbook collisions are errors. Do not silently overwrite existing files;
the eventual export API should require an explicit replace option. Publication and
replacement should occur only after successful planning and writing to temporary paths.

## Validation and implementation boundary

The schema uses [JSON Schema Draft 2020-12](https://json-schema.org/draft/2020-12).
Unknown fields are rejected. Explicit fields avoid hidden policy defaults; schema
annotations do not insert values. `config_version` is independent of metadata sidecar,
conversion, and hash versions.

JSON Schema validates structure. A semantic validator must additionally check unique
profile names, existing source/sort/partition columns, duplicate sort keys, partition
references into `date_columns`, dtype compatibility, integer-date precision, valid
timezone names, the descending grid list, template tokens, capacity, and expanded
output names. Data scans check actual date values and bucket counts. Do not imply
that schema validation alone proves an export is possible.

Introduce a planner that produces a reviewable manifest before writing: interpret
keys, count buckets, choose boundaries, allocate books, and resolve names. Keep temporary
partition keys and source ordinals out of exported data. Scan/count and materialize only
the data needed for writing; external sorting and bounded-memory writing need measurement
against the installed libraries. A cell budget limits output size, not exporter RAM.

The current `ExcelWriterBase.write(df, path, options)` creates a single-sheet workbook.
It needs a multi-sheet workbook interface with an explicit open/write-sheet/close
lifecycle, or an iterator of sheet specifications. Repeated calls to the existing method
would overwrite the file. Keep writer selection in the existing `ExcelWriteConfig`;
the new planner handles layout rather than duplicating writer options.

Suggested implementation acceptance cases: exactly-at/one-over row and cell limits;
single-sheet bypass; wide data that exceeds the budget below Excel's row limit; year
merging; each calendar grid; one huge month/day; balanced-workbook header accounting;
YY century boundaries and leading zeros; invalid/null dates; timezone year boundaries;
numeric and abbreviated month ranges (including cross-year ranges); literal worksheet
prefixes across all sheet templates; name collisions and length limits after prefixing;
stable tie order; header-only output; and combined
export counts/hashes matching the expected ToExcel-converted source. Do not assume each
fragment's hash equals the whole source hash. Record assignments and validate the union.
