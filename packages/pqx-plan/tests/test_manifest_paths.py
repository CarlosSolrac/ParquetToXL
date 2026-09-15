"""Frozen tests for the destination-relative rule every manifest path carries.

Named ``test_manifest_paths`` rather than ``test_paths`` because ``pqx-common`` already has a
``test_paths``. Per-package ``tests/`` directories carry no ``__init__.py`` -- deliberately, so
twenty test modules do not share one namespace -- which makes pytest's module names
basename-derived and repository-wide unique. The collision is a collection error, not a silent
shadowing, but it is still the author's to avoid.
"""

from __future__ import annotations

import pytest
from pqx_plan.paths import require_destination_relative

ACCEPTED: list[str] = [
    "annual_review-2024.xlsx",
    "reports/annual_review-2024.xlsx",
    "a/b/c/d.xlsx",
    "sales.parquet.json",
    "has spaces and (parentheses).xlsx",
    "..leading-dots-are-not-a-parent-segment.xlsx",
    "trailing..dots.xlsx",
    "sub/..hidden/book.xlsx",
]
"""Ordinary names the rule must not refuse. The last three are the ones a naive substring test
for ``..`` gets wrong: a dot pair inside a segment is not a parent segment."""


@pytest.mark.parametrize("value", ACCEPTED)
def test_an_ordinary_relative_path_is_accepted(value: str) -> None:
    assert require_destination_relative(value) == value


def test_an_empty_path_is_refused() -> None:
    with pytest.raises(ValueError, match="may not be empty"):
        require_destination_relative("")


@pytest.mark.parametrize("value", ["/mnt/export/book.xlsx", "\\\\server\\share\\book.xlsx", "/book.xlsx"])
def test_an_absolute_path_is_refused(value: str) -> None:
    # Joining an absolute path wins silently: `Path("/a") / "/b"` is `/b`, so verification would
    # read, and reconciliation would delete, somewhere other than where the run published.
    with pytest.raises(ValueError, match="is absolute"):
        require_destination_relative(value)


@pytest.mark.parametrize("value", ["C:/export/book.xlsx", "d:book.xlsx"])
def test_a_drive_qualified_path_is_refused(value: str) -> None:
    with pytest.raises(ValueError, match="drive-qualified"):
        require_destination_relative(value)


@pytest.mark.parametrize("value", ["az://container/book.xlsx", "abfss://fs@acct.dfs.core.windows.net/book.xlsx"])
def test_a_uri_is_refused(value: str) -> None:
    with pytest.raises(ValueError, match="URI scheme"):
        require_destination_relative(value)


@pytest.mark.parametrize("value", ["../sibling/book.xlsx", "reports/../../book.xlsx", "..", "sub\\..\\book.xlsx"])
def test_a_parent_segment_is_refused(value: str) -> None:
    # Reconciliation's delete set is a listing minus the manifest's own entries, so an entry
    # resolving above the destination is an instruction to delete outside the directory the
    # ownership check cleared.
    with pytest.raises(ValueError, match="segment"):
        require_destination_relative(value)


def test_a_backslash_hides_nothing() -> None:
    # Names are generated on Linux, so a backslash arrives here as an ordinary character rather
    # than as a separator. Both spellings of the same escape must be refused.
    with pytest.raises(ValueError, match="segment"):
        require_destination_relative("reports\\..\\..\\book.xlsx")
