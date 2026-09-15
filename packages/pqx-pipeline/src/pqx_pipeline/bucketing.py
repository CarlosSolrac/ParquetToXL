"""Putting a source's rows into calendar buckets and into final output order.

The missing link between a date column and a plan. The planner works over bucket *counts*; the
writer slices the frame in plan order. Both only work if the frame is arranged so that consecutive
slices are consecutive buckets, which is what this module produces.

**Order.** Calendar exports order buckets by ``period_order``, then apply the requested sort within
each final bucket before any row-based overflow split. So a calendar export sorted by customer is
sorted by customer *inside each sheet*, not globally across all years. An empty sort list preserves
source order inside each bucket, and **no hidden date sort is injected into the requested row
sort**: the bucket rank is a separate leading key, not an extra entry in the caller's list.

**Temporary keys never reach exported data.** ``partitioning-spec.md`` is explicit, and the reason
is arithmetic rather than tidiness: an extra column changes both the digest the sidecar recorded
and the additive identity verification relies on. They are dropped before the frame is returned,
and a test asserts the returned columns are the ones that went in.

⚠️ **Decoding is scalar, one value at a time.** This is the debt the Phase B decision record flagged
and it is now load-bearing: a forty-million-row source pays forty million Python calls here. The
replacement is a Polars expression built in ``pqx-calendar`` beside the scalar path and
property-tested against it -- **not** here, where it would become a second statement of the
century-window rule. This function is the single call site, so the swap is local.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import polars as pl
from pqx_calendar.decoding import decode_cell
from pqx_calendar.periods import UNDATED, Bucket, order_buckets, period_key, require_base_period_supported
from pqx_calendar.points import CalendarPoint

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pqx_calendar.columns import DateColumn
    from pqx_calendar.periods import BasePeriod
    from pqx_plan.config import CalendarPartitioning, SortKey

__all__ = ["BUCKET_RANK_COLUMN", "SOURCE_ORDINAL_COLUMN", "BucketingError", "bucket_of_row", "ordered_by_bucket"]

BUCKET_RANK_COLUMN: Final[str] = "__pqx_bucket_rank"
SOURCE_ORDINAL_COLUMN: Final[str] = "__pqx_source_ordinal"
"""Temporary, and dropped before the frame is returned. Named with a prefix no Parquet column
plausibly carries, and checked against the real columns rather than assumed to be free."""


class BucketingError(Exception):
    """A source's rows could not be put into buckets."""


def bucket_of_row(value: object, column: DateColumn, partitioning: CalendarPartitioning, *, column_name: str, row_ordinal: int) -> Bucket:
    """Return the bucket one row falls into.

    Args:
        value: The raw partition-column value, or ``None``.
        column: The registered date column.
        partitioning: The profile's calendar settings.
        column_name: The source column name, for any refusal.
        row_ordinal: The zero-based source row ordinal, for any refusal.

    Returns:
        The calendar bucket, or the undated one.

    Raises:
        BucketingError: The value is null and the profile's ``null_dates`` is ``error``. No date
            policy may discard rows, so the only alternatives are a bucket and a refusal.
        DateDecodeError: The value is not null and cannot be interpreted.
    """
    if value is None:
        if partitioning.null_dates == "error":
            message: str = f"row {row_ordinal} has no {column_name!r}, and null_dates is 'error'"
            raise BucketingError(message)
        return UNDATED
    point: CalendarPoint = decode_cell(column, value, column_name=column_name, row_ordinal=row_ordinal)
    return period_key(point, _grid_of(partitioning))


def _grid_of(partitioning: CalendarPartitioning) -> BasePeriod:
    """Return the base period the row keys are taken at.

    **Not the profile's ``base_period``** when that is ``year``. Subdividing an oversized year needs
    month-level counts, and sorting by the month key also puts the months of one year contiguous and
    in order -- so the same key serves both the fine counts the planner asks for and the arrangement
    the writer slices. A coarser key would make a subdivided year's months arrive interleaved.
    """
    return "year-month-day" if partitioning.base_period == "year-month-day" else "year-month"


def _free_name(frame: pl.DataFrame, wanted: str) -> str:
    """Return a column name not already in the frame, suffixing until one is free."""
    name: str = wanted
    suffix: int = 1
    while name in frame.columns:
        name = f"{wanted}_{suffix}"
        suffix += 1
    return name


def ordered_by_bucket(
    frame: pl.DataFrame,
    column: DateColumn,
    partitioning: CalendarPartitioning,
    *,
    column_name: str,
    sort: Sequence[SortKey] = (),
) -> tuple[pl.DataFrame, dict[Bucket, int]]:
    """Return the frame in final output order, and how many rows fall in each bucket.

    Args:
        frame: The source's rows, as read.
        column: The registered date column named by ``column_name``.
        partitioning: The profile's calendar settings.
        column_name: The partition column.
        sort: The sheet's requested sort, applied *within* each bucket.

    Returns:
        The frame arranged so consecutive slices are consecutive buckets, carrying exactly the
        columns it arrived with; and the row count per bucket, keyed at the precision
        ``plan_calendar_sheets`` expects.

    Raises:
        BucketingError: The partition column is not in the frame, or a null was found under
            ``null_dates: error``.
        BasePeriodUnsupportedError: The column's encoding cannot express the profile's grid.
        DateDecodeError: A value cannot be interpreted.
    """
    if column_name not in frame.columns:
        message: str = f"the frame has no column {column_name!r}; it holds {frame.columns}"
        raise BucketingError(message)
    require_base_period_supported(column, partitioning.base_period)

    buckets: list[Bucket] = [bucket_of_row(value, column, partitioning, column_name=column_name, row_ordinal=index) for index, value in enumerate(frame[column_name].to_list())]
    ordered: tuple[Bucket, ...] = order_buckets(buckets, partitioning.period_order)
    rank_of: dict[Bucket, int] = {bucket: rank for rank, bucket in enumerate(ordered)}

    rank_column: str = _free_name(frame, BUCKET_RANK_COLUMN)
    ordinal_column: str = _free_name(frame, SOURCE_ORDINAL_COLUMN)
    bucket: Bucket
    working: pl.DataFrame = frame.with_columns(
        pl.Series(rank_column, [rank_of[bucket] for bucket in buckets], dtype=pl.Int64),
        pl.Series(ordinal_column, list(range(frame.height)), dtype=pl.Int64),
    )

    by: list[str] = [rank_column, *(key.column for key in sort), ordinal_column]
    # The bucket rank leads and the source ordinal trails; between them sits the caller's list
    # exactly as given. Preserving the ordinal as the final tie-breaker is what makes the same
    # sources, configuration and planner version produce the same assignment every run.
    descending: list[bool] = [False, *(key.direction == "descending" for key in sort), False]
    nulls_last: list[bool] = [False, *(key.nulls == "last" for key in sort), False]
    working = working.sort(by=by, descending=descending, nulls_last=nulls_last)

    counts: dict[Bucket, int] = {}
    for bucket in buckets:
        counts[bucket] = counts.get(bucket, 0) + 1
    return working.drop(rank_column, ordinal_column), {bucket: counts[bucket] for bucket in ordered}
