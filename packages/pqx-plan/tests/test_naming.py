"""Frozen tests for rendering and validating the names a plan produces."""

from __future__ import annotations

from typing import Any

import pytest
from pqx_calendar.labels import CoverageSpan
from pqx_calendar.periods import PeriodKey
from pqx_common.names import PortableNameError
from pqx_plan.allocate import AllocatedWorkbook
from pqx_plan.config import NamingSettings
from pqx_plan.naming import NamedWorkbook, name_workbooks, reject_traversal, render_template, sheet_period_label, workbook_period_label_for
from pqx_plan.partition import PlannedSheet

STEMS: dict[str, str] = {"sales": "sales_2025", "returns": "returns_2025"}


def _naming(**changes: Any) -> NamingSettings:  # noqa: ANN401
    """The example configuration's naming block, with overrides."""
    settings: dict[str, Any] = {
        "workbook": "{profile}_{period_label}_{workbook_index:03d}.xlsx",
        "worksheet": "{source} {period_label}",
        "single_worksheet": "{source} Data",
        "overflow_worksheet": "{source} {period_label}_p{part_index:02d}",
        "month_format": "numeric",
        "worksheet_prefix": "",
        "sheet_collision": "error",
    }
    settings.update(changes)
    return NamingSettings.model_validate(settings)


def _year(alias: str, year: int, rows: int = 10) -> PlannedSheet:
    """A period sheet covering one year."""
    return PlannedSheet(source_alias=alias, kind="period", rows=rows, coverage=CoverageSpan.of(PeriodKey("year", year)))


def _month(alias: str, year: int, month: int, rows: int = 10) -> PlannedSheet:
    """A period sheet covering one month."""
    return PlannedSheet(source_alias=alias, kind="period", rows=rows, coverage=CoverageSpan.of(PeriodKey("month", year, month)))


def _overflow(alias: str, year: int, month: int, part: int) -> PlannedSheet:
    """An overflow fragment of one month."""
    return PlannedSheet(source_alias=alias, kind="overflow", rows=10, coverage=CoverageSpan.of(PeriodKey("month", year, month)), part_index=part, parts=3)


def _undated(alias: str) -> PlannedSheet:
    """An undated sheet."""
    return PlannedSheet(source_alias=alias, kind="undated", rows=3)


def _named(sheets: list[PlannedSheet], naming: NamingSettings) -> NamedWorkbook:
    """Name a single workbook holding the given sheets."""
    return name_workbooks([AllocatedWorkbook(index=1, sheets=tuple(sheets))], naming, profile="annual_review", stems=STEMS)[0]


# --------------------------------------------------------------------------------------
# Templates
# --------------------------------------------------------------------------------------


def test_a_template_expands_its_tokens_with_zero_padding() -> None:
    assert render_template("{profile}_{period_label}_{workbook_index:03d}.xlsx", {"profile": "annual", "period_label": "2025", "workbook_index": 7}) == "annual_2025_007.xlsx"


def test_a_template_naming_an_unavailable_token_fails_naming_it() -> None:
    with pytest.raises(KeyError, match="quarter"):
        render_template("{profile}_{quarter}.xlsx", {"profile": "annual"})


@pytest.mark.parametrize("template", ["{profile.__class__}", "{profile.__class__.__mro__}", "{profile[0]}", "{period_label[0]}"])
def test_a_template_may_not_reach_into_a_value(template: str) -> None:
    # format_map bounds which NAMES a template can reach and does nothing about what it reaches
    # THROUGH them. Left alone, "{profile.__class__}" renders <class 'str'> and the usual route
    # into __globals__ is open -- from a configuration file.
    with pytest.raises(ValueError, match="reaches into a value"):
        render_template(template, {"profile": "annual", "period_label": "2025"})


def test_a_malformed_template_is_refused() -> None:
    with pytest.raises(ValueError, match="expected '}'"):
        render_template("{profile", {"profile": "annual"})


def test_a_format_specification_a_token_cannot_take_is_refused() -> None:
    with pytest.raises(ValueError, match="Unknown format code"):
        render_template("{profile:03d}", {"profile": "annual"})


# --------------------------------------------------------------------------------------
# Labels
# --------------------------------------------------------------------------------------


def test_a_dated_sheet_labels_its_coverage() -> None:
    assert sheet_period_label(_year("sales", 2025), "numeric") == "2025"
    assert sheet_period_label(_month("sales", 2025, 1), "abbreviated") == "2025-Jan"


