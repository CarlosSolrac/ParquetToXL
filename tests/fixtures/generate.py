"""Deterministic generation of the Parquet and Excel fixtures the test suite reads.

Two Parquet files, one column per scalar Polars dtype, 1000 rows each. The leading rows
carry edge cases per dtype, one row after them is a known delta row, and the rest are
seeded pseudo-random. ``parquet_b`` differs from ``parquet_a`` in exactly one cell per
column -- the delta row -- so a digest that ignores a changed cell is detectable per column
rather than only for the frame.

The **Parquet** files regenerate byte-identically: the schema is written in sorted column
order and every value comes from a fixed seed through the generator in this module, never
from ``random`` or from the clock.

The **workbooks** do not, and cannot. Writing the same frame twice a second apart produces
files that differ, and the difference is confined to one zip member -- ``docProps/core.xml``,
which carries ``dcterms:created`` and ``dcterms:modified`` wall-clock stamps. Every cell is
identical; only that metadata moves. Measured for all three writers: DuckDB is stable,
xlsxwriter and rustpy are not. xlsxwriter could be pinned with ``set_properties``, but
rustpy-xlsxwriter exposes no document-properties API at all, so the chosen writer cannot be
made byte-stable from Python. Compare workbook *contents* rather than workbook hashes.

``ensure_fixtures`` never rewrites a file that already exists, so this costs nothing in
practice: regeneration only happens for a file that is missing.

Every workbook is written three times, by three independent writers, and all three copies
are kept. The set is a sanity check: where the writers agree, the behaviour belongs to
Excel, and where they disagree, it belongs to the writer. None of them is yet the canonical
one, so none claims the unqualified filename -- each carries its writer in its name.

- ``_duckdb.xlsx`` comes from DuckDB's ``excel`` extension, straight from the Parquet with
  **no casting at all**, which makes it the honest baseline for what a dtype does alone.
- ``_polars.xlsx`` comes from ``DataFrame.write_excel``, which is what ``PolarsExcelWriter``
  will wrap in phase 5.
- ``_rustpy.xlsx`` comes from ``rustpy-xlsxwriter``, Rust bindings over the ``rust_xlsxwriter``
  crate, a third implementation of the same file format.

None of the three writes this frame unmodified, and what each one refuses is informative.

The Polars path needs three adjustments, all forced by xlsxwriter's own limits:

- ``Binary`` raises ``TypeError`` outright, so it is hex-encoded before writing. That column
  therefore holds different text in each workbook and is not comparable across them.
- A tz-aware ``Datetime`` raises, so the workbook is opened with ``remove_timezone``. That
  keeps the UTC wall clock, where DuckDB instead converts to the machine's local time.
- ``NaN`` and ``+/-inf`` raise, so the workbook is opened with ``nan_inf_to_errors``, which
  turns them into Excel error cells. DuckDB round-trips all three exactly.

The rustpy path needs exactly one, and it is the spec's own rule rather than a workaround:
a string longer than ``EXCEL_CELL_LIMIT`` makes it **raise**, where DuckDB writes the
oversized string anyway and xlsxwriter silently truncates it. Refusing is the most defensible
of the three, so the frame is truncated to the limit before writing. In exchange it renders
three dtypes as Python text rather than as cell values -- ``Duration`` becomes
``str(timedelta)``, ``Time`` becomes ``str(time)``, and ``Binary`` becomes ``repr(bytes)``,
which writes Python source syntax into the cell, quotes and escape sequences included.

Two DuckDB behaviours are worth stating for the same reason:

- DuckDB reads a Parquet ``Datetime("us", "UTC")`` as ``TIMESTAMP WITH TIME ZONE`` and
  writes it to the sheet in the machine's **local** time, so the value read back is shifted
  by the local UTC offset and carries no zone to undo it with. That makes a digest taken
  from a DuckDB workbook depend on the machine that wrote it.
- DuckDB silently drops NUL from strings, folds CRLF to LF, and strips surrounding
  whitespace. The other two preserve all three.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import polars as pl
import rustpy_xlsxwriter
import xlsxwriter

DATA_DIR: Path = Path(__file__).parent / "data"
"""Where the fixtures land. Gitignored; rebuilt on demand rather than tracked."""

ROW_COUNT: int = 1000
EDGE_ROW_COUNT: int = 48
"""Rows 0 to 47 hold the per-dtype edge cases.

