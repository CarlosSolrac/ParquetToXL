"""Putting a source's rows into calendar buckets and into final output order.

The missing link between a date column and a plan. The planner works over bucket *counts*; the
writer slices the frame in plan order. Both only work if the frame is arranged so that consecutive
slices are consecutive buckets, which is what this module produces.

**Order.** Calendar exports order buckets by ``period_order``, then apply the requested sort within
each final bucket before any row-based overflow split. So a calendar export sorted by customer is
sorted by customer *inside each sheet*, not globally across all years. An empty sort list preserves
source order inside each bucket, and **no hidden date sort is injected into the requested row
sort**: the bucket key is a separate leading key, not an extra entry in the caller's list.

**Temporary keys never reach exported data.** ``partitioning-spec.md`` is explicit, and the reason
is arithmetic rather than tidiness: an extra column changes both the digest the sidecar recorded
and the additive identity verification relies on. They are dropped before the frame is returned,
and a test asserts the returned columns are the ones that went in.

**Decoding runs over distinct values, not over rows.** A date column repeats: forty million rows
hold a few hundred distinct months, or a few thousand distinct days. So the scalar decoder is
called once per *value* and the answers are mapped back onto the frame, which leaves
``pqx-calendar``'s decoder the only statement of the century-window rule anywhere -- the second
statement a Polars expression here would have become is the risk the Phase B record flagged.

**A wall-clock timestamp column is collapsed to its calendar day first**, because it can
otherwise be distinct in every row and the walk would be no shorter than the one it replaced. A
column read against a *named zone* is not collapsed: there the decoder converts the instant through
this machine's timezone database, Polars would convert through its own bundled one, and the two
disagree often enough to move a row to the wrong day in silence. So a source of unique zoned
instants still pays one decode per row, and that is the price of the decoder remaining the only
authority on which day an instant falls in.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import polars as pl
import polars.selectors as cs
from pqx_calendar.columns import DatetimeDateColumn, SourceWallClockTimezone
from pqx_calendar.decoding import decode_cell
from pqx_calendar.errors import DateDecodeError
from pqx_calendar.periods import UNDATED, Bucket, PeriodKey, order_buckets, period_key, require_base_period_supported
from pqx_calendar.points import CalendarPoint

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pqx_calendar.columns import DateColumn
    from pqx_calendar.periods import BasePeriod
    from pqx_plan.config import CalendarPartitioning, SortKey

__all__ = ["BUCKET_KEY_COLUMN", "SOURCE_ORDINAL_COLUMN", "BucketingError", "bucket_arrangement", "bucket_of_key", "bucket_of_row", "key_of_bucket", "ordered_by_bucket"]

BUCKET_KEY_COLUMN: Final[str] = "__pqx_bucket_key"
SOURCE_ORDINAL_COLUMN: Final[str] = "__pqx_source_ordinal"
_GROUPING_COLUMN: Final[str] = "__pqx_bucket_grouping"
_REPRESENTATIVE_COLUMN: Final[str] = "__pqx_bucket_representative"
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


def key_of_bucket(bucket: PeriodKey) -> int:
    """Return the packed sort key a dated bucket has.

    ``PeriodKey.sort_key`` written in radix 100 -- ``year * 10000 + month * 100 + day``, with an
    absent month or day contributing zero. Month never exceeds 12 and day never exceeds 31, so no
    field can carry into the one above it and numeric order on the key is *exactly* lexicographic
    order on ``(year, month, day)``. That equivalence is what lets the frame sort on this single
    column rather than on a rank derived from :func:`order_buckets`.

    Args:
        bucket: The dated bucket. The undated one has no key: it is a null in the column, which is
            what places it last under both period orders.

    Returns:
        The packed key.
    """
    return bucket.year * 10000 + (bucket.month or 0) * 100 + (bucket.day or 0)


def bucket_of_key(key: int) -> PeriodKey:
    """Return the bucket a packed key names.

    The precision is recovered rather than stored: an absent month or day is zero where a present
    one is 1-12 or 1-31, so the zeros themselves say which fields the key carries.

    Args:
        key: A key as :func:`key_of_bucket` produces.

    Returns:
        The bucket, at the precision its digits imply.
    """
    year: int = key // 10000
    month: int = (key // 100) % 100
    day: int = key % 100
    if day:
        return PeriodKey("day", year, month, day)
    if month:
        return PeriodKey("month", year, month)
    return PeriodKey("year", year)


def _grouping(column: DateColumn, dtype: pl.DataType, partition: cs.Selector) -> pl.Expr:
    """Return the expression whose distinct values are guaranteed to share a bucket.

    The partition column itself, except for a timestamp column read as a wall clock. Such a column
    can be distinct in every row -- forty million instants, forty million decodes -- while the
    bucket depends only on which day each reading falls on, so collapsing to that day first is what
    keeps the walk short for a column that repeats no value at all.

    **Wall-clock mode only, and the reason is whose timezone database answers.** Under
    ``source_wall_clock`` the decoder reads the year, month and day straight off the value Polars
    rendered, so Polars' database has already decided the reading and
    ``replace_time_zone(None)`` agrees with it by construction. Under a named zone the decoder
    converts the instant itself, through *this machine's* database, and the two do not always
    agree -- ``Africa/Casablanca`` in October 2026 is an hour apart between them, which is enough
    to move a row to the wrong day and say nothing. So a zoned column is not collapsed, and a
    source of unique zoned instants still pays one decode per row.

    The collapse is a grouping key and nothing more. The scalar decoder still runs, on a real value
    drawn out of each group, and still authors every answer and every refusal.

    Args:
        column: The registered date column.
        dtype: The frame column's dtype, which decides whether a collapse is available at all.
        partition: The partition column, selected literally.

    Returns:
        The grouping expression.
    """
    if not isinstance(column, DatetimeDateColumn) or not isinstance(dtype, pl.Datetime):
        return partition.as_expr()
    if not isinstance(column.calendar_timezone, SourceWallClockTimezone):
        return partition.as_expr()
    # Discarding the zone rather than converting through it: wall-clock mode wants the reading, not
    # the instant, and on a naive column this is a no-op.
    return partition.as_expr().dt.replace_time_zone(None).dt.date()


def _free_name(frame: pl.DataFrame, wanted: str) -> str:
    """Return a column name not already in the frame, suffixing until one is free."""
    name: str = wanted
    suffix: int = 1
    while name in frame.columns:
        name = f"{wanted}_{suffix}"
        suffix += 1
    return name


def bucket_arrangement(
    frame: pl.DataFrame,
    column: DateColumn,
    partitioning: CalendarPartitioning,
    *,
    column_name: str,
    sort: Sequence[SortKey] = (),
) -> tuple[pl.Series, dict[Bucket, int]]:
    """Return the source row positions in final output order, and the count per bucket.

    The arrangement rather than the arranged frame, so that it can be **computed over the source's
    own values and applied to a converted copy of it**. The two are not interchangeable to sort
    over: ToExcel writes ``True`` as ``-1.0``, which reverses a boolean sort key, turns an empty
    binary value into a null, which moves under ``nulls_last``, and collapses the largest integers
    onto one float, which makes distinct values tie. Deciding the order here and gathering there
    keeps every one of those reading as it does in the source.

    Args:
        frame: The source's rows, as read.
        column: The registered date column named by ``column_name``.
        partitioning: The profile's calendar settings.
        column_name: The partition column.
        sort: The sheet's requested sort, applied *within* each bucket.

    Returns:
        The source row positions, in the order the output wants them; and the row count per
        bucket, keyed at the precision ``plan_calendar_sheets`` expects.

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

    # Selected with ``cs.by_name`` rather than ``pl.col``, here and below: a source column may
    # legally be named ``*`` or ``^date$``, which ``pl.col`` would read as a wildcard and a regex.
    partition: cs.Selector = cs.by_name(column_name)
    key_column: str = _free_name(frame, BUCKET_KEY_COLUMN)
    ordinal_column: str = _free_name(frame, SOURCE_ORDINAL_COLUMN)
    grouping_column: str = _free_name(frame, _GROUPING_COLUMN)
    representative_column: str = _free_name(frame, _REPRESENTATIVE_COLUMN)
    working: pl.DataFrame = frame.with_columns(
        pl.int_range(0, pl.len(), dtype=pl.Int64).alias(ordinal_column),
        _grouping(column, frame.schema[column_name], partition).alias(grouping_column),
    )

    # One row per group, carrying the earliest source row in it and the value from that row, and
    # ordered by it. Decoding runs over this rather than over every row, so a forty-million-row
    # source pays one decode per distinct date rather than one per row. The representative is taken
    # from the earliest row so that a refusal names the row the per-row walk would have stopped at.
    distinct: pl.DataFrame = (
        working.filter(partition.is_not_null()).group_by(grouping_column).agg(partition.as_expr().sort_by(pl.col(ordinal_column)).first().alias(representative_column), pl.col(ordinal_column).min()).sort(ordinal_column)
    )

    grid: BasePeriod = _grid_of(partitioning)
    keys: list[int] = []
    refusal_ordinal: int | None = None
    refusal_value: object = None
    value: object
    ordinal: int
    for value, ordinal in distinct.select(representative_column, ordinal_column).iter_rows():
        try:
            point: CalendarPoint = decode_cell(column, value, column_name=column_name, row_ordinal=ordinal)
        except DateDecodeError:
            # Ordered by first appearance, so this is the value the scalar loop would have stopped
            # at. Nothing after it can refuse sooner, which is why the walk stops here.
            refusal_ordinal, refusal_value = ordinal, value
            break
        keys.append(key_of_bucket(period_key(point, grid)))

    error_ordinal: int | None = None
    if partitioning.null_dates == "error":
        error_ordinal = working.select(pl.col(ordinal_column).filter(partition.is_null()).min()).item()
    if refusal_ordinal is not None and (error_ordinal is None or refusal_ordinal < error_ordinal):
        # Decoded a second time purely to raise. Re-running the scalar path rather than rebuilding
        # its message keeps that wording authored in one place, and it cannot succeed here.
        decode_cell(column, refusal_value, column_name=column_name, row_ordinal=refusal_ordinal)
    if error_ordinal is not None:
        bucket_of_row(None, column, partitioning, column_name=column_name, row_ordinal=error_ordinal)

    # The lookup runs against the frame's own grouping column, never against the values the walk
    # decoded. ``iter_rows`` renders each value through Python, which has no precision finer than a
    # microsecond: matching on that would leave a ``Datetime("ns")`` column stranded in the undated
    # bucket, and two timestamps inside one microsecond would render as one repeated key that
    # Polars refuses outright. Truncation cannot move a *date*, so decoding itself is unharmed.
    # Reached only when nothing refused, so ``keys`` lines up with ``distinct`` row for row. An
    # empty mapping is well defined: a frame with no dated rows yields an all-null key column,
    # which is the undated bucket for every row. Nulls never match, so they need no entry.
    working = working.with_columns(
        pl.col(grouping_column).replace_strict(distinct[grouping_column], pl.Series(keys, dtype=pl.Int64), default=None, return_dtype=pl.Int64).alias(key_column),
    )

    by: list[str] = [key_column, *(key.column for key in sort), ordinal_column]
    # The bucket key leads and the source ordinal trails; between them sits the caller's list
    # exactly as given. Preserving the ordinal as the final tie-breaker is what makes the same
    # sources, configuration and planner version produce the same assignment every run.
    descending: list[bool] = [partitioning.period_order == "descending", *(key.direction == "descending" for key in sort), False]
    # The undated bucket is null, never a number, so ``nulls_last`` places it last under both
    # period orders -- structurally, rather than by getting a comparator right.
    nulls_last: list[bool] = [True, *(key.nulls == "last" for key in sort), False]
    working = working.sort(by=by, descending=descending, nulls_last=nulls_last)

    counts: dict[Bucket, int] = {}
    key_value: int | None
    count: int
    for key_value, count in working.group_by(key_column).len().iter_rows():
        counts[UNDATED if key_value is None else bucket_of_key(key_value)] = count
    ordered: tuple[Bucket, ...] = order_buckets(counts, partitioning.period_order)
    return working[ordinal_column], {bucket: counts[bucket] for bucket in ordered}


