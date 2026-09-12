# What an Excel round trip does to Polars data

Everything here was measured by running `generate.py` and reading the workbooks back, not
taken from documentation. Where a number appears, a script produced it. The fixture frame is
1000 rows across all 19 scalar Polars dtypes, with 48 leading edge-case rows.

The reader throughout is `python-calamine`, which is what `fast_excel_reader` will use.
`pl.read_excel` is not used anywhere: it requires `fastexcel`, which is not a dependency of
this project.

## Summary for the impatient

- **No writer is faithful.** All three lose data, on *disjoint* sets of dtypes.
- **Nulls are unrecoverable.** Every null of every dtype reads back as `''`, which is
  indistinguishable from an empty string. This defeats `encode_value`'s separate null
  sentinel unless the conversion normalises first.
- **A column stops being one type.** A `datetime` column returns a mix of `datetime`, `time`
  and `str`. `pl.Series(cells)` raises outright.
- **`rust_xlsxwriter` is the chosen writer**, via `rustpy-xlsxwriter`. It is not the fastest
  at scale; see [Speed](#speed).

## The three writers

| | fed from | adjustments it forces |
| --- | --- | --- |
| `_duckdb.xlsx` | Parquet, no casts at all | none |
| `_polars.xlsx` | `DataFrame.write_excel` | three |
| `_rustpy.xlsx` | `rustpy-xlsxwriter` | one |

All three are kept as a standing sanity check. Where they agree, the behaviour belongs to
Excel; where they disagree, it belongs to the writer. None claims the unqualified filename.

**Polars needs three workarounds**, each a hard `TypeError` otherwise: `Binary` must be
hex-encoded first, the workbook must be opened with `remove_timezone`, and it must be opened
with `nan_inf_to_errors`.

**rustpy needs one**, and it is this project's own rule rather than a workaround: a string
over 32,767 characters makes it **raise**. DuckDB writes the oversized string anyway;
xlsxwriter silently truncates it. Refusing is the most defensible of the three.

## Per-dtype behaviour

Identical across all three writers unless noted.

| dtype | comes back as | damage |
| --- | --- | --- |
| `Int8/16/32`, `UInt8/16/32` | `float` | none inside ±2^53 |
| `Int64`, `UInt64` | `float` | **extremes inexact**; the three writers round differently |
| `Float32` | `float` | DuckDB rounds to `892.482`; the others keep `892.4819946289062` |
| `Float64` | `float` | DuckDB keeps NaN/±inf exactly; **Polars and rustpy write `''`** |
| `Boolean` | `bool` | none — survives as a real boolean |
| `String` | `str` | see [Strings](#strings) |
| `Binary` | `str` | see below — all three differ |
| `Date` | `date` | **pre-1900 corrupts**, differently per writer |
| `Time` | `time` | none, except **rustpy writes `str(time)`** |
| `Datetime(us, UTC)` | `datetime` | **DuckDB shifts to local time**; all three degrade edges |
| `Duration` | `float`/`str` | units differ per writer; none matches the spec |
| `Decimal` | `float` | precision lost at the extremes |
| `Categorical` | `str` | none |
| `Null` | `str` | becomes `''` |

### Binary is rendered three incompatible ways

For source `b"\xff\xfe"`:

| writer | cell contents |
| --- | --- |
| DuckDB | `\xFF\xFE` — DuckDB's own BLOB escape, printable bytes passed through as characters |
| Polars | `fffe` — because this project hex-encodes before handing it over |
| rustpy | `b'\xff\xfe'` — **`repr(bytes)`**, Python source syntax written into the cell |

`bytes(range(256))` becomes 748 characters through DuckDB and 512 through hex.

### Duration units differ and none is seconds

For `timedelta(seconds=1)`:

| writer | cell | recover with |
| --- | --- | --- |
| DuckDB | `1000000.0` | ÷ 1e6 |
| Polars | `1.157e-05` | × 86400 |
| rustpy | `'0:00:01'` | parse `str(timedelta)` |

The spec calls for `Duration → Float64` seconds. No writer produces that on its own, so it
has to be the conversion's job.

### Temporal edge cases

| source | DuckDB | Polars | rustpy |
| --- | --- | --- | --- |
| `date(1899,12,31)` | `date(1900,1,1)` | `time(0,0)` | `time(0,0)` |
| `date(1900,1,1)` | `date(1900,1,2)` | `date(1900,1,1)` | `date(1900,1,1)` |
| `datetime(1970,1,1, UTC)` | `datetime(1969,12,31,18:00)` | `date(1970,1,1)` | `date(1970,1,1)` |
| `datetime(9999,12,31,…)` | shifted datetime | **`2958466.0`** raw serial | `datetime(9999,12,31,23:59:59)` |

DuckDB's `1900-01-02` is Excel's inherited Lotus 1-2-3 leap-year bug. Its 6-hour shift is
this machine's UTC offset, which means **a digest taken from a DuckDB workbook depends on
the machine that wrote it**.

## Strings

41 string cases are in the fixture. These survive all three writers unchanged: astral emoji,
ZWJ family sequences, skin-tone modifiers, regional-indicator flags, RTL Hebrew and Arabic,
bidi override, combining vs precomposed `é` (never folded together), ZWSP, ZWNJ, NBSP, BOM,
U+2028/U+2029, astral CJK, and the control characters `\x01 \x07 \x08 \x0b \x0c \x1b \x7f`.

That last group is a surprise worth recording: the XLSX specification forbids most C0
control characters in cell text, yet all three writers emit them and calamine reads them back
intact.

What does not survive:

| case | DuckDB | Polars | rustpy |
| --- | --- | --- | --- |
| `'nul\x00inside'` | **`'nulinside'`** | intact | intact |
| `'a\r\nb'` | **`'a\nb'`** | intact | intact |
| `'  padded  '` | **`'padded'`** | intact | intact |
| 32,800 characters | written whole | **cut to 32,767** | **raises** |
| `None` | `''` | `''` | `''` |

### Formula injection

`=SUM(1+1)` written through `DataFrame.write_excel` at its defaults comes back as the float
**`0.0`**: a stored string silently became a live formula. `XLSXWRITER_OPTIONS` sets
`strings_to_formulas: False` to stop it. DuckDB and rustpy both write it as literal text.

Any string column carrying user-supplied data can contain such a value.

## Speed

Same data, best of three runs, DuckDB's connection reused so its setup is not counted
against it. rustpy is fed a generator with `autofit=False`.

| rows | DuckDB | rustpy | winner |
| --- | --- | --- | --- |
| 1,000 | 0.022s | 0.020s | rustpy, 9% |
| 5,000 | 0.126s | 0.099s | rustpy, 22% |
| 10,000 | 0.223s | 0.212s | rustpy, 5% |
| 20,000 | 0.448s | 0.454s | DuckDB, 1% |
| 50,000 | 0.921s | 1.231s | **DuckDB, 25%** |

`polars/xlsxwriter` is far behind both: **5.338s** at 50,000 rows, roughly 5x DuckDB.

**The crossover is near 15,000–20,000 rows.** rustpy is fastest below it and DuckDB is
fastest above it. rustpy was chosen anyway; the choice is defensible on correctness — it is
the only writer that refuses to guess about an oversized cell, and the only one that
preserves NUL, CRLF and surrounding whitespace while also not shifting timestamps — but
"fastest" is not accurate above ~20k rows and should not be relied on for sizing.

### Configuring rustpy for speed

- `autofit=False`. Autofit measures every cell to size columns; its own documentation says
  to turn it off for large datasets. Measured effect at 50k rows was within noise here, but
  it grows with column count.
- `dedupe_strings=False` (the default). Enabling it buffers the whole sheet to build a shared
  string table, which **switches the writer out of constant-memory mode**.
- Feed `DataFrame.iter_rows(named=True)`, not `to_dicts()`. The writer accepts a generator,
  and streaming is what makes constant-memory mode meaningful. Verified: generator and list
  produce byte-identical workbooks.

## Consequences for the conversion — all now resolved

Each of these was a gap between what `DataframeConversionToExcel` modelled and what a file
actually does. All five are closed; the first three by amending the model, the last two by
choosing a reader and keeping the truncation rule.

1. **Nulls and empty strings merge.** Resolved: the conversion maps `""` to null. The two
   readers do not even agree on what an empty cell returns — `fastexcel` says `None`,
   `python-calamine` says `''` — which is itself proof the distinction cannot be carried.
2. **NaN and ±inf do not survive.** Resolved: the conversion maps them to null. No writer
   measured can store them.
3. **Timestamps lose sub-second precision.** Resolved: the conversion truncates `Datetime`
   to whole seconds. Measured, `23:47:16.854775` reads back as `23:47:16`.
4. **The reader cannot infer dtypes.** Resolved by giving it the schema instead of letting
   it guess. This is not a nicety: `pl.read_excel` with no schema **fails outright** on this
   data, downcasting integral-looking floats and overflowing on `Int64`'s maximum.
5. **Truncation is the project's rule.** Unchanged: only rustpy enforces the 32,767 limit,
   and it enforces by raising, so the conversion truncates before the writer sees a value.

**Verified end to end.** Convert the fixture frame, write it with the default writer, read it
back with the converted schema: **all 19 columns match the model in both dtype and value.**

## Choosing the reader: fastexcel over python-calamine

They are bindings to the same Rust calamine crate, but they are not interchangeable.

| | python-calamine | fastexcel |
| --- | --- | --- |
| `date(1899,12,31)` | `time(0, 0)` — date destroyed | **recovered** |
| `datetime(1899,12,31)` | `time(0, 0)` — unrecoverable | **recovered** |
| empty cell | `''` | `None` |
| column typing | mixed Python types per column | the model's dtypes, given `schema_overrides` |

Schema-driven coercion of the calamine output reaches 17 of 19 columns. The last two cannot
be fixed downstream: by the time a cell arrives the pre-1900 date is already gone, replaced
by a bare time-of-day. `fastexcel` needs no coercion layer at all.

`DataframeConversionToExcel` implements the spec's cast table with one deliberate departure,
found by a Codex review: **`Categorical` is truncated as well as cast to `String`**, though
the table lists truncation for `String` and `Binary` only. Without it an oversized label
passes the first conversion untouched and is cut by the second, so the frame and the
`schema_or_data_changed` flag would depend on how many times the conversion ran. Idempotency
is load-bearing for the headline round-trip test, so it wins over the narrower reading.

A second review found that the conversion must select columns **by position**, not by name.
`pl.col(name)` reads a name wrapped in `^...$` as a regex and `*` as every column, and both
are legal Parquet column names: measured, `pl.col("^a$")` beside a column named `a` silently
returned that other column's values, a frame whose only column was `^a$` came back empty, and
`*` raised `DuplicateError`.

## Fixture invariants

Checked by script, not assumed:

- 19 columns, 1000 rows, schema written in sorted column order.
- `parquet_b` differs from `parquet_a` at exactly one cell per column, all 19, at row 48.
- The **Parquet** files regenerate byte-identically — matching SHA-256 across runs.
- `part1 + part2` reconstructs `full` for all three writers, across all 19,000 cells.

The **workbooks** do not regenerate byte-identically, and one of them cannot be made to.
Writing the same frame twice a second apart differs in exactly one zip member,
`docProps/core.xml`, which carries `dcterms:created` and `dcterms:modified`. Every cell is
identical. DuckDB is byte-stable; xlsxwriter and rustpy are not. xlsxwriter could be pinned
with `set_properties`, but **rustpy-xlsxwriter exposes no document-properties API**, so the
chosen writer cannot be made byte-stable from Python. Compare workbook contents, never
workbook hashes.

One trap worth knowing: three rows contain NaN, and NaN never equals itself, so a naive
`==` comparison reports the concatenation check as failing when it is fine.

## The writers fail soft; guard rails were needed

Five rounds of review on `excel/writer.py` turned up the same shape of defect repeatedly:
`polars.write_excel` builds an Excel *table*, and `xlsxwriter.add_table` **warns, discards
data, and still returns**. A caller sees success and an empty or truncated workbook. All
measured:

| Input | `polars-xlsxwriter` | `rustpy-xlsxwriter` |
| --- | --- | --- |
| string beginning `=` | live formula; read back `0.0` | literal text |
| URL over 2,079 chars | **cell dropped**, warning only | preserved |
| columns `a` and `A` | one column and all rows lost | correct |
| 16,385 columns | workbook reads back `(0, 0)` | raises |
| zero-row frame | headers kept | **no header row at all** |
| empty column name `""` | renamed `Column1` | renamed `1`, **row dropped** |

`XLSXWRITER_WORKBOOK_OPTIONS` now disables formula and URL reinterpretation. The dimension
and case-collision checks refuse the frame before the destination is opened, so a bad input
cannot replace a good file with an empty one. An empty column name is refused by **both**
writers, since neither preserves it.

## A reader limitation, not a writer one

Column names Polars reads as selectors — `*`, and anything shaped `^...$` — are legal
Parquet names, and **both writers store them correctly**. Verified against the raw sheet:
`header=['*', 'b'], data=[[1.0, 2.0]]`.

`pl.read_excel` is what cannot read them back, raising
`DuplicateError: projections contained duplicate output name 'b'`. This was initially
reported as a writer defect; it is not. It belongs to `fast_excel_reader`, which will need
to avoid letting Polars interpret header text as a selector — the same trap the conversion
hit with `pl.col(name)`, solved there by selecting on position instead.

## Verdict: only the default writer is round-trip safe

Seven rounds of review on `excel/writer.py` settled this. `rustpy-xlsxwriter` round-trips the
converted fixture frame exactly. `polars-xlsxwriter` does not, and two of its losses cannot
be guarded around -- only avoided -- because they are how `xlsxwriter` serializes:

| ordinary converted value | `rustpy-xlsxwriter` | `polars-xlsxwriter` |
| --- | --- | --- |
| `1.2345678901234567` | exact | **`1.234567890123457`** |
| `1900-01-01 12:00 UTC` | exact | **`1899-12-31 12:00`** |

Neither is an edge case; the first is an ordinary `Float64`. Fixing either means not calling
`write_excel` at all, at which point it stops being the Polars writer. So the guarantee is
scoped instead: **the digest contract belongs to the default writer.** `PolarsExcelWriter`
stays registered as the pure-Python fallback for platforms without the Rust wheel, and as a
second implementation that fails differently -- which is exactly what caught most of the
findings above. Its tests assert shape, not values, and its two losses are pinned so that a
future release fixing them shows up as a failure.

The 1900-01-01 shift is Excel's inherited Lotus 1-2-3 leap-year bug, which DuckDB also has.

## What `fast_excel_reader` still has to solve

Two problems are the reader's, not the writer's, and neither is fixed yet:

1. **Trailing all-null rows vanish.** A row whose cells are all empty produces no `<row>`
   element, so the row extent is not stored. Measured on `[1.0, None, None]`: both writers
   produce a workbook that reads back with one row. The metadata already carries the fix --
   `DataframeColumnMetadata.value_count` is the row count -- so the reader can restore the
   extent rather than trusting the sheet.
2. **Selector-shaped headers break `pl.read_excel`.** `*` and `^...$` are legal Parquet
   column names, both writers store them correctly, and the read raises
   `DuplicateError: projections contained duplicate output name`. The reader must not let
   Polars interpret header text as a selector -- the same trap the conversion hit with
   `pl.col(name)`, solved there by selecting on position.

