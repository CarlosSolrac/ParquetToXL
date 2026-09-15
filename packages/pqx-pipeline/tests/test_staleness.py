"""Frozen tests for selection: what has to be rebuilt, decided over metadata only."""

from __future__ import annotations

import datetime as dt
import json
import os
from typing import TYPE_CHECKING, Any

import polars as pl
import pytest
from pqx_common.paths import ZPath
from pqx_pipeline.ingest import build_sidecar, read_parquet
from pqx_pipeline.locations import receipt_path
from pqx_pipeline.staleness import ProfileStaleness, SourceStaleness, excel_stale, read_receipt, sidecar_stale, truncate_to_second
from pqx_plan.receipt import RunReceipt, SourceStamp
from pqx_sidecar.store import sidecar_path

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from upath import UPath

DESCRIBED: dt.datetime = dt.datetime(2026, 9, 15, 10, 0, tzinfo=dt.UTC)
EXPORTED: dt.datetime = dt.datetime(2026, 9, 15, 11, 0, tzinfo=dt.UTC)
CONFIG_MODIFIED: dt.datetime = dt.datetime(2026, 9, 14, 9, 0, tzinfo=dt.UTC)
HASH: str = "2f1a" * 16


def _source(tmp_path: Path, name: str = "sales.parquet", rows: int = 4) -> UPath:
    """A real Parquet file with a sidecar beside it."""
    path: UPath = ZPath(str(tmp_path / name))
    pl.DataFrame({"id": pl.Series(list(range(rows)), dtype=pl.Int64)}).write_parquet(str(path))
    build_sidecar(read_parquet(path), original_path=path, created_utc=DESCRIBED)
    return path


def _receipt(**changes: object) -> RunReceipt:
    """A receipt for an export built after the sidecar was written."""
    fields: dict[str, object] = {
        "config_id": "cfg-1",
        "profile": "annual_review",
        "excel_created_utc": EXPORTED,
        "config_modified_utc": CONFIG_MODIFIED,
        "resolved_config_hash": HASH,
        "planner_version": "1",
        "sources": {"sales": SourceStamp(source_modified_utc=DESCRIBED, sidecar_created_utc=DESCRIBED)},
    }
    fields.update(changes)
    return RunReceipt.model_validate(fields)


# --------------------------------------------------------------------------------------
# Truncation
# --------------------------------------------------------------------------------------


def test_comparisons_drop_sub_second_precision() -> None:
    # Azure Blob reports last-modified at one-second granularity while a local filesystem reports
    # finer, so the same file staged from one and described from the other differs in a way that
    # is an artefact of the store rather than a change to the data.
    fine: dt.datetime = dt.datetime(2026, 9, 15, 12, 0, 0, 123456, tzinfo=dt.UTC)
    assert truncate_to_second(fine) == dt.datetime(2026, 9, 15, 12, 0, tzinfo=dt.UTC)


# --------------------------------------------------------------------------------------
# sidecar_stale
# --------------------------------------------------------------------------------------


def test_a_described_and_unchanged_source_is_fresh(tmp_path: Path) -> None:
    verdict: SourceStaleness = sidecar_stale("sales", _source(tmp_path))
    assert not verdict.stale
    assert verdict.signals == ()
    assert verdict.described is not None


def test_a_source_with_no_sidecar_is_stale(tmp_path: Path) -> None:
    path: UPath = ZPath(str(tmp_path / "sales.parquet"))
    pl.DataFrame({"id": [1]}).write_parquet(str(path))
    assert sidecar_stale("sales", path).signals == ("sidecar-missing-or-unreadable",)


def test_an_unreadable_sidecar_is_treated_exactly_as_a_missing_one(tmp_path: Path) -> None:
    # A sidecar this build cannot parse describes the source no better than none at all, and a
    # v2 file after the v3 bump reaches here by design.
    source: UPath = _source(tmp_path)
    sidecar_path(source).write_text("not json", encoding="utf-8")
    assert sidecar_stale("sales", source).signals == ("sidecar-missing-or-unreadable",)
    assert sidecar_stale("sales", source).described is None


