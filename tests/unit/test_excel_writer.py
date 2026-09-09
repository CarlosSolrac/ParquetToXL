"""Frozen tests for the Excel writer registry and the two registered writers.

Read-only for an implementer. If one of these looks wrong, stop and report it rather than
editing it.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import polars as pl
import pytest
from python_calamine import CalamineWorkbook

from parquet_to_xl.conversion.to_excel import DataframeConversionToExcel
from parquet_to_xl.excel.writer import (
    EXCEL_MAX_COLUMNS,
    EXCEL_WRITERS,
    ExcelWriteConfig,
    ExcelWriterBase,
    PolarsExcelWriter,
    RustpyExcelWriter,
    get_excel_writer,
    register_excel_writer,
)
from parquet_to_xl.paths import ZPath

if TYPE_CHECKING:
    from upath import UPath


def _converted() -> pl.DataFrame:
    """A frame that has been through ToExcel, which is what writers expect."""
    source: pl.DataFrame = pl.DataFrame(
        {
            "i": pl.Series([1, None, 3], dtype=pl.Int64),
            "s": pl.Series(["a", "", None], dtype=pl.String),
            "b": pl.Series([True, False, None], dtype=pl.Boolean),
            "d": pl.Series([dt.date(2020, 1, 1), None, dt.date(1999, 12, 31)], dtype=pl.Date),
            "y": pl.Series([b"\xff", b"", None], dtype=pl.Binary),
        },
    )
    return DataframeConversionToExcel().metadata_of_converted_dataframe(source, []).converted_dataframe


def _read_back(path: UPath, schema: dict[str, pl.DataType]) -> pl.DataFrame:
    return pl.read_excel(str(path), schema_overrides=schema)


def test_the_default_writer_is_rustpy() -> None:
    assert ExcelWriteConfig().writer == "rustpy-xlsxwriter"
    assert ExcelWriteConfig().writer == RustpyExcelWriter.identifier


def test_the_default_config_carries_no_options() -> None:
    assert ExcelWriteConfig().options == {}


def test_both_writers_are_registered() -> None:
    assert set(EXCEL_WRITERS) >= {"rustpy-xlsxwriter", "polars-xlsxwriter"}
    assert EXCEL_WRITERS["rustpy-xlsxwriter"] is RustpyExcelWriter
    assert EXCEL_WRITERS["polars-xlsxwriter"] is PolarsExcelWriter


def test_the_registry_resolves_an_identifier_to_an_instance() -> None:
    writer: ExcelWriterBase = get_excel_writer("rustpy-xlsxwriter")
    assert isinstance(writer, RustpyExcelWriter)


def test_an_unknown_identifier_raises_and_names_what_is_registered() -> None:
    with pytest.raises(KeyError) as caught:
        get_excel_writer("no-such-writer")
    assert "rustpy-xlsxwriter" in str(caught.value)


def test_registering_a_duplicate_identifier_raises() -> None:
    # Silently replacing would make the winner depend on import order.
    class _Clashing(ExcelWriterBase):
        identifier: ClassVar[str] = "rustpy-xlsxwriter"

        def write(self, df: pl.DataFrame, path: UPath, options: dict[str, object]) -> None:
            """Never called."""

    with pytest.raises(ValueError, match="already registered"):
        register_excel_writer(_Clashing)


def test_the_base_class_is_abstract() -> None:
    import inspect

    assert inspect.isabstract(ExcelWriterBase)


def test_the_default_writer_round_trips_the_model_exactly(tmp_path: Path) -> None:
    # The property that matters, and it belongs to the default writer alone: the conversion
    # models the round trip, so what comes back must equal what went in, dtype and value.
    frame: pl.DataFrame = _converted()
    target: UPath = ZPath(str(tmp_path / "default.xlsx"))
    get_excel_writer("rustpy-xlsxwriter").write(frame, target, {})
    back: pl.DataFrame = _read_back(target, dict(frame.schema))
    assert dict(back.schema) == dict(frame.schema)
    assert back.to_dicts() == frame.to_dicts()


def test_the_polars_writer_produces_a_readable_workbook_of_the_right_shape(tmp_path: Path) -> None:
    # Deliberately weaker than the default writer contract. This one is not round-trip safe
    # -- see the two measured losses below -- so it is held to shape, not to values.
    frame: pl.DataFrame = _converted()
    target: UPath = ZPath(str(tmp_path / "polars.xlsx"))
    get_excel_writer("polars-xlsxwriter").write(frame, target, {})
    back: pl.DataFrame = _read_back(target, dict(frame.schema))
    assert dict(back.schema) == dict(frame.schema)
    assert back.columns == frame.columns
    assert back.height == frame.height


def test_the_polars_writer_loses_float64_precision(tmp_path: Path) -> None:
    # Pinned as a known limitation, not a bug awaiting a fix: xlsxwriter serializes numbers
    # with too few significant digits, and avoiding that means not calling write_excel at
    # all. The default writer returns the value exactly, which is why it is the one carrying
    # the digest guarantee. Should this ever start failing, the limitation is gone and both
    # this test and the class docstring should be revisited.
    precise: float = 1.2345678901234567
    frame: pl.DataFrame = pl.DataFrame({"a": pl.Series([precise], dtype=pl.Float64)})
    target: UPath = ZPath(str(tmp_path / "precision.xlsx"))
    PolarsExcelWriter().write(frame, target, {})
    assert _read_back(target, dict(frame.schema))["a"].to_list() != [precise]

    default_target: UPath = ZPath(str(tmp_path / "precision-default.xlsx"))
    RustpyExcelWriter().write(frame, default_target, {})
    assert _read_back(default_target, dict(frame.schema))["a"].to_list() == [precise]


def test_the_polars_writer_shifts_1900_01_01_by_a_day(tmp_path: Path) -> None:
    # Excel inherited a leap-year bug from Lotus 1-2-3. DuckDB shows it too; the default
    # writer does not. Pinned for the same reason as the precision loss above.
    moment: dt.datetime = dt.datetime(1900, 1, 1, 12, 0, tzinfo=dt.UTC)
    frame: pl.DataFrame = pl.DataFrame({"t": pl.Series([moment], dtype=pl.Datetime("us", "UTC"))})
    target: UPath = ZPath(str(tmp_path / "lotus.xlsx"))
    PolarsExcelWriter().write(frame, target, {})
    assert _read_back(target, dict(frame.schema))["t"].to_list() == [moment - dt.timedelta(days=1)]

    default_target: UPath = ZPath(str(tmp_path / "lotus-default.xlsx"))
    RustpyExcelWriter().write(frame, default_target, {})
    assert _read_back(default_target, dict(frame.schema))["t"].to_list() == [moment]


def test_a_trailing_all_null_row_survives_neither_writer(tmp_path: Path) -> None:
    # Not a writer defect: a row whose cells are all empty produces no row element in the
    # sheet, so the row extent is not stored at all. Both writers lose it. The metadata
    # already carries what is needed to restore it -- value_count is the row count -- so
    # this belongs to fast_excel_reader, and is recorded in the findings.
    frame: pl.DataFrame = pl.DataFrame({"a": pl.Series([1.0, None, None], dtype=pl.Float64)})
    identifier: str
    for identifier in ("rustpy-xlsxwriter", "polars-xlsxwriter"):
        target: UPath = ZPath(str(tmp_path / f"{identifier}-trailing.xlsx"))
        get_excel_writer(identifier).write(frame, target, {})
        assert _read_back(target, dict(frame.schema)).height == 1


@pytest.mark.parametrize("identifier", ["rustpy-xlsxwriter", "polars-xlsxwriter"])
def test_the_sheet_name_option_is_honoured(identifier: str, tmp_path: Path) -> None:
    target: UPath = ZPath(str(tmp_path / f"{identifier}-named.xlsx"))
    get_excel_writer(identifier).write(_converted(), target, {"sheet_name": "Records"})
    sheets: dict[str, pl.DataFrame] = pl.read_excel(str(target), sheet_id=0)
    assert list(sheets) == ["Records"]


@pytest.mark.parametrize("identifier", ["rustpy-xlsxwriter", "polars-xlsxwriter"])
def test_an_unknown_option_is_rejected_rather_than_ignored(identifier: str, tmp_path: Path) -> None:
    # A typo in configuration would otherwise be invisible.
    target: UPath = ZPath(str(tmp_path / f"{identifier}-bad.xlsx"))
    with pytest.raises(ValueError, match="unknown excel writer options"):
        get_excel_writer(identifier).write(_converted(), target, {"sheetname": "Oops"})


@pytest.mark.parametrize("identifier", ["rustpy-xlsxwriter", "polars-xlsxwriter"])
def test_a_non_string_sheet_name_is_rejected(identifier: str, tmp_path: Path) -> None:
    target: UPath = ZPath(str(tmp_path / f"{identifier}-int.xlsx"))
    with pytest.raises(ValueError, match="must be a string"):
        get_excel_writer(identifier).write(_converted(), target, {"sheet_name": 7})


@pytest.mark.parametrize("identifier", ["rustpy-xlsxwriter", "polars-xlsxwriter"])
def test_an_existing_file_is_overwritten(identifier: str, tmp_path: Path) -> None:
    target: UPath = ZPath(str(tmp_path / f"{identifier}-twice.xlsx"))
    target.write_text("not a workbook")
    get_excel_writer(identifier).write(_converted(), target, {})
    assert _read_back(target, dict(_converted().schema)).height == 3


@pytest.mark.parametrize("identifier", ["rustpy-xlsxwriter", "polars-xlsxwriter"])
def test_the_written_column_order_is_the_frames(identifier: str, tmp_path: Path) -> None:
    frame: pl.DataFrame = _converted()
    target: UPath = ZPath(str(tmp_path / f"{identifier}-order.xlsx"))
    get_excel_writer(identifier).write(frame, target, {})
    assert _read_back(target, dict(frame.schema)).columns == frame.columns


@pytest.mark.parametrize("identifier", ["rustpy-xlsxwriter", "polars-xlsxwriter"])
def test_a_zero_row_frame_keeps_its_headers(identifier: str, tmp_path: Path) -> None:
    # Regression, from a Codex review. Streaming a zero-row frame yields no rows, so the
    # writer never learned a column name and wrote a sheet with no header at all -- which
    # pl.read_excel then refuses with NoDataError. An empty query result must still produce
    # a readable workbook describing its columns.
    empty: pl.DataFrame = pl.DataFrame(schema={"a": pl.Float64, "b": pl.String})
    target: UPath = ZPath(str(tmp_path / f"{identifier}-empty.xlsx"))
    get_excel_writer(identifier).write(empty, target, {})
    back: pl.DataFrame = pl.read_excel(str(target), raise_if_empty=False)
    assert back.columns == ["a", "b"]
    assert back.height == 0


@pytest.mark.parametrize("identifier", ["rustpy-xlsxwriter", "polars-xlsxwriter"])
def test_a_long_url_string_is_written_as_text_not_a_hyperlink(identifier: str, tmp_path: Path) -> None:
    # Regression, from a Codex review. At its default xlsxwriter turns a URL-shaped string
    # into a hyperlink, and one past Excel's 2,079-character link limit is then dropped
    # entirely with only a warning -- the cell simply vanished from the workbook.
    url: str = "https://example.com/" + "a" * 2100
    frame: pl.DataFrame = pl.DataFrame({"s": pl.Series([url, "short"], dtype=pl.String)})
    target: UPath = ZPath(str(tmp_path / f"{identifier}-url.xlsx"))
    get_excel_writer(identifier).write(frame, target, {})
    back: pl.DataFrame = _read_back(target, {"s": pl.String()})
    assert back["s"].to_list() == [url, "short"]


def test_rustpy_writes_columns_differing_only_by_case(tmp_path: Path) -> None:
    frame: pl.DataFrame = pl.DataFrame({"a": pl.Series([1.0], dtype=pl.Float64), "A": pl.Series([2.0], dtype=pl.Float64)})
    target: UPath = ZPath(str(tmp_path / "case-rustpy.xlsx"))
    RustpyExcelWriter().write(frame, target, {})
    back: pl.DataFrame = pl.read_excel(str(target))
    assert back.columns == ["a", "A"]
    assert back.height == 1


def test_polars_refuses_columns_differing_only_by_case(tmp_path: Path) -> None:
    # Regression, from a Codex review. write_excel builds an Excel table, and add_table
    # folds header case: given "a" and "A" it warned, dropped one, and still returned --
    # the workbook read back with one column and no rows. Refusing loudly beats that.
    frame: pl.DataFrame = pl.DataFrame({"a": pl.Series([1.0], dtype=pl.Float64), "A": pl.Series([2.0], dtype=pl.Float64)})
    target: UPath = ZPath(str(tmp_path / "case-polars.xlsx"))
    with pytest.raises(ValueError, match="differ only by case"):
        PolarsExcelWriter().write(frame, target, {})


def test_polars_refuses_more_columns_than_a_worksheet_holds(tmp_path: Path) -> None:
    # Regression, from a Codex review. write_excel returned successfully and produced a
    # workbook that read back as (0, 0) -- an oversized frame silently replaced whatever
    # was at the destination with nothing.
    wide: pl.DataFrame = pl.DataFrame({f"c{i}": pl.Series([1.0], dtype=pl.Float64) for i in range(EXCEL_MAX_COLUMNS + 1)})
    target: UPath = ZPath(str(tmp_path / "wide.xlsx"))
    with pytest.raises(ValueError, match="columns"):
        PolarsExcelWriter().write(wide, target, {})
    assert not target.exists()


def test_polars_refuses_more_rows_than_a_worksheet_holds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The guard, not Excel's limit: building a frame of over a million rows to reach the real
    # ceiling would cost more than the check is worth, so the ceiling is patched down. The
    # header row counts toward it, which is why three rows exceed a limit of three.
    monkeypatch.setattr("parquet_to_xl.excel.writer.EXCEL_MAX_ROWS", 3)
    frame: pl.DataFrame = pl.DataFrame({"a": pl.Series([1.0, 2.0, 3.0], dtype=pl.Float64)})
    target: UPath = ZPath(str(tmp_path / "tall.xlsx"))
    with pytest.raises(ValueError, match="rows plus a header"):
        PolarsExcelWriter().write(frame, target, {})
    assert not target.exists()


@pytest.mark.parametrize("identifier", ["rustpy-xlsxwriter", "polars-xlsxwriter"])
def test_an_empty_column_name_is_refused_by_both_writers(identifier: str, tmp_path: Path) -> None:
    # Regression, from a Codex review, reproduced differently than reported: it is not
    # polars-only. Measured, polars renames the column to the generated "Column1", and
    # rustpy renames it to "1" *and drops the row*. A silently renamed column breaks
    # schema-driven reading, so both refuse rather than corrupt.
    frame: pl.DataFrame = pl.DataFrame({"": pl.Series([1.0], dtype=pl.Float64)})
    target: UPath = ZPath(str(tmp_path / f"{identifier}-unnamed.xlsx"))
    with pytest.raises(ValueError, match="empty column name"):
        get_excel_writer(identifier).write(frame, target, {})
    assert not target.exists()


@pytest.mark.parametrize("identifier", ["rustpy-xlsxwriter", "polars-xlsxwriter"])
def test_selector_shaped_column_names_are_written_faithfully(identifier: str, tmp_path: Path) -> None:
    # "*" and "^a$" are legal Parquet names that Polars reads as selectors. Both writers
    # handle them correctly -- verified against the raw sheet, because pl.read_excel is what
    # cannot read them back, raising DuplicateError. That is a reader limitation, recorded in
    # the findings; refusing them at the writer would blame the wrong component.
    frame: pl.DataFrame = pl.DataFrame({"*": pl.Series([1.0], dtype=pl.Float64), "b": pl.Series([2.0], dtype=pl.Float64)})
    target: UPath = ZPath(str(tmp_path / f"{identifier}-selector.xlsx"))
    get_excel_writer(identifier).write(frame, target, {})
    rows: list[list[Any]] = CalamineWorkbook.from_path(str(target)).get_sheet_by_index(0).to_python(skip_empty_area=False)
    assert [str(cell) for cell in rows[0]] == ["*", "b"]
    assert rows[1] == [1.0, 2.0]


@pytest.mark.parametrize("identifier", ["rustpy-xlsxwriter", "polars-xlsxwriter"])
def test_a_fractional_time_round_trips_at_microsecond_precision(identifier: str, tmp_path: Path) -> None:
    # Regression, from a Codex review. write_excel emitted a numeric time cell whose fraction
    # was lost -- 23:59:59.999999 read back as 00:00 -- so choosing the second writer silently
    # changed values and broke the digest. Rendering Time as text makes both writers agree.
    frame: pl.DataFrame = pl.DataFrame(
        {
            "t": pl.Series([dt.time(23, 59, 59, 999999), None, dt.time(12, 0), dt.time(0, 0, 0, 1)], dtype=pl.Time),
            "k": pl.Series([1.0, 2.0, 3.0, 4.0], dtype=pl.Float64),
        },
    )
    target: UPath = ZPath(str(tmp_path / f"{identifier}-time.xlsx"))
    get_excel_writer(identifier).write(frame, target, {})
    back: pl.DataFrame = _read_back(target, dict(frame.schema))
    assert back["t"].to_list() == frame["t"].to_list()


def test_rustpy_refuses_a_cell_over_excels_limit(tmp_path: Path) -> None:
    # Its distinguishing behaviour: it declines rather than truncating or writing anyway.
    # The conversion cuts strings precisely so a converted frame never reaches this.
    oversized: pl.DataFrame = pl.DataFrame({"s": pl.Series(["x" * 40000], dtype=pl.String)})
    target: UPath = ZPath(str(tmp_path / "oversized.xlsx"))
    with pytest.raises(RuntimeError, match="32,767"):
        RustpyExcelWriter().write(oversized, target, {})


def test_a_config_can_name_the_second_writer() -> None:
    config: ExcelWriteConfig = ExcelWriteConfig(writer="polars-xlsxwriter", options={"sheet_name": "S"})
    writer: ExcelWriterBase = get_excel_writer(config.writer)
    assert isinstance(writer, PolarsExcelWriter)
    assert config.options == {"sheet_name": "S"}


def test_the_config_round_trips_through_pydantic() -> None:
    config: ExcelWriteConfig = ExcelWriteConfig(writer="rustpy-xlsxwriter", options={"sheet_name": "S"})
    restored: dict[str, Any] = config.model_dump()
    assert ExcelWriteConfig.model_validate(restored) == config