Sized by the widest column rather than chosen: the string column carries 42 distinct cases
and ``padded`` truncates anything longer than this, which would silently drop the tail.
``tests/unit/test_fixtures.py`` fails before a column fills every slot.
"""

DELTA_ROW: int = EDGE_ROW_COUNT
"""The single row whose value differs between ``parquet_a`` and ``parquet_b``, per column."""

PART_ROW_COUNT: int = 500
SEED: int = 0x51501234ABCD0001
EXCEL_CELL_LIMIT: int = 32767

ENUM_CATEGORIES: pl.Enum = pl.Enum(["red", "green", "blue", ""])
"""The enum column's dtype. Its categories are part of the dtype, so they are fixed here."""
"""Excel's documented maximum characters per cell. Recorded because the writer ignores it."""

LCG_MODULUS: int = 1 << 64
LCG_MULTIPLIER: int = 6364136223846793005
LCG_INCREMENT: int = 1442695040888963407

COPY_TO_XLSX: str = """
COPY (
    SELECT *
    FROM read_parquet($source)
    LIMIT $row_limit
    OFFSET $row_offset
)
TO $target
(FORMAT xlsx, HEADER true)
"""
"""Written with no CAST anywhere, so each dtype reaches the sheet as DuckDB holds it."""

SLICES: dict[str, tuple[int, int]] = {"full": (ROW_COUNT, 0), "part1": (PART_ROW_COUNT, 0), "part2": (PART_ROW_COUNT, PART_ROW_COUNT)}
"""Workbook name suffix to (row count, first row), shared by both writers so the two agree."""

RUSTPY_AUTOFIT: bool = False
"""Off for speed. Autofit measures every cell to size the columns, and its own documentation
says to disable it on large datasets. ``dedupe_strings`` is left at its default ``False`` for
the same reason but a stronger one: enabling it buffers the whole sheet to build a shared
string table, which switches the writer out of constant-memory mode."""

XLSXWRITER_OPTIONS: dict[str, bool] = {"remove_timezone": True, "nan_inf_to_errors": True, "strings_to_formulas": False}
"""The first two are forced: without them xlsxwriter raises on tz-aware datetimes and NaN/inf.

``strings_to_formulas`` is not forced, it is a correctness fix. Left at its default, a cell
whose text begins with ``=`` is written as a live formula: the fixture string ``=SUM(1+1)``
was measured coming back as the float ``0.0``, not as text. Any string column holding
user-supplied data can contain such a value, so the default silently converts data into
computation. DuckDB writes the same string as literal text.
"""


class _Lcg:
    """A seeded 64-bit linear congruential generator.

    Written out rather than taken from ``random`` for two reasons: the fixtures must
    regenerate identically on any machine and any Python build, which the standard library
    does not promise across versions, and ``random`` trips Ruff's ``S311``. The multiplier
    and increment are Knuth's MMIX constants.
    """

    state: int

    def __init__(self, seed: int) -> None:
        self.state = seed

    def below(self, bound: int) -> int:
        """Return a value in ``range(bound)``.

        Args:
            bound: Exclusive upper bound, at least 1.

        Returns:
            A deterministic value drawn from the generator's state.
        """
        self.state = (self.state * LCG_MULTIPLIER + LCG_INCREMENT) % LCG_MODULUS
        # The low bits of an LCG cycle far too regularly to use directly.
        return (self.state >> 16) % bound


def padded[T](edges: list[T]) -> list[T]:
    """Cycle a column's edge values up to ``EDGE_ROW_COUNT``.

    Columns carry different numbers of interesting values, but the delta row has to sit at
    the same index in every column for "one changed cell per column" to mean anything.

    Args:
        edges: The values this column considers edge cases, at least one.

    Returns:
        Exactly ``EDGE_ROW_COUNT`` values, repeating the input as needed.
    """
    return [edges[index % len(edges)] for index in range(EDGE_ROW_COUNT)]


