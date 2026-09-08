"""Shared fixtures for the whole test suite."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import pytest
import structlog


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
