"""Frozen tests for the resolved-configuration fingerprint.

The backstop for a forgotten ``config_modified_utc`` bump, so what matters is that it is stable
against everything that does not change the export and sensitive to everything that does.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from pqx_plan.config import ExportConfig
from pqx_plan.corpus import BASE
from pqx_plan.fingerprint import FINGERPRINT_ALGORITHM, canonical_json, resolved_config_hash


def _config(document: dict[str, Any] | None = None) -> ExportConfig:
    """Parse the base document, or a mutation of it."""
    return ExportConfig.model_validate(BASE if document is None else document)


def test_a_fingerprint_is_sixty_four_hexadecimal_characters() -> None:
    digest: str = resolved_config_hash(_config())
    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")
    assert FINGERPRINT_ALGORITHM == "sha256"


def test_the_same_configuration_fingerprints_the_same_twice() -> None:
    assert resolved_config_hash(_config()) == resolved_config_hash(_config())


def test_key_order_in_the_file_does_not_change_the_fingerprint() -> None:
    # Reordering fields in the file is not an edit to the export, so it must not rebuild one.
    reordered: dict[str, Any] = dict(reversed(list(copy.deepcopy(BASE).items())))
    assert resolved_config_hash(_config(reordered)) == resolved_config_hash(_config())


def test_reformatting_the_file_does_not_change_the_fingerprint() -> None:
    # The hash is taken over the parsed model, so indentation never reaches it.
    reformatted: dict[str, Any] = json.loads(json.dumps(BASE, indent=4))
    assert resolved_config_hash(_config(reformatted)) == resolved_config_hash(_config())


def test_an_equivalent_timestamp_in_another_zone_does_not_change_the_fingerprint() -> None:
    # UtcDatetime normalises on the way in, so two spellings of one instant are one configuration.
    shifted: dict[str, Any] = {**copy.deepcopy(BASE), "config_modified_utc": "2026-09-13T10:04:11-06:00"}
    assert resolved_config_hash(_config(shifted)) == resolved_config_hash(_config())


def _with(path: tuple[str | int, ...], value: object) -> dict[str, Any]:
    """Return the base document with one nested key set to ``value``."""
    changed: dict[str, Any] = copy.deepcopy(BASE)
    cursor: Any = changed
    step: str | int
    for step in path[:-1]:
        cursor = cursor[step]
    cursor[path[-1]] = value
    return changed


@pytest.mark.parametrize(
    ("path", "value"),
    [
        pytest.param(("profiles", 0, "naming", "month_format"), "abbreviated", id="month_format"),
        pytest.param(("profiles", 0, "naming", "workbook"), "{profile}_{workbook_index:04d}.xlsx", id="workbook_template"),
        pytest.param(("profiles", 0, "naming", "worksheet_prefix"), "debits", id="worksheet_prefix"),
        pytest.param(("profiles", 0, "limits", "max_cells_per_workbook"), 20_000_000, id="cell_budget"),
        pytest.param(("profiles", 0, "limits", "max_data_rows_per_worksheet"), 500_000, id="row_limit"),
        pytest.param(("profiles", 0, "partitioning", "period_order"), "descending", id="period_order"),
        pytest.param(("profiles", 0, "partitioning", "year_split_months"), [6, 1], id="year_split_months"),
        pytest.param(("profiles", 0, "partitioning", "base_period"), "year-month", id="base_period"),
        pytest.param(("profiles", 0, "output_subdirectory"), "annual", id="output_subdirectory"),
        pytest.param(("output_directory",), "az://exports/quarterly", id="output_directory"),
        pytest.param(("config_modified_utc",), "2026-09-14T16:04:11Z", id="config_modified_utc"),
        pytest.param(("config_id",), "11111111-2222-3333-4444-555555555555", id="config_id"),
        pytest.param(("scratch_root",), "/mnt/fast", id="scratch_root"),
        pytest.param(("sources", "sales", "path"), "../data/sales_v2.parquet", id="source_path"),
        pytest.param(("profiles", 0, "sheets", 0, "sort"), [], id="sort_keys"),
    ],
)
def test_any_change_to_the_configuration_changes_the_fingerprint(path: tuple[str | int, ...], value: object) -> None:
    # Deliberately blunt: the hash covers the whole document rather than a chosen subset of
    # digest-relevant fields. A field wrongly excluded produces a stale export every check calls
    # current, which is far more expensive than one unnecessary rebuild.
    assert resolved_config_hash(_config(_with(path, value))) != resolved_config_hash(_config())


def test_adding_a_schema_pointer_changes_the_fingerprint() -> None:
    # Follows from the blunt rule, and is worth pinning: adding "$schema" to a file rebuilds the
    # export once. That is the accepted cost of not maintaining a relevance list.
    pointed: dict[str, Any] = {**copy.deepcopy(BASE), "$schema": "./export-config.schema.json"}
    assert resolved_config_hash(_config(pointed)) != resolved_config_hash(_config())


def test_canonical_json_sorts_keys_and_drops_whitespace() -> None:
    assert canonical_json({"b": 1, "a": {"d": 2, "c": 3}}) == '{"a":{"c":3,"d":2},"b":1}'


def test_canonical_json_keeps_non_ascii_as_itself() -> None:
    # So the text does not depend on which writer produced it.
    assert canonical_json({"alias": "ventas_méxico"}) == '{"alias":"ventas_méxico"}'


def test_canonical_json_is_stable_across_equal_documents() -> None:
    first: dict[str, Any] = {"a": [1, 2, {"z": None, "y": True}], "b": "x"}
    second: dict[str, Any] = {"b": "x", "a": [1, 2, {"y": True, "z": None}]}
    assert canonical_json(first) == canonical_json(second)