def _integer_columns(rng: _Lcg, *, delta: bool) -> dict[str, pl.Series]:
    """Build the ten signed and unsigned integer columns.

    Args:
        rng: The generator supplying the non-edge rows.
        delta: Whether to write the ``parquet_b`` value into the delta row.

    Returns:
        The columns, keyed by name.
    """
    spec: dict[str, tuple[pl.DataType, int, int]] = {
        "int8": (pl.Int8(), -128, 127),
        "int16": (pl.Int16(), -32768, 32767),
        "int32": (pl.Int32(), -2147483648, 2147483647),
        "int64": (pl.Int64(), -9223372036854775808, 9223372036854775807),
        "uint8": (pl.UInt8(), 0, 255),
        "uint16": (pl.UInt16(), 0, 65535),
        "uint32": (pl.UInt32(), 0, 4294967295),
        "uint64": (pl.UInt64(), 0, 18446744073709551615),
        # The 128-bit widths were absent until a dtype vocabulary built from this frame
        # silently dropped them. A fixture that does not carry a dtype cannot catch its loss.
        "int128": (pl.Int128(), -170141183460469231731687303715884105728, 170141183460469231731687303715884105727),
        "uint128": (pl.UInt128(), 0, 340282366920938463463374607431768211455),
    }
    built: dict[str, pl.Series] = {}
    name: str
    for name in spec:
        dtype: pl.DataType
        low: int
        high: int
        dtype, low, high = spec[name]
        # Both bounds matter: the extremes are exactly where a float64 Excel cell stops
        # being able to hold the value, which is what uint64 demonstrates.
        values: list[int | None] = padded([low, high, 0 if low < 0 else 1, None, low + 1, high - 1])
        values.append(1 if delta else 0)
        while len(values) < ROW_COUNT:
            values.append(low + rng.below(high - low + 1))
        built[name] = pl.Series(name, values, dtype=dtype)
    return built


def _float_columns(rng: _Lcg, *, delta: bool) -> dict[str, pl.Series]:
    """Build the three float columns, carrying the non-finite and signed-zero cases.

    Args:
        rng: The generator supplying the non-edge rows.
        delta: Whether to write the ``parquet_b`` value into the delta row.

    Returns:
        The columns, keyed by name.
    """
    built: dict[str, pl.Series] = {}
    name: str
    widths: dict[str, tuple[pl.DataType, float]] = {
        "float16": (pl.Float16(), 65504.0),
        "float32": (pl.Float32(), 3.4028234663852886e38),
        "float64": (pl.Float64(), 1.7976931348623157e308),
    }
    for name in widths:
        dtype: pl.DataType
        largest: float
        dtype, largest = widths[name]
        values: list[float | None] = padded([0.0, -0.0, float("nan"), float("inf"), float("-inf"), 1.5, None, largest, -largest])
        values.append(2.5 if delta else 1.25)
        while len(values) < ROW_COUNT:
            values.append(rng.below(1000000) / 1000.0)
        built[name] = pl.Series(name, values, dtype=dtype)
    return built


