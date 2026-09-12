"""Frozen tests for ``build_columns_metadata``.

Read-only for an implementer. If one of these looks wrong, stop and report it rather than
editing it: everything downstream assumes the test is the specification.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import polars as pl
import pytest

from parquet_to_xl.hashing.base import DataFrameHasherBaseClass
from parquet_to_xl.hashing.binary_aggregate import BinaryAggregateHashedDataframe, DataFrameHasherBinaryAggregateHash
from parquet_to_xl.metadata import scalars
from parquet_to_xl.metadata.builder import build_columns_metadata
from parquet_to_xl.metadata.column import DataframeColumnMetadata
from parquet_to_xl.metadata.columns import DataframeColumnsMetadata


class _ShapeHasher(DataFrameHasherBaseClass):
    """A second, recognisable hasher, so ordering and multiplicity are testable.

    Its digests encode the shape it was handed rather than the content, which makes an entry
    produced by this hasher impossible to confuse with one from the real hasher.
    """

    def hash_column(self, column: pl.Series) -> BinaryAggregateHashedDataframe:
        return BinaryAggregateHashedDataframe(scope="column", digest_hex=f"{len(column):032x}")

    def hash_dataframe(self, df: pl.DataFrame) -> BinaryAggregateHashedDataframe:
        return BinaryAggregateHashedDataframe(scope="dataframe", digest_hex=f"{df.width:032x}")


def _frame() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "i": [3, 1, 2, None],
            "s": ["b", "a", None, "a"],
            "f": [1.5, None, 2.5, 2.5],
        },
        schema={"i": pl.Int64, "s": pl.String, "f": pl.Float64},
    )


def test_columns_are_described_in_dataframe_order() -> None:
    result: DataframeColumnsMetadata = build_columns_metadata(_frame(), [])
    assert [column.name for column in result.columns] == ["i", "s", "f"]


def test_statistics_match_polars_exactly() -> None:
    # Asserted against Polars itself rather than hardcoded, because "match Polars" is the
    # contract; a hardcoded number would drift silently on a Polars upgrade.
    df: pl.DataFrame = _frame()
    result: DataframeColumnsMetadata = build_columns_metadata(df, [])
    record: DataframeColumnMetadata
    for record in result.columns:
        series: pl.Series = df[record.name]
        assert record.min_value == series.min()
        assert record.max_value == series.max()
        assert record.value_count == len(series)
        assert record.unique_count == series.n_unique()
        assert record.null_count == series.null_count()
        assert record.polars_dtype == str(series.dtype)


def test_value_count_is_rows_including_nulls() -> None:
    result: DataframeColumnsMetadata = build_columns_metadata(_frame(), [])
    record: DataframeColumnMetadata
    for record in result.columns:
        assert record.value_count == 4
    # Every column of a frame has the same row count; that invariant is the point of it.
    assert len({record.value_count for record in result.columns}) == 1


def test_unique_count_treats_null_as_one_distinct_value() -> None:
    # Polars' n_unique counts null as a bucket: [1, 1, None, None] is 2, not 3.
    df: pl.DataFrame = pl.DataFrame({"x": [1, 1, None, None]}, schema={"x": pl.Int64})
    result: DataframeColumnsMetadata = build_columns_metadata(df, [])
    assert result.columns[0].unique_count == 2
    assert result.columns[0].null_count == 2
    assert result.columns[0].value_count == 4


def test_all_null_column_reports_none_extremes() -> None:
    df: pl.DataFrame = pl.DataFrame({"x": [None, None]}, schema={"x": pl.Int64})
    record: DataframeColumnMetadata = build_columns_metadata(df, []).columns[0]
    assert record.min_value is None
    assert record.max_value is None
    assert record.value_count == 2
    assert record.null_count == 2


def test_dtype_flags_delegate_to_the_scalars_helpers() -> None:
    df: pl.DataFrame = pl.DataFrame(
        {
            "i": [1],
            "f": [1.0],
            "b": [True],
            "s": ["x"],
            "c": ["x"],
            "y": [b"\x01"],
            "d": [Decimal("1.5")],
            "t": [dt.date(2020, 1, 1)],
        },
        schema={"i": pl.Int64, "f": pl.Float64, "b": pl.Boolean, "s": pl.String, "c": pl.Categorical, "y": pl.Binary, "d": pl.Decimal(10, 2), "t": pl.Date},
    )
    result: DataframeColumnsMetadata = build_columns_metadata(df, [])
    record: DataframeColumnMetadata
    for record in result.columns:
        dtype: pl.DataType = df[record.name].dtype
        assert record.is_numeric is scalars.is_numeric(dtype)
        assert record.is_float is scalars.is_float(dtype)
        assert record.is_integer is scalars.is_integer(dtype)
        assert record.is_decimal is scalars.is_decimal(dtype)
        assert record.is_text is scalars.is_text(dtype)
        assert record.is_boolean is scalars.is_boolean(dtype)


def test_one_hash_per_hasher_per_column_in_the_order_given() -> None:
    real: DataFrameHasherBinaryAggregateHash = DataFrameHasherBinaryAggregateHash()
    shape: _ShapeHasher = _ShapeHasher()
    result: DataframeColumnsMetadata = build_columns_metadata(_frame(), [real, shape])
    record: DataframeColumnMetadata
    for record in result.columns:
        assert len(record.hashes) == 2
        assert all(entry.scope == "column" for entry in record.hashes)
        # The shape hasher's digest is the column length, so position two is identifiable.
        assert record.hashes[1].digest_hex == f"{4:032x}"
        assert record.hashes[0].digest_hex != record.hashes[1].digest_hex


def test_dataframe_hashes_carry_one_entry_per_hasher() -> None:
    real: DataFrameHasherBinaryAggregateHash = DataFrameHasherBinaryAggregateHash()
    shape: _ShapeHasher = _ShapeHasher()
    result: DataframeColumnsMetadata = build_columns_metadata(_frame(), [real, shape])
    assert len(result.dataframe_hashes) == 2
    assert all(entry.scope == "dataframe" for entry in result.dataframe_hashes)
    assert result.dataframe_hashes[1].digest_hex == f"{3:032x}"


def test_no_hashers_produces_empty_hash_lists_rather_than_failing() -> None:
    result: DataframeColumnsMetadata = build_columns_metadata(_frame(), [])
    assert result.dataframe_hashes == []
    assert all(record.hashes == [] for record in result.columns)


def test_empty_frame_still_produces_one_dataframe_hash_per_hasher() -> None:
    shape: _ShapeHasher = _ShapeHasher()
    result: DataframeColumnsMetadata = build_columns_metadata(pl.DataFrame(), [shape])
    assert result.columns == []
    assert len(result.dataframe_hashes) == 1


def test_the_result_round_trips_through_pydantic() -> None:
    result: DataframeColumnsMetadata = build_columns_metadata(_frame(), [DataFrameHasherBinaryAggregateHash()])
    assert DataframeColumnsMetadata.model_validate(result.model_dump()) == result


def test_a_nested_extreme_is_refused_rather_than_recorded() -> None:
    # Tested against the helper directly because Polars blocks the public route first:
    # Series.min() on a List column raises InvalidOperationError before the builder sees a
    # value. The guard still earns its place -- it is the non-matching half of the narrowing
    # pyright requires, and raising beats silently recording "no value" for a column that
    # has one.
    assert scalars.as_column_scalar(None) is None
    assert scalars.as_column_scalar(3) == 3
    with pytest.raises(TypeError):
        scalars.as_column_scalar([1, 2])
