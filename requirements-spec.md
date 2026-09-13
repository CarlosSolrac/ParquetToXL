# ParquetToXL — Dataframe Metadata, Hashing & Fast Excel I/O

**Status:** design approved 2026-09-06. Implemented through phase 5 except
`fast_excel_reader`; see *Implementation status* below. Amended repeatedly against
measurement -- every amendment records what was measured and why the original text did not
survive contact with the libraries.

## Implementation status

| Phase | State | Notes |
| --- | --- | --- |
| 0 · Prereq | **done** | deps, `[build-system]`, src layout, `configure_logging`, golden style module |
| 1 · Canonical + hashing | **done** | `encode_value` / `encode_series`, tags `0x00`-`0x0C`, additive xxh3-128 |
| 2 · Metadata models | **done** | the three models and `build_columns_metadata` |
| 3 · Conversions | **done** | base, `None`, `ToExcel` at version 3.0, local wall-clock preservation and idempotency pinned |
| 4 · Extract | **done** | `extract_metadata_from_dataframe`, five failure modes pinned |
| 5 · Excel | **writers done, reader pending** | registry, `ExcelWriteConfig`, both writers. `fast_excel_reader` is the one unbuilt unit |
| 6 · Fixtures | **done** | `generate.py`, 20 files across three writers, invariants under test |
| 7 · Integration | **partly done** | `ZPath` landed early as a phase 4 prerequisite; the single-workbook half of the headline test is in `integration/test_excel_roundtrip.py`. The split-workbook half and the reader benchmark wait on the reader |

After the 2026-09-12 wall-clock and row-hashing amendments: 264 tests, 100% statement and
100% branch coverage. Ruff, formatting, declarations, pyright and mypy pass.

**What is left.** `fast_excel_reader`, the headline test's split-workbook half that
depends on it, and the JSON sidecar schema and persistence API. The reader also inherits two measured problems that are its to solve, not the
writers': a trailing all-null row leaves no trace in a sheet and must be restored from
`value_count`, and `pl.read_excel` raises `DuplicateError` on selector-shaped column names
(`*`, `^...$`) that both writers store correctly.

## Context

`ParquetToXL` is a Python 3.13 project (uv, Ruff strict rule set, pyright + mypy strict,
pytest with a merge-only coverage gate, `tools/check_declarations.py`). At the time this
spec was written there was no runtime code and no runtime dependencies; the package now
sits at 100% statement and 100% branch coverage while CI still only enforces 90.

The goal is a library that, given a Polars DataFrame loaded from a Parquet file, produces a
validated metadata record describing every column, computes **order-independent** content
hashes, models what happens to that data when it is round-tripped through Excel, and can
read Excel back fast. The headline acceptance test: hash a DataFrame read back from a
generated `.xlsx` and confirm it equals the "Excel hash" recorded in the JSON sidecar for
the Parquet file — including when the rows were split across two 500-row workbooks and reassembled
in a different order.

### Design decisions

| Topic | Decision |
| --- | --- |
| Logging | `structlog` (the original "Serilog" note reinterpreted for Python) |
| `DataframeColumnMetadata` | one object **per column**; a wrapper `DataframeColumnsMetadata` holds the ordered list + whole-frame hashes |
| Conversions | **explicit required parameter** to `extract_metadata_from_dataframe` |
| Hasher API | base class exposes **both** `hash_column()` and `hash_dataframe()` |
| Dtype coverage | **all scalar Polars dtypes**, no nested types |
| `fast_excel_reader` | **`fastexcel`** + `ThreadPoolExecutor` across sheets; one path in, one frame out. Amended from `python-calamine` on measurement: the two are bindings to the same Rust calamine crate, but `fastexcel` recovers pre-1900 dates that `python-calamine` degrades to a bare time-of-day, and given `schema_overrides` it returns the model's dtypes directly instead of a column of mixed Python types |
| Binary aggregate hash | **xxh3_128 v2**; aggregate cell hashes and hashes of each row's ordered cell hashes mod 2¹²⁸. Row order may change; column order and row relationships are bound. Column names are not hashed. |
| Persistence | **one JSON sidecar per Parquet file**. JSON schema, filename convention, and read/write API remain to be defined; metadata is not embedded in Parquet. |
| Fixture files | generated on first run into `tests/fixtures/data/`, **gitignored** |
| `ZPath` | **a `zpath()` factory returning a real `UPath`**, not a subclass; construction is the only seam (future Azure/AWS credential wiring). Amended on measurement: UPath 0.3.10 picks one concrete class per protocol from a registry, so a bare `class ZPath(UPath)` cannot be constructed at all. UPath accepts `**storage_options` natively, so the seam survives as one factory in one place, with `ZPath` kept as a CapWords alias of it |
| `HashedDataframe` | Pydantic **discriminated union on `identifier`**, per-hasher subclass + `version` |
| Reload flag | named **`schema_or_data_changed`**; `True` when a converter altered data or schema |
| Excel writer | a config-driven **writer registry**, default **`rustpy-xlsxwriter`**. Amended on measurement: `polars.write_excel` is five times slower than DuckDB at 50k rows and is **not round-trip safe** -- it loses `Float64` precision and shifts 1900-01-01 by a day. It stays registered as the pure-Python fallback and as a second implementation that fails differently |
| Package | `src/parquet_to_xl/`, src layout; remove the empty `main.py` |
| Build backend | add `[build-system]` = `hatchling` + `[tool.hatch.build.targets.wheel] packages = ["src/parquet_to_xl"]`; uv installs the package editable so tests run against the installed package |
| Method | **TDD** — test first for every unit, watch it fail, then implement |