def test_a_changed_source_is_stale(tmp_path: Path) -> None:
    source: UPath = _source(tmp_path)
    later: float = source.stat().st_mtime + 3600
    os.utime(str(source), (later, later))
    assert "source-changed" in sidecar_stale("sales", source).signals


def test_a_source_restored_to_an_older_copy_is_stale_too(tmp_path: Path) -> None:
    # Compared with != and not >. Restoring yesterday's Parquet over today's moves its mtime
    # backwards, and > would call that "not new" and leave the stale description standing.
    source: UPath = _source(tmp_path)
    earlier: float = source.stat().st_mtime - 86_400
    os.utime(str(source), (earlier, earlier))
    assert "source-changed" in sidecar_stale("sales", source).signals


def test_a_sub_second_difference_is_not_a_change(tmp_path: Path) -> None:
    source: UPath = _source(tmp_path)
    nudged: float = float(int(source.stat().st_mtime)) + 0.4
    os.utime(str(source), (nudged, nudged))
    assert "source-changed" not in sidecar_stale("sales", source).signals


def test_a_missing_source_raises_rather_than_reporting_staleness(tmp_path: Path) -> None:
    # A wiring failure, not a statement about freshness.
    with pytest.raises(FileNotFoundError):
        sidecar_stale("sales", ZPath(str(tmp_path / "absent.parquet")))


# --------------------------------------------------------------------------------------
# excel_stale
# --------------------------------------------------------------------------------------


def _fresh(described: dt.datetime = DESCRIBED) -> SourceStaleness:
    """A source that needs no re-describing, carrying a sidecar written at ``described``."""
    from pqx_frame.metadata.dataframe import DataframeMetadata
    from pqx_sidecar.document import SidecarDocument

    metadata: DataframeMetadata = DataframeMetadata.model_construct(modified_utc=described, full_path="/d/sales.parquet", column_metadata_of_conversions=[])
    return SourceStaleness(alias="sales", signals=(), described=SidecarDocument.model_construct(created_utc=described, metadata=metadata))


def test_a_profile_that_has_never_been_exported_is_stale() -> None:
    verdict: ProfileStaleness = excel_stale("annual_review", [_fresh()], None, config_modified_utc=CONFIG_MODIFIED, resolved_config_hash=HASH, planner_version="1")
    assert verdict.stale
    assert verdict.signals == ("no-receipt",)


def test_a_current_export_is_not_rebuilt() -> None:
    verdict: ProfileStaleness = excel_stale("annual_review", [_fresh()], _receipt(), config_modified_utc=CONFIG_MODIFIED, resolved_config_hash=HASH, planner_version="1")
    assert not verdict.stale
    assert verdict.reasons == ()


def test_a_stale_source_makes_its_profile_stale() -> None:
    # Selection picks profiles, not files: per-file skipping would leave a workbook mixing old
    # and new sheets.
    stale: SourceStaleness = SourceStaleness(alias="sales", signals=("source-changed",))
    verdict: ProfileStaleness = excel_stale("annual_review", [stale], _receipt(), config_modified_utc=CONFIG_MODIFIED, resolved_config_hash=HASH, planner_version="1")
    assert verdict.stale
    assert verdict.reasons == ("source-changed",)


def test_a_source_described_after_the_export_makes_the_profile_stale() -> None:
    # The two markers are independent: a run that died after publishing sidecars but before the
    # receipt rebuilds only the export.
    after: SourceStaleness = _fresh(described=EXPORTED + dt.timedelta(minutes=5))
    verdict: ProfileStaleness = excel_stale("annual_review", [after], _receipt(), config_modified_utc=CONFIG_MODIFIED, resolved_config_hash=HASH, planner_version="1")
    assert verdict.signals == ("described-after-export",)


