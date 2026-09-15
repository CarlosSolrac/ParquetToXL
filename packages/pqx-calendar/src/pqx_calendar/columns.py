"""The ``date_columns`` configuration models: what a source declares as interpretable.

These are the Pydantic side of ``docs/export-config.schema.json``'s ``dateColumn`` and
``calendarTimezone`` definitions. Both sides discriminate on the same field and carry one
shape per kind, which is what makes the corpus check in ``export-pipeline-spec.md`` a
comparison of behaviour rather than of spelling.

They live here, not in ``pqx-plan`` with the rest of ``ExportConfig``, because the
dependency arrow runs ``pqx-calendar -> pqx-plan``: decoding cannot import the planner.
``ExportConfig`` embeds :data:`DateColumn` when Phase C builds it.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal, Self

from pydantic import BaseModel, Field, model_validator

from pqx_calendar.points import MAX_YEAR, MIN_YEAR, DatePrecision

type IntDateFormat = Literal["YYMM", "YYYYMM", "YYMMDD", "YYYYMMDD"]
"""The integer encodings v1 interprets. Anything else is a refusal, not a guess."""

INT_FORMAT_WIDTHS: Final[dict[IntDateFormat, int]] = {"YYMM": 4, "YYYYMM": 6, "YYMMDD": 6, "YYYYMMDD": 8}
"""Digit count each format occupies once a nonnegative value is zero-padded to it."""

INT_FORMAT_PRECISIONS: Final[dict[IntDateFormat, DatePrecision]] = {"YYMM": "month", "YYYYMM": "month", "YYMMDD": "day", "YYYYMMDD": "day"}
"""What each format locates. The two month-only encodings cannot express a day base period."""

TWO_DIGIT_YEAR_FORMATS: Final[frozenset[IntDateFormat]] = frozenset({"YYMM", "YYMMDD"})
"""The formats whose year is ambiguous without a window. Exactly the ones spelled ``YY``."""

MAX_WINDOW_START: Final[int] = MAX_YEAR - 99
"""The last window start whose hundred-year span stays inside the supported years."""


class _CalendarModel(BaseModel, extra="forbid", frozen=True):
    """Shared configuration for every model in this module.

    ``extra="forbid"`` mirrors the schema's ``unevaluatedProperties: false``: a misspelled
    key is a configuration bug that would otherwise take effect as a default. ``frozen``
    because a resolved configuration is hashed, and a mutable field would let the hash and
    the value disagree.

    Set as class keywords rather than a ``model_config`` assignment, matching ``pqx_plan``'s
    models and keeping the house rule that every name is annotated before its first binding.
    """


class SourceWallClockTimezone(_CalendarModel, extra="forbid", frozen=True):
    """Take the source's local calendar date, whatever zone it was written in.

    Consistent with the existing ToExcel policy, and the only mode v1 allows for naive
    input: assigning a zone to a naive timestamp requires a DST ambiguity policy, which
    does not exist here and must not be improvised per column.
    """

    mode: Literal["source_wall_clock"]


class ZoneTimezone(_CalendarModel, extra="forbid", frozen=True):
    """Convert timezone-aware input to a named IANA zone before deriving the date.

    The zone is validated when the column is first used rather than at construction: a
    configuration may be read on a machine whose ``tzdata`` is older than the one that will
    run the export, and refusing at parse time would reject a file the exporter can honour.
    """

    mode: Literal["zone"]
    zone: str = Field(min_length=1)


type CalendarTimezone = Annotated[SourceWallClockTimezone | ZoneTimezone, Field(discriminator="mode")]
"""How a datetime column becomes a calendar date.

A discriminated union rather than the original's single string field holding either a zone
name or the sentinel ``"source_wall_clock"``. That shape makes the sentinel collide with the
IANA namespace and gives a reader no way to tell a typo from a mode.
"""


class DateColumnBase(_CalendarModel, extra="forbid", frozen=True):
    """The shared base of the three registered column kinds.

    It deliberately declares no ``type`` field. A base declaring ``type: str`` would make each
    member's ``Literal`` an incompatible override of a mutable attribute, and the discriminator
    has to be the narrow literal on the member for the union to resolve at all.
    """


class DateDateColumn(DateColumnBase, extra="forbid", frozen=True):
    """A Parquet ``date`` column, used as the calendar date it already is."""

    type: Literal["date"]


class DatetimeDateColumn(DateColumnBase, extra="forbid", frozen=True):
    """A Parquet ``datetime`` column, resolved to a calendar date by :attr:`calendar_timezone`."""

    type: Literal["datetime"]
    calendar_timezone: CalendarTimezone


class IntDateColumn(DateColumnBase, extra="forbid", frozen=True):
    """A signed or unsigned integer column encoding a date in a declared fixed-width format.

    One model and one schema branch. Expressing the four-digit and two-digit formats as two
    ``oneOf`` members both carrying ``"type": "int"`` cannot be modelled at all under
    ``Field(discriminator="type")``, which requires unique discriminator values, so the
    window field stays optional on the model and the rule becomes a validator.
    """

    type: Literal["int"]
    format: IntDateFormat
    two_digit_year_window_start: int | None = Field(default=None, ge=MIN_YEAR, le=MAX_WINDOW_START)

    @model_validator(mode="after")
    def check_window_matches_format(self) -> Self:
        """Require the window for a ``YY`` format and forbid it for a four-digit one.

        Forbidden rather than merely unused. A window sitting beside ``YYYYMM`` looks like
        it governs something and governs nothing, which is the kind of configuration bug
        that survives for years because every value it would have changed was already right.

        Returns:
            This model, unchanged.

        Raises:
            ValueError: The window is missing where it is required, or present where it
                does nothing.
        """
        needs_window: bool = self.format in TWO_DIGIT_YEAR_FORMATS
        if needs_window and self.two_digit_year_window_start is None:
            missing: str = f"format {self.format!r} has a two-digit year, so two_digit_year_window_start is required"
            raise ValueError(missing)
        if not needs_window and self.two_digit_year_window_start is not None:
            forbidden: str = f"format {self.format!r} has a four-digit year, so two_digit_year_window_start must be absent"
            raise ValueError(forbidden)
        return self

    @property
    def width(self) -> int:
        """Return the digit count this format occupies after zero-padding."""
        return INT_FORMAT_WIDTHS[self.format]

    @property
    def precision(self) -> DatePrecision:
        """Return whether this format locates a day or only a month."""
        return INT_FORMAT_PRECISIONS[self.format]


type DateColumn = Annotated[DateDateColumn | DatetimeDateColumn | IntDateColumn, Field(discriminator="type")]
"""One registered date column, selected on its declared Parquet type."""
