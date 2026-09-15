"""Frozen tests for period labels, reproducing ``partitioning-spec.md``'s tables exactly.

The tables are transcribed here as data rather than paraphrased into assertions, so that a
change to a label is a change to a row a reader can find in the spec.
"""

from __future__ import annotations

import pytest
from pqx_calendar.grids import year_grid_spans
from pqx_calendar.labels import MONTH_ABBREVIATIONS, RANGE_SEPARATOR, UNDATED_LABEL, UNSPLIT_LABEL, CoverageError, CoverageSpan, MonthFormat, period_label, render_month, render_year, workbook_period_label
from pqx_calendar.periods import UNDATED, Bucket, PeriodKey

YEAR_2025: CoverageSpan = CoverageSpan.of(PeriodKey("year", 2025))
YEARS_2022_2024: CoverageSpan = CoverageSpan(PeriodKey("year", 2022), PeriodKey("year", 2024))
MONTH_2025_01: CoverageSpan = CoverageSpan.of(PeriodKey("month", 2025, 1))
MONTHS_WITHIN_A_YEAR: CoverageSpan = CoverageSpan(PeriodKey("month", 2025, 1), PeriodKey("month", 2025, 6))
MONTHS_ACROSS_A_YEAR: CoverageSpan = CoverageSpan(PeriodKey("month", 2025, 11), PeriodKey("month", 2026, 2))
DAY_2025_01_31: CoverageSpan = CoverageSpan.of(PeriodKey("day", 2025, 1, 31))
DAYS_ACROSS_A_MONTH: CoverageSpan = CoverageSpan(PeriodKey("day", 2025, 1, 31), PeriodKey("day", 2025, 2, 2))


@pytest.mark.parametrize(
    ("span", "numeric", "abbreviated"),
    [
        (YEAR_2025, "2025", "2025"),
        (YEARS_2022_2024, "2022~2024", "2022~2024"),
        (MONTH_2025_01, "2025-01", "2025-Jan"),
        (MONTHS_WITHIN_A_YEAR, "2025-01~06", "2025-Jan~Jun"),
        (MONTHS_ACROSS_A_YEAR, "2025-11~2026-02", "2025-Nov~2026-Feb"),
        (DAY_2025_01_31, "2025-01-31", "2025-01-31"),
        (DAYS_ACROSS_A_MONTH, "2025-01-31~2025-02-02", "2025-01-31~2025-02-02"),
    ],
)
def test_the_period_label_table(span: CoverageSpan, numeric: str, abbreviated: str) -> None:
    assert period_label(span, "numeric") == numeric
    assert period_label(span, "abbreviated") == abbreviated


@pytest.mark.parametrize(
    ("months_per_bucket", "numeric", "abbreviated"),
    [
        (6, ["2025-01~06", "2025-07~12"], ["2025-Jan~Jun", "2025-Jul~Dec"]),
        (4, ["2025-01~04", "2025-05~08", "2025-09~12"], ["2025-Jan~Apr", "2025-May~Aug", "2025-Sep~Dec"]),
        (3, ["2025-01~03", "2025-04~06", "2025-07~09", "2025-10~12"], ["2025-Jan~Mar", "2025-Apr~Jun", "2025-Jul~Sep", "2025-Oct~Dec"]),
        (2, ["2025-01~02", "2025-03~04", "2025-05~06", "2025-07~08", "2025-09~10", "2025-11~12"], ["2025-Jan~Feb", "2025-Mar~Apr", "2025-May~Jun", "2025-Jul~Aug", "2025-Sep~Oct", "2025-Nov~Dec"]),
        (
            1,
            ["2025-01", "2025-02", "2025-03", "2025-04", "2025-05", "2025-06", "2025-07", "2025-08", "2025-09", "2025-10", "2025-11", "2025-12"],
            ["2025-Jan", "2025-Feb", "2025-Mar", "2025-Apr", "2025-May", "2025-Jun", "2025-Jul", "2025-Aug", "2025-Sep", "2025-Oct", "2025-Nov", "2025-Dec"],
        ),
    ],
)
def test_the_year_split_label_table(months_per_bucket: int, numeric: list[str], abbreviated: list[str]) -> None:
    # The second table in partitioning-spec.md, driven through the grid function rather than
    # hand-built spans, so the grid and the labels are checked to agree.
    spans: tuple[tuple[PeriodKey, PeriodKey], ...] = year_grid_spans(2025, months_per_bucket)
    assert [period_label(CoverageSpan(first, last), "numeric") for first, last in spans] == numeric
    assert [period_label(CoverageSpan(first, last), "abbreviated") for first, last in spans] == abbreviated


@pytest.mark.parametrize("month_format", ["numeric", "abbreviated"])
def test_the_undated_bucket_labels_itself(month_format: MonthFormat) -> None:
    assert period_label(UNDATED, month_format) == UNDATED_LABEL == "Undated"


@pytest.mark.parametrize("month_format", ["numeric", "abbreviated"])
def test_a_bare_key_labels_as_the_single_period_it_names(month_format: MonthFormat) -> None:
    assert period_label(PeriodKey("month", 2025, 1), month_format) == period_label(MONTH_2025_01, month_format)