def _temporal_columns(rng: _Lcg, *, delta: bool) -> dict[str, pl.Series]:
    """Build the date, time, datetime and duration columns.

    The date column deliberately reaches below Excel's 1900 serial epoch, which is where an
    Excel round-trip stops being lossless, and the datetime column reaches the 2262
    nanosecond boundary the canonical encoding calls out.

    Args:
        rng: The generator supplying the non-edge rows.
        delta: Whether to write the ``parquet_b`` value into the delta row.

    Returns:
        The columns, keyed by name.
    """
    dates: list[dt.date | None] = padded([dt.date(1970, 1, 1), dt.date(1899, 12, 31), dt.date(1900, 1, 1), dt.date(9999, 12, 31), None, dt.date(2000, 2, 29), dt.date(2262, 4, 11)])
    dates.append(dt.date(2024, 6, 1) if delta else dt.date(2024, 5, 31))
    times: list[dt.time | None] = padded([dt.time(0, 0), dt.time(23, 59, 59, 999999), None, dt.time(12, 0), dt.time(0, 0, 0, 1)])
    times.append(dt.time(6, 30) if delta else dt.time(6, 29))
    stamps: list[dt.datetime | None] = padded(
        [
            dt.datetime(1970, 1, 1, tzinfo=dt.UTC),
            dt.datetime(1899, 12, 31, tzinfo=dt.UTC),
            dt.datetime(2262, 4, 11, 23, 47, 16, 854775, tzinfo=dt.UTC),
            dt.datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=dt.UTC),
            None,
            dt.datetime(2000, 2, 29, 12, 0, tzinfo=dt.UTC),
        ]
    )
    stamps.append(dt.datetime(2024, 6, 1, tzinfo=dt.UTC) if delta else dt.datetime(2024, 5, 31, tzinfo=dt.UTC))
    # Duration("us") is int64 microseconds, so timedelta.max does not fit; 100000 days does.
    spans: list[dt.timedelta | None] = padded([dt.timedelta(0), dt.timedelta(microseconds=1), dt.timedelta(microseconds=-1), dt.timedelta(days=100000), dt.timedelta(days=-100000), None, dt.timedelta(seconds=1)])
    spans.append(dt.timedelta(hours=2) if delta else dt.timedelta(hours=1))
    while len(dates) < ROW_COUNT:
        dates.append(dt.date(1970, 1, 1) + dt.timedelta(days=rng.below(30000)))
        times.append(dt.time(rng.below(24), rng.below(60), rng.below(60)))
        stamps.append(dt.datetime(1970, 1, 1, tzinfo=dt.UTC) + dt.timedelta(seconds=rng.below(2000000000)))
        spans.append(dt.timedelta(seconds=rng.below(1000000)))
    return {
        "date": pl.Series("date", dates, dtype=pl.Date()),
        "time": pl.Series("time", times, dtype=pl.Time()),
        "datetime": pl.Series("datetime", stamps, dtype=pl.Datetime("us", "UTC")),
        "duration": pl.Series("duration", spans, dtype=pl.Duration("us")),
    }