### Assumptions

- `timezones` is `list[str]` of IANA names; converted with `zoneinfo.ZoneInfo`.
- ToExcel boolean mapping: `True → -1.0`, `False → 0.0`, `null → null`, dtype `Float64`.
- ToExcel `Duration → Float64` seconds; `Binary →` lowercase-hex `String`; `Categorical → String`.
- ToExcel also models three losses a real round trip inflicts: `""` → null, `NaN`/`±inf` → null,
  and `Datetime` truncated to whole seconds. See the conversion section.
- Zoned datetimes preserve **local wall-clock time**: remove the timezone without converting
  to UTC, then truncate. Output is naive `Datetime("us")`; even removing UTC changes schema.
  DST-fold instants with the same local clock reading intentionally become equal.
- The two 500-row Excel files are rows `[0:500]` and `[500:1000]` of the 1000-row frame.
- Fixture RNG is a small linear congruential generator written out in `generate.py`, not
  stdlib `random`. Amended: `random` trips Ruff `S311`, which this project does not
  suppress, and the standard library does not promise identical streams across versions --
  which byte-identical regeneration needs. numpy is still not a runtime dependency.
- Excel string cell limit treated as 32,767 characters.

## New dependencies

Runtime (`[project].dependencies`): `polars`, `pydantic`, `universal-pathlib`, `adlfs`,
`xxhash`, `structlog`, `fastexcel`, `python-calamine`, `rustpy-xlsxwriter`, `xlsxwriter`,
`duckdb`, `tzdata`.

`rustpy-xlsxwriter` is the default Excel writer and `fastexcel` the reader. `xlsxwriter` and
`python-calamine` are kept as the second writer and as a cross-check reader; `duckdb` writes
the third fixture workbook. Three independent writers are a standing sanity check: where they
agree the behaviour is Excel's, where they disagree it is the writer's.

`tzdata` is not optional and is deliberately unconditional. Windows ships no system
timezone database, so without it `zoneinfo.TZPATH` is empty and **every** IANA lookup
fails — including `ZoneInfo("UTC")`, which means simply reading a value out of a
`Datetime("us", "UTC")` column raises `ZoneInfoNotFoundError`. Linux CI images usually
do carry a system database, so omitting it produces the worst kind of bug: green in CI,
broken on a developer's machine. Pinning it also makes conversions depend on the
lockfile rather than on whatever tz database version the host happens to ship, which
matters for a library whose entire purpose is comparing content digests across machines.
Dev: `pytest`, `pytest-cov`, `ruff`, `pyright`, `mypy`, `pre-commit`, `numpy`.
`uv.lock` is regenerated and committed (CI installs `--locked`).

## Package layout

