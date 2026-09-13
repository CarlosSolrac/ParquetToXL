"""The headline claim, end to end: a digest survives a workbook, and survives being split.

This is the strongest statement the project makes, and the reason the hasher is
order-independent at all. Everything else exists to support it.

``tests/integration/test_excel_roundtrip.py`` covers the single-workbook half through
``pl.read_excel``. This one goes through ``fast_excel_reader`` and adds the half that
motivated the design: 1000 rows written as two 500-row workbooks, read back, concatenated in
the *wrong* order, and hashed to the same value the metadata recorded before anything was
written.

The workbooks are written here rather than taken from ``tests/fixtures/data/``. The fixture
workbooks are written from the **source** frame, so their ``Duration`` is ``str(timedelta)``
and their ``Binary`` is ``repr(bytes)``; the digest contract is about the *converted* frame,
which is what a caller writing a workbook for this purpose would write.

Read-only for an implementer. If one of these looks wrong, stop and report it rather than
editing it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import polars as pl
import pytest

from parquet_to_xl.conversion.none import DataframeConversionNone
from parquet_to_xl.conversion.to_excel import DataframeConversionToExcel
from parquet_to_xl.excel.fast_reader import fast_excel_reader
from parquet_to_xl.excel.writer import ExcelWriteConfig, get_excel_writer
from parquet_to_xl.hashing.binary_aggregate import DataFrameHasherBinaryAggregateHash
from parquet_to_xl.metadata.extract import extract_metadata_from_dataframe
from parquet_to_xl.paths import ZPath

if TYPE_CHECKING:
    from pathlib import Path

    from upath import UPath

    from parquet_to_xl.metadata.dataframe import DataframeMetadata

FIXTURE_STEMS: list[str] = ["parquet_a", "parquet_b"]

HALF: int = 500
"""Where the 1000-row frame is cut, as the spec specifies: rows [0:500] and [500:1000]."""

TO_EXCEL_INDEX: int = 1
"""Which conversion record carries the Excel digest, given the conversions passed below."""


def _convert(frame: pl.DataFrame) -> pl.DataFrame:
    """Apply the Excel round-trip model. Idempotent, so it is safe on both operands."""
    return DataframeConversionToExcel().metadata_of_converted_dataframe(frame, []).converted_dataframe


def _digest(frame: pl.DataFrame) -> str:
    return DataFrameHasherBinaryAggregateHash().hash_dataframe(frame).digest_hex


def _write(frame: pl.DataFrame, target: UPath) -> UPath:
    get_excel_writer(ExcelWriteConfig().writer).write(frame, target, {})
    return target


def _read_back(target: UPath, model: pl.DataFrame, expected_rows: int) -> pl.DataFrame:
    """Read a workbook with the reader, at the converted frame's schema."""
    return fast_excel_reader(target, schema=dict(model.schema), expected_rows=expected_rows)


def _recorded_excel_digest(stem: str, fixture_files: dict[str, Path]) -> str:
    """The Excel digest ``extract_metadata_from_dataframe`` writes down before any file exists."""
    source_path: Path = fixture_files[stem]
    recorded: DataframeMetadata | None = extract_metadata_from_dataframe(
        pl.read_parquet(source_path),
        ZPath(str(source_path)),
        [],
        [DataFrameHasherBinaryAggregateHash()],
        [DataframeConversionNone(), DataframeConversionToExcel()],
    )
    assert recorded is not None
    return recorded.column_metadata_of_conversions[TO_EXCEL_INDEX].dataframe_hashes[0].digest_hex


@pytest.mark.parametrize("stem", FIXTURE_STEMS)
def test_one_workbook_reproduces_the_recorded_digest(stem: str, fixture_files: dict[str, Path], tmp_path: Path) -> None:
    converted: pl.DataFrame = _convert(pl.read_parquet(fixture_files[stem]))
    target: UPath = _write(converted, ZPath(str(tmp_path / f"{stem}_full.xlsx")))

    back: pl.DataFrame = _read_back(target, converted, converted.height)
    assert _digest(_convert(back)) == _recorded_excel_digest(stem, fixture_files)


@pytest.mark.parametrize("stem", FIXTURE_STEMS)
def test_two_workbooks_reassembled_in_the_wrong_order_reproduce_it_too(stem: str, fixture_files: dict[str, Path], tmp_path: Path) -> None:
    # The claim the whole design exists for. Row order is not part of the digest, so the two
    # halves may come back in either order; the row relationships inside them are, so a cell
    # that moved between rows would still be caught.
    converted: pl.DataFrame = _convert(pl.read_parquet(fixture_files[stem]))
    assert converted.height == 2 * HALF

    first: UPath = _write(converted.slice(0, HALF), ZPath(str(tmp_path / f"{stem}_part1.xlsx")))
    second: UPath = _write(converted.slice(HALF, HALF), ZPath(str(tmp_path / f"{stem}_part2.xlsx")))

    reassembled: pl.DataFrame = pl.concat(
        [_read_back(second, converted, HALF), _read_back(first, converted, HALF)],
        how="vertical_relaxed",
    )
    assert reassembled.height == converted.height
    assert _digest(_convert(reassembled)) == _recorded_excel_digest(stem, fixture_files)


def test_the_two_fixtures_do_not_share_a_digest(fixture_files: dict[str, Path], tmp_path: Path) -> None:
    # parquet_b differs from parquet_a by one cell per column. A digest that survived the
    # round trip but could not tell the two apart would be worthless.
    digests: list[str] = []
    stem: str
    for stem in FIXTURE_STEMS:
        converted: pl.DataFrame = _convert(pl.read_parquet(fixture_files[stem]))
        target: UPath = _write(converted, ZPath(str(tmp_path / f"{stem}_distinct.xlsx")))
        digests.append(_digest(_convert(_read_back(target, converted, converted.height))))
    assert digests[0] != digests[1]


@pytest.mark.parametrize("stem", FIXTURE_STEMS)
def test_the_halves_and_the_whole_agree_before_any_file_is_written(stem: str, fixture_files: dict[str, Path]) -> None:
    # Isolates the hasher's own additivity from anything the workbook does, so a failure in
    # the tests above can be read as "the file lost something" rather than "the sum is wrong".
    converted: pl.DataFrame = _convert(pl.read_parquet(fixture_files[stem]))
    halves: pl.DataFrame = pl.concat([converted.slice(HALF, HALF), converted.slice(0, HALF)], how="vertical")
    assert _digest(halves) == _digest(converted)
