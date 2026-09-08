"""The shared constructor for column metadata, used by the source path and every conversion."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    import polars as pl

    from parquet_to_xl.hashing.base import DataFrameHasherBaseClass
    from parquet_to_xl.metadata.columns import DataframeColumnsMetadata


def build_columns_metadata(df: pl.DataFrame, hashers: Sequence[DataFrameHasherBaseClass]) -> DataframeColumnsMetadata:
    """Describe every column of a frame, and the frame as a whole.

    One function serves both the source-frame path and every conversion, which is what
    keeps their outputs comparable: the same statistics, computed the same way, over
    whatever frame is handed in.

    For each column, in dataframe order, it records the name, ``str()`` of the dtype, the
    six dtype flags from ``metadata.scalars``, one ``hash_column`` result per hasher, and
    Polars' own ``min``, ``max``, ``len``, ``n_unique`` and ``null_count``. It then calls
    each hasher's ``hash_dataframe`` once for the wrapper.

    Two counting details are inherited from Polars rather than invented here, and both are
    pinned by tests: ``value_count`` is the row count including nulls, and ``unique_count``
    treats null as a single distinct value, so ``[1, 1, None, None]`` counts 2.

    An empty frame produces an empty column list and still calls each hasher once for the
    frame digest, so the wrapper always carries one entry per hasher.

    Args:
        df: The frame to describe. May be a source frame or a converted one.
        hashers: The hashers to run. Each contributes one entry to every column's
            ``hashes`` and one to ``dataframe_hashes``, in the order given.

    Returns:
        The wrapper holding the per-column records and the frame-level digests.
    """
    raise NotImplementedError