```
src/parquet_to_xl/
  __init__.py
  logging.py            configure_logging(json_output: bool) -> None   (structlog + stdlib bridge)
  paths.py              zpath() -> UPath, ZPath alias                  (a factory, not a subclass)
  py.typed                                                             (marker; the package is strict-typed)
  hashing/
    canonical.py        encode_value(value) -> bytes                   (type-tagged, null sentinel)
    __init__.py         HashedDataframe alias                          (see note below)
    base.py             DataFrameHasherBaseClass, HashedDataframeBase
    binary_aggregate.py DataFrameHasherBinaryAggregateHash, BinaryAggregateHashedDataframe
  metadata/
    scalars.py          ColumnScalar type alias, dtype flag helpers
    column.py           DataframeColumnMetadata           (per column)
    columns.py          DataframeColumnsMetadata          (wrapper)
    builder.py          build_columns_metadata(df, hashers) -> DataframeColumnsMetadata
    dataframe.py        DataframeMetadata
    extract.py          extract_metadata_from_dataframe(...)
  conversion/
    base.py             DataframeConversionBaseClass, ConvertedDataframe (frozen dataclass)
    none.py             DataframeConversionNone
    to_excel.py         DataframeConversionToExcel
  excel/
    writer.py           ExcelWriterBase, registry, RustpyExcelWriter (default),
                        PolarsExcelWriter, ExcelWriteConfig
    fast_reader.py      fast_excel_reader(path, *, sheet_names=None, max_workers=None) -> pl.DataFrame
stubs/                  hand-written stubs for dependencies pyright strict cannot use
  xlsxwriter/           Workbook construction; the package ships no types
  rustpy_xlsxwriter/    write_worksheet; ships a .pyi but no py.typed marker
  python_calamine/      narrows one declaration typed with a bare os.PathLike
tests/
  conftest.py           session-scoped ensure_fixtures() fixture
  fixtures/
    generate.py         deterministic parquet + excel builder
    data/               generated, gitignored
  unit/                  one module per unit above
  integration/
    test_excel_roundtrip.py        built: the single-workbook half
    test_excel_hash_roundtrip.py   pending: the split-workbook headline test
```

`tests/fixtures/data/` is added to `.gitignore`. `main.py` is deleted.

## Components

### `zpath()` — `paths.py`

A factory function, not a subclass. Overrides construction only, as the single seam for
cloud credentials:

```python
def zpath(*args: JoinablePathLike,
          protocol: str | None = None,
          storage_options: Mapping[str, object] | None = None,
          **kwargs: object) -> UPath:

ZPath: Final = zpath  # CapWords alias, so call sites read as a constructor
```

It merges `storage_options` with the native `**kwargs` spelling (kwargs win), rejects a live
credential object, and returns `UPath(...)`. No normalization, no protocol pinning. Every call
site in this library uses `zpath`/`ZPath`, never `Path`/`UPath` directly; paths are *annotated*
as `UPath`, since the factory returns whichever implementation is registered for the protocol.

A subclass is not an option: since universal-pathlib 0.3.9, `UPath.__new__` raises `TypeError`
for a subclass that is not registered for the detected protocol — for cloud URIs *and* for local
paths — and the `_protocol_dispatch = False` escape hatch is deprecated in favour of
`upath.extensions.ProxyUPath`. The factory is also what keeps `os.fspath()` working on local
paths, since it returns the real `LocalPath` (a `pathlib.Path` subclass) that polars and
calamine accept directly.

Credential resolution beyond explicit arguments is left to `adlfs`, which already reads the
`AZURE_STORAGE_*` environment variables and falls back to `DefaultAzureCredential` — the
mechanism that makes the same code work against `az login` locally and a managed identity on a
cluster. On PySpark, pass `str(path)` across the driver/executor boundary and rebuild with
`zpath()` there: `UPath.__reduce__` carries storage options, so pickling a path pickles its
credentials.

### Canonical value encoding — `hashing/canonical.py`

`encode_value(value: object) -> bytes` maps a single Polars scalar to deterministic bytes.
Every result is `tag_byte + payload` so a float `0.0` can never collide with an empty
string. Tags `0x00`–`0x05` are exactly the **post-conversion** type set, so a frame that has been
through `DataframeConversionToExcel` uses only those. Tags `0x06`–`0x0A` exist because
`build_columns_metadata` also hashes the **source** frame, where `Int64`, `Boolean`,
`Binary`, `Decimal` and `Duration` columns are still in their original dtypes — the
original six-tag table could not hash a source frame at all.

Two dispatch orders are load-bearing, because Python's type hierarchy works against the
table: `bool` subclasses `int`, so it must be tested first or every boolean encodes as an
integer; and `datetime` subclasses `date`, so it must be tested first or every timestamp
silently loses its time of day.

Hashers call `encode_series(column)`, never `encode_value` over `Series.to_list()`. Three
dtypes store more precision than the Python scalar they convert to, and the conversion is
silent — the values simply come back equal. `Time` is always nanoseconds since midnight
while `datetime.time` resolves only to microseconds, so *every* `Time` column loses its
bottom three digits; `Datetime("ns")` and `Duration("ns")` lose the same three to
`datetime.datetime` and `datetime.timedelta`. For those three `encode_series` encodes the
physical `int64` directly.

