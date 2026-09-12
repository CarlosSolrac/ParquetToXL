"""Golden style reference, read by every ticket.

This is the shortest module that exercises each house rule an implementation is judged
against, so a ticket can point at a worked example instead of restating prose. It is not
part of the library's API and nothing imports it.

Shown here: Google docstrings on the module, every class, and every non-trivial function;
a full annotation on every signature including ``-> None``; a frozen dataclass rather than
a plain class for state; and a declaration before the first binding of every local --
plain assignments, ``for`` targets, and ``with ... as`` targets alike.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ColumnTally:
    """Present and missing value counts for one named column."""

    name: str
    present: int
    missing: int


def tally_column(name: str, values: list[object]) -> ColumnTally:
    """Count the present and missing values in one column.

    Args:
        name: Column name, carried through to the result.
        values: The column's values, where ``None`` counts as missing.

    Returns:
        The tally for this column.
    """
    present: int = 0
    missing: int = 0
    value: object
    for value in values:
        if value is None:
            missing += 1
        else:
            present += 1
    return ColumnTally(name=name, present=present, missing=missing)


def write_tallies(path: Path, tallies: list[ColumnTally]) -> None:
    """Write one tab-separated tally per line.

    The ``with`` target is declared like any other binding; that is the form most often
    missed, because it does not look like an assignment.

    Args:
        path: Destination file, overwritten if it already exists.
        tallies: Tallies to write, in the order given.
    """
    handle: io.TextIOWrapper
    with path.open("w", encoding="utf-8") as handle:
        tally: ColumnTally
        for tally in tallies:
            handle.write(f"{tally.name}\t{tally.present}\t{tally.missing}\n")
