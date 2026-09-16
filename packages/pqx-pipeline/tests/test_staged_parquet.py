"""Frozen tests for a converted frame surviving a round trip through Parquet.

Its own module because it is its own ticket. **Nothing stages a frame yet** -- see
``docs/backlog.md`` item 3 for why the work stopped and what has to be decided before it resumes.
These tests exist because the properties it would need are cheap to establish now and expensive to
discover the hard way later, and because knowing they hold is most of what makes the decision.

The write phase holds each source's converted frame in memory from planning until the last workbook
is written. Spending disk to bound that means writing the converted frame to Parquet in scratch and
reading each sheet back, which is only available if the round trip gives back exactly what went in.

Two properties, both load-bearing:

- **Values and dtypes.** Fragment digests in the manifest are taken over the frames the writer
  receives. If Parquet gave back a value or a dtype that differed at all, every published digest
  would shift and ``pqx verify`` would reject exports that are perfectly good.
- **Row order.** ``_slices`` cuts sheets with a positional cursor -- each sheet takes the next N
  rows -- so the file's order *is* the plan's order. Polars concatenates row groups in order, so
  this is expected to hold; it is asserted because the cost of it not holding is silent
  misassignment of rows to sheets rather than an error.

Read-only for an implementer. If one of these looks wrong, stop and report it rather than
editing it.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import TYPE_CHECKING

import polars as pl
import pytest
from pqx_frame.conversion.to_excel import EXCEL_CELL_LIMIT, DataframeConversionToExcel
from pqx_frame.hashing.binary_aggregate import DataFrameHasherBinaryAggregateHash

if TYPE_CHECKING:
    from pathlib import Path

ROWS: int = 40
"""Enough rows that an order change would be visible, and that more than one row group is
plausible once the writer is asked for small ones."""


def _source() -> pl.DataFrame:
    """Every scalar dtype an export can carry, at the values that move under conversion."""
    return pl.DataFrame(
        {
            "int64": pl.Series([-9223372036854775808 + index for index in range(ROWS)], dtype=pl.Int64),
            "uint64": pl.Series([18446744073709551615 - index for index in range(ROWS)], dtype=pl.UInt64),
            "float64": pl.Series([float("inf"), float("nan"), -0.0, *[index / 7 for index in range(ROWS - 3)]], dtype=pl.Float64),
            "boolean": pl.Series([index % 3 == 0 for index in range(ROWS)], dtype=pl.Boolean),
            "string": pl.Series(["x" * (EXCEL_CELL_LIMIT + 7), "", *[f"row {index}" for index in range(ROWS - 2)]], dtype=pl.String),
            "binary": pl.Series([b"\xff\xfe", b"", *[bytes([index % 256]) for index in range(ROWS - 2)]], dtype=pl.Binary),
            "date": pl.Series([dt.date(1899, 12, 31) + dt.timedelta(days=index * 97) for index in range(ROWS)], dtype=pl.Date),
            "time": pl.Series([dt.time(index % 24, index % 60, 59, 999999) for index in range(ROWS)], dtype=pl.Time),
            "datetime": pl.Series([dt.datetime(1970, 1, 1, tzinfo=dt.UTC) + dt.timedelta(days=index * 997) for index in range(ROWS)], dtype=pl.Datetime("us", "UTC")),
            "duration": pl.Series([dt.timedelta(microseconds=1 - index) for index in range(ROWS)], dtype=pl.Duration("us")),
            "decimal": pl.Series([Decimal(f"{index}.2500") for index in range(ROWS)], dtype=pl.Decimal(18, 4)),
            "categorical": pl.Series([f"c{index % 5}" for index in range(ROWS)], dtype=pl.Categorical),
            "nulls": pl.Series([None if index % 4 else index for index in range(ROWS)], dtype=pl.Int64),
        },
    )


def _converted() -> pl.DataFrame:
    """The frame as ToExcel leaves it, which is what would be staged."""
    return DataframeConversionToExcel().metadata_of_converted_dataframe(_source(), []).converted_dataframe


def _digest(df: pl.DataFrame) -> str:
    return DataFrameHasherBinaryAggregateHash().hash_dataframe(df).digest_hex


def _round_trip(df: pl.DataFrame, tmp_path: Path) -> pl.DataFrame:
    """Write the frame to Parquet and read it back, as staging would."""
    staged: Path = tmp_path / "staged.parquet"
    df.write_parquet(staged)
    return pl.read_parquet(staged)


def test_every_dtype_the_conversion_emits_survives_the_round_trip(tmp_path: Path) -> None:
    converted: pl.DataFrame = _converted()
    returned: pl.DataFrame = _round_trip(converted, tmp_path)
    assert returned.schema == converted.schema
    assert returned.columns == converted.columns
    assert returned.height == converted.height


def test_the_digest_of_the_whole_frame_is_unchanged_by_the_round_trip(tmp_path: Path) -> None:
    # Stronger than schema equality and closer to what the manifest records: this is the number
    # `expected_whole` carries.
    converted: pl.DataFrame = _converted()
    assert _digest(_round_trip(converted, tmp_path)) == _digest(converted)


def test_the_rows_come_back_in_the_order_they_went_in(tmp_path: Path) -> None:
    # _slices cuts with a positional cursor, so the file's order is the plan's order. A reordering
    # would not raise anywhere; it would put rows in the wrong sheets.
    converted: pl.DataFrame = _converted()
    returned: pl.DataFrame = _round_trip(converted, tmp_path)
    assert returned["string"].to_list() == converted["string"].to_list()
    assert returned["date"].to_list() == converted["date"].to_list()


@pytest.mark.parametrize(("offset", "length"), [(0, 1), (0, 9), (9, 9), (17, 13), (ROWS - 1, 1)])
def test_the_digest_of_a_slice_is_unchanged_by_the_round_trip(offset: int, length: int, tmp_path: Path) -> None:
    # The property the manifest actually rests on: a fragment is a positional slice, and its digest
    # is published. Equal whole-frame digests would not catch two rows having swapped places.
    converted: pl.DataFrame = _converted()
    returned: pl.DataFrame = _round_trip(converted, tmp_path)
    assert _digest(returned.slice(offset, length)) == _digest(converted.slice(offset, length))


def test_a_slice_read_lazily_by_offset_matches_the_same_slice_of_the_frame(tmp_path: Path) -> None:
    # How the writer would actually take a sheet: scan the staged file and push the offset down,
    # rather than reading the whole thing back and slicing it in memory.
    converted: pl.DataFrame = _converted()
    staged: Path = tmp_path / "staged.parquet"
    converted.write_parquet(staged)
    taken: pl.DataFrame = pl.scan_parquet(staged).slice(11, 7).collect()
    assert _digest(taken) == _digest(converted.slice(11, 7))


def test_the_whole_digest_equals_the_digests_of_its_chunks_combined(tmp_path: Path) -> None:
    # What lets `expected_whole` be taken over the staged file without holding it in memory.
    # Independent of how the plan cut fragments, so it still catches a row the plan dropped --
    # which summing the published fragment digests would not.
    converted: pl.DataFrame = _converted()
    returned: pl.DataFrame = _round_trip(converted, tmp_path)
    hasher: DataFrameHasherBinaryAggregateHash = DataFrameHasherBinaryAggregateHash()
    chunks: list[pl.DataFrame] = [returned.slice(offset, 7) for offset in range(0, returned.height, 7)]
    combined: str = hasher.combine([hasher.hash_dataframe(chunk) for chunk in chunks]).digest_hex
    assert combined == _digest(converted)