`Time` keeps tag `0x05` and its nanosecond payload, which is byte-identical to the scalar
path for microsecond-granular data, so no digest recorded before this change moves. The two
nanosecond variants get tags `0x0B` and `0x0C` rather than being folded into `0x04` and
`0x0A`: those payloads are microseconds, and widening them to nanoseconds would overflow
`int64` in 2262 — before the far-future dates the fixtures call for.


| Value | Encoding |
| --- | --- |
| `None` / null | `b"\x00"` |
| `Float64` | `b"\x01" + struct.pack("<d", v)`; `NaN → canonical quiet NaN`, `-0.0 → 0.0` |
| `String` | `b"\x02" + v.encode("utf-8")` |
| `Date` | `b"\x03" + struct.pack("<q", days_since_epoch)` |
| `Datetime` | `b"\x04" + struct.pack("<q", micros_since_epoch)`; **naive → assumed UTC**, aware → converted to UTC. Microseconds come from integer `timedelta` subtraction, not `timestamp()`: a float64 mantissa runs out around 2255, well inside the range Polars holds and inside the far-future dates the fixtures specify |
| `Time` | `b"\x05" + struct.pack("<q", nanos_since_midnight)` |
| `int` | `b"\x06" + str(v).encode("utf-8")` — decimal text, because `UInt64`'s maximum does not fit a signed 8-byte pack |
| `bool` | `b"\x07" + (b"\x01" if v else b"\x00")` |
| `bytes` | `b"\x08" + v` |
| `Decimal` | `b"\x09" +` plain decimal text with trailing zeros stripped, so `1.25` and `1.250` agree. Textual, not `normalize()`, which rounds to the ambient context precision and would let an unrelated caller change a digest |
| `timedelta` | `b"\x0a" + str(microseconds).encode("utf-8")` — decimal text, because `timedelta` spans about ten times what int64 microseconds can hold and a packed payload overflows on values Polars accepts |
| `Datetime("ns")` column | `b"\x0b" + struct.pack("<q", nanos_since_epoch)` |
| `Duration("ns")` column | `b"\x0c" + struct.pack("<q", nanoseconds)` |

The type tag is a property of the value, not the column name. `Float64`, `Date`,
`Datetime`, and `Time` all serialize to 8 bytes via `struct.pack`; tags keep them distinct.
Source-frame datetime hashes still describe instants. The ToExcel conversion removes the
timezone first, so converted datetime hashes describe local wall-clock readings.

**Round-trip canonicalization.** `calamine` returns Excel datetimes as naive and may infer
`Int64` for whole-number columns, so the reader's output is not dtype-identical to
`DataframeConversionToExcel(source)`. To make the headline test compare like with like,
both operands pass through `DataframeConversionToExcel._convert` before hashing (idempotent
on already-converted frames), and `encode_value` normalizes datetimes to UTC as above.

### Hashers — `hashing/base.py`, `hashing/binary_aggregate.py`

```python
class DataFrameHasherBaseClass(ABC):
    @abstractmethod
    def hash_column(self, column: pl.Series) -> HashedDataframe: ...
    @abstractmethod
    def hash_dataframe(self, df: pl.DataFrame) -> HashedDataframe: ...
```

`HashedDataframeBase(BaseModel, frozen=True)`: `identifier: str`, `version: int`.
`BinaryAggregateHashedDataframe(HashedDataframeBase)`:
`identifier: Literal["binary-aggregate-xxh3-128"]`, `version: int = 2`,
`scope: Literal["column", "dataframe"]`, `bit_width: Literal[128] = 128`,
`digest_hex: str` (`Field(pattern=r"^[0-9a-f]{32}$")`),
`row_digest_hex: str | None = None` (same hex pattern; the aggregate of the logical row-hash
column for a v2 dataframe, absent for column hashes and legacy v1 records).

`version` and the `encode_value` tag table are one contract. Once any digest has been
persisted, changing a tag or a payload requires incrementing `version` in the same change:
otherwise a stored digest and a freshly computed one carry identical algorithm labels while
disagreeing, and unchanged data reads as modified. Version 2 changes dataframe aggregation
to include row relationships; column digest payloads and canonical value encodings remain
unchanged. Compare algorithm versions before comparing digests; recompute old dataframe
digests from data because v1 aggregate sums cannot recover row relationships.
`HashedDataframe = Annotated[BinaryAggregateHashedDataframe, Field(discriminator="identifier")]`
— a one-member discriminated union, extensible without touching consumers. It is defined in
`hashing/__init__.py`, not `base.py`: `base.py` would need the concrete subclass to build the
alias while `binary_aggregate.py` needs `HashedDataframeBase` from `base.py`, which is a real
import cycle that fails at runtime. The package module is the one place that knows every union
member, so a second hasher is added there and nowhere else, and `base.py` never learns about
any concrete hasher. Round-trips
through `model_validate` / `model_dump` selecting the subclass by `identifier`.

