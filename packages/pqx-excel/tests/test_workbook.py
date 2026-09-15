"""Frozen tests for the multi-sheet workbook writer.

Read-only for an implementer. If one of these looks wrong, stop and report it rather than
editing it.
"""

from __future__ import annotations

import zipfile
from typing import TYPE_CHECKING

import polars as pl
import pytest
import rustpy_xlsxwriter
from pqx_common.names import WORKSHEET_NAME_LIMIT, PortableNameError, WorksheetNameError, validate_worksheet_name
from pqx_common.paths import ZPath
from pqx_excel.workbook import AUTOFIT, DEDUPE_STRINGS, SheetNameError, validate_sheet_name, write_workbook
from pqx_excel.writer import EXCEL_MAX_COLUMNS

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from upath import UPath


def _frame(rows: int, *, first_column: str = "a") -> pl.DataFrame:
    """A small converted-shaped frame of the requested height."""
    return pl.DataFrame({first_column: pl.Series(list(range(rows)), dtype=pl.Int64), "s": pl.Series([f"r{index}" for index in range(rows)], dtype=pl.String)})


# --------------------------------------------------------------------------------------
# Sheet-name validation
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["2025", "sales 2025-01~06", "debits 2025-Jan~Jun", "a" * WORKSHEET_NAME_LIMIT])
def test_a_legal_sheet_name_passes_both_checks(name: str) -> None:
    assert validate_sheet_name(name) == name


@pytest.mark.parametrize("name", ["", "a" * (WORKSHEET_NAME_LIMIT + 1), "bad/name", "has:colon", "a[b]", "History", "'quoted'", "tab\there"])
def test_an_illegal_sheet_name_is_refused(name: str) -> None:
    with pytest.raises(PortableNameError):
        validate_sheet_name(name)


@pytest.mark.parametrize("name", ["History", "'quoted'", "tab\there", "nul\x00here"])
def test_the_project_rule_catches_what_the_library_lets_through(name: str) -> None:
    # Measured, not assumed: rustpy-xlsxwriter's own validate_sheet_name accepts all of these.
    # It is a length-and-charset check, so it is layered rather than relied on.
    assert rustpy_xlsxwriter.validate_sheet_name(name) is True
    with pytest.raises(WorksheetNameError):
        validate_sheet_name(name)


def test_the_project_rule_is_a_strict_superset_of_the_librarys() -> None:
    # The property that makes the library check a backstop rather than a second opinion. If a
    # release ever tightens the library's rule, this fails and says which name to encode.
    candidates: list[str] = [chr(code) for code in range(32, 127)] + ["\t", "\n", "\x00", "é", "​", " "]
    character: str
    template: str
    for character in candidates:
        for template in ["a{}b", "{}ab", "ab{}", "{}"]:
            name: str = template.format(character)
            try:
                validate_worksheet_name(name)
            except PortableNameError:
                continue
            assert rustpy_xlsxwriter.validate_sheet_name(name) is True, f"the library refuses {name!r}, which this project's rule allows"


def test_a_name_the_library_alone_refuses_is_reported_as_a_tightened_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    # Unreachable with the installed version, by the superset property above. Driven here so
    # the backstop is exercised rather than merely asserted, since the property is a fact about
    # a version and not a promise.
    def refuse_everything(name: str) -> bool:  # noqa: ARG001
        return False

    monkeypatch.setattr(rustpy_xlsxwriter, "validate_sheet_name", refuse_everything)
    with pytest.raises(SheetNameError, match="has tightened"):
        validate_sheet_name("2025")


# --------------------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------------------


def test_a_workbook_holds_every_sheet_in_the_order_given(tmp_path: Path) -> None:
    target: UPath = ZPath(str(tmp_path / "multi.xlsx"))
    written: tuple[str, ...] = write_workbook([("2025-Jan", _frame(2)), ("2025-Feb", _frame(3)), ("2025-Mar", _frame(1))], target)
    assert written == ("2025-Jan", "2025-Feb", "2025-Mar")
    back: dict[str, pl.DataFrame] = pl.read_excel(str(target), sheet_id=0)
    assert list(back) == ["2025-Jan", "2025-Feb", "2025-Mar"]
    assert [frame.height for frame in back.values()] == [2, 3, 1]


def test_one_sheet_is_a_legitimate_workbook(tmp_path: Path) -> None:
    target: UPath = ZPath(str(tmp_path / "single.xlsx"))
    assert write_workbook([("Data", _frame(2))], target) == ("Data",)


def test_many_sheets_cost_nothing_structural(tmp_path: Path) -> None:
    # Gate 0a measured sheet count as free: flat peak RSS from 1 to 32 sheets. This asserts the
    # functional half of that -- that all of them arrive, correct and distinct.
    target: UPath = ZPath(str(tmp_path / "many.xlsx"))
    sheets: list[tuple[str, pl.DataFrame]] = [(f"s{index:02d}", _frame(index + 1)) for index in range(32)]
    assert len(write_workbook(sheets, target)) == 32
    back: dict[str, pl.DataFrame] = pl.read_excel(str(target), sheet_id=0)
    assert [frame.height for frame in back.values()] == list(range(1, 33))