def test_the_month_abbreviations_are_the_fixed_english_ones() -> None:
    # Spelled out rather than derived from %b: strftime reads the computer's locale, and
    # Sep against Sept is exactly the kind of difference that changes a filename.
    assert MONTH_ABBREVIATIONS == ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    assert render_month(9, "abbreviated") == "Sep"


@pytest.mark.parametrize("month", range(1, 13))
def test_numeric_months_always_use_two_digits(month: int) -> None:
    assert len(render_month(month, "numeric")) == 2
    assert render_month(month, "numeric") == f"{month:02d}"


def test_the_range_separator_is_a_bare_tilde() -> None:
    # No backslash is stored before it, in JSON or in an output name.
    assert RANGE_SEPARATOR == "~"
    assert "\\" not in period_label(MONTHS_WITHIN_A_YEAR, "numeric")


def test_a_year_below_one_thousand_is_padded_to_four_digits() -> None:
    # Invisible in every row of the spec's tables, which are all four-digit years. It decides
    # whether year 5 sorts before or after 1999 in a directory listing.
    assert render_year(5) == "0005"
    assert period_label(CoverageSpan.of(PeriodKey("year", 5)), "numeric") == "0005"


def test_a_single_period_carries_no_redundant_range_endpoint() -> None:
    assert RANGE_SEPARATOR not in period_label(YEAR_2025, "numeric")
    assert RANGE_SEPARATOR not in period_label(MONTH_2025_01, "numeric")
    assert RANGE_SEPARATOR not in period_label(DAY_2025_01_31, "numeric")


def test_a_day_label_carries_no_month_name_in_either_format() -> None:
    # The table's own answer: 2025-Jan-31 appears nowhere in it.
    assert period_label(DAY_2025_01_31, "abbreviated") == "2025-01-31"


def test_a_month_range_crossing_a_year_names_both_years() -> None:
    # 2025-11~02 would read as a span running backwards inside 2025.
    assert period_label(MONTHS_ACROSS_A_YEAR, "numeric").count("-") == 2


def test_the_unsplit_label_is_the_one_balanced_output_uses() -> None:
    assert UNSPLIT_LABEL == "Data"


# --------------------------------------------------------------------------------------
# Coverage spans
# --------------------------------------------------------------------------------------


def test_a_span_over_several_keys_takes_the_earliest_and_latest() -> None:
    span: CoverageSpan = CoverageSpan.over([PeriodKey("month", 2025, 6), PeriodKey("month", 2025, 1), PeriodKey("month", 2025, 3)])
    assert span == MONTHS_WITHIN_A_YEAR


def test_a_span_over_one_key_is_single() -> None:
    assert CoverageSpan.over([PeriodKey("year", 2025)]).is_single


def test_a_span_reports_the_precision_its_endpoints_share() -> None:
    assert MONTHS_WITHIN_A_YEAR.precision == "month"
    assert YEAR_2025.precision == "year"
    assert DAY_2025_01_31.precision == "day"


def test_a_span_over_nothing_is_refused() -> None:
    with pytest.raises(CoverageError, match="at least one bucket"):
        CoverageSpan.over([])


def test_endpoints_of_different_precision_are_refused() -> None:
    with pytest.raises(CoverageError, match="disagree on precision"):
        CoverageSpan(PeriodKey("year", 2025), PeriodKey("month", 2025, 6))


def test_a_span_running_backwards_is_refused() -> None:
    # Ranges name the earlier endpoint first even when sheets are ordered descending, so a
    # caller under period_order: descending must not hand its endpoints over as it holds them.
    with pytest.raises(CoverageError, match="runs backwards"):
        CoverageSpan(PeriodKey("month", 2025, 6), PeriodKey("month", 2025, 1))


# --------------------------------------------------------------------------------------
# Workbook coverage
# --------------------------------------------------------------------------------------


def test_a_workbook_summarises_earliest_through_latest_coverage() -> None:
    buckets: list[Bucket] = [PeriodKey("month", 2025, 3), PeriodKey("month", 2025, 1), PeriodKey("month", 2025, 6)]
    assert workbook_period_label(buckets, "numeric") == "2025-01~06"


def test_a_workbook_holding_undated_rows_too_gets_the_suffix() -> None:
    buckets: list[Bucket] = [PeriodKey("month", 2025, 1), UNDATED]
    assert workbook_period_label(buckets, "abbreviated") == "2025-Jan_Undated"


def test_an_all_undated_workbook_is_labelled_undated_with_no_prefix() -> None:
    assert workbook_period_label([UNDATED], "numeric") == "Undated"


def test_a_workbook_holding_nothing_is_refused() -> None:
    with pytest.raises(CoverageError, match="at least one bucket"):
        workbook_period_label([], "numeric")


def test_a_workbook_label_reports_coverage_not_a_guarantee_of_every_period() -> None:
    # A merged range describes coverage; missing periods do not create empty sheets, so a
    # workbook holding only January and June is still labelled 2025-01~06.
    buckets: list[Bucket] = [PeriodKey("month", 2025, 1), PeriodKey("month", 2025, 6)]
    assert workbook_period_label(buckets, "numeric") == "2025-01~06"


def test_a_workbook_spanning_years_names_both() -> None:
    buckets: list[Bucket] = [PeriodKey("year", 2022), PeriodKey("year", 2024), UNDATED]
    assert workbook_period_label(buckets, "numeric") == "2022~2024_Undated"