`DataFrameHasherBinaryAggregateHash`:
- `hash_column(column)`: `total = 0`; for each value `total = (total + xxh3_128(encode_value(v)).intdigest()) % (1 << 128)`; return with `scope="column"`, `digest_hex = f"{total:032x}"`.
- `hash_all(df)`: compute cell hashes column by column in bounded batches, preserving row
  positions. Use the canonical **16-byte big-endian** xxh3 digest, never hex text, as the
  working cell value. Accumulate each original column's integer hashes mod 2¹²⁸.
- For each row, concatenate those fixed-width cell hashes in dataframe column order and
  hash the result with xxh3-128. This is a logical extra column, not a mutation of the
  caller's dataframe. Sum its values mod 2¹²⁸ into `row_digest_hex`.
- `hash_dataframe(df)`: return `(sum(original column totals) + row total) mod 2¹²⁸` as
  `digest_hex`, with `scope="dataframe"`. `hash_all` also returns the ordered column records,
  so metadata building does not encode and hash each cell twice.

This costs O(rows × columns), hashes each canonical cell once plus one hash per row, and
uses at most 4096 rows per batch, reduced for wide frames toward a 4 MiB cell-hash payload.
The payload target excludes Python buffer overhead and permits one exceptionally wide row.
No sorting, Python hash randomization, or runtime-dependent dataframe hashing is used.

Local timing on 2026-09-12 (Python 3.13.14, Polars 1.44.1, median of three runs, ten Int64
columns) measured the shared hashing pass at 0.0406 s / 0.2007 s / 0.3958 s for 10k / 50k /
100k rows. The previous metadata hashing path, which encoded all cells once for column
totals and again for the dataframe total, took 0.0738 s / 0.3839 s / 0.7692 s respectively.
These timings exclude metadata statistics and file I/O; they are local observations, not
a performance guarantee for other dtypes, machines, or a native/vectorized implementation.

Properties this buys (all asserted by tests): shuffling rows leaves the digest unchanged;
concatenating the two 500-row halves reproduces the 1000-row digest; swapping values between
different rows changes the dataframe digest even when column totals do not. Column reorder
changes the digest; renaming columns does not. Duplicate and null rows contribute normally.
Empty frames and empty columns have zero aggregates. As before, this is a non-cryptographic
change detector, not a proof of equality or an adversarial integrity check.

### Column metadata — `metadata/column.py`, `columns.py`, `builder.py`

`DataframeColumnMetadata(BaseModel, frozen=True)` — one per column:
`name: str`, `polars_dtype: str` (str of the `pl.DataType`),
flags `is_numeric, is_float, is_integer, is_decimal, is_text, is_boolean: bool`
(the first four delegate to Polars; `is_text` and `is_boolean` have no Polars predicate and
are derived — `is_text` is true for `String` **and** `Categorical`, which is
dictionary-encoded text and converts to `String`; note also that Polars reports `Decimal`
as numeric and `Boolean` as *not* numeric),
`hashes: list[HashedDataframe]`,
stats `min_value: ColumnScalar | None`, `max_value: ColumnScalar | None`,
`value_count: int`, `unique_count: int`, `null_count: int`.
(`min_value`/`max_value` not `min`/`max` — Ruff `A` forbids shadowing builtins.)
`ColumnScalar = float | int | str | bool | bytes | datetime | date | time | timedelta | Decimal`.

`DataframeColumnsMetadata(BaseModel, frozen=True)` — the wrapper:
`columns: list[DataframeColumnMetadata]` (dataframe order),
`dataframe_hashes: list[HashedDataframe]` (one per hasher, from `hash_dataframe`).

Both models originally carried a `description: str`. It is dropped: `build_columns_metadata`
receives a `pl.DataFrame`, which carries no column documentation, so nothing could populate
the field and no consumer reads it. A required field no constructor can fill would have to
be invented at every call site.

`build_columns_metadata(df, hashers)` is the shared constructor used by both the
source-frame path and every conversion: per column it derives the dtype flags, computes
stats with Polars, and calls each hasher's `hash_all` for both levels of digests. The base
implementation delegates to `hash_column` and `hash_dataframe` for compatibility; the
binary aggregate implementation reuses the working cell hashes.

### Conversions — `conversion/base.py`, `none.py`, `to_excel.py`

```python
@dataclass(frozen=True)
class ConvertedDataframe:
    columns_metadata: DataframeColumnsMetadata
    converted_dataframe: pl.DataFrame
    schema_or_data_changed: bool
```

