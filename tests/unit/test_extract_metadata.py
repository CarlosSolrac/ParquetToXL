"""Frozen tests for ``extract_metadata_from_dataframe``.

Read-only for an implementer. If one of these looks wrong, stop and report it rather than
editing it.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from parquet_to_xl.conversion.base import DataframeConversionBaseClass
from parquet_to_xl.conversion.none import DataframeConversionNone
from parquet_to_xl.conversion.to_excel import DataframeConversionToExcel
from parquet_to_xl.hashing.base import DataFrameHasherBaseClass
from parquet_to_xl.hashing.binary_aggregate import BinaryAggregateHashedDataframe, DataFrameHasherBinaryAggregateHash
from parquet_to_xl.metadata.dataframe import DataframeMetadata
from parquet_to_xl.metadata.extract import extract_metadata_from_dataframe
from parquet_to_xl.paths import ZPath

if TYPE_CHECKING:
    from upath import UPath


def _frame() -> pl.DataFrame:
    return pl.DataFrame({"i": [3, 1, None], "s": ["b", None, "a"]}, schema={"i": pl.Int64, "s": pl.String})


def _written(tmp_path: Path, name: str = "source.parquet") -> UPath:
    target: UPath = ZPath(str(tmp_path / name))
    # write_parquet is typed for str | Path | IO[bytes], and a UPath is none of those even
    # when it wraps a local file, so the string form is what crosses that boundary.
    _frame().write_parquet(str(target))
    return target


def test_the_happy_path_fills_every_field(tmp_path: Path) -> None:
    path: UPath = _written(tmp_path)
    result: DataframeMetadata | None = extract_metadata_from_dataframe(_frame(), path, ["UTC"], [DataFrameHasherBinaryAggregateHash()], [DataframeConversionNone()])
    assert result is not None
    assert result.file_name == "source.parquet"
    assert result.full_path == str(path)
    assert result.modified_utc.tzinfo is not None
    assert set(result.modified_in_timezones) == {"UTC"}
    assert [column.name for column in result.source_columns_metadata.columns] == ["i", "s"]
    assert len(result.column_metadata_of_conversions) == 1


def test_modified_utc_is_timezone_aware_and_in_utc(tmp_path: Path) -> None:
    result: DataframeMetadata | None = extract_metadata_from_dataframe(_frame(), _written(tmp_path), [], [], [])
    assert result is not None
    assert result.modified_utc.tzinfo is not None
    assert result.modified_utc.utcoffset() == dt.timedelta(0)


def test_modified_utc_matches_the_file_on_disk(tmp_path: Path) -> None:
    path: UPath = _written(tmp_path)
    expected: dt.datetime = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.UTC)
    result: DataframeMetadata | None = extract_metadata_from_dataframe(_frame(), path, [], [], [])
    assert result is not None
    assert result.modified_utc == expected


def test_the_timezone_dict_is_keyed_by_the_names_given(tmp_path: Path) -> None:
    zones: list[str] = ["UTC", "America/Chicago", "Asia/Tokyo"]
    result: DataframeMetadata | None = extract_metadata_from_dataframe(_frame(), _written(tmp_path), zones, [], [])
    assert result is not None
    assert list(result.modified_in_timezones) == zones


def test_every_timezone_entry_names_the_same_instant(tmp_path: Path) -> None:
    # They differ in wall-clock reading and offset, never in the moment. Comparing aware
    # datetimes compares instants, which is exactly the property being asserted.
    result: DataframeMetadata | None = extract_metadata_from_dataframe(_frame(), _written(tmp_path), ["UTC", "America/Chicago", "Asia/Tokyo"], [], [])
    assert result is not None
    moment: dt.datetime
    for moment in result.modified_in_timezones.values():
        assert moment == result.modified_utc
    offsets: set[dt.timedelta | None] = {moment.utcoffset() for moment in result.modified_in_timezones.values()}
    assert len(offsets) == 3


def test_a_timezone_entry_carries_that_zone(tmp_path: Path) -> None:
    result: DataframeMetadata | None = extract_metadata_from_dataframe(_frame(), _written(tmp_path), ["Asia/Tokyo"], [], [])
    assert result is not None
    assert result.modified_in_timezones["Asia/Tokyo"].tzinfo == ZoneInfo("Asia/Tokyo")


def test_no_timezones_yields_an_empty_mapping(tmp_path: Path) -> None:
    result: DataframeMetadata | None = extract_metadata_from_dataframe(_frame(), _written(tmp_path), [], [], [])
    assert result is not None
    assert result.modified_in_timezones == {}


def test_one_wrapper_per_conversion_in_the_order_given(tmp_path: Path) -> None:
    result: DataframeMetadata | None = extract_metadata_from_dataframe(_frame(), _written(tmp_path), [], [], [DataframeConversionNone(), DataframeConversionToExcel()])
    assert result is not None
    assert len(result.column_metadata_of_conversions) == 2
    # None leaves Int64 alone; ToExcel widens it. That is what identifies the order.
    assert result.column_metadata_of_conversions[0].columns[0].polars_dtype == "Int64"
    assert result.column_metadata_of_conversions[1].columns[0].polars_dtype == "Float64"


def test_conversions_describe_the_converted_frame_not_the_source(tmp_path: Path) -> None:
    result: DataframeMetadata | None = extract_metadata_from_dataframe(_frame(), _written(tmp_path), [], [], [DataframeConversionToExcel()])
    assert result is not None
    assert result.source_columns_metadata.columns[0].polars_dtype == "Int64"
    assert result.column_metadata_of_conversions[0].columns[0].polars_dtype == "Float64"


def test_no_conversions_yields_an_empty_list(tmp_path: Path) -> None:
    result: DataframeMetadata | None = extract_metadata_from_dataframe(_frame(), _written(tmp_path), [], [], [])
    assert result is not None
    assert result.column_metadata_of_conversions == []


def test_one_hash_per_hasher_reaches_the_source_metadata(tmp_path: Path) -> None:
    result: DataframeMetadata | None = extract_metadata_from_dataframe(_frame(), _written(tmp_path), [], [DataFrameHasherBinaryAggregateHash()], [])
    assert result is not None
    assert len(result.source_columns_metadata.dataframe_hashes) == 1
    assert all(len(column.hashes) == 1 for column in result.source_columns_metadata.columns)


def test_the_result_round_trips_through_pydantic(tmp_path: Path) -> None:
    result: DataframeMetadata | None = extract_metadata_from_dataframe(_frame(), _written(tmp_path), ["UTC"], [DataFrameHasherBinaryAggregateHash()], [DataframeConversionNone()])
    assert result is not None
    assert DataframeMetadata.model_validate(result.model_dump()) == result


def test_a_missing_path_returns_none_and_logs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Output is asserted through capsys, not caplog, to match the rest of this suite:
    # structlog's default factory prints straight to stdout and creates no stdlib records,
    # so caplog sees nothing at all unless configure_logging has routed through stdlib.
    missing: UPath = ZPath(str(tmp_path / "does-not-exist.parquet"))
    result: DataframeMetadata | None = extract_metadata_from_dataframe(_frame(), missing, [], [], [])
    assert result is None
    assert "extract_metadata_failed" in capsys.readouterr().out


def test_an_unknown_timezone_returns_none_and_logs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    result: DataframeMetadata | None = extract_metadata_from_dataframe(_frame(), _written(tmp_path), ["Mars/Olympus_Mons"], [], [])
    assert result is None
    assert "extract_metadata_failed" in capsys.readouterr().out


def test_a_failing_hasher_returns_none_and_logs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    class _Exploding(DataFrameHasherBaseClass):
        def hash_column(self, column: pl.Series) -> BinaryAggregateHashedDataframe:
            message: str = f"no digest for column {column.name!r}"
            raise RuntimeError(message)

        def hash_dataframe(self, df: pl.DataFrame) -> BinaryAggregateHashedDataframe:
            message: str = f"no digest for a frame of {df.width} columns"
            raise RuntimeError(message)

    result: DataframeMetadata | None = extract_metadata_from_dataframe(_frame(), _written(tmp_path), [], [_Exploding()], [])
    assert result is None
    assert "extract_metadata_failed" in capsys.readouterr().out


def test_a_failing_conversion_returns_none_and_logs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    class _Refusing(DataframeConversionBaseClass):
        identifier: ClassVar[str] = "test-refusing"
        version: ClassVar[str] = "1.0"
        version_number: ClassVar[int] = 1
        description: ClassVar[str] = "Always raises."

        def _convert(self, df: pl.DataFrame) -> tuple[pl.DataFrame, bool]:
            message: str = f"cannot convert a frame of {df.width} columns"
            raise ValueError(message)

    result: DataframeMetadata | None = extract_metadata_from_dataframe(_frame(), _written(tmp_path), [], [], [_Refusing()])
    assert result is None
    assert "extract_metadata_failed" in capsys.readouterr().out


def test_a_nested_dtype_returns_none_rather_than_raising(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Nested dtypes are out of scope, and this boundary is where that stops being a crash.
    nested: pl.DataFrame = pl.DataFrame({"x": pl.Series("x", [[1, 2]], dtype=pl.List(pl.Int64))})
    result: DataframeMetadata | None = extract_metadata_from_dataframe(nested, _written(tmp_path), [], [], [])
    assert result is None
    assert "extract_metadata_failed" in capsys.readouterr().out


def test_the_logged_failure_carries_the_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    missing: UPath = ZPath(str(tmp_path / "absent.parquet"))
    extract_metadata_from_dataframe(_frame(), missing, [], [], [])
    assert "absent.parquet" in capsys.readouterr().out


def test_the_logged_failure_includes_a_traceback(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # logger.exception, not logger.error: without the traceback a None return is unreadable,
    # because the caller sees no exception and the log line alone names no cause.
    missing: UPath = ZPath(str(tmp_path / "absent.parquet"))
    extract_metadata_from_dataframe(_frame(), missing, [], [], [])
    out: str = capsys.readouterr().out
    assert "Traceback (most recent call last)" in out
    assert "FileNotFoundError" in out
