"""The pytest fixtures every package's tests need.

Registered as a ``pytest11`` entry point rather than re-exported from each package's
``conftest.py``. A non-root ``conftest`` may not declare ``pytest_plugins`` -- pytest
refuses it -- and copying the fixtures into six conftests would let them drift. The entry
point makes them available wherever pytest runs, from the workspace root or from one
package directory, which is the whole reason this package exists.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import pytest
import structlog

from pqx_testing.generate import ensure_fixtures

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def restore_global_logging_state() -> Iterator[None]:
    """Snapshot and restore the process-wide logging state around every test.

    ``configure_logging`` mutates state no test owns: structlog's configuration, and the
    root standard-library logger's handlers and level. Without this the order tests happen
    to run in would change what they observe, and a failure would point at the wrong test.
    """
    saved_config: dict[str, Any] = structlog.get_config().copy()
    root: logging.Logger = logging.getLogger()
    saved_handlers: list[logging.Handler] = root.handlers[:]
    saved_level: int = root.level
    yield
    structlog.configure(**saved_config)
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)


@pytest.fixture(scope="session")
def fixture_files() -> dict[str, Path]:
    """Build any missing Parquet and Excel fixture, and return every fixture path.

    Session-scoped because generation writes fourteen files and reuses whatever is already
    on disk; per-test scope would re-resolve the same paths for no benefit.

    Returns:
        Paths keyed by fixture name, e.g. ``"parquet_a"``.
    """
    return ensure_fixtures()