def _other_columns(rng: _Lcg, *, delta: bool) -> dict[str, pl.Series]:
    """Build the boolean, string, binary, decimal, categorical and enum columns.

    The string column carries a value longer than ``EXCEL_CELL_LIMIT`` so the round-trip can
    be measured against Excel's documented maximum rather than assumed to respect it.

    Args:
        rng: The generator supplying the non-edge rows.
        delta: Whether to write the ``parquet_b`` value into the delta row.

    Returns:
        The columns, keyed by name.
    """
    flags: list[bool | None] = padded([True, False, None])
    flags.append(bool(delta))
    texts: list[str | None] = padded(
        [
            # Plain and structural cases.
            "",
            "a",
            None,
            "unicode-ünïcødé",
            "x" * (EXCEL_CELL_LIMIT + 33),
            "line\nbreak",
            "tab\there",
            "a\r\nb",
            'quote"s',
            "  padded  ",
            "0",
            "TRUE",
            "0012",
            "2020-01-01",
            "'quoted",
            # A leading "=" is data here, not computation. xlsxwriter turns it into a live
            # formula unless strings_to_formulas is off; see XLSXWRITER_OPTIONS.
            "=SUM(1+1)",
            # C0 and C1 control characters. The XLSX spec forbids most of these in cell
            # text, yet both writers emit them and calamine reads them back unchanged --
            # except NUL, which DuckDB drops, taking the rest of nothing with it.
            "nul\x00inside",
            "soh\x01",
            "bel\x07",
            "backspace\x08",
            "vtab\x0b",
            "formfeed\x0c",
            "escape\x1b",
            "delete\x7f",
            # Emoji, including the sequences that are several code points per glyph: a
            # zero-width-joiner family, a skin-tone modifier, and a regional-indicator flag.
            "snow \u2603",
            "target \U0001f3af",
            "family \U0001f468\u200d\U0001f469\u200d\U0001f467\u200d\U0001f466",
            "thumb \U0001f44d\U0001f3fd",
            "flag \U0001f1ef\U0001f1f5",
            # A normalization trap: these two render identically and must not be folded
            # together. Escaped, because the difference is invisible in a source listing.
            "e\u0301 decomposed",
            "\u00e9 precomposed",
            # Invisible and directional formatting, escaped for the same reason.
            "zwsp a\u200bb",
            "zwnj a\u200cb",
            "nbsp a\u00a0b",
            "bom \ufeff",
            "rtl \u05d0\u05d1\u05d2",
            "arabic \u0627\u0644\u0639\u0631\u0628\u064a\u0629",
            "bidi override a\u202eb",
            # Unicode has its own line and paragraph separators, distinct from \n.
            "line sep a\u2028b",
            "para sep a\u2029b",
            # Astral-plane CJK, which is a surrogate pair in UTF-16.
            "\U00020000 ext-b",
            "mixed \U0001f3af \u05d0 \u2603 end",
        ]
    )
    texts.append("delta-b" if delta else "delta-a")
    blobs: list[bytes | None] = padded([b"", b"\x00", b"\xff\xfe", None, bytes(range(256)), b"\x00\x01\x02"])
    blobs.append(b"\xbb" if delta else b"\xaa")
    numbers: list[Decimal | None] = padded([Decimal("0.0000"), Decimal("1.2500"), Decimal("-9.9900"), None, Decimal("99999999999999.9999"), Decimal("-99999999999999.9999")])
    numbers.append(Decimal("2.5000") if delta else Decimal("1.5000"))
    labels: list[str | None] = padded(["alpha", "beta", None, "gamma", ""])
    labels.append("delta" if delta else "epsilon")
    # Enum's categories are part of its dtype, so the set is declared once and every value
    # has to come from it -- unlike Categorical, which accepts anything. The empty label is
    # here for the same reason it is in the categorical column: it becomes null in Excel.
    choices: list[str | None] = padded(["red", "green", None, "blue", ""])
    choices.append("green" if delta else "red")
    while len(flags) < ROW_COUNT:
        flags.append(rng.below(2) == 0)
        texts.append(f"row-{rng.below(1000000)}")
        blobs.append(bytes([rng.below(256), rng.below(256)]))
        numbers.append(Decimal(rng.below(10000000)) / Decimal(10000))
        labels.append(("alpha", "beta", "gamma")[rng.below(3)])
        choices.append(("red", "green", "blue")[rng.below(3)])
    return {
        "boolean": pl.Series("boolean", flags, dtype=pl.Boolean()),
        "string": pl.Series("string", texts, dtype=pl.String()),
        "binary": pl.Series("binary", blobs, dtype=pl.Binary()),
        "decimal": pl.Series("decimal", numbers, dtype=pl.Decimal(18, 4)),
        "categorical": pl.Series("categorical", labels, dtype=pl.Categorical()),
        "enum": pl.Series("enum", choices, dtype=ENUM_CATEGORIES),
    }


def _write_workbooks_rustpy(frame: pl.DataFrame, stem: str) -> list[Path]:
    """Write one frame's three workbooks through ``rustpy-xlsxwriter``.

    Strings are cut to ``EXCEL_CELL_LIMIT`` first, because this writer raises on a longer
    one rather than truncating or ignoring it. That refusal is the reason the truncation
    here is a rule and not a workaround: the other two writers disagree with each other
    about what an oversized cell means, and this one declines to guess.

    Args:
        frame: The source frame, exactly as written to Parquet.
        stem: The fixture name, used as the workbook filename prefix.

    Returns:
        The workbook paths, in full, part1, part2 order.
    """
    trimmed: pl.DataFrame = frame.with_columns(pl.col("string").str.slice(0, EXCEL_CELL_LIMIT))
    written: list[Path] = []
    label: str
    for label in SLICES:
        row_limit: int
        row_offset: int
        row_limit, row_offset = SLICES[label]
        target: Path = DATA_DIR / f"{stem}_{label}_rustpy.xlsx"
        if not target.exists():
            # iter_rows streams; to_dicts() materialises every row first. The writer accepts
            # either and produces byte-identical output, so streaming is free here and is
            # what keeps constant-memory mode meaningful on a frame that does not fit twice.
            rows: Iterator[dict[str, Any]] = trimmed.slice(row_offset, row_limit).iter_rows(named=True)
            rustpy_xlsxwriter.write_worksheet(rows, target, "Sheet1", autofit=RUSTPY_AUTOFIT)
        written.append(target)
    return written


