"""Validating a workbook against a sidecar, with nothing else to hand.

This is what the sidecar is *for*, and it is the one claim no other test makes. The headline
round-trip test proves a digest survives a workbook, but it compares against metadata still
sitting in memory. ``test_sidecar_roundtrip.py`` proves the record survives the file. Neither
joins the two: given a ``.xlsx`` and the ``.parquet.json`` beside its source, and no access to
the original frame, can you decide whether the workbook holds the data the sidecar describes?

Every test below reaches the answer through ``_validate``, which takes two paths and opens the
original frame at no point. Three things have to come out of the sidecar for that to work --
the reader's schema, the row extent, and the expected digest -- and one test per item pins
what happens when it is missing.

The schema comes straight back as a Polars dtype through ``ColumnDtype.to_polars()``. An
earlier revision of this module carried a lookup table here, because the dtype was stored as
``str()`` of the Polars dtype and could not be reversed; the neutral vocabulary in
``metadata.dtypes`` is what removed the need for it.

Read-only for an implementer. If one of these looks wrong, stop and report it rather than
editing it.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, NamedTuple

import polars as pl

from parquet_to_xl.conversion.none import DataframeConversionNone
from parquet_to_xl.conversion.to_excel import DataframeConversionToExcel
from parquet_to_xl.excel.fast_reader import fast_excel_reader
from parquet_to_xl.excel.writer import ExcelWriteConfig, get_excel_writer
from parquet_to_xl.hashing.binary_aggregate import DataFrameHasherBinaryAggregateHash
from parquet_to_xl.metadata.extract import extract_metadata_from_dataframe
from parquet_to_xl.paths import ZPath
from parquet_to_xl.sidecar.store import get_sidecar_store

if TYPE_CHECKING:
    from pathlib import Path

    from upath import UPath

    from parquet_to_xl.metadata.column import DataframeColumnMetadata
    from parquet_to_xl.metadata.columns import DataframeColumnsMetadata
    from parquet_to_xl.metadata.dataframe import DataframeMetadata
    from parquet_to_xl.sidecar.document import SidecarDocument


def _frame() -> pl.DataFrame:
    """A small frame whose last two rows are entirely null.

    ``when`` is zoned, which is what real Parquet data looks like and what the fixtures use.
    ToExcel strips the zone preserving the local wall-clock reading, so the stored dtype is
    the naive one a validator has to rebuild.

    The trailing run is deliberate: an all-null row emits no ``<row>`` element, so a workbook
    written from this frame is two rows shorter than the frame, and only ``value_count`` from
    the sidecar can put the missing rows back.
    """
    return pl.DataFrame(
        {
            "i": [1, 2, None, None],
            "s": ["a", "b", None, None],
            "b": [True, False, None, None],
            "when": [dt.datetime(2020, 1, 1, 12, 0, 0, tzinfo=dt.UTC), dt.datetime(2021, 6, 15, 8, 30, 0, tzinfo=dt.UTC), None, None],
            "day": [dt.date(2020, 1, 1), dt.date(2021, 6, 15), None, None],
        },
        schema={"i": pl.Int64, "s": pl.String, "b": pl.Boolean, "when": pl.Datetime("us", "UTC"), "day": pl.Date},
    )


def _convert(frame: pl.DataFrame) -> pl.DataFrame:
    return DataframeConversionToExcel().metadata_of_converted_dataframe(frame, []).converted_dataframe


class _Subject(NamedTuple):
    """The two paths a validator is given, and nothing else."""

    parquet: UPath
    workbook: UPath


def _publish(tmp_path: Path, source: pl.DataFrame, *, written: pl.DataFrame | None = None) -> _Subject:
    """Record ``source`` as Parquet plus sidecar, and write a workbook.

    Args:
        tmp_path: Where both files go.
        source: The frame the sidecar describes.
        written: The frame the workbook is built from, when it should differ from ``source``.
            This is how a tampered workbook is produced: the sidecar still describes the
            original, which is exactly the situation validation exists to detect.

    Returns:
        The Parquet path and the workbook path.
    """
    parquet: UPath = ZPath(str(tmp_path / "sales.parquet"))
    source.write_parquet(str(parquet))
    recorded: DataframeMetadata | None = extract_metadata_from_dataframe(
        source,
        parquet,
        [],
        [DataFrameHasherBinaryAggregateHash()],
        [DataframeConversionNone(), DataframeConversionToExcel()],
    )
    assert recorded is not None
    get_sidecar_store("json").write(recorded, parquet)

    workbook: UPath = ZPath(str(tmp_path / "sales.xlsx"))
    get_excel_writer(ExcelWriteConfig().writer).write(_convert(source if written is None else written), workbook, {})
    return _Subject(parquet, workbook)


def _excel_record(parquet: UPath) -> DataframeColumnsMetadata:
    """The ToExcel record from the sidecar on disk, found by identifier rather than position."""
    document: SidecarDocument = get_sidecar_store("json").read(parquet)
    found: list[DataframeColumnsMetadata] = [
        record for record in document.metadata.column_metadata_of_conversions if record.conversion is not None and record.conversion.identifier == DataframeConversionToExcel.identifier
    ]
    assert len(found) == 1
    return found[0]


def _read_as_the_sidecar_describes(subject: _Subject, *, use_row_extent: bool = True) -> pl.DataFrame:
    """Read the workbook using only what the sidecar says it should contain."""
    record: DataframeColumnsMetadata = _excel_record(subject.parquet)
    schema: dict[str, pl.DataType] = {column.name: column.dtype.to_polars() for column in record.columns}
    rows: int | None = record.columns[0].value_count if use_row_extent else None
    return fast_excel_reader(subject.workbook, schema=schema, expected_rows=rows)


def _validate(subject: _Subject, *, use_row_extent: bool = True) -> bool:
    """Decide whether the workbook holds the data the sidecar describes.

    The whole operation, from two paths. The original frame is never opened -- the Parquet
    path is used to locate the sidecar and for nothing else.
    """
    record: DataframeColumnsMetadata = _excel_record(subject.parquet)
    back: pl.DataFrame = _read_as_the_sidecar_describes(subject, use_row_extent=use_row_extent)
    digest: str = DataFrameHasherBinaryAggregateHash().hash_dataframe(_convert(back)).digest_hex
    return digest == record.dataframe_hashes[0].digest_hex


def test_a_faithful_workbook_validates_against_its_sidecar(tmp_path: Path) -> None:
    # The claim this module exists for: two paths in, a verdict out.
    assert _validate(_publish(tmp_path, _frame())) is True


def test_a_dictionary_encoded_column_validates(tmp_path: Path) -> None:
    # Regression. ToExcel routes text on scalars.is_text, which omitted Enum, so an Enum
    # column was passed through unconverted and fast_excel_reader then refused the dtype its
    # own writer had produced -- validation raised rather than returning a verdict. Categorical
    # sits alongside it here because it always worked, so a failure points at the right one.
    frame: pl.DataFrame = pl.DataFrame(
        {
            "e": pl.Series("e", ["a", "b", None], dtype=pl.Enum(["a", "b"])),
            "c": pl.Series("c", ["x", "y", None], dtype=pl.Categorical()),
        }
    )
    assert _validate(_publish(tmp_path, frame)) is True


def test_the_sidecar_supplies_the_reader_schema(tmp_path: Path) -> None:
    # The reader never infers, and a validator has no converted frame to copy dtypes from,
    # so the schema has to be reconstructible from what was stored. Compared against the live
    # conversion here, which is the thing a validator does not get to see.
    subject: _Subject = _publish(tmp_path, _frame())
    record: DataframeColumnsMetadata = _excel_record(subject.parquet)
    rebuilt: dict[str, pl.DataType] = {column.name: column.dtype.to_polars() for column in record.columns}
    assert rebuilt == dict(_convert(_frame()).schema)


def test_the_row_extent_comes_from_value_count(tmp_path: Path) -> None:
    # value_count is the one statistic that survived the phase 8 removals, and this is why.
    # The frame's last two rows are all null, so the sheet holds two rows and the workbook
    # cannot say how many are missing; without the extent the digest is computed over the
    # wrong number of rows and validation fails on a workbook that is in fact faithful.
    subject: _Subject = _publish(tmp_path, _frame())
    assert _read_as_the_sidecar_describes(subject, use_row_extent=False).height == 2
    assert _read_as_the_sidecar_describes(subject, use_row_extent=True).height == 4
    assert _validate(subject, use_row_extent=False) is False
    assert _validate(subject, use_row_extent=True) is True


def test_a_changed_cell_fails_validation(tmp_path: Path) -> None:
    tampered: pl.DataFrame = _frame().with_columns(pl.Series("i", [1, 99, None, None], dtype=pl.Int64))
    assert _validate(_publish(tmp_path, _frame(), written=tampered)) is False


def test_a_value_moved_between_rows_fails_validation(tmp_path: Path) -> None:
    # Row order is deliberately not part of the digest, so this is the case that would slip
    # past a weaker scheme: the same values, the same column totals, different rows.
    swapped: pl.DataFrame = _frame().with_columns(pl.Series("s", ["b", "a", None, None], dtype=pl.String))
    assert _validate(_publish(tmp_path, _frame(), written=swapped)) is False


def test_a_reordered_workbook_still_validates(tmp_path: Path) -> None:
    # The other half of the same property. Whole rows in a different order are the same data,
    # and a validator must not report a difference that is not one.
    reordered: pl.DataFrame = _frame()[[3, 1, 0, 2]]
    assert _validate(_publish(tmp_path, _frame(), written=reordered)) is True


def test_the_per_column_digests_name_which_column_changed(tmp_path: Path) -> None:
    # The sidecar stores a digest per column as well as per frame, so a validator can report
    # where a workbook diverged rather than only that it did.
    tampered: pl.DataFrame = _frame().with_columns(pl.Series("i", [1, 99, None, None], dtype=pl.Int64))
    subject: _Subject = _publish(tmp_path, _frame(), written=tampered)

    record: DataframeColumnsMetadata = _excel_record(subject.parquet)
    back: pl.DataFrame = _convert(_read_as_the_sidecar_describes(subject))
    hasher: DataFrameHasherBinaryAggregateHash = DataFrameHasherBinaryAggregateHash()

    disagreed: list[str] = []
    column: DataframeColumnMetadata
    for column in record.columns:
        if hasher.hash_column(back[column.name]).digest_hex != column.hashes[0].digest_hex:
            disagreed.append(column.name)
    assert disagreed == ["i"]


def test_the_sidecar_names_the_columns_the_workbook_must_hold(tmp_path: Path) -> None:
    subject: _Subject = _publish(tmp_path, _frame())
    record: DataframeColumnsMetadata = _excel_record(subject.parquet)
    assert [column.name for column in record.columns] == ["i", "s", "b", "when", "day"]
    assert _read_as_the_sidecar_describes(subject).columns == [column.name for column in record.columns]