def ordered_by_bucket(
    frame: pl.DataFrame,
    column: DateColumn,
    partitioning: CalendarPartitioning,
    *,
    column_name: str,
    sort: Sequence[SortKey] = (),
) -> tuple[pl.DataFrame, dict[Bucket, int]]:
    """Return the frame in final output order, and how many rows fall in each bucket.

    :func:`bucket_arrangement` applied to the frame it was computed over.

    ⚠️ **Nothing in the pipeline calls this.** The run computes an arrangement over the source as
    read and applies it to a *converted* copy, so no stage arranges a frame in place any more. It
    is kept because this module's tests are written against it: they read as statements about which
    rows come out where, which is the behaviour :func:`bucket_arrangement` is responsible for, and
    rewriting fifty of them to gather by hand would bury that behind mechanism. Delete it only
    together with them -- and if a caller for it ever appears, delete this paragraph instead.

    Args:
        frame: The source's rows, as read.
        column: The registered date column named by ``column_name``.
        partitioning: The profile's calendar settings.
        column_name: The partition column.
        sort: The sheet's requested sort, applied *within* each bucket.

    Returns:
        The frame arranged so consecutive slices are consecutive buckets, carrying exactly the
        columns it arrived with; and the row count per bucket.
    """
    order: pl.Series
    counts: dict[Bucket, int]
    order, counts = bucket_arrangement(frame, column, partitioning, column_name=column_name, sort=sort)
    return frame[order], counts