def test_a_zero_row_sheet_keeps_its_header(tmp_path: Path) -> None:
    # A row iterator over no rows carries no column names, so the writer would emit a sheet with
    # no header at all and pl.read_excel would raise NoDataError. The frame is handed over whole.
    target: UPath = ZPath(str(tmp_path / "empty.xlsx"))
    write_workbook([("Full", _frame(2)), ("Empty", _frame(0))], target)
    back: dict[str, pl.DataFrame] = pl.read_excel(str(target), sheet_id=0, raise_if_empty=False)
    assert back["Empty"].columns == ["a", "s"]
    assert back["Empty"].height == 0


def test_a_generator_of_sheets_is_accepted(tmp_path: Path) -> None:
    target: UPath = ZPath(str(tmp_path / "lazy.xlsx"))
    sheets: Iterator[tuple[str, pl.DataFrame]] = ((f"s{index}", _frame(1)) for index in range(3))
    assert write_workbook(sheets, target) == ("s0", "s1", "s2")


def test_a_workbook_with_no_sheets_is_refused(tmp_path: Path) -> None:
    target: UPath = ZPath(str(tmp_path / "none.xlsx"))
    with pytest.raises(ValueError, match="at least one worksheet"):
        write_workbook([], target)
    assert not target.exists()


def test_two_sheet_names_differing_only_in_case_are_refused(tmp_path: Path) -> None:
    # Excel treats them as one sheet, so the second would overwrite the first.
    target: UPath = ZPath(str(tmp_path / "collide.xlsx"))
    with pytest.raises(PortableNameError, match="collides"):
        write_workbook([("Sales", _frame(1)), ("sales", _frame(1))], target)


def test_an_illegal_name_on_a_later_sheet_leaves_nothing_written(tmp_path: Path) -> None:
    # Every sheet is validated before the first is added, so a workbook is never left
    # half-written at the destination by a name the tenth sheet would have failed on.
    target: UPath = ZPath(str(tmp_path / "late.xlsx"))
    sheets: list[tuple[str, pl.DataFrame]] = [(f"s{index}", _frame(1)) for index in range(9)]
    sheets.append(("History", _frame(1)))
    with pytest.raises(PortableNameError):
        write_workbook(sheets, target)
    assert not target.exists()


def test_an_unnameable_column_is_refused(tmp_path: Path) -> None:
    target: UPath = ZPath(str(tmp_path / "unnameable.xlsx"))
    frame: pl.DataFrame = pl.DataFrame({"": pl.Series([1], dtype=pl.Int64)})
    with pytest.raises(ValueError, match="empty column name"):
        write_workbook([("Data", frame)], target)
    assert not target.exists()


def test_a_sheet_wider_than_a_worksheet_is_refused(tmp_path: Path) -> None:
    target: UPath = ZPath(str(tmp_path / "wide.xlsx"))
    wide: pl.DataFrame = pl.DataFrame({f"c{index}": pl.Series([1], dtype=pl.Int64) for index in range(EXCEL_MAX_COLUMNS + 1)})
    with pytest.raises(ValueError, match="a worksheet holds"):
        write_workbook([("Wide", wide)], target)


def test_a_sheet_taller_than_a_worksheet_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Driven with the constant patched down: exercising it honestly needs a frame of over a
    # million rows, and the guard is the same rule on the other axis.
    monkeypatch.setattr("pqx_excel.workbook.EXCEL_MAX_ROWS", 3)
    target: UPath = ZPath(str(tmp_path / "tall.xlsx"))
    with pytest.raises(ValueError, match="rows plus a header"):
        write_workbook([("Tall", _frame(3))], target)


def test_a_reused_name_across_calls_is_fine(tmp_path: Path) -> None:
    # Uniqueness is per workbook, not global: two workbooks may each hold a sheet called 2025.
    first: UPath = ZPath(str(tmp_path / "one.xlsx"))
    second: UPath = ZPath(str(tmp_path / "two.xlsx"))
    assert write_workbook([("2025", _frame(1))], first) == ("2025",)
    assert write_workbook([("2025", _frame(1))], second) == ("2025",)


def test_the_two_writer_settings_are_off_and_stay_off() -> None:
    # autofit for determinism -- gate 0a measured its memory cost at nothing, so the memory
    # reason the single-sheet writer used to give is gone. dedupe_strings for memory: ~1.2 KiB
    # per row, per sheet, which is what would take the writer out of constant-memory mode.
    assert AUTOFIT is False
    assert DEDUPE_STRINGS is False


def test_column_widths_do_not_depend_on_how_rows_were_split(tmp_path: Path) -> None:
    # The determinism autofit=False buys: the same rows split differently produce the same
    # bytes for the sheets that hold the same rows.
    narrow: UPath = ZPath(str(tmp_path / "narrow.xlsx"))
    wide: UPath = ZPath(str(tmp_path / "wide.xlsx"))
    short: pl.DataFrame = pl.DataFrame({"s": pl.Series(["a"], dtype=pl.String)})
    long: pl.DataFrame = pl.DataFrame({"s": pl.Series(["a" * 200], dtype=pl.String)})
    write_workbook([("Data", short)], narrow)
    write_workbook([("Data", short), ("Other", long)], wide)
    first: zipfile.ZipFile
    second: zipfile.ZipFile
    with zipfile.ZipFile(str(narrow)) as first, zipfile.ZipFile(str(wide)) as second:
        assert first.read("xl/worksheets/sheet1.xml") == second.read("xl/worksheets/sheet1.xml")
