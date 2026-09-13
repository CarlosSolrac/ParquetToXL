"""Frozen tests for ``fast_excel_reader``.

Read-only for an implementer. If one of these looks wrong, stop and report it rather than
editing it.

Every assertion here about what a workbook does to data was measured first, against the live
libraries, and the measurements are recorded in
``tests/fixtures/excel-round-trip-findings.md``.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

import polars as pl
import pytest
import xlsxwriter

from parquet_to_xl.conversion.to_excel import DataframeConversionToExcel
from parquet_to_xl.excel.fast_reader import fast_excel_reader
from parquet_to_xl.excel.writer import ExcelWriteConfig, get_excel_writer
from parquet_to_xl.paths import ZPath

if TYPE_CHECKING:
    from pathlib import Path

    from upath import UPath

SIMPLE_SCHEMA: dict[str, pl.DataType] = {"num": pl.Float64(), "txt": pl.String()}


def _write_default(frame: pl.DataFrame, target: UPath) -> UPath:
    """Write one sheet with the configured default writer."""
    get_excel_writer(ExcelWriteConfig().writer).write(frame, target, {})
    return target


def _write_sheets(target: Path, sheets: dict[str, pl.DataFrame]) -> UPath:
    """Write a multi-sheet workbook. Neither registered writer writes more than one sheet."""
    book: xlsxwriter.Workbook = xlsxwriter.Workbook(str(target))
    name: str
    frame: pl.DataFrame
    for name, frame in sheets.items():
        sheet: xlsxwriter.Worksheet = book.add_worksheet(name)
        sheet.write_row(0, 0, frame.columns)
        index: int
        row: tuple[Any, ...]
        for index, row in enumerate(frame.iter_rows(), start=1):
            sheet.write_row(index, 0, row)
    book.close()
    return ZPath(str(target))


def _same(left: object, right: object) -> bool:
    """Equality that treats two NaNs as equal, which ``==`` does not."""
    if isinstance(left, float) and isinstance(right, float) and math.isnan(left) and math.isnan(right):
        return True
    try:
        return bool(left == right)
    except Exception:
        return False


def test_a_single_sheet_returns_its_rows(tmp_path: Path) -> None:
    frame: pl.DataFrame = pl.DataFrame({"num": [1.0, 2.0], "txt": ["a", "b"]})
    target: UPath = _write_default(frame, ZPath(str(tmp_path / "one.xlsx")))
    assert fast_excel_reader(target, schema=SIMPLE_SCHEMA).equals(frame)


def test_sheets_are_concatenated_in_workbook_order(tmp_path: Path) -> None:
    target: UPath = _write_sheets(
        tmp_path / "multi.xlsx",
        {
            "first": pl.DataFrame({"num": [1.0], "txt": ["a"]}),
            "second": pl.DataFrame({"num": [2.0], "txt": ["b"]}),
            "third": pl.DataFrame({"num": [3.0], "txt": ["c"]}),
        },
    )
    assert fast_excel_reader(target, schema=SIMPLE_SCHEMA)["num"].to_list() == [1.0, 2.0, 3.0]


def test_sheet_names_selects_and_orders(tmp_path: Path) -> None:
    target: UPath = _write_sheets(
        tmp_path / "pick.xlsx",
        {
            "first": pl.DataFrame({"num": [1.0], "txt": ["a"]}),
            "second": pl.DataFrame({"num": [2.0], "txt": ["b"]}),
            "third": pl.DataFrame({"num": [3.0], "txt": ["c"]}),
        },
    )
    got: pl.DataFrame = fast_excel_reader(target, sheet_names=["third", "first"], schema=SIMPLE_SCHEMA)
    assert got["num"].to_list() == [3.0, 1.0]


def test_an_unknown_sheet_name_is_refused(tmp_path: Path) -> None:
    target: UPath = _write_default(pl.DataFrame({"num": [1.0], "txt": ["a"]}), ZPath(str(tmp_path / "known.xlsx")))
    with pytest.raises(KeyError, match="absent"):
        fast_excel_reader(target, sheet_names=["absent"], schema=SIMPLE_SCHEMA)


def test_one_worker_and_the_default_return_the_same_data(tmp_path: Path) -> None:
    target: UPath = _write_sheets(
        tmp_path / "workers.xlsx",
        {f"s{index}": pl.DataFrame({"num": [float(index)], "txt": [f"t{index}"]}) for index in range(6)},
    )
    serial: pl.DataFrame = fast_excel_reader(target, schema=SIMPLE_SCHEMA, max_workers=1)
    parallel: pl.DataFrame = fast_excel_reader(target, schema=SIMPLE_SCHEMA)
    assert serial.equals(parallel)
    assert serial["num"].to_list() == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]


def test_selector_shaped_column_names_survive(tmp_path: Path) -> None:
    # Both are legal Parquet names and both writers store them correctly. pl.read_excel
    # raises DuplicateError on each; this reader must not.
    frame: pl.DataFrame = pl.DataFrame({"*": [1.0], "^a$": [2.0], "a": [3.0]})
    target: UPath = _write_default(frame, ZPath(str(tmp_path / "selectors.xlsx")))
    got: pl.DataFrame = fast_excel_reader(target, schema={"*": pl.Float64(), "^a$": pl.Float64(), "a": pl.Float64()})
    assert got.columns == ["*", "^a$", "a"]
    assert got.row(0) == (1.0, 2.0, 3.0)


def test_a_trailing_all_null_row_is_restored_from_the_expected_count(tmp_path: Path) -> None:
    # A row of entirely empty cells emits no <row> element, so the sheet does not record it.
    frame: pl.DataFrame = pl.DataFrame({"num": [1.0, None, None], "txt": ["a", None, None]})
    target: UPath = _write_default(frame, ZPath(str(tmp_path / "trailing.xlsx")))

    assert fast_excel_reader(target, schema=SIMPLE_SCHEMA).height == 1
    restored: pl.DataFrame = fast_excel_reader(target, schema=SIMPLE_SCHEMA, expected_rows=3)
    assert restored.height == 3
    assert restored.equals(frame)


def test_interior_and_leading_all_null_rows_need_no_restoring(tmp_path: Path) -> None:
    # Measured: only a trailing run vanishes. Rows with content after them keep their place.
    frame: pl.DataFrame = pl.DataFrame({"num": [None, 2.0, None, 4.0], "txt": [None, "b", None, "d"]})
    target: UPath = _write_default(frame, ZPath(str(tmp_path / "interior.xlsx")))
    assert fast_excel_reader(target, schema=SIMPLE_SCHEMA).equals(frame)


def test_more_rows_than_expected_is_an_error(tmp_path: Path) -> None:
    target: UPath = _write_default(pl.DataFrame({"num": [1.0, 2.0], "txt": ["a", "b"]}), ZPath(str(tmp_path / "over.xlsx")))
    with pytest.raises(ValueError, match="more rows"):
        fast_excel_reader(target, schema=SIMPLE_SCHEMA, expected_rows=1)


def test_a_dtype_the_reader_cannot_map_is_refused(tmp_path: Path) -> None:
    target: UPath = _write_default(pl.DataFrame({"num": [1.0], "txt": ["a"]}), ZPath(str(tmp_path / "dtype.xlsx")))
    with pytest.raises(ValueError, match="num"):
        fast_excel_reader(target, schema={"num": pl.Int64(), "txt": pl.String()})


def test_a_zero_row_workbook_keeps_its_columns(tmp_path: Path) -> None:
    frame: pl.DataFrame = pl.DataFrame({"num": [], "txt": []}, schema=SIMPLE_SCHEMA)
    target: UPath = _write_default(frame, ZPath(str(tmp_path / "empty.xlsx")))
    got: pl.DataFrame = fast_excel_reader(target, schema=SIMPLE_SCHEMA)
    assert got.shape == (0, 2)
    assert got.columns == ["num", "txt"]


@pytest.mark.parametrize("stem", ["parquet_a", "parquet_b"])
def test_every_converted_fixture_column_returns_with_its_dtype_and_values(stem: str, fixture_files: dict[str, Path], tmp_path: Path) -> None:
    # The claim the schema parameter exists to make: given the converted frame's dtypes, all
    # 19 scalar columns come back matching the model in both dtype and value.
    source: pl.DataFrame = pl.read_parquet(fixture_files[stem])
    converted: pl.DataFrame = DataframeConversionToExcel().metadata_of_converted_dataframe(source, []).converted_dataframe
    target: UPath = _write_default(converted, ZPath(str(tmp_path / f"{stem}.xlsx")))

    got: pl.DataFrame = fast_excel_reader(target, schema=dict(converted.schema), expected_rows=converted.height)
    assert got.columns == converted.columns
    assert dict(got.schema) == dict(converted.schema)
    name: str
    for name in converted.columns:
        want: list[Any] = converted[name].to_list()
        have: list[Any] = got[name].to_list()
        differing: list[int] = [index for index in range(len(want)) if not _same(want[index], have[index])]
        assert not differing, f"{name}: {len(differing)} cells differ, first at row {differing[0]}"


def test_without_a_schema_the_dtypes_are_inferred(tmp_path: Path) -> None:
    # Supported, and documented as the thing not to rely on: inference is measured to fail
    # on the fixture data. On a simple sheet it works, which is what this pins.
    frame: pl.DataFrame = pl.DataFrame({"num": [1.0, 2.0], "txt": ["a", "b"]})
    target: UPath = _write_default(frame, ZPath(str(tmp_path / "inferred.xlsx")))
    got: pl.DataFrame = fast_excel_reader(target)
    assert got.columns == ["num", "txt"]
    assert got["txt"].to_list() == ["a", "b"]


def test_rows_missing_from_a_multi_sheet_read_are_refused_not_relocated(tmp_path: Path) -> None:
    # Regression: padding the concatenated frame put the first sheet's lost trailing row
    # after the second sheet's rows, silently reordering the data. [1.0, null] then [2.0]
    # came back as [1.0, 2.0, null]. Nothing in the file says which sheet lost a row, so
    # the reader refuses instead of guessing.
    target: UPath = _write_sheets(
        tmp_path / "split_gap.xlsx",
        {
            "first": pl.DataFrame({"num": [1.0, None], "txt": ["a", None]}),
            "second": pl.DataFrame({"num": [2.0], "txt": ["b"]}),
        },
    )
    with pytest.raises(ValueError, match="which sheet lost them"):
        fast_excel_reader(target, schema=SIMPLE_SCHEMA, expected_rows=3)

    # Read separately, each with its own extent, the rows land where they belong.
    first: pl.DataFrame = fast_excel_reader(target, sheet_names=["first"], schema=SIMPLE_SCHEMA, expected_rows=2)
    second: pl.DataFrame = fast_excel_reader(target, sheet_names=["second"], schema=SIMPLE_SCHEMA, expected_rows=1)
    assert pl.concat([first, second], how="vertical")["num"].to_list() == [1.0, None, 2.0]


def test_a_cell_the_dtype_cannot_represent_is_refused(tmp_path: Path) -> None:
    # The gap this closes: fastexcel turns an unparseable cell into null, and null is what
    # an empty cell returns too, so a cell edited from empty to text read back as empty and
    # the digest did not move. dtype_coercion="strict" does not catch it; the error report
    # does. Written cell by cell because no writer here produces text in a Float64 column.
    target: Path = tmp_path / "edited.xlsx"
    book: xlsxwriter.Workbook = xlsxwriter.Workbook(str(target))
    sheet: xlsxwriter.Worksheet = book.add_worksheet("Sheet1")
    sheet.write_row(0, 0, ["num", "txt"])
    sheet.write_row(1, 0, [1.0, "a"])
    sheet.write_row(2, 0, ["changed", "b"])
    book.close()

    with pytest.raises(ValueError, match="indistinguishable from an empty cell") as caught:
        fast_excel_reader(ZPath(str(target)), schema=SIMPLE_SCHEMA)
    assert "'num' row 1" in str(caught.value)


def test_an_empty_cell_is_not_mistaken_for_a_dropped_one(tmp_path: Path) -> None:
    # The other half: a genuinely empty cell is a legitimate null and must still read back.
    # The row keeps a value in the other column so this stays a test about the empty cell
    # rather than about the trailing all-null row, which is a different behaviour entirely.
    frame: pl.DataFrame = pl.DataFrame({"num": [1.0, None], "txt": ["a", "b"]})
    target: UPath = _write_default(frame, ZPath(str(tmp_path / "empty_cell.xlsx")))
    assert fast_excel_reader(target, schema=SIMPLE_SCHEMA).equals(frame)


def test_many_dropped_cells_are_counted_in_full_but_listed_in_part(tmp_path: Path) -> None:
    # A column of the wrong type produces one error per row; the message names a few and
    # says how many there were, rather than running to the length of the column.
    target: Path = tmp_path / "many_bad.xlsx"
    book: xlsxwriter.Workbook = xlsxwriter.Workbook(str(target))
    sheet: xlsxwriter.Worksheet = book.add_worksheet("Sheet1")
    sheet.write_row(0, 0, ["num", "txt"])
    index: int
    for index in range(1, 9):
        sheet.write_row(index, 0, [f"bad{index}", "t"])
    book.close()

    with pytest.raises(ValueError, match="holds 8 cell") as caught:
        fast_excel_reader(ZPath(str(target)), schema=SIMPLE_SCHEMA)
    assert "and 3 more" in str(caught.value)


def test_a_value_in_a_null_column_is_refused(tmp_path: Path) -> None:
    # Regression, and the one hole to_arrow_with_errors cannot see: asking fastexcel for its
    # "null" dtype discards a cell's content and reports no error at all, so an edited cell
    # produced a digest identical to the untouched original. Null columns are read as text
    # for exactly this reason.
    target: Path = tmp_path / "null_column.xlsx"
    book: xlsxwriter.Workbook = xlsxwriter.Workbook(str(target))
    sheet: xlsxwriter.Worksheet = book.add_worksheet("Sheet1")
    sheet.write_row(0, 0, ["keep", "empty"])
    sheet.write_row(1, 0, [1.0, None])
    sheet.write_row(2, 0, [2.0, "sneaked in"])
    book.close()

    schema: dict[str, pl.DataType] = {"keep": pl.Float64(), "empty": pl.Null()}
    with pytest.raises(ValueError, match="the schema says are empty") as caught:
        fast_excel_reader(ZPath(str(target)), schema=schema)
    assert "'empty' row 1" in str(caught.value)


def test_a_genuinely_empty_null_column_reads_back_as_null(tmp_path: Path) -> None:
    # The other half: a column the model says is empty and which is empty must still arrive
    # as Null, not as the text it was read through.
    target: Path = tmp_path / "null_ok.xlsx"
    book: xlsxwriter.Workbook = xlsxwriter.Workbook(str(target))
    sheet: xlsxwriter.Worksheet = book.add_worksheet("Sheet1")
    sheet.write_row(0, 0, ["keep", "empty"])
    sheet.write_row(1, 0, [1.0, None])
    sheet.write_row(2, 0, [2.0, None])
    book.close()

    got: pl.DataFrame = fast_excel_reader(ZPath(str(target)), schema={"keep": pl.Float64(), "empty": pl.Null()})
    assert got.schema["empty"] == pl.Null
    assert got["empty"].to_list() == [None, None]
    assert got["keep"].to_list() == [1.0, 2.0]
