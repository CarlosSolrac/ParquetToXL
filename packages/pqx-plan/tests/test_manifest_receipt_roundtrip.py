"""Gate 0c: the manifest and receipt survive a round trip through real JSON bytes.

The contract between write and verify. Nothing writes a file before this exists, and nothing
here is proved by ``model_validate(model.model_dump())`` -- that comparison never leaves Python,
so it cannot see a ``datetime`` that serialises to text and revalidates as a different instant,
a ``dict`` key that is not a string, or a nested model whose discriminator does not survive.
Every round trip below goes model -> JSON text -> **UTF-8 bytes on disk** -> ``json.loads`` ->
``model_validate``, which is the path the pipeline actually takes.

The fixture is deliberately awkward rather than minimal: two sources fanned into shared
workbooks, one source split by period and the other balanced, an overflow part, an undated
period, a sidecar written somewhere other than beside the workbooks, and a resolved
configuration holding every JSON shape including a null and a nested array.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from pqx_frame.hashing.binary_aggregate import BinaryAggregateHashedDataframe, DataFrameHasherBinaryAggregateHash
from pqx_frame.metadata.columns import ConversionIdentity
from pqx_plan.config import ExportConfig
from pqx_plan.corpus import BASE
from pqx_plan.manifest import MANIFEST_VERSION, Fragment, RunManifest, SourceFragments
from pqx_plan.receipt import RECEIPT_VERSION, RunReceipt, SourceStamp
from pydantic import ValidationError

CONVERSION: ConversionIdentity = ConversionIdentity(identifier="to-excel", version="4.0", version_number=4)
COLUMNS: list[str] = ["order_id", "customer", "amount", "placed_on"]


def _digest(rows: int, offset: int) -> BinaryAggregateHashedDataframe:
    """Return a real hash record, produced by the hasher rather than invented.

    A hand-written hex string would satisfy the field's pattern and prove nothing about whether
    the hasher's own output survives serialisation -- which is the half of the round trip the
    manifest cannot do without.

    Args:
        rows: How many rows to hash.
        offset: Mixed into the values, so two fragments do not share a digest.

    Returns:
        The whole-frame record for that shape.
    """
    frame: pl.DataFrame = pl.DataFrame(
        {
            "order_id": pl.int_range(offset, offset + rows, eager=True),
            "customer": pl.Series([f"customer-{(index + offset) % 7}" for index in range(rows)], dtype=pl.String),
            "amount": pl.Series([(index + offset) * 1.5 for index in range(rows)], dtype=pl.Float64),
            "placed_on": pl.Series([dt.date(2024, 1, 1) + dt.timedelta(days=(index + offset) % 365) for index in range(rows)], dtype=pl.Date),
        },
    )
    return DataFrameHasherBinaryAggregateHash().hash_all(frame)[1]


def _fragment(workbook: str, sheet: str, alias: str, rows: int, offset: int, *, period: str | None, part: int | None = None) -> Fragment:
    """Build one fragment.

    Args:
        workbook: Destination-relative workbook name.
        sheet: Final sheet name, after prefixing and collision resolution.
        alias: The source these rows came from.
        rows: Data rows, header excluded.
        offset: Passed to ``_digest`` so each fragment differs.
        period: Coverage label, or ``None`` for balanced output.
        part: Overflow part index, or ``None``.

    Returns:
        The fragment.
    """
    return Fragment(workbook=workbook, sheet_name=sheet, source_alias=alias, period_label=period, part_index=part, row_count=rows, column_names=COLUMNS, expected=_digest(rows, offset))


def _manifest() -> RunManifest:
    """Build the awkward manifest the module docstring describes.

    Returns:
        A valid manifest with fan-in, an overflow part, an undated period and balanced output.
    """
    sales: SourceFragments = SourceFragments(
        source_alias="sales",
        source_path="abfss://landing@acct.dfs.core.windows.net/curated/sales.parquet",
        sidecar_path="sidecars/sales.parquet.json",
        conversion=CONVERSION,
        total_and_disjoint=True,
        expected_whole=_digest(30, 0),
        expected_row_count=30,
        fragments=[
            _fragment("annual_review-2024.xlsx", "sales 2024-01", "sales", 10, 0, period="2024-01"),
            _fragment("annual_review-2024.xlsx", "sales 2024-02 (1)", "sales", 10, 10, period="2024-02", part=1),
            _fragment("annual_review-2024.xlsx", "sales 2024-02 (2)", "sales", 6, 20, period="2024-02", part=2),
            _fragment("annual_review-2024.xlsx", "sales Undated", "sales", 4, 26, period="Undated"),
        ],
    )
    returns: SourceFragments = SourceFragments(
        source_alias="returns",
        source_path="//fileserver/share/returns.parquet",
        sidecar_path="sidecars/returns.parquet.json",
        conversion=CONVERSION,
        total_and_disjoint=True,
        expected_whole=_digest(12, 100),
        expected_row_count=12,
        fragments=[
            _fragment("annual_review-2024.xlsx", "returns Data", "returns", 7, 100, period=None),
            _fragment("annual_review-overflow.xlsx", "returns Data", "returns", 5, 107, period=None),
        ],
    )
    return RunManifest(
        run_id="run-2026-09-15T07-00-00Z-0001",
        config_path="/opt/exports/annual.json",
        profile="annual_review",
        planner_version="0.1.0",
        resolved_config=ExportConfig.model_validate(BASE),
        output_directory="abfss://out@acct.dfs.core.windows.net/exports",
        started_utc=dt.datetime(2026, 9, 15, 7, 0, 0, tzinfo=dt.UTC),
        sources=[sales, returns],
        workbooks=["annual_review-2024.xlsx", "annual_review-overflow.xlsx"],
        sidecars=["sidecars/sales.parquet.json", "sidecars/returns.parquet.json"],
    )


def _receipt(excel_created_utc: dt.datetime | None = None) -> RunReceipt:
    """Build a receipt for the same run.

    Args:
        excel_created_utc: T4, defaulting to 07:04:31 UTC. Taken as an argument so a caller can
            hand in the same instant in another zone and watch it arrive normalised -- through
            the constructor, which validates, rather than through ``model_copy(update=...)``,
            which by design does not.

    Returns:
        A valid receipt naming both sources.
    """
    return RunReceipt(
        config_id="0f7c1a94-3b52-4c8e-9a1d-6e2f0b5d7c33",
        profile="annual_review",
        excel_created_utc=excel_created_utc if excel_created_utc is not None else dt.datetime(2026, 9, 15, 7, 4, 31, tzinfo=dt.UTC),
        config_modified_utc=dt.datetime(2026, 9, 14, 22, 15, 0, tzinfo=dt.UTC),
        resolved_config_hash="2f1a" * 16,
        planner_version="0.1.0",
        sources={
            "sales": SourceStamp(source_modified_utc=dt.datetime(2026, 9, 15, 3, 22, 11, tzinfo=dt.UTC), sidecar_created_utc=dt.datetime(2026, 9, 15, 7, 1, 2, tzinfo=dt.UTC)),
            "returns": SourceStamp(source_modified_utc=dt.datetime(2026, 9, 12, 19, 5, 0, tzinfo=dt.UTC), sidecar_created_utc=dt.datetime(2026, 9, 15, 7, 1, 3, tzinfo=dt.UTC)),
        },
    )


def _through_disk(model: RunManifest | RunReceipt, path: Path) -> dict[str, Any]:
    """Write the model as UTF-8 JSON, read the bytes back, and return the decoded object.

    Args:
        model: The model to serialise.
        path: Where to write it.

    Returns:
        Whatever ``json.loads`` made of the bytes that were actually on disk. Declared rather
        than narrowed: ``json.loads`` is annotated ``Any``, so an ``isinstance`` narrowing gives
        ``dict[Unknown, Unknown]``, which pyright strict then refuses to index.
    """
    path.write_text(model.model_dump_json(indent=2), encoding="utf-8")
    decoded: dict[str, Any] = json.loads(path.read_bytes().decode("utf-8"))
    return decoded


def test_a_manifest_survives_a_round_trip_through_bytes_on_disk(tmp_path: Path) -> None:
    original: RunManifest = _manifest()
    assert RunManifest.model_validate(_through_disk(original, tmp_path / "annual_review.manifest.json")) == original


def test_a_receipt_survives_a_round_trip_through_bytes_on_disk(tmp_path: Path) -> None:
    original: RunReceipt = _receipt()
    assert RunReceipt.model_validate(_through_disk(original, tmp_path / "annual_review.receipt.json")) == original


def test_what_reaches_the_file_is_json_the_web_editor_could_read(tmp_path: Path) -> None:
    # Not a restatement of the round trip: a model can round-trip through a representation no
    # other reader would understand. These are the shapes anything else parsing the file sees.
    written: dict[str, Any] = _through_disk(_manifest(), tmp_path / "m.json")
    assert written["manifest_version"] == MANIFEST_VERSION
    assert written["started_utc"] == "2026-09-15T07:00:00Z"
    # The resolved configuration is now the real model rather than an opaque JSON object, so
    # what reaches the file is what a web editor would read back as a configuration: a null that
    # stays null, a nested array of mixed numbers, and the discriminated unions resolved.
    assert written["resolved_config"]["scratch_root"] is None
    assert written["resolved_config"]["config_version"] == 1
    assert written["resolved_config"]["profiles"][0]["partitioning"]["year_split_months"] == [6, 4, 3, 2, 1]
    assert written["resolved_config"]["sources"]["returns"]["date_columns"]["returned_on"]["type"] == "int"
    assert written["sources"][0]["fragments"][0]["expected"]["identifier"] == "binary-aggregate-xxh3-128"
    assert written["sources"][0]["fragments"][0]["expected"]["row_digest_hex"] is not None
    assert written["sources"][0]["fragments"][0]["part_index"] is None


def test_receipt_stamps_stay_keyed_by_alias_through_the_file(tmp_path: Path) -> None:
    # A JSON object's keys are strings, so a mapping keyed by anything else would arrive back as
    # something else. Selection looks stamps up by alias, so this is the whole point of the field.
    written: dict[str, Any] = _through_disk(_receipt(), tmp_path / "r.json")
    assert sorted(written["sources"]) == ["returns", "sales"]
    assert written["sources"]["sales"]["source_modified_utc"] == "2026-09-15T03:22:11Z"


def test_an_offset_timestamp_reaches_the_file_already_in_utc(tmp_path: Path) -> None:
    # Two runs recording one instant from machines in different zones must produce identical
    # text, or `resolved_config_hash`-style comparisons over these files would depend on the zone
    # the writer happened to be in.
    mexico: RunReceipt = _receipt(dt.datetime(2026, 9, 15, 1, 4, 31, tzinfo=dt.timezone(-dt.timedelta(hours=6))))
    assert _through_disk(mexico, tmp_path / "r.json")["excel_created_utc"] == "2026-09-15T07:04:31Z"


def test_a_naive_timestamp_is_refused_on_construction() -> None:
    with pytest.raises(ValidationError, match="naive"):
        _receipt(dt.datetime(2026, 9, 15, 7, 4, 31))  # noqa: DTZ001


def test_a_naive_timestamp_in_a_stored_file_is_refused_on_read(tmp_path: Path) -> None:
    # The path that matters operationally: selection reads a receipt somebody else wrote, and a
    # timestamp with no zone in it cannot be compared with T1 from the source store at all.
    stored: dict[str, Any] = _through_disk(_receipt(), tmp_path / "r.json")
    with pytest.raises(ValidationError, match="naive"):
        RunReceipt.model_validate({**stored, "excel_created_utc": "2026-09-15T07:04:31"})


def test_a_manifest_from_a_future_version_is_refused() -> None:
    # Refused by the Literal rather than by whichever field happened to move, which is what makes
    # the error name the real problem.
    with pytest.raises(ValidationError, match="manifest_version"):
        RunManifest.model_validate({**_manifest().model_dump(mode="json"), "manifest_version": MANIFEST_VERSION + 1})


def test_a_receipt_from_a_future_version_is_refused() -> None:
    with pytest.raises(ValidationError, match="receipt_version"):
        RunReceipt.model_validate({**_receipt().model_dump(mode="json"), "receipt_version": RECEIPT_VERSION + 1})


def test_both_models_are_frozen() -> None:
    with pytest.raises(ValidationError):
        _manifest().profile = "other"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        _receipt().profile = "other"  # type: ignore[misc]


def test_a_row_count_that_disagrees_with_its_fragments_still_loads() -> None:
    # Deliberate. Per-source check 4 exists to report a dropped fragment with a clearer error
    # than a digest difference gives; a manifest that could not be loaded when it disagreed
    # would turn that report into a validation failure naming the wrong thing.
    manifest: RunManifest = _manifest()
    widened: RunManifest = RunManifest.model_validate(
        {**manifest.model_dump(mode="json"), "sources": [{**manifest.sources[0].model_dump(mode="json"), "expected_row_count": 999}, manifest.sources[1].model_dump(mode="json")]}
    )
    assert widened.sources[0].expected_row_count == 999
    assert sum(fragment.row_count for fragment in widened.sources[0].fragments) == 30


def test_a_fragment_naming_an_unlisted_workbook_is_refused() -> None:
    manifest: RunManifest = _manifest()
    with pytest.raises(ValidationError, match="workbooks the manifest does not list"):
        RunManifest.model_validate({**manifest.model_dump(mode="json"), "workbooks": ["annual_review-2024.xlsx"]})


def test_two_fragments_claiming_one_sheet_are_refused() -> None:
    manifest: RunManifest = _manifest()
    duplicated: dict[str, Any] = manifest.sources[0].model_dump(mode="json")
    duplicated["fragments"][1]["sheet_name"] = duplicated["fragments"][0]["sheet_name"]
    with pytest.raises(ValidationError, match="more than one fragment claims"):
        RunManifest.model_validate({**manifest.model_dump(mode="json"), "sources": [duplicated, manifest.sources[1].model_dump(mode="json")]})


def test_two_fragments_whose_sheets_differ_only_by_case_are_refused() -> None:
    # Excel cannot hold both `Sales` and `sales` in one workbook, so an allocation that produced
    # both is the same overwrite as an exact duplicate, spelled differently.
    manifest: RunManifest = _manifest()
    shouted: dict[str, Any] = manifest.sources[0].model_dump(mode="json")
    shouted["fragments"][1]["sheet_name"] = shouted["fragments"][0]["sheet_name"].upper()
    with pytest.raises(ValidationError, match="without case"):
        RunManifest.model_validate({**manifest.model_dump(mode="json"), "sources": [shouted, manifest.sources[1].model_dump(mode="json")]})


def test_two_workbooks_differing_only_by_case_are_refused() -> None:
    # `Sales.xlsx` and `sales.xlsx` are one file on SMB, and the host that writes this manifest is
    # Linux, where they are two and nothing would object.
    manifest: RunManifest = _manifest()
    with pytest.raises(ValidationError, match="unique without case"):
        RunManifest.model_validate({**manifest.model_dump(mode="json"), "workbooks": [*manifest.workbooks, "Annual_Review-2024.xlsx"]})


def test_a_sidecar_a_source_names_but_the_manifest_does_not_list_is_refused() -> None:
    # `sidecars` is a keep-set entry exactly as `workbooks` is: an omission here has the run that
    # published the sidecar delete it again on the way out.
    manifest: RunManifest = _manifest()
    with pytest.raises(ValidationError, match="sidecars the manifest does not list"):
        RunManifest.model_validate({**manifest.model_dump(mode="json"), "sidecars": ["sidecars/sales.parquet.json"]})


def test_one_sheet_name_reused_in_another_workbook_is_accepted() -> None:
    # The returns source already does this: "returns Data" appears in both workbooks, which is
    # ordinary rather than a collision. The check is on the pair, not on the name.
    assert [fragment.sheet_name for fragment in _manifest().sources[1].fragments] == ["returns Data", "returns Data"]


def test_a_fragment_filed_under_another_sources_alias_is_refused() -> None:
    manifest: RunManifest = _manifest()
    misfiled: dict[str, Any] = manifest.sources[0].model_dump(mode="json")
    misfiled["fragments"][0]["source_alias"] = "returns"
    with pytest.raises(ValidationError, match="fragments belonging to"):
        RunManifest.model_validate({**manifest.model_dump(mode="json"), "sources": [misfiled, manifest.sources[1].model_dump(mode="json")]})


def test_two_source_entries_under_one_alias_are_refused() -> None:
    # The second entry contributes no fragments, so the sheet and workbook checks have nothing to
    # say and the alias check is the one under test rather than the one that happened to fire.
    manifest: RunManifest = _manifest()
    empty: dict[str, Any] = {**manifest.sources[0].model_dump(mode="json"), "fragments": []}
    with pytest.raises(ValidationError, match="uses each of these aliases"):
        RunManifest.model_validate({**manifest.model_dump(mode="json"), "sources": [manifest.sources[0].model_dump(mode="json"), empty]})


def test_an_absolute_workbook_path_is_refused() -> None:
    manifest: RunManifest = _manifest()
    with pytest.raises(ValidationError, match="is absolute"):
        RunManifest.model_validate({**manifest.model_dump(mode="json"), "workbooks": ["/mnt/out/annual_review-2024.xlsx", "annual_review-overflow.xlsx"]})


def test_a_negative_row_count_is_refused() -> None:
    manifest: RunManifest = _manifest()
    negative: dict[str, Any] = manifest.sources[0].model_dump(mode="json")
    negative["fragments"][0]["row_count"] = -1
    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        RunManifest.model_validate({**manifest.model_dump(mode="json"), "sources": [negative, manifest.sources[1].model_dump(mode="json")]})


def test_the_source_path_is_the_remote_one_and_is_not_forced_relative() -> None:
    # The trap `export-pipeline-spec.md` names: `extract_metadata_from_dataframe` stats whatever
    # path it is handed, so the pipeline passes the frame read from scratch together with the
    # original remote path. Constraining this field to a relative path would make recording that
    # impossible and quietly turn T1 into the stage-in time.
    assert _manifest().sources[0].source_path.startswith("abfss://")
    assert _manifest().sources[1].source_path.startswith("//")
