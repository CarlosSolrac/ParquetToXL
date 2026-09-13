"""Per-column metadata: dtype flags, content hashes, and summary statistics."""

from __future__ import annotations

from pydantic import BaseModel

from parquet_to_xl.hashing import HashedDataframe


class DataframeColumnMetadata(BaseModel, frozen=True):
    """Everything recorded about one column of a dataframe.

    The two counts are independent facts rather than a partition:

    - ``value_count`` is the number of rows, nulls included, so it is identical for every
      column of a frame and can be used as a consistency check. ``fast_excel_reader`` takes
      its ``expected_rows`` from it, which is what restores a trailing run of all-null rows.
    - ``null_count`` is how many of those rows are null.

    No extremes and no distinct count are recorded. Both were write-only -- nothing in this
    library ever read them -- and the extremes were what made this model impossible to
    persist. Typed as a union containing ``str``, they let Pydantic serialize binary,
    temporal and decimal values to text and reload them as ``str``, while non-UTF-8 bytes
    failed to serialize at all. Every field that remains survives a JSON round trip
    unchanged, which is what the sidecar depends on.
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

    value_count: int
    null_count: int
