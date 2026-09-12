"""Per-column metadata: dtype flags, content hashes, and summary statistics."""

from __future__ import annotations

from pydantic import BaseModel

from parquet_to_xl.hashing import HashedDataframe
from parquet_to_xl.metadata.scalars import ColumnScalar


class DataframeColumnMetadata(BaseModel, frozen=True):
    """Everything recorded about one column of a dataframe.

    The three counts are deliberately independent facts rather than a partition, and two of
    them count nulls in ways worth stating:

    - ``value_count`` is the number of rows, nulls included, so it is identical for every
      column of a frame and can be used as a consistency check.
    - ``unique_count`` is Polars' ``n_unique()``, which treats null as one of its buckets:
      a column of ``[1, 1, None, None]`` has a unique count of 2, not 3.
    - ``null_count`` is how many of those rows are null.

    ``min_value`` and ``max_value`` are Polars' own, and are ``None`` when the column is
    empty or holds nothing but nulls -- not because the extremes are unknown, but because
    there is no value to report.
    """

    name: str
    polars_dtype: str
    """``str()`` of the Polars dtype, which carries parameters: ``Datetime(time_unit='us', time_zone='UTC')``."""

    is_numeric: bool
    is_float: bool
    is_integer: bool
    is_decimal: bool
    is_text: bool
    is_boolean: bool

    hashes: list[HashedDataframe]
    """One entry per hasher supplied to the builder, each scoped to ``"column"``."""

    min_value: ColumnScalar | None
    max_value: ColumnScalar | None
    value_count: int
    unique_count: int
    null_count: int