def test_an_undated_sheet_labels_itself_undated() -> None:
    assert sheet_period_label(_undated("sales"), "numeric") == "Undated"


def test_a_sheet_describing_no_calendar_labels_itself_data() -> None:
    assert sheet_period_label(PlannedSheet(source_alias="sales", kind="period", rows=10), "numeric") == "Data"


def test_a_workbook_summarises_earliest_through_latest() -> None:
    assert workbook_period_label_for([_year("sales", 2022), _year("sales", 2024)], "numeric") == "2022~2024"


def test_a_workbook_holding_undated_rows_too_gets_the_suffix() -> None:
    assert workbook_period_label_for([_year("sales", 2025), _undated("sales")], "numeric") == "2025_Undated"


def test_an_all_undated_workbook_is_labelled_undated() -> None:
    assert workbook_period_label_for([_undated("sales")], "numeric") == "Undated"


def test_a_workbook_of_balanced_sheets_is_labelled_data() -> None:
    assert workbook_period_label_for([PlannedSheet(source_alias="sales", kind="period", rows=10)], "numeric") == "Data"


def test_a_workbook_mixing_precisions_summarises_at_the_coarsest_grain() -> None:
    # Fan-in makes this ordinary: one source's 2025 is subdivided into months while another's
    # fits a single year sheet. The summary can only be stated honestly at the coarser grain.
    assert workbook_period_label_for([_month("sales", 2025, 4), _year("returns", 2025)], "numeric") == "2025"


def test_a_workbook_mixing_month_and_day_precision_summarises_by_month() -> None:
    daily: PlannedSheet = PlannedSheet(source_alias="sales", kind="period", rows=10, coverage=CoverageSpan.of(PeriodKey("day", 2025, 3, 15)))
    assert workbook_period_label_for([daily, _month("returns", 2025, 1)], "numeric") == "2025-01~03"


# --------------------------------------------------------------------------------------
# Naming a workbook
# --------------------------------------------------------------------------------------


def test_every_name_in_the_example_configuration_renders() -> None:
    named: NamedWorkbook = _named([_year("sales", 2025), _year("returns", 2025)], _naming())
    assert named.filename == "annual_review_2025_001.xlsx"
    assert [sheet.name for sheet in named.sheets] == ["sales 2025", "returns 2025"]


def test_an_overflow_sheet_uses_its_own_template_and_fragment_index() -> None:
    named: NamedWorkbook = _named([_overflow("sales", 2025, 7, 1), _overflow("sales", 2025, 7, 2)], _naming())
    assert [sheet.name for sheet in named.sheets] == ["sales 2025-07_p01", "sales 2025-07_p02"]


def test_a_single_sheet_export_uses_the_single_worksheet_template() -> None:
    single: PlannedSheet = PlannedSheet(source_alias="sales", kind="single", rows=10)
    assert _named([single], _naming()).sheets[0].name == "sales Data"


def test_the_source_stem_token_renders_the_file_basename() -> None:
    named: NamedWorkbook = _named([_year("sales", 2025)], _naming(worksheet="{source_stem} {period_label}"))
    assert named.sheets[0].name == "sales_2025 2025"


def test_the_sheet_index_token_counts_within_its_workbook() -> None:
    named: NamedWorkbook = _named([_year("sales", 2024), _year("returns", 2025)], _naming(worksheet="{source} {sheet_index}"))
    assert [sheet.name for sheet in named.sheets] == ["sales 1", "returns 2"]


def test_the_month_format_reaches_every_rendered_name() -> None:
    named: NamedWorkbook = _named([_month("sales", 2025, 1)], _naming(month_format="abbreviated"))
    assert named.filename == "annual_review_2025-Jan_001.xlsx"
    assert named.sheets[0].name == "sales 2025-Jan"


# --------------------------------------------------------------------------------------
# The prefix
# --------------------------------------------------------------------------------------


def test_a_prefix_is_prepended_with_one_space() -> None:
    named: NamedWorkbook = _named([_month("sales", 2025, 1)], _naming(worksheet_prefix="debits", worksheet="{period_label}"))
    assert named.sheets[0].name == "debits 2025-01"


def test_a_prefix_reaches_undated_and_overflow_names_too() -> None:
    named: NamedWorkbook = _named([_overflow("sales", 2025, 7, 1), _undated("sales")], _naming(worksheet_prefix="debits"))
    assert [sheet.name for sheet in named.sheets] == ["debits sales 2025-07_p01", "debits sales Undated"]