(Plain frozen dataclass, not Pydantic — it holds a live DataFrame and needs no validation.)

```python
class DataframeConversionBaseClass(ABC):
    identifier: ClassVar[str]
    version: ClassVar[str]
    version_number: ClassVar[int]
    description: ClassVar[str]

    def metadata_of_converted_dataframe(
        self, df: pl.DataFrame, hashers: Sequence[DataFrameHasherBaseClass]
    ) -> ConvertedDataframe:
        converted, changed = self._convert(df)
        return ConvertedDataframe(build_columns_metadata(converted, hashers), converted, changed)

    @abstractmethod
    def _convert(self, df: pl.DataFrame) -> tuple[pl.DataFrame, bool]: ...
```

`DataframeConversionNone._convert` → `(df, False)`.

`DataframeConversionToExcel._convert` applies the Excel round-trip model, then reports
`schema_or_data_changed = converted.schema != df.schema or any_value_moved`:
- `Int*/UInt*/Decimal → Float64`
- `Float32/Float64 → Float64`, and **`NaN`/`±inf` become null**
- `Boolean → Float64` via `True→-1.0`, `False→0.0`, `null→null`
- `String`/`Categorical` `→ String` cut at 32,767 chars, and **`""` becomes null**
- `Binary →` lowercase-hex `String`, then the same rule (so `b""` becomes null)
- `Duration → Float64` seconds
- **`Datetime → Datetime("us")`: remove timezone preserving local wall-clock time, then truncate to whole seconds**
- `Time` truncated to microseconds; `Date` and `Null` unchanged

Three of those rules destroy information, and each was added after measuring that a real
round trip destroys it first. The conversion has to lose exactly what the file loses, or no
digest taken before writing can match one taken after:

- **`""` → null.** A worksheet cannot distinguish an empty string cell from an empty cell.
  The two readers do not even agree on what comes back — `fastexcel` returns `None`,
  `python-calamine` returns `''` — which is itself proof the distinction cannot be kept.
- **`NaN`/`±inf` → null.** No writer measured can store them: `xlsxwriter` refuses outright
  without `nan_inf_to_errors`, and `rustpy-xlsxwriter` emits an empty cell.
- **`Datetime` → whole seconds.** Measured, `23:47:16.854775` reads back as `23:47:16`.

`Categorical` is also cut at the limit, though the original table listed truncation for
`String` and `Binary` only: without it an oversized label survives the first pass and is cut
by the second, which breaks idempotency.

With these rules, all 19 fixture columns round-trip exactly — write the converted frame,
read it back with the converted schema, and dtype and value both match.

The metadata and hashes for this conversion are computed on the **converted** values, so
they describe what Excel will actually hold.

### `extract_metadata_from_dataframe` — `metadata/extract.py`

```python
def extract_metadata_from_dataframe(
    df: pl.DataFrame,
    path: UPath,
    timezones: Sequence[str],
    hashers: Sequence[DataFrameHasherBaseClass],
    conversions: Sequence[DataframeConversionBaseClass],
) -> DataframeMetadata | None:
```

Body wrapped in `try/except Exception`: on any failure it logs via `structlog` (`logger.exception`)
and returns `None`. On success returns `DataframeMetadata(frozen=True)`:
`file_name: str` (`path.name`), `full_path: str` (`str(path)`),
`modified_utc: datetime` (aware, from `path.stat().st_mtime` → `datetime.fromtimestamp(mt, tz=UTC)`),
`modified_in_timezones: dict[str, datetime]` (keyed by the input tz names),
`source_columns_metadata: DataframeColumnsMetadata` (`build_columns_metadata(df, hashers)`),
`column_metadata_of_conversions: list[DataframeColumnsMetadata]`
(one per supplied conversion, `conv.metadata_of_converted_dataframe(df, hashers).columns_metadata`).

### JSON sidecar persistence — format pending

Persist one JSON file for each Parquet file. Do not embed the metadata in the Parquet file.
The current extraction API returns an in-memory model only; no sidecar read/write API is
implemented. Filename conventions and the JSON schema have not yet been agreed.

The schema design must settle a schema version, conversion identifiers and versions,
source-file association, typed scalar statistics (including binary, Decimal and temporal
values), non-finite statistics, and row counts needed to reconstruct blank Excel rows.
Plain Pydantic JSON dumping is not yet a persistence contract: binary extrema can fail
UTF-8 serialization and the scalar union can reload temporal and decimal values as strings.
Acceptance tests must serialize to actual JSON bytes and reload them, rather than testing
only `model_dump()` / `model_validate()` with Python objects.

