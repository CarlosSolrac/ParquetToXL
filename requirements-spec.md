# ParquetToXL — Dataframe Metadata, Hashing & Fast Excel I/O

**Status:** design approved 2026-09-06; implementation pending (separate session).

## Context

`ParquetToXL` is a Python 3.13 project (uv, Ruff strict rule set, pyright + mypy strict,
pytest with a 90% branch-coverage gate, `tools/check_declarations.py`). At the time this
spec was written there was no runtime code and no runtime dependencies.

The goal is a library that, given a Polars DataFrame loaded from a Parquet file, produces a
validated metadata record describing every column, computes **order-independent** content
hashes, models what happens to that data when it is round-tripped through Excel, and can
read Excel back fast. The headline acceptance test: hash a DataFrame read back from a
generated `.xlsx` and confirm it equals the "Excel hash" recorded in the Parquet file's
metadata — including when the rows were split across two 500-row workbooks and reassembled
in a different order.

### Design decisions

| Topic | Decision |
| --- | --- |
| Logging | `structlog` (the original "Serilog" note reinterpreted for Python) |
| `DataframeColumnMetadata` | one object **per column**; a wrapper `DataframeColumnsMetadata` holds the ordered list + whole-frame hashes |
| Conversions | **explicit required parameter** to `extract_metadata_from_dataframe` |
| Hasher API | base class exposes **both** `hash_column()` and `hash_dataframe()` |
| Dtype coverage | **all scalar Polars dtypes**, no nested types |
| `fast_excel_reader` | `python-calamine` + `ThreadPoolExecutor` across sheets; one path in, one frame out |
| Binary aggregate hash | **xxh3_128**, per-value hash summed mod 2¹²⁸ (row-order independent); **no column binding** |
| Fixture files | generated on first run into `tests/fixtures/data/`, **gitignored** |
| `ZPath` | thin hook-point subclass of `UPath`; override init only (future Azure/AWS credential wiring) |
| `HashedDataframe` | Pydantic **discriminated union on `identifier`**, per-hasher subclass + `version` |
| Reload flag | named **`schema_or_data_changed`**; `True` when a converter altered data or schema |
| Excel writer | `polars.write_excel` behind a small config-driven **writer registry** (speed first) |
| Package | `src/parquet_to_xl/`, src layout; remove the empty `main.py` |
| Build backend | add `[build-system]` = `hatchling` + `[tool.hatch.build.targets.wheel] packages = ["src/parquet_to_xl"]`; uv installs the package editable so tests run against the installed package |
| Method | **TDD** — test first for every unit, watch it fail, then implement |

### Assumptions

- `timezones` is `list[str]` of IANA names; converted with `zoneinfo.ZoneInfo`.
- ToExcel boolean mapping: `True → -1.0`, `False → 0.0`, `null → null`, dtype `Float64`.
- ToExcel `Duration → Float64` seconds; `Binary →` lowercase-hex `String`; `Categorical → String`.
- The two 500-row Excel files are rows `[0:500]` and `[500:1000]` of the 1000-row frame.
- Fixture RNG is stdlib `random` seeded with a module constant (no numpy dependency).
- Excel string cell limit treated as 32,767 characters.

## New dependencies

Runtime (`[project].dependencies`): `polars`, `pydantic`, `universal-pathlib`, `xxhash`,
`structlog`, `python-calamine`, `xlsxwriter`, `tzdata`.

`tzdata` is not optional and is deliberately unconditional. Windows ships no system
timezone database, so without it `zoneinfo.TZPATH` is empty and **every** IANA lookup
fails — including `ZoneInfo("UTC")`, which means simply reading a value out of a
`Datetime("us", "UTC")` column raises `ZoneInfoNotFoundError`. Linux CI images usually
do carry a system database, so omitting it produces the worst kind of bug: green in CI,
broken on a developer's machine. Pinning it also makes conversions depend on the
lockfile rather than on whatever tz database version the host happens to ship, which
matters for a library whose entire purpose is comparing content digests across machines.
Dev: none beyond the existing `pytest` / `pytest-cov`.
`uv.lock` is regenerated and committed (CI installs `--locked`).

## Package layout

```
src/parquet_to_xl/
  __init__.py
  logging.py            configure_logging(json_output: bool) -> None   (structlog + stdlib bridge)
  paths.py              ZPath(UPath)
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
    writer.py           ExcelWriterBase, registry, PolarsExcelWriter, ExcelWriteConfig
    fast_reader.py      fast_excel_reader(path, *, sheet_names=None, max_workers=None) -> pl.DataFrame
tests/
  conftest.py
  fixtures/
    generate.py         deterministic parquet + excel builder
    data/               generated, gitignored
  unit/                  one module per unit above
  integration/
    test_excel_hash_roundtrip.py
```

