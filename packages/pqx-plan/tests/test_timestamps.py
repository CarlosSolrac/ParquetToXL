"""Frozen tests for the timestamp rule every recorded instant in ``pqx_plan`` carries."""

from __future__ import annotations

import datetime as dt

import pytest
from pqx_plan.timestamps import require_utc


class _OffsetlessZone(dt.tzinfo):
    """A ``tzinfo`` that admits to knowing no offset.

    ``datetime.tzinfo`` allows ``utcoffset`` to return ``None``, and such a datetime is naive in
    every way that matters -- comparing it with an aware one still raises ``TypeError`` -- while
    ``tzinfo is None`` is ``False``. Checking only that attribute would let it through.

    The parameters are named ``dt`` because the base class names them that and a caller may pass
    by keyword; that it shadows this module's ``datetime`` alias inside three bodies that use
    neither is the lesser of the two problems.
    """

    def utcoffset(self, dt: dt.datetime | None) -> dt.timedelta | None:  # noqa: ARG002
        """Return no offset at all."""
        return None

    def tzname(self, dt: dt.datetime | None) -> str | None:  # noqa: ARG002
        """Return no name either."""
        return None

    def dst(self, dt: dt.datetime | None) -> dt.timedelta | None:  # noqa: ARG002
        """Return no daylight-saving offset."""
        return None


def test_a_utc_datetime_passes_through_unchanged() -> None:
    value: dt.datetime = dt.datetime(2026, 9, 15, 12, 30, 45, tzinfo=dt.UTC)
    assert require_utc(value) == value
    assert require_utc(value).tzinfo is dt.UTC


def test_an_offset_datetime_is_normalised_to_the_same_instant_in_utc() -> None:
    # Mexico is UTC-6; 06:30 there is 12:30 UTC. The instant is preserved and the text is not,
    # which is the point: two receipts for one instant must serialise identically.
    local: dt.datetime = dt.datetime(2026, 9, 15, 6, 30, 45, tzinfo=dt.timezone(-dt.timedelta(hours=6)))
    normalised: dt.datetime = require_utc(local)
    assert normalised == local
    assert normalised.isoformat() == "2026-09-15T12:30:45+00:00"


def test_a_naive_datetime_is_refused() -> None:
    with pytest.raises(ValueError, match="naive"):
        require_utc(dt.datetime(2026, 9, 15, 12, 30, 45))  # noqa: DTZ001


def test_a_zone_that_knows_no_offset_is_refused() -> None:
    with pytest.raises(ValueError, match="naive"):
        require_utc(dt.datetime(2026, 9, 15, 12, 30, 45, tzinfo=_OffsetlessZone()))
