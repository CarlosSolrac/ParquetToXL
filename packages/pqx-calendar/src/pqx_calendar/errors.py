"""The errors this package raises, and the identifying context each one carries."""

from __future__ import annotations


class CalendarError(Exception):
    """Base class for every refusal this package makes.

    Present so a caller can catch the whole family without naming each member, and so a
    later addition here does not escape a handler written today.
    """


class DateDecodeError(CalendarError):
    """A non-null value in a registered date column could not be interpreted.

    ``partitioning-spec.md`` requires that such an error name the column, the source row
    ordinal and the offending value. The decoder itself knows only the value and the reason:
    the column name lives in the source's ``date_columns`` mapping and the ordinal exists
    only while a frame is being scanned, both of which are the caller's context, not this
    package's. So the error is raised bare and enriched on the way out by
    :func:`pqx_calendar.decoding.decode_cell`, which takes both as required keyword
    arguments precisely so that a caller cannot forget to supply them.

    Attributes:
        reason: Why the value was refused, without the positional context.
        value: The offending value, exactly as it was presented.
        column_name: The source column the value came from, or ``None`` before enrichment.
        row_ordinal: The zero-based source row ordinal, or ``None`` before enrichment.
    """

    def __init__(self, reason: str, value: object, *, column_name: str | None = None, row_ordinal: int | None = None) -> None:
        """Initialize the error.

        Args:
            reason: Why the value was refused.
            value: The offending value.
            column_name: The source column, when the caller knows it.
            row_ordinal: The zero-based source row ordinal, when the caller knows it.
        """
        self.reason: str = reason
        self.value: object = value
        self.column_name: str | None = column_name
        self.row_ordinal: int | None = row_ordinal
        super().__init__(self._render())

    def _render(self) -> str:
        """Return the message, naming whatever positional context is known."""
        where: str = ""
        if self.column_name is not None:
            where += f" in column {self.column_name!r}"
        if self.row_ordinal is not None:
            where += f" at source row {self.row_ordinal}"
        return f"{self.value!r}{where}: {self.reason}"

    def located(self, *, column_name: str, row_ordinal: int) -> DateDecodeError:
        """Return the same refusal, now naming where it happened.

        A new instance rather than a mutation: the bare error may already be the argument
        of an ``except`` clause further up, and rewriting its message underneath that
        handler would change what a log line said after it was written.

        Args:
            column_name: The source column the value came from.
            row_ordinal: The zero-based source row ordinal.

        Returns:
            An equivalent error carrying the column and ordinal.
        """
        return DateDecodeError(self.reason, self.value, column_name=column_name, row_ordinal=row_ordinal)


class BasePeriodUnsupportedError(CalendarError):
    """A base period was requested that the column's encoding cannot express.

    Only one case exists in v1: ``year-month-day`` against a month-only integer encoding.
    ``partitioning-spec.md`` requires a refusal rather than an invented day, because a
    synthesised first-of-the-month would partition and sort plausibly and wrongly.
    """


class UnknownTimezoneError(CalendarError):
    """A column named an IANA zone this machine's timezone database does not contain.

    Raised when the column is first used rather than when the configuration is parsed. A
    configuration is often read on a different machine from the one that runs the export,
    and refusing at parse time would reject a zone the exporter could have honoured.
    """