`tests/fixtures/data/` is added to `.gitignore`. `main.py` is deleted.

## Components

### `ZPath` — `paths.py`

Thin subclass of `upath.UPath`. Overrides construction only, as the single future seam for
cloud credentials:

```python
class ZPath(UPath):
    def __init__(self, *args: str | os.PathLike[str],
                 storage_options: Mapping[str, str] | None = None,
                 **kwargs: object) -> None:
```

For now it pops `storage_options`, stores it on the instance, and forwards everything else
to `super().__init__`. No normalization, no protocol pinning. `isinstance(ZPath(...), UPath)`
holds. Every call site in this library uses `ZPath`, never `Path`/`UPath` directly.
UPath dispatches through `__new__`; the tricky part is threading the kwarg through both
`__new__` and `__init__` without breaking UPath's protocol handlers — pinned by tests.

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
| `timedelta` | `b"\x0a" + struct.pack("<q", microseconds)` |

The type tag is a property of the value, not the column, so it is kept under the
"no column binding" decision. It is constant within a single-column digest, but
`hash_dataframe` pools every value of every column into one additive sum, and `Float64`,
`Date`, `Datetime`, and `Time` all serialize to 8 bytes via `struct.pack` — the tag stops
e.g. `Date(1)` and `Datetime(1)` contributing an identical term to that sum.

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
`identifier: Literal["binary-aggregate-xxh3-128"]`, `version: int = 1`,
`scope: Literal["column", "dataframe"]`, `bit_width: Literal[128] = 128`,
`digest_hex: str` (`Field(pattern=r"^[0-9a-f]{32}$")`).
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
- `hash_dataframe(df)`: accumulate over every value of every column (equivalently the mod-2¹²⁸ sum of the column digests); `scope="dataframe"`.

Properties this buys (all asserted by tests): shuffling rows leaves the digest unchanged;
concatenating the two 500-row halves reproduces the 1000-row digest; one changed cell
changes the digest.

### Column metadata — `metadata/column.py`, `columns.py`, `builder.py`

`DataframeColumnMetadata(BaseModel, frozen=True)` — one per column:
`description: str`, `name: str`, `polars_dtype: str` (str of the `pl.DataType`),
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
`description: str`, `columns: list[DataframeColumnMetadata]` (dataframe order),
`dataframe_hashes: list[HashedDataframe]` (one per hasher, from `hash_dataframe`).

`build_columns_metadata(df, hashers)` is the shared constructor used by both the
source-frame path and every conversion: per column it derives the dtype flags, computes
stats with Polars, and calls each hasher's `hash_column`; then calls each hasher's
`hash_dataframe` for the wrapper.

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
`schema_or_data_changed = converted.schema != df.schema or any_string_truncated`:
- `Int*/UInt*/Float32/Decimal → Float64`
- `Boolean → Float64` via `True→-1.0`, `False→0.0`, `null→null`
- `String → String` truncated at 32,767 chars (records whether any row was cut)
- `Binary →` lowercase-hex `String`, then the same truncation
- `Categorical → String` (labels)
- `Duration → Float64` seconds
- `Date` / `Datetime(UTC)` / `Time` unchanged; `Null` unchanged

The metadata and hashes for this conversion are computed on the **converted** values, so
they describe what Excel will actually hold.

### `extract_metadata_from_dataframe` — `metadata/extract.py`

```python
def extract_metadata_from_dataframe(
    df: pl.DataFrame,
    path: ZPath,
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

### Excel writer registry — `excel/writer.py`

```python
class ExcelWriterBase(ABC):
    identifier: ClassVar[str]
    @abstractmethod
    def write(self, df: pl.DataFrame, path: ZPath, options: Mapping[str, object]) -> None: ...

EXCEL_WRITERS: dict[str, type[ExcelWriterBase]] = {}
def register_excel_writer(cls): ...            # decorator
def get_excel_writer(identifier: str) -> ExcelWriterBase: ...   # raises on unknown

class ExcelWriteConfig(BaseModel):
    writer: str = "polars-xlsxwriter"
    options: dict[str, object] = {}