def test_an_empty_prefix_adds_nothing() -> None:
    assert _named([_month("sales", 2025, 1)], _naming(worksheet_prefix="", worksheet="{period_label}")).sheets[0].name == "2025-01"


def test_a_prefix_does_not_reach_the_workbook_filename() -> None:
    assert _named([_year("sales", 2025)], _naming(worksheet_prefix="debits")).filename == "annual_review_2025_001.xlsx"


def test_the_prefix_counts_toward_the_worksheet_length_limit() -> None:
    # Which is why validation happens after expansion rather than on the template.
    # 27 + one space + "2025" is 32 characters, one over Excel's limit.
    long_prefix: str = "a" * 27
    with pytest.raises(PortableNameError, match="characters"):
        _named([_year("sales", 2025)], _naming(worksheet_prefix=long_prefix, worksheet="{period_label}"))


# --------------------------------------------------------------------------------------
# Collisions
# --------------------------------------------------------------------------------------


def test_two_sources_rendering_one_name_collide_under_the_error_policy() -> None:
    naming: NamingSettings = _naming(worksheet="{period_label}", sheet_collision="error")
    with pytest.raises(PortableNameError, match="sheet_collision is 'error'"):
        _named([_year("sales", 2025), _year("returns", 2025)], naming)


def test_the_suffix_policy_resolves_a_collision() -> None:
    naming: NamingSettings = _naming(worksheet="{period_label}", sheet_collision="suffix")
    named: NamedWorkbook = _named([_year("sales", 2025), _year("returns", 2025), _year("sales", 2025)], naming)
    assert [sheet.name for sheet in named.sheets] == ["2025", "2025_2", "2025_3"]


def test_worksheet_uniqueness_is_per_workbook_not_global() -> None:
    # Two workbooks may each hold a sheet called 2025.
    books: list[AllocatedWorkbook] = [AllocatedWorkbook(index=1, sheets=(_year("sales", 2025),)), AllocatedWorkbook(index=2, sheets=(_year("sales", 2025),))]
    named: tuple[NamedWorkbook, ...] = name_workbooks(books, _naming(), profile="annual_review", stems=STEMS)
    assert [book.sheets[0].name for book in named] == ["sales 2025", "sales 2025"]


def test_two_workbooks_rendering_one_filename_is_always_an_error() -> None:
    # Unlike a worksheet collision, this has no policy that resolves it.
    books: list[AllocatedWorkbook] = [AllocatedWorkbook(index=1, sheets=(_year("sales", 2025),)), AllocatedWorkbook(index=2, sheets=(_year("sales", 2025),))]
    with pytest.raises(PortableNameError, match="collides"):
        name_workbooks(books, _naming(workbook="{profile}_{period_label}.xlsx"), profile="annual_review", stems=STEMS)


def test_an_invalid_rendered_workbook_name_is_refused() -> None:
    with pytest.raises(PortableNameError, match="Windows and SMB forbid"):
        _named([_year("sales", 2025)], _naming(workbook="{profile}:{workbook_index}.xlsx"))


def test_an_invalid_rendered_worksheet_name_is_refused() -> None:
    with pytest.raises(PortableNameError, match="Excel forbids"):
        _named([_year("sales", 2025)], _naming(worksheet="{source}/{period_label}"))


def test_naming_no_workbooks_yields_nothing() -> None:
    assert name_workbooks([], _naming(), profile="annual_review", stems=STEMS) == ()


def test_a_plain_template_passes_the_traversal_check() -> None:
    reject_traversal("{profile}_{period_label}_{workbook_index:03d}.xlsx")


def test_zero_padding_is_not_traversal() -> None:
    # A format specification is applied to the value and cannot walk into it, so it stays.
    assert render_template("{workbook_index:05d}", {"workbook_index": 7}) == "00007"


def test_a_workbook_of_day_precision_sheets_summarises_by_day() -> None:
    # A `year-month-day` base, where the coarsest grain present is already the finest one.
    first: PlannedSheet = PlannedSheet(source_alias="sales", kind="period", rows=10, coverage=CoverageSpan.of(PeriodKey("day", 2025, 1, 31)))
    last: PlannedSheet = PlannedSheet(source_alias="returns", kind="period", rows=10, coverage=CoverageSpan.of(PeriodKey("day", 2025, 2, 2)))
    assert workbook_period_label_for([first, last], "numeric") == "2025-01-31~2025-02-02"
