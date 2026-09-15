"""Frozen tests for the sidecar store: where a record goes, and what comes back.

Read-only for an implementer. If one of these looks wrong, stop and report it rather than
editing it: everything downstream assumes the test is the specification.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import TYPE_CHECKING, ClassVar

import pytest
from pqx_common.paths import ZPath
from pqx_frame.hashing.binary_aggregate import BinaryAggregateHashedDataframe
from pqx_frame.metadata.columns import DataframeColumnsMetadata
from pqx_frame.metadata.dataframe import DataframeMetadata
from pqx_sidecar.document import SIDECAR_SCHEMA_VERSION, SidecarDocument
from pqx_sidecar.store import JsonSidecarStore, SidecarStoreBase, get_sidecar_store, register_sidecar_store, sidecar_path
from pydantic import ValidationError

if TYPE_CHECKING:
    from pathlib import Path

    from upath import UPath

SIDECAR_CREATED: dt.datetime = dt.datetime(2026, 9, 15, 12, 0, tzinfo=dt.UTC)
"""T2 for these tests. Fixed, so nothing here depends on when it ran."""


def _metadata() -> DataframeMetadata:
    moment: dt.datetime = dt.datetime(2020, 1, 1, 12, 0, tzinfo=dt.UTC)
    wrapper: DataframeColumnsMetadata = DataframeColumnsMetadata(
        columns=[],
        dataframe_hashes=[BinaryAggregateHashedDataframe(scope="dataframe", digest_hex="c" * 32)],
    )
    return DataframeMetadata(
        file_name="sales.parquet",
        full_path="/data/sales.parquet",
        modified_utc=moment,
        modified_in_timezones={"UTC": moment},
        source_columns_metadata=wrapper,
        column_metadata_of_conversions=[wrapper],
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("sales.parquet", "sales.parquet.json"),
        ("sales.v2.parquet", "sales.v2.parquet.json"),
        ("sales", "sales.json"),
    ],
    ids=["ordinary", "multi-suffix", "no-suffix"],
)
def test_the_sidecar_sits_beside_its_source_with_the_suffix_appended(source: str, expected: str, tmp_path: Path) -> None:
    # Appended rather than replaced: sales.parquet and sales.csv in one directory must not
    # both claim sales.json, and a real sales.json must not be mistaken for a sidecar.
    target: UPath = sidecar_path(ZPath(str(tmp_path / source)))
    assert target.name == expected
    assert target.parent == ZPath(str(tmp_path))


def test_the_registry_resolves_an_identifier_to_an_instance() -> None:
    assert isinstance(get_sidecar_store("json"), JsonSidecarStore)


def test_an_unknown_identifier_raises_and_names_what_is_registered() -> None:
    caught: pytest.ExceptionInfo[KeyError]
    with pytest.raises(KeyError) as caught:
        get_sidecar_store("no-such-store")
    assert "json" in str(caught.value)


def test_registering_a_duplicate_identifier_raises() -> None:
    # Silently replacing would make the winner depend on import order.
    class _Clashing(SidecarStoreBase):
        identifier: ClassVar[str] = "json"

        def write(self, metadata: DataframeMetadata, source_path: UPath, *, created_utc: dt.datetime) -> UPath:
            """Never called."""
            raise NotImplementedError

        def read(self, source_path: UPath) -> SidecarDocument:
            """Never called."""
            raise NotImplementedError

    with pytest.raises(ValueError, match="already registered"):
        register_sidecar_store(_Clashing)


def test_a_written_record_reloads_equal(tmp_path: Path) -> None:
    source: UPath = ZPath(str(tmp_path / "sales.parquet"))
    original: DataframeMetadata = _metadata()
    written: UPath = get_sidecar_store("json").write(original, source, created_utc=SIDECAR_CREATED)
    assert written == sidecar_path(source)
    assert written.exists()
    assert get_sidecar_store("json").read(source).metadata == original


def test_the_file_is_readable_json_carrying_the_version_and_the_digest(tmp_path: Path) -> None:
    # The point of a sidecar is that something other than this library can read it.
    source: UPath = ZPath(str(tmp_path / "sales.parquet"))
    written: UPath = get_sidecar_store("json").write(_metadata(), source, created_utc=SIDECAR_CREATED)
    loaded: object = json.loads(written.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    assert loaded["schema_version"] == SIDECAR_SCHEMA_VERSION
    assert "c" * 32 in written.read_text(encoding="utf-8")


def test_an_unknown_schema_version_raises_and_names_both_versions(tmp_path: Path) -> None:
    source: UPath = ZPath(str(tmp_path / "sales.parquet"))
    written: UPath = get_sidecar_store("json").write(_metadata(), source, created_utc=SIDECAR_CREATED)
    written.write_text(written.read_text(encoding="utf-8").replace(f'"schema_version": {SIDECAR_SCHEMA_VERSION}', '"schema_version": 99'), encoding="utf-8")

    caught: pytest.ExceptionInfo[ValueError]
    with pytest.raises(ValueError, match="schema_version") as caught:
        get_sidecar_store("json").read(source)
    assert "99" in str(caught.value)
    assert str(SIDECAR_SCHEMA_VERSION) in str(caught.value)


def test_a_corrupt_file_raises_rather_than_returning_a_partial_document(tmp_path: Path) -> None:
    source: UPath = ZPath(str(tmp_path / "sales.parquet"))
    sidecar_path(source).write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match=re.escape("sales.parquet.json")):
        get_sidecar_store("json").read(source)


def test_a_json_document_that_is_not_an_object_raises(tmp_path: Path) -> None:
    # Valid JSON, wrong shape. Without the check this reaches Pydantic as a list and fails
    # with a message about the model rather than about the file.
    source: UPath = ZPath(str(tmp_path / "sales.parquet"))
    sidecar_path(source).write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON object"):
        get_sidecar_store("json").read(source)


def test_a_missing_sidecar_raises_filenotfound(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        get_sidecar_store("json").read(ZPath(str(tmp_path / "absent.parquet")))


def test_a_remote_path_round_trips() -> None:
    # The store goes through UPath.read_text/write_text, so unlike the Excel writers and the
    # reader -- which hand str(path) to a library that opens it as a local filename -- it is
    # not restricted to the local filesystem.
    source: UPath = ZPath("memory://data/sales.parquet")
    original: DataframeMetadata = _metadata()
    written: UPath = get_sidecar_store("json").write(original, source, created_utc=SIDECAR_CREATED)
    assert written.protocol == "memory"
    assert get_sidecar_store("json").read(source).metadata == original


def test_the_document_defaults_to_the_current_schema_version() -> None:
    assert SidecarDocument(metadata=_metadata(), created_utc=SIDECAR_CREATED).schema_version == SIDECAR_SCHEMA_VERSION


def test_the_document_is_frozen() -> None:
    document: SidecarDocument = SidecarDocument(metadata=_metadata(), created_utc=SIDECAR_CREATED)
    attribute: str = "schema_version"
    with pytest.raises(ValueError, match="frozen"):
        setattr(document, attribute, 2)


def test_the_base_class_is_abstract() -> None:
    with pytest.raises(TypeError):
        SidecarStoreBase()  # type: ignore[abstract]


def test_a_document_records_when_it_was_written_as_well_as_when_the_source_changed() -> None:
    # T2 and the source's mtime are two clocks answering different questions: that one says when
    # the data changed, this one says when we last looked.
    document: SidecarDocument = SidecarDocument(metadata=_metadata(), created_utc=SIDECAR_CREATED)
    assert document.created_utc == SIDECAR_CREATED
    assert document.created_utc != document.metadata.modified_utc


def test_a_document_will_not_stamp_itself() -> None:
    # No default. T2 feeds excel_stale through `T2(s) > T4`, and a fabricated instant there is a
    # rebuild that never happens or one that never stops.
    with pytest.raises(ValidationError):
        SidecarDocument(metadata=_metadata())  # type: ignore[call-arg]


def test_a_naive_created_utc_is_refused() -> None:
    with pytest.raises(ValidationError):
        SidecarDocument(metadata=_metadata(), created_utc=dt.datetime(2026, 9, 15, 12, 0))  # noqa: DTZ001


def test_created_utc_normalises_to_utc(tmp_path: Path) -> None:
    # Two runs recording the same instant in different zones must produce the same text, or a
    # comparison over sidecars becomes a comparison over the writer's timezone.
    source: UPath = ZPath(str(tmp_path / "sales.parquet"))
    local: dt.datetime = dt.datetime(2026, 9, 15, 6, 0, tzinfo=dt.timezone(-dt.timedelta(hours=6)))
    get_sidecar_store("json").write(_metadata(), source, created_utc=local)
    assert get_sidecar_store("json").read(source).created_utc == SIDECAR_CREATED
    assert '"created_utc": "2026-09-15T12:00:00Z"' in sidecar_path(source).read_text(encoding="utf-8")


def test_the_whole_run_stamps_one_instant(tmp_path: Path) -> None:
    # A store reading the clock per call would give sidecars written seconds apart different
    # answers to the same staleness question.
    first: UPath = ZPath(str(tmp_path / "a.parquet"))
    second: UPath = ZPath(str(tmp_path / "b.parquet"))
    store: SidecarStoreBase = get_sidecar_store("json")
    store.write(_metadata(), first, created_utc=SIDECAR_CREATED)
    store.write(_metadata(), second, created_utc=SIDECAR_CREATED)
    assert store.read(first).created_utc == store.read(second).created_utc


def test_a_version_two_sidecar_is_refused_rather_than_migrated(tmp_path: Path) -> None:
    # There is no honest value to migrate to: when a v2 sidecar was written is exactly what it
    # does not record. Every existing sidecar stops loading and is regenerated, which the
    # selection rule already handles -- an unreadable sidecar is stale by definition.
    source: UPath = ZPath(str(tmp_path / "sales.parquet"))
    get_sidecar_store("json").write(_metadata(), source, created_utc=SIDECAR_CREATED)
    written: UPath = sidecar_path(source)
    legacy: str = written.read_text(encoding="utf-8").replace(f'"schema_version": {SIDECAR_SCHEMA_VERSION}', '"schema_version": 2')
    written.write_text(legacy, encoding="utf-8")
    caught: pytest.ExceptionInfo[ValueError]
    with pytest.raises(ValueError, match="schema_version") as caught:
        get_sidecar_store("json").read(source)
    assert "2" in str(caught.value)
    assert str(SIDECAR_SCHEMA_VERSION) in str(caught.value)


def test_the_schema_version_is_three() -> None:
    assert SIDECAR_SCHEMA_VERSION == 3