def test_a_configuration_changed_after_the_export_makes_the_profile_stale() -> None:
    verdict: ProfileStaleness = excel_stale("annual_review", [_fresh()], _receipt(), config_modified_utc=EXPORTED + dt.timedelta(hours=1), resolved_config_hash=HASH, planner_version="1")
    assert "config-modified" in verdict.signals


def test_a_forgotten_timestamp_bump_is_caught_by_the_hash() -> None:
    # The backstop: exact, clock-free, and it fires when the timestamp does not.
    verdict: ProfileStaleness = excel_stale("annual_review", [_fresh()], _receipt(), config_modified_utc=CONFIG_MODIFIED, resolved_config_hash="ffff" * 16, planner_version="1")
    assert verdict.signals == ("resolved-config-hash",)


def test_both_configuration_signals_are_recorded_when_both_fire() -> None:
    # So a forgotten bump is visible rather than merely compensated for -- a run that fired only
    # the hash is one whose author forgot the timestamp.
    verdict: ProfileStaleness = excel_stale("annual_review", [_fresh()], _receipt(), config_modified_utc=EXPORTED + dt.timedelta(hours=1), resolved_config_hash="ffff" * 16, planner_version="1")
    assert verdict.signals == ("config-modified", "resolved-config-hash")


def test_a_changed_planner_invalidates_an_export_every_timestamp_calls_current() -> None:
    # partitioning-spec.md promises determinism "for the same sources, resolved configuration and
    # planner version".
    verdict: ProfileStaleness = excel_stale("annual_review", [_fresh()], _receipt(), config_modified_utc=CONFIG_MODIFIED, resolved_config_hash=HASH, planner_version="2")
    assert verdict.signals == ("planner-version",)


def test_forcing_rebuilds_regardless() -> None:
    verdict: ProfileStaleness = excel_stale("annual_review", [_fresh()], _receipt(), config_modified_utc=CONFIG_MODIFIED, resolved_config_hash=HASH, planner_version="1", forced=True)
    assert verdict.stale
    assert verdict.signals == ("forced",)


def test_forcing_a_never_exported_profile_records_both() -> None:
    verdict: ProfileStaleness = excel_stale("annual_review", [_fresh()], None, config_modified_utc=CONFIG_MODIFIED, resolved_config_hash=HASH, planner_version="1", forced=True)
    assert verdict.signals == ("forced", "no-receipt")


def test_a_source_with_no_sidecar_is_not_also_counted_as_described_after_the_export() -> None:
    # It is already stale on its own account, and it has no T2 to compare.
    missing: SourceStaleness = SourceStaleness(alias="sales", signals=("sidecar-missing-or-unreadable",))
    verdict: ProfileStaleness = excel_stale("annual_review", [missing], _receipt(), config_modified_utc=CONFIG_MODIFIED, resolved_config_hash=HASH, planner_version="1")
    assert verdict.signals == ()
    assert verdict.reasons == ("sidecar-missing-or-unreadable",)


def test_the_reasons_are_distinct_and_in_the_order_checked() -> None:
    first: SourceStaleness = SourceStaleness(alias="a", signals=("source-changed",))
    second: SourceStaleness = SourceStaleness(alias="b", signals=("source-changed", "hasher-version"))
    verdict: ProfileStaleness = excel_stale("annual_review", [first, second], None, config_modified_utc=CONFIG_MODIFIED, resolved_config_hash=HASH, planner_version="1")
    assert verdict.reasons == ("no-receipt", "source-changed", "hasher-version")


# --------------------------------------------------------------------------------------
# read_receipt
# --------------------------------------------------------------------------------------


def test_a_destination_with_no_receipt_reports_none(tmp_path: Path) -> None:
    assert read_receipt(ZPath(str(tmp_path)), "annual_review") is None


def test_a_written_receipt_reads_back(tmp_path: Path) -> None:
    destination: UPath = ZPath(str(tmp_path))
    receipt_path(destination, "annual_review").write_text(_receipt().model_dump_json(indent=2), encoding="utf-8")
    loaded: RunReceipt | None = read_receipt(destination, "annual_review")
    assert loaded is not None
    assert loaded.excel_created_utc == EXPORTED


