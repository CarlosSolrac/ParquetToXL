"""Frozen tests for ``configure_logging``.

Read-only for an implementer. If one of these looks wrong, stop and report it rather than
editing it: everything downstream assumes the test is the specification.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
import structlog

from parquet_to_xl.logging import configure_logging


def test_json_output_emits_one_json_object_per_event(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(True)
    structlog.get_logger("unit").info("hello", answer=42)
    payload: dict[str, Any] = json.loads(capsys.readouterr().out.strip())
    assert payload["event"] == "hello"
    assert payload["answer"] == 42
    assert payload["level"] == "info"
    assert "timestamp" in payload


def test_console_output_is_human_readable_not_json(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(False)
    structlog.get_logger("unit").info("hello")
    out: str = capsys.readouterr().out
    assert "hello" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out.strip())


def test_repeated_configuration_does_not_duplicate_output(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(True)
    configure_logging(True)
    structlog.get_logger("unit").info("once")
    lines: list[str] = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 1


def test_stdlib_loggers_reach_the_same_destination(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(True)
    logging.getLogger("third_party").info("from stdlib")
    assert "from stdlib" in capsys.readouterr().out


def test_reconfiguration_affects_a_logger_obtained_earlier(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(False)
    logger: structlog.stdlib.BoundLogger = structlog.get_logger("unit")
    logger.info("first")
    configure_logging(True)
    logger.info("second")
    lines: list[str] = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    payload: dict[str, Any] = json.loads(lines[-1])
    assert payload["event"] == "second"
