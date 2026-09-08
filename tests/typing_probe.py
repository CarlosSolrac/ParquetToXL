"""Proof that every runtime dependency type-checks clean under pyright strict and mypy strict.

A dependency that ships no inline types fails ``reportMissingTypeStubs`` at the *import*
line, which means every ticket importing it fails on a diagnostic having nothing to do with
the ticket. Settling the audit once, here, keeps that class of failure out of the
implementation loop entirely.

This module is a checked artifact, not a test: it is never collected or executed. Each
dependency is imported and one representative member is touched, because
``reportMissingTypeStubs`` fires on the import while ``reportUnknownMemberType`` needs an
actual attribute access to have anything to report. ``pre-commit run --all-files`` runs
both checkers over it in CI, so a dependency upgrade that drops inline types is caught
here rather than inside somebody's ticket.
"""

from __future__ import annotations

import datetime as dt
from typing import Final

import polars as pl
import pydantic
import python_calamine
import structlog
import xlsxwriter
import xxhash
from upath import UPath

PROBE_EPOCH: Final[dt.datetime] = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


class _ProbeModel(pydantic.BaseModel, frozen=True):
    """Minimal frozen model standing in for the metadata models Phase 2 will define."""

    identifier: str
    version: int = 1


def probe_polars() -> int:
    """Return the height of a trivial frame, exercising polars' constructor and dtypes."""
    frame: pl.DataFrame = pl.DataFrame({"n": [1.0, 2.0]}, schema={"n": pl.Float64})
    return frame.height


def probe_pydantic() -> str:
    """Round-trip a model through ``model_dump`` and ``model_validate``."""
    model: _ProbeModel = _ProbeModel(identifier="probe")
    dumped: dict[str, object] = model.model_dump()
    return _ProbeModel.model_validate(dumped).identifier


def probe_upath() -> str:
    """Return the name component of a ``UPath``, the base class ``ZPath`` will subclass."""
    path: UPath = UPath("probe.parquet")
    return path.name


def probe_xxhash() -> int:
    """Return an xxh3-128 digest as an int, the exact call the binary-aggregate hasher makes."""
    return xxhash.xxh3_128(b"probe").intdigest()


def probe_structlog() -> str:
    """Return a bound logger's repr, exercising structlog's configuration entry point."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger("probe")
    return repr(logger)


def probe_calamine() -> str:
    """Return the name of calamine's workbook loader without touching the filesystem."""
    loader: type[python_calamine.CalamineWorkbook] = python_calamine.CalamineWorkbook
    return loader.__name__


def probe_xlsxwriter() -> str:
    """Return the name of the workbook type polars writes through under the hood."""
    workbook: type[xlsxwriter.Workbook] = xlsxwriter.Workbook
    return workbook.__name__