### Excel writer registry — `excel/writer.py`

```python
class ExcelWriterBase(ABC):
    identifier: ClassVar[str]
    @abstractmethod
    def write(self, df: pl.DataFrame, path: UPath, options: Mapping[str, object]) -> None: ...

EXCEL_WRITERS: dict[str, type[ExcelWriterBase]] = {}
def register_excel_writer(cls): ...            # decorator
def get_excel_writer(identifier: str) -> ExcelWriterBase: ...   # raises on unknown

class ExcelWriteConfig(BaseModel):
    writer: str = "rustpy-xlsxwriter"
    options: dict[str, object] = {}
```

`PolarsExcelWriter` (`identifier = "polars-xlsxwriter"`): `df.write_excel(workbook=str(path),
worksheet=..., include_header=True, autofit=False, **options)` — no formatting, tuned for
creation speed. The fixture builder selects its writer through `ExcelWriteConfig`.

### `fast_excel_reader` — `excel/fast_reader.py`

```python
def fast_excel_reader(
    path: UPath, *, sheet_names: Sequence[str] | None = None, max_workers: int | None = None
) -> pl.DataFrame:
```

Reads through `fastexcel` (`pl.read_excel`), resolves target sheets
(all, or `sheet_names`), submits one `_read_sheet` job per sheet to a `ThreadPoolExecutor`
(the Rust parser releases the GIL), builds a `pl.DataFrame` per sheet, and returns `pl.concat(frames, how="vertical_relaxed")`
in sheet order. `max_workers=1` and the default must return identical data — asserted.

**The reader is given the target schema; it never infers.** Callers pass the dtypes of the
ToExcel-converted frame as `schema_overrides`. This is not an optimization: `pl.read_excel`
with no schema **fails outright** on real data, because it downcasts integral-looking floats
and overflows on `Int64`'s maximum. With the schema supplied, all 19 fixture columns come
back matching the model in both dtype and value.
Implementation note to validate with the benchmark test: if per-sheet extraction on one
workbook handle does not actually parallelize, open one handle per worker thread.

## Test fixture — `tests/fixtures/generate.py`

Deterministic, seeded, build-on-demand. `ensure_fixtures() -> FixtureSet` (called by a
session-scoped `conftest.py` fixture):

1. **Two Parquet files**, 1000 rows, one column per scalar Polars dtype
   (`Int8/16/32/64`, `UInt8/16/32/64`, `Float32/64`, `Boolean`, `String`, `Binary`,
   `Date`, `Time`, `Datetime("us","UTC")`, `Duration`, `Decimal`, `Categorical`).
   First rows carry edge cases per dtype (min, max, `0`, null, `""`, a `>32767`-char
   string, `NaN`/`±inf` for floats, epoch and far-future dates); remaining rows are
   seeded-random. `parquet_b` is `parquet_a` with **exactly one cell changed per column**.
   Regeneration is byte-identical (fixed seed, sorted schema).
2. **Three Excel files per Parquet** (6 total), written with `PolarsExcelWriter` from the
   ToExcel-converted frame: `*_full.xlsx` (1000 rows), `*_part1.xlsx` (rows 0–499),
   `*_part2.xlsx` (rows 500–999).
3. Any file already present on disk is reused; only missing files are built.

## Tests (TDD — write first, watch fail, implement)

