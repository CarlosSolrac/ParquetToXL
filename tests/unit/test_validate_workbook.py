"""Frozen tests for ``validate_workbook``: the sidecar's payoff, as one call.

``test_sidecar_validation.py`` proves the parts compose. This module is about the shipped
function, and in particular about the checks a caller would otherwise have to remember: that
the sidecar was written by rules this build implements, and that it carries a digest at all.
Skipping those does not fail loudly -- it compares a digest computed under different rules
against one computed under these, and reports a difference in the data that is really a
difference in the algorithm.

Read-only for an implementer. If one of these looks wrong, stop and report it rather than
editing it.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

import polars as pl
import pytest
import xlsxwriter

from parquet_to_xl.conversion.none import DataframeConversionNone
from parquet_to_xl.conversion.to_excel import DataframeConversionToExcel
from parquet_to_xl.excel.writer import ExcelWriteConfig, get_excel_writer
from parquet_to_xl.hashing.binary_aggregate import BinaryAggregateHashedDataframe, DataFrameHasherBinaryAggregateHash
from parquet_to_xl.metadata.extract import extract_metadata_from_dataframe
from parquet_to_xl.paths import ZPath
from parquet_to_xl.sidecar.store import get_sidecar_store, sidecar_path
from parquet_to_xl.sidecar.validation import (
    COLUMNS_DIFFER,
    DIGEST_MISMATCH,
    NO_EXCEL_DIGEST,
    SUPPORTED_HASHER_IDENTIFIER,
    SUPPORTED_HASHER_VERSION,
    UNSUPPORTED_CONVERSION,
    UNSUPPORTED_HASHER,
    VALID,
    WORKBOOK_UNREADABLE,
    Verdict,
    validate_workbook,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from upath import UPath

    from parquet_to_xl.conversion.base import DataframeConversionBaseClass
    from parquet_to_xl.hashing.base import DataFrameHasherBaseClass
    from parquet_to_xl.metadata.dataframe import DataframeMetadata


def _frame() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "n": [1, 2, 3, None],
            "s": ["a", "b", "c", None],
        },
        schema={"n": pl.Int64, "s": pl.String},
    )


def _publish(
    tmp_path: Path,
    *,
    written: pl.DataFrame | None = None,
    hashers: Sequence[DataFrameHasherBaseClass] | None = None,
    conversions: Sequence[DataframeConversionBaseClass] | None = None,
) -> tuple[UPath, UPath]:
    """Record a frame as Parquet plus sidecar, and write a workbook. Returns both paths."""
    source: pl.DataFrame = _frame()
    parquet: UPath = ZPath(str(tmp_path / "sales.parquet"))
    source.write_parquet(str(parquet))
    recorded: DataframeMetadata | None = extract_metadata_from_dataframe(
        source,
        parquet,
        [],
        [DataFrameHasherBinaryAggregateHash()] if hashers is None else hashers,
        [DataframeConversionNone(), DataframeConversionToExcel()] if conversions is None else conversions,
    )
    assert recorded is not None
    get_sidecar_store("json").write(recorded, parquet)

    workbook: UPath = ZPath(str(tmp_path / "sales.xlsx"))
    converted: pl.DataFrame = DataframeConversionToExcel().metadata_of_converted_dataframe(source if written is None else written, []).converted_dataframe
    get_excel_writer(ExcelWriteConfig().writer).write(converted, workbook, {})
    return parquet, workbook


def _converted() -> pl.DataFrame:
    """The frame as ToExcel leaves it, which is what a workbook is written from."""
    return DataframeConversionToExcel().metadata_of_converted_dataframe(_frame(), []).converted_dataframe


def _rewrite_sidecar(parquet: UPath, old: str, new: str) -> None:
    """Edit the stored JSON as text, to stand in for a file another build wrote.

    Textual rather than structural on purpose: the point is to produce bytes an older build
    would have written, and reaching for the models to do it would validate the result against
    the very schema the test is trying to step outside of.
    """
    target: UPath = sidecar_path(parquet)
    document: str = target.read_text(encoding="utf-8")
    assert old in document, f"{old!r} not present to replace"
    target.write_text(document.replace(old, new), encoding="utf-8")


def test_a_faithful_workbook_is_valid(tmp_path: Path) -> None:
    parquet: UPath
    workbook: UPath
    parquet, workbook = _publish(tmp_path)
    verdict: Verdict = validate_workbook(parquet, workbook)
    assert verdict.kind == VALID
    assert verdict.valid is True
    assert verdict.expected_digest == verdict.actual_digest


def test_a_changed_cell_is_a_digest_mismatch_and_both_digests_are_reported(tmp_path: Path) -> None:
    # The digests are on the verdict because "they differ" is rarely the end of the question.
    tampered: pl.DataFrame = _frame().with_columns(pl.Series("n", [1, 99, 3, None], dtype=pl.Int64))
    parquet: UPath
    workbook: UPath
    parquet, workbook = _publish(tmp_path, written=tampered)
    verdict: Verdict = validate_workbook(parquet, workbook)
    assert verdict.kind == DIGEST_MISMATCH
    assert verdict.valid is False
    assert verdict.expected_digest is not None
    assert verdict.actual_digest is not None
    assert verdict.expected_digest != verdict.actual_digest


def test_reordered_rows_are_still_valid(tmp_path: Path) -> None:
    parquet: UPath
    workbook: UPath
    parquet, workbook = _publish(tmp_path, written=_frame()[[3, 1, 0, 2]])
    assert validate_workbook(parquet, workbook).valid is True


def test_renamed_headers_are_caught_although_the_digest_cannot_see_them(tmp_path: Path) -> None:
    # The digest deliberately excludes column names -- renaming a column must not look like a
    # change of data -- so a workbook with entirely different headers produces the identical
    # digest. Without a name check it validated, which is the worst kind of wrong answer: a
    # confident yes about a file that does not hold what the sidecar describes.
    parquet: UPath
    workbook: UPath
    parquet, workbook = _publish(tmp_path)
    renamed: pl.DataFrame = _converted().rename({"n": "WRONG", "s": "ALSO_WRONG"})
    get_excel_writer(ExcelWriteConfig().writer).write(renamed, workbook, {})

    verdict: Verdict = validate_workbook(parquet, workbook)
    assert verdict.kind == COLUMNS_DIFFER
    assert verdict.valid is False
    assert "WRONG" in verdict.detail
    assert verdict.actual_digest is None


def test_a_header_altered_into_a_name_the_reader_would_generate_is_caught(tmp_path: Path) -> None:
    # The check has to read the header cells as written, not the names the reader hands back.
    # A sheet headed ``n, n`` is deduplicated to ``n, n_1`` on the way in, so a record of
    # exactly those columns matched, the values were untouched, and the digest agreed: a
    # tampered workbook validated. Blank headers do the same through ``__UNNAMED__N``.
    source: pl.DataFrame = pl.DataFrame({"n": [1.0, 2.0], "n_1": [10.0, 20.0]})
    parquet: UPath = ZPath(str(tmp_path / "collide.parquet"))
    source.write_parquet(str(parquet))
    recorded: DataframeMetadata | None = extract_metadata_from_dataframe(source, parquet, [], [DataFrameHasherBinaryAggregateHash()], [DataframeConversionNone(), DataframeConversionToExcel()])
    assert recorded is not None
    get_sidecar_store("json").write(recorded, parquet)

    workbook: UPath = ZPath(str(tmp_path / "collide.xlsx"))
    book: xlsxwriter.Workbook = xlsxwriter.Workbook(str(workbook))
    sheet: xlsxwriter.Worksheet = book.add_worksheet("Sheet1")
    sheet.write_row(0, 0, ["n", "n"])
    row: tuple[object, ...]
    index: int
    for index, row in enumerate(source.iter_rows(), start=1):
        sheet.write_row(index, 0, list(row))
    book.close()

    verdict: Verdict = validate_workbook(parquet, workbook)
    assert verdict.kind == COLUMNS_DIFFER
    assert verdict.valid is False


def test_a_missing_column_is_caught(tmp_path: Path) -> None:
    parquet: UPath
    workbook: UPath
    parquet, workbook = _publish(tmp_path)
    get_excel_writer(ExcelWriteConfig().writer).write(_converted().select("n"), workbook, {})
    assert validate_workbook(parquet, workbook).kind == COLUMNS_DIFFER


def test_reordered_columns_are_caught(tmp_path: Path) -> None:
    # Column order is bound by the digest too, so this would be a mismatch either way; the
    # name check reaches it first and says which columns moved rather than only that they did.
    parquet: UPath
    workbook: UPath
    parquet, workbook = _publish(tmp_path)
    get_excel_writer(ExcelWriteConfig().writer).write(_converted().select(["s", "n"]), workbook, {})
    assert validate_workbook(parquet, workbook).kind == COLUMNS_DIFFER


def test_a_cell_that_reads_but_cannot_be_hashed_is_a_verdict(tmp_path: Path) -> None:
    # An Excel date serial of 2958466 parses to a Polars date in year 10000. The reader is
    # happy; the hasher raises when it reads the value out. Conversion and hashing therefore
    # sit inside the workbook-error boundary, or one damaged file aborts a whole batch.
    source: pl.DataFrame = pl.DataFrame({"d": [dt.date(2020, 1, 1)]}, schema={"d": pl.Date})
    parquet: UPath = ZPath(str(tmp_path / "dates.parquet"))
    source.write_parquet(str(parquet))
    recorded: DataframeMetadata | None = extract_metadata_from_dataframe(source, parquet, [], [DataFrameHasherBinaryAggregateHash()], [DataframeConversionNone(), DataframeConversionToExcel()])
    assert recorded is not None
    get_sidecar_store("json").write(recorded, parquet)

    workbook: UPath = ZPath(str(tmp_path / "dates.xlsx"))
    book: xlsxwriter.Workbook = xlsxwriter.Workbook(str(workbook))
    sheet: xlsxwriter.Worksheet = book.add_worksheet("Sheet1")
    sheet.write_row(0, 0, ["d"])
    # A bare serial, no cell format: the reader is given the dtype by the sidecar, so the
    # number alone is enough to produce the out-of-range date.
    sheet.write_row(1, 0, [2958466])
    book.close()

    verdict: Verdict = validate_workbook(parquet, workbook)
    assert verdict.kind == WORKBOOK_UNREADABLE
    assert verdict.valid is False


def test_a_sidecar_from_another_conversion_version_is_refused_not_compared(tmp_path: Path) -> None:
    # The check that matters most. A record written under to-excel 3.0 left Enum unconverted,
    # so its digests describe different rules; comparing them would report a difference in the
    # data that is really a difference in the algorithm.
    parquet: UPath
    workbook: UPath
    parquet, workbook = _publish(tmp_path)

    # Only the to-excel record carries "4.0"; the None conversion is at "1.0".
    _rewrite_sidecar(parquet, '"version": "4.0"', '"version": "3.0"')
    verdict: Verdict = validate_workbook(parquet, workbook)
    assert verdict.kind == UNSUPPORTED_CONVERSION
    assert "3.0" in verdict.detail
    assert DataframeConversionToExcel.version in verdict.detail
    # No comparison was attempted, so there is no digest to report.
    assert verdict.actual_digest is None


def test_a_sidecar_from_another_hasher_version_is_refused(tmp_path: Path) -> None:
    parquet: UPath
    workbook: UPath
    parquet, workbook = _publish(tmp_path)

    _rewrite_sidecar(parquet, '"version": 2', '"version": 1')
    verdict: Verdict = validate_workbook(parquet, workbook)
    assert verdict.kind == UNSUPPORTED_HASHER
    assert "1" in verdict.detail


def test_a_sidecar_recorded_without_the_excel_conversion_has_nothing_to_compare(tmp_path: Path) -> None:
    parquet: UPath
    workbook: UPath
    parquet, workbook = _publish(tmp_path, conversions=[DataframeConversionNone()])
    verdict: Verdict = validate_workbook(parquet, workbook)
    assert verdict.kind == NO_EXCEL_DIGEST
    assert verdict.valid is False


def test_a_sidecar_recorded_without_hashers_has_nothing_to_compare(tmp_path: Path) -> None:
    # The conversion record is present but carries no digest, which is a different mistake
    # from omitting the conversion and worth saying so.
    parquet: UPath
    workbook: UPath
    parquet, workbook = _publish(tmp_path, hashers=[])
    verdict: Verdict = validate_workbook(parquet, workbook)
    assert verdict.kind == NO_EXCEL_DIGEST
    assert "digest" in verdict.detail


def test_a_workbook_the_reader_refuses_is_a_verdict_not_an_exception(tmp_path: Path) -> None:
    # A damaged workbook is a statement about the file under validation, so it comes back as
    # a verdict. A batch job checking many files should not stop at the first bad one.
    parquet: UPath
    workbook: UPath
    parquet, workbook = _publish(tmp_path)
    workbook.write_text("this is not a workbook", encoding="utf-8")
    verdict: Verdict = validate_workbook(parquet, workbook)
    assert verdict.kind == WORKBOOK_UNREADABLE
    assert verdict.valid is False


def test_a_missing_workbook_raises_rather_than_returning_a_verdict(tmp_path: Path) -> None:
    # The line is deliberate: a verdict is a judgement about a file that is there. A path that
    # is not is a wiring mistake, and hiding it as "invalid" would make it hard to find.
    parquet: UPath
    unused: UPath
    parquet, unused = _publish(tmp_path)
    assert unused.name.endswith(".xlsx")
    with pytest.raises(FileNotFoundError):
        validate_workbook(parquet, ZPath(str(tmp_path / "absent.xlsx")))


def test_a_missing_sidecar_raises(tmp_path: Path) -> None:
    source: pl.DataFrame = _frame()
    parquet: UPath = ZPath(str(tmp_path / "unrecorded.parquet"))
    source.write_parquet(str(parquet))
    workbook: UPath = ZPath(str(tmp_path / "unrecorded.xlsx"))
    get_excel_writer(ExcelWriteConfig().writer).write(DataframeConversionToExcel().metadata_of_converted_dataframe(source, []).converted_dataframe, workbook, {})
    with pytest.raises(FileNotFoundError):
        validate_workbook(parquet, workbook)


def test_the_supported_hasher_constants_track_the_model() -> None:
    # The constants exist so the check reads plainly; this keeps them from drifting from the
    # record the hasher actually produces.
    produced: BinaryAggregateHashedDataframe = DataFrameHasherBinaryAggregateHash().hash_dataframe(_frame())
    assert produced.identifier == SUPPORTED_HASHER_IDENTIFIER
    assert produced.version == SUPPORTED_HASHER_VERSION


def test_the_verdict_is_frozen(tmp_path: Path) -> None:
    parquet: UPath
    workbook: UPath
    parquet, workbook = _publish(tmp_path)
    verdict: Verdict = validate_workbook(parquet, workbook)
    attribute: str = "kind"
    with pytest.raises(AttributeError):
        setattr(verdict, attribute, DIGEST_MISMATCH)
