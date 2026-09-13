"""The shared constructor for column metadata, used by the source path and every conversion."""

from __future__ import annotations

from typing import TYPE_CHECKING

from parquet_to_xl.metadata import scalars
from parquet_to_xl.metadata.column import DataframeColumnMetadata
from parquet_to_xl.metadata.columns import DataframeColumnsMetadata

if TYPE_CHECKING:
    from collections.abc import Sequence

    import polars as pl

    from parquet_to_xl.hashing import HashedDataframe
    from parquet_to_xl.hashing.base import DataFrameHasherBaseClass


def build_columns_metadata(df: pl.DataFrame, hashers: Sequence[DataFrameHasherBaseClass]) -> DataframeColumnsMetadata:
    """Describe every column of a frame, and the frame as a whole.

    One function serves both the source-frame path and every conversion, which is what
    keeps their outputs comparable: the same statistics, computed the same way, over
    whatever frame is handed in.

    For each column, in dataframe order, it records the name, ``str()`` of the dtype, the
    six dtype flags from ``metadata.scalars``, one ``hash_column`` result per hasher, and
    Polars' own ``len`` and ``null_count``. Each hasher's ``hash_all`` supplies both column
    and frame digests, reusing cell hashes when supported.

    ``value_count`` is the row count including nulls, so it is the same for every column of
    a frame. No extremes or distinct count are computed: nothing read them, and they cost
    three Polars aggregations per column, of which ``n_unique`` was a full hash aggregation.

    A nested column is refused. That refusal used to be a side effect of computing the
    extremes -- ``Series.min()`` on a ``List`` raises -- so removing the statistics would
    have quietly made an out-of-scope dtype describable whenever no hasher was supplied to
    catch it. It is stated outright here instead, which is where it should always have been.

    An empty frame produces an empty column list and still calls each hasher once for the
    frame digest, so the wrapper always carries one entry per hasher.

    Args:
        df: The frame to describe. May be a source frame or a converted one.
        hashers: The hashers to run. Each contributes one entry to every column's
            ``hashes`` and one to ``dataframe_hashes``, in the order given.

    Returns:
        The wrapper holding the per-column records and the frame-level digests.

    Raises:
        TypeError: A column has a nested dtype, which the spec puts out of scope.
    """
    columns: list[DataframeColumnMetadata] = []
    hashed: list[tuple[tuple[HashedDataframe, ...], HashedDataframe]] = [hasher.hash_all(df) for hasher in hashers]
    index: int
    name: str
    for index, name in enumerate(df.columns):
        series: pl.Series = df[name]
        dtype: pl.DataType = series.dtype
        if scalars.is_nested(dtype):
            message: str = f"column {name!r} has dtype {dtype}; nested dtypes are out of scope"
            raise TypeError(message)
        columns.append(
            DataframeColumnMetadata(
                name=name,
                polars_dtype=str(dtype),
                is_numeric=scalars.is_numeric(dtype),
                is_float=scalars.is_float(dtype),
                is_integer=scalars.is_integer(dtype),
                is_decimal=scalars.is_decimal(dtype),
                is_text=scalars.is_text(dtype),
                is_boolean=scalars.is_boolean(dtype),
                hashes=[column_hashes[index] for column_hashes, _ in hashed],
                value_count=len(series),
                null_count=series.null_count(),
            )
        )
    return DataframeColumnsMetadata(columns=columns, dataframe_hashes=[whole for _, whole in hashed])
