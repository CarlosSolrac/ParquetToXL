"""The timestamp type every recorded instant in this package carries."""

from __future__ import annotations

import datetime as dt
from typing import Annotated

from pydantic import AfterValidator


def require_utc(value: dt.datetime) -> dt.datetime:
    """Refuse a naive datetime and normalise an aware one to UTC.

    Every timestamp this package records exists to be compared against one written by
    something else -- a source store's mtime against a sidecar's recording of it, a sidecar's
    creation against a receipt's. Python refuses to compare an aware datetime with a naive one
    at all, raising ``TypeError`` from inside whichever comparison happens to reach it first,
    and two naive datetimes from different stores compare *successfully* and wrongly. Both
    failures surface far from the model that admitted them, so the model does not admit them.

    Normalising rather than merely accepting means the serialised form is stable: two receipts
    recording the same instant, one stamped ``+00:00`` and one ``-06:00``, produce the same
    text, so a hash or a diff over receipts is not a hash over the writer's timezone.

    Args:
        value: The datetime to check.

    Returns:
        The same instant, expressed in UTC.

    Raises:
        ValueError: ``value`` carries no timezone, so which instant it names is unknowable.
    """
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        message: str = f"{value!r} is naive; every recorded timestamp must carry a timezone"
        raise ValueError(message)
    return value.astimezone(dt.UTC)


type UtcDatetime = Annotated[dt.datetime, AfterValidator(require_utc)]
"""A timezone-aware datetime, normalised to UTC on the way in.

Applied through ``Annotated`` rather than by a ``field_validator`` on each model, so a field
added later cannot forget it: the rule travels with the type, not with the class.
"""