def test_an_unreadable_receipt_is_refused_rather_than_treated_as_absent(tmp_path: Path) -> None:
    # It still marks the directory as owned, and reading it as "never exported" would let a run
    # reconcile away another configuration's output.
    destination: UPath = ZPath(str(tmp_path))
    receipt_path(destination, "annual_review").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="validation error"):
        read_receipt(destination, "annual_review")


# --------------------------------------------------------------------------------------
# Version drift
# --------------------------------------------------------------------------------------


def _rewrite_sidecar(source: UPath, mutate: Callable[[dict[str, Any]], None]) -> None:
    """Rewrite a real sidecar with one field changed, so drift is reached through the store."""
    document: dict[str, Any] = json.loads(sidecar_path(source).read_text(encoding="utf-8"))
    mutate(document)
    sidecar_path(source).write_text(json.dumps(document, indent=2), encoding="utf-8")


def test_a_sidecar_from_an_older_conversion_is_stale(tmp_path: Path) -> None:
    # A bump to DataframeConversionToExcel leaves T1 untouched, so without this the run proceeds
    # all the way to verification before unsupported-conversion fires -- the right answer, after
    # staging, reading and writing everything.
    source: UPath = _source(tmp_path)

    def older(document: dict[str, Any]) -> None:
        """Pretend the sidecar was written by a previous conversion release."""
        document["metadata"]["column_metadata_of_conversions"][0]["conversion"]["version"] = "3.0"

    _rewrite_sidecar(source, older)
    assert "conversion-version" in sidecar_stale("sales", source).signals


def test_a_sidecar_from_an_older_hasher_is_stale(tmp_path: Path) -> None:
    source: UPath = _source(tmp_path)

    def older(document: dict[str, Any]) -> None:
        """Pretend the digest was aggregated without row relationships."""
        document["metadata"]["column_metadata_of_conversions"][0]["dataframe_hashes"][0]["version"] = 1

    _rewrite_sidecar(source, older)
    assert "hasher-version" in sidecar_stale("sales", source).signals


def test_a_sidecar_with_no_excel_record_is_stale(tmp_path: Path) -> None:
    # Extracted without the conversion the export depends on, so it has to be re-described either
    # way, and saying so here is cheaper than discovering it at verification.
    source: UPath = _source(tmp_path)

    def drop(document: dict[str, Any]) -> None:
        """Remove the ToExcel record entirely."""
        document["metadata"]["column_metadata_of_conversions"] = []

    _rewrite_sidecar(source, drop)
    verdict: SourceStaleness = sidecar_stale("sales", source)
    assert "conversion-version" in verdict.signals
    assert "hasher-version" in verdict.signals


def test_a_record_from_another_conversion_is_skipped_rather_than_read(tmp_path: Path) -> None:
    # The ToExcel record is found by identifier rather than by position, so a sidecar carrying
    # another conversion's record first still reads the right one.
    source: UPath = _source(tmp_path)

    def prepend_other(document: dict[str, Any]) -> None:
        """Put a foreign conversion's record ahead of the ToExcel one."""
        records: list[dict[str, Any]] = document["metadata"]["column_metadata_of_conversions"]
        foreign: dict[str, Any] = json.loads(json.dumps(records[0]))
        foreign["conversion"]["identifier"] = "to-something-else"
        records.insert(0, foreign)

    _rewrite_sidecar(source, prepend_other)
    assert sidecar_stale("sales", source).signals == ()


def test_a_sidecar_recording_the_conversion_but_no_digest_is_stale(tmp_path: Path) -> None:
    source: UPath = _source(tmp_path)

    def drop_digests(document: dict[str, Any]) -> None:
        """Pretend it was extracted without hashers."""
        document["metadata"]["column_metadata_of_conversions"][0]["dataframe_hashes"] = []

    _rewrite_sidecar(source, drop_digests)
    assert "hasher-version" in sidecar_stale("sales", source).signals