def build_frame(*, delta: bool) -> pl.DataFrame:
    """Build one of the two source frames.

    Args:
        delta: ``False`` builds ``parquet_a``; ``True`` builds ``parquet_b``, which is
            identical except for one cell per column at ``DELTA_ROW``.

    Returns:
        A frame of ``ROW_COUNT`` rows with columns in sorted name order.
    """
    rng: _Lcg = _Lcg(SEED)
    columns: dict[str, pl.Series] = {}
    columns.update(_integer_columns(rng, delta=delta))
    columns.update(_float_columns(rng, delta=delta))
    columns.update(_temporal_columns(rng, delta=delta))
    columns.update(_other_columns(rng, delta=delta))
    return pl.DataFrame({name: columns[name] for name in sorted(columns)})


def _write_workbooks_duckdb(connection: duckdb.DuckDBPyConnection, source: Path, stem: str) -> list[Path]:
    """Write one Parquet file's three workbooks through DuckDB, casting nothing.

    Args:
        connection: An open DuckDB connection with the ``excel`` extension loaded.
        source: The Parquet file to read.
        stem: The fixture name, used as the workbook filename prefix.

    Returns:
        The workbook paths, in full, part1, part2 order.
    """
    written: list[Path] = []
    label: str
    for label in SLICES:
        row_limit: int
        row_offset: int
        row_limit, row_offset = SLICES[label]
        target: Path = DATA_DIR / f"{stem}_{label}_duckdb.xlsx"
        if not target.exists():
            connection.execute(COPY_TO_XLSX, {"source": str(source), "row_limit": row_limit, "row_offset": row_offset, "target": str(target)})
        written.append(target)
    return written


def _write_workbooks_polars(frame: pl.DataFrame, stem: str) -> list[Path]:
    """Write one frame's three workbooks through ``DataFrame.write_excel``.

    The frame is hex-encoded in ``binary`` first and the workbook is opened with
    ``XLSXWRITER_OPTIONS``, because xlsxwriter raises on bytes, on tz-aware datetimes and on
    non-finite floats. Those three are the writer's limits, not a conversion this project
    chose, and they are why this workbook and its DuckDB twin disagree.

    Args:
        frame: The source frame, exactly as written to Parquet.
        stem: The fixture name, used as the workbook filename prefix.

    Returns:
        The workbook paths, in full, part1, part2 order.
    """
    encoded: pl.DataFrame = frame.with_columns(pl.col("binary").bin.encode("hex"))
    written: list[Path] = []
    label: str
    for label in SLICES:
        row_limit: int
        row_offset: int
        row_limit, row_offset = SLICES[label]
        target: Path = DATA_DIR / f"{stem}_{label}_polars.xlsx"
        if not target.exists():
            book: xlsxwriter.Workbook = xlsxwriter.Workbook(str(target), XLSXWRITER_OPTIONS)
            try:
                encoded.slice(row_offset, row_limit).write_excel(workbook=book, worksheet="Sheet1")
            finally:
                book.close()
        written.append(target)
    return written


def ensure_fixtures() -> dict[str, Path]:
    """Create any missing fixture file and return every fixture path.

    Files already on disk are left untouched, so a run that needs one missing workbook does
    not rewrite the other seven.

    Returns:
        Paths keyed by fixture name, e.g. ``"parquet_a"`` and ``"parquet_a_full"``.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    frames: dict[str, pl.DataFrame] = {}
    stem: str
    for stem in ("parquet_a", "parquet_b"):
        source: Path = DATA_DIR / f"{stem}.parquet"
        if not source.exists():
            build_frame(delta=stem == "parquet_b").write_parquet(source)
        paths[stem] = source
        frames[stem] = pl.read_parquet(source)
    connection: duckdb.DuckDBPyConnection = duckdb.connect(":memory:")
    try:
        connection.execute("INSTALL excel")
        connection.execute("LOAD excel")
        for stem in ("parquet_a", "parquet_b"):
            book: Path
            for book in [*_write_workbooks_duckdb(connection, paths[stem], stem), *_write_workbooks_polars(frames[stem], stem), *_write_workbooks_rustpy(frames[stem], stem)]:
                paths[book.stem] = book
    finally:
        connection.close()
    return paths