```

`PolarsExcelWriter` (`identifier = "polars-xlsxwriter"`): `df.write_excel(workbook=str(path),
worksheet=..., include_header=True, autofit=False, **options)` — no formatting, tuned for
creation speed. The fixture builder selects its writer through `ExcelWriteConfig`.

### `fast_excel_reader` — `excel/fast_reader.py`

```python
def fast_excel_reader(
    path: ZPath, *, sheet_names: Sequence[str] | None = None, max_workers: int | None = None
) -> pl.DataFrame:
```

Opens `python_calamine.CalamineWorkbook.from_path(str(path))`, resolves target sheets
(all, or `sheet_names`), submits one `_read_sheet` job per sheet to a `ThreadPoolExecutor`
(`calamine` releases the GIL while parsing), builds a `pl.DataFrame` per sheet from the
cell matrix (`infer_schema_length=None`), and returns `pl.concat(frames, how="vertical_relaxed")`
in sheet order. `max_workers=1` and the default must return identical data — asserted.
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
| `unit/test_paths.py` | `ZPath` is a `UPath`; string round-trip; `storage_options` accepted and stored; forwards to UPath without breaking local-fs ops |
| `unit/test_canonical.py` | type tags; null sentinel distinct; `NaN` / `-0.0` normalized; float `0.0` ≠ `""`; date/datetime/time stable |
| `unit/test_binary_aggregate.py` | row-shuffle → same digest; one cell changed → different digest; dataframe digest == mod-2¹²⁸ sum of column digests; concat of halves == whole; `digest_hex` is 32 lowercase hex; discriminated-union `model_validate`/`model_dump` round-trip |
| `unit/test_conversion_none.py` | frame unchanged; `schema_or_data_changed is False`; metadata columns mirror input |
| `unit/test_conversion_to_excel.py` | numerics/decimal/float32 → `Float64`; bool → `-1.0`/`0.0`; long string truncated + flag `True`; already-Excel-safe frame → flag `False`; duration → seconds; categorical → string; binary → hex |
| `unit/test_metadata_builder.py` | dtype flags correct per dtype; stats (`min/max/value_count/unique_count/null_count`) match Polars; one `HashedDataframe` per hasher per column |
| `unit/test_extract_metadata.py` | happy path fills every field; `modified_utc` tz-aware; per-tz dict keyed by input names; one wrapper per conversion; unreadable path / bad input → `None` + logged |
| `unit/test_excel_writer.py` | registry resolves default; unknown identifier raises; `PolarsExcelWriter` output is calamine-readable; `ExcelWriteConfig` defaults |
| `unit/test_fast_excel_reader.py` | known workbook → expected frame; multi-sheet concatenation in order; `max_workers=1` == default |
| `unit/test_fixtures.py` | regeneration byte-identical; `parquet_a` vs `parquet_b` differ in exactly one cell per column; all 8 files exist after `ensure_fixtures()` |
| `integration/test_excel_hash_roundtrip.py` | **headline:** for each Parquet file — build `DataframeMetadata` (hashers `[BinaryAggregateHash]`, conversions `[None, ToExcel]`); read `*_full.xlsx` with `fast_excel_reader`, run it through `ToExcel._convert`, hash; assert it equals the ToExcel wrapper's dataframe digest. Then read `*_part1`+`*_part2`, concat in reverse order, same path, assert equal to that digest. Assert `parquet_a` Excel digest ≠ `parquet_b` Excel digest. |

## Verification

```bash
uv add polars pydantic universal-pathlib xxhash structlog python-calamine xlsxwriter
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

That 90 is coverage.py's **combined** statement-and-branch figure when `branch = true` --
`(executed statements + taken branches) / (total statements + total branches)` -- not two
separate thresholds. A single `fail_under` cannot express "90% statement and 85% branch";
enforcing those independently would mean parsing `coverage json` in CI. This paragraph
replaces the earlier "≥ 90% stmt / 85% branch on changed code" note, which described a gate
that could not be configured as written.

End-to-end check: after `uv run pytest tests/integration -q`, confirm the 8 files exist
under `tests/fixtures/data/` and a second run reuses them (no regeneration).

## Out of scope

Nested dtypes (`List`, `Struct`, `Array`, `Object`); Excel formatting/styling; additional
hashers or converters beyond the two named; real Azure/AWS credential wiring in `ZPath`
(hook only); reading `.xls`; streaming/lazy frames; a CLI (`main.py` is removed).

## Execution notes

- Adding the seven runtime dependencies and regenerating `uv.lock`, and adding the
  `[build-system]` block, are prerequisites for the first implementation step.
- Every new variable annotated before first binding (`tools/check_declarations.py`);
  prefer frozen Pydantic models / frozen dataclasses; Google-style docstrings on modules,
  classes, and non-trivial functions.