| Module | Key assertions |
| --- | --- |
| `unit/test_paths.py` | `zpath()` returns the registered implementation; local paths satisfy `os.fspath()`; string round-trip; `storage_options` accepted via mapping and kwargs and inherited by derived paths; pickle round-trip; live credential objects rejected; a direct `UPath` subclass raises |
| `unit/test_canonical.py` | type tags; null sentinel distinct; `NaN` / `-0.0` normalized; float `0.0` ≠ `""`; date/datetime/time stable |
| `unit/test_binary_aggregate.py`, `unit/test_row_hashing.py` | row-shuffle → same digest; moved cells / reordered columns → different dataframe digest; dataframe digest == mod-2¹²⁸ sum of original column totals plus row total; concat and aggregate sums of halves == whole; batch independence; `digest_hex` is 32 lowercase hex; model round-trip |
| `integration/test_wall_clock_roundtrip.py` | UTC, New York DST folds, and Kolkata preserve local clock readings as naive datetimes through the default writer and schema-driven reader; nulls, idempotency, and digest equality |
| `unit/test_conversion_none.py` | frame unchanged; `schema_or_data_changed is False`; metadata columns mirror input |
| `unit/test_conversion_to_excel.py` | numerics/decimal/float32 → `Float64`; bool → `-1.0`/`0.0`; long string truncated + flag `True`; already-Excel-safe frame → flag `False`; duration → seconds; categorical → string; binary → hex |
| `unit/test_metadata_builder.py` | dtype flags correct per dtype; stats (`min/max/value_count/unique_count/null_count`) match Polars; one `HashedDataframe` per hasher per column |
| `unit/test_extract_metadata.py` | happy path fills every field; `modified_utc` tz-aware; per-tz dict keyed by input names; one wrapper per conversion; unreadable path / bad input → `None` + logged |
| `unit/test_excel_writer.py` | registry resolves the default and refuses a duplicate identifier; unknown identifier raises; the **default writer** round-trips the model exactly while `PolarsExcelWriter` is held only to shape; both refuse what they cannot represent; `ExcelWriteConfig` defaults to `rustpy-xlsxwriter` |
| `unit/test_fast_excel_reader.py` | known workbook → expected frame; multi-sheet concatenation in order; `max_workers=1` == default |
| `unit/test_fixtures.py` | Parquet regeneration byte-identical; `parquet_a` vs `parquet_b` differ in exactly one cell per column; all **20** files exist after `ensure_fixtures()` and a second call reuses them; `_padded` truncation pinned, with an early warning before a column fills every edge slot |
| `integration/test_excel_hash_roundtrip.py` | **headline:** for each Parquet file — build `DataframeMetadata` (hashers `[BinaryAggregateHash]`, conversions `[None, ToExcel]`); read `*_full.xlsx` with `fast_excel_reader`, run it through `ToExcel._convert`, hash; assert it equals the ToExcel wrapper's dataframe digest. Then read `*_part1`+`*_part2`, concat in reverse order, same path, assert equal to that digest. Assert `parquet_a` Excel digest ≠ `parquet_b` Excel digest. |

## Verification

```bash
uv add polars pydantic universal-pathlib adlfs xxhash structlog tzdata \
       rustpy-xlsxwriter fastexcel python-calamine xlsxwriter duckdb
uv lock

# per-unit, test-first
uv run pytest tests/unit -q
uv run pytest tests/integration/test_excel_hash_roundtrip.py -q

# full gate -- reports locally; the threshold is applied by CI on merge (see below)
uv run pytest tests --cov --cov-report=term-missing --cov-branch
uv run pre-commit run --files <changed files>                                    # ruff, ruff-format, check-declarations, pyright, mypy
uv run python -m tools.check_declarations src tests
uv run pyright src tests
uv run mypy src tests
```

The coverage threshold is **merge-only**. `[tool.coverage.run] source` scopes the figure to
`src/parquet_to_xl`, and `pyproject.toml` carries no `fail_under`, so local and per-phase runs
report without failing while the package is still largely unimplemented stubs. CI adds
`--cov-fail-under=90` on the pull-request job.

**That 90 is now well below what the suite achieves.** Every module in `src/parquet_to_xl`
is at 100% statement and 100% branch, and has been since phase 2, so the gate no longer
catches a regression until nearly a tenth of the package stops being exercised. Raising it
to 100 is a one-line CI change and is recommended, but it is a CI edit and so is left to a
deliberate decision rather than folded into an implementation commit.

That 90 is coverage.py's **combined** statement-and-branch figure when `branch = true` --
`(executed statements + taken branches) / (total statements + total branches)` -- not two
separate thresholds. A single `fail_under` cannot express "90% statement and 85% branch";
enforcing those independently would mean parsing `coverage json` in CI. This paragraph
replaces the earlier "≥ 90% stmt / 85% branch on changed code" note, which described a gate
that could not be configured as written.

End-to-end check: after `uv run pytest tests/integration -q`, confirm the 20 files exist
under `tests/fixtures/data/` and a second run reuses them (no regeneration). Both are
asserted by `unit/test_fixtures.py` rather than left to inspection.

## Out of scope

Nested dtypes (`List`, `Struct`, `Array`, `Object`); Excel formatting/styling; additional
hashers or converters beyond the two named; real Azure/AWS credential wiring in `zpath()`
(hook only); reading `.xls`; streaming/lazy frames; a CLI (`main.py` is removed).

## Execution notes

- Adding the seven runtime dependencies and regenerating `uv.lock`, and adding the
  `[build-system]` block, are prerequisites for the first implementation step.
- Every new variable annotated before first binding (`tools/check_declarations.py`);
  prefer frozen Pydantic models / frozen dataclasses; Google-style docstrings on modules,
  classes, and non-trivial functions.
