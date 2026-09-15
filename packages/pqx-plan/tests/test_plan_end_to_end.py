"""One plan, all the way through: configuration to named workbooks and worksheets.

The per-module tests each pin one rule. This checks that the rules compose -- that the ordering
allocation consumes is the one partitioning produced, and that the names rendered at the end
describe the sheets planned at the start.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pqx_calendar.periods import UNDATED, Bucket, PeriodKey
from pqx_plan.allocate import AllocatedWorkbook, allocate_workbooks, order_profile_sheets
from pqx_plan.capacity import SourceShape, fits_one_workbook
from pqx_plan.config import CalendarPartitioning, ExportConfig, ProfileSettings
from pqx_plan.naming import NamedSheet, NamedWorkbook, name_workbooks
from pqx_plan.partition import PlannedSheet, plan_calendar_sheets
from pqx_plan.semantics import validate_config

EXAMPLE: Path = Path(__file__).resolve().parents[3] / "docs" / "export-config.example.json"


def _example() -> ExportConfig:
    """The configuration the specs ship, structurally and semantically valid."""
    document: dict[str, Any] = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    return validate_config(ExportConfig.model_validate(document))


def _plan(config: ExportConfig, shapes: dict[str, SourceShape], counts: dict[str, dict[Bucket, int]]) -> tuple[NamedWorkbook, ...]:
    """Run one profile end to end, from bucket counts to named workbooks."""
    profile: ProfileSettings = config.profiles[0]
    assert isinstance(profile.partitioning, CalendarPartitioning)
    partitioning: CalendarPartitioning = profile.partitioning
    per_source: list[tuple[int, tuple[PlannedSheet, ...]]] = [(position, plan_calendar_sheets(shapes[sheet.source], counts[sheet.source], partitioning, profile.limits)) for position, sheet in enumerate(profile.sheets)]
    ordered: tuple[PlannedSheet, ...] = order_profile_sheets(per_source, partitioning.period_order)
    widths: dict[str, int] = {alias: shape.columns for alias, shape in shapes.items()}
    books: tuple[AllocatedWorkbook, ...] = allocate_workbooks(ordered, widths, profile.limits.max_cells_per_workbook)
    return name_workbooks(books, profile.naming, profile=profile.name, stems={alias: shape.stem for alias, shape in shapes.items()})


def test_the_shipped_example_plans_a_small_export_into_one_workbook() -> None:
    config: ExportConfig = _example()
    shapes: dict[str, SourceShape] = {
        "sales": SourceShape(alias="sales", stem="sales", columns=12, rows=300),
        "returns": SourceShape(alias="returns", stem="returns", columns=4, rows=50),
    }
    counts: dict[str, dict[Bucket, int]] = {
        "sales": {PeriodKey("month", 2024, 6): 100, PeriodKey("month", 2025, 3): 200},
        "returns": {PeriodKey("month", 2024, 6): 20, PeriodKey("month", 2025, 3): 30},
    }
    named: tuple[NamedWorkbook, ...] = _plan(config, shapes, counts)
    assert len(named) == 1
    assert named[0].filename == "annual_review_2024~2025_001.xlsx"
    # Greedy merging puts both years in one sheet per source, and period-then-source ordering
    # puts sales first because it is the first sheet entry.
    assert [sheet.name for sheet in named[0].sheets] == ["sales 2024~2025", "returns 2024~2025"]


def test_every_row_survives_the_plan() -> None:
    # The property the whole pipeline depends on: every source row belongs to exactly one final
    # sheet per profile, and no date policy discards one.
    config: ExportConfig = _example()
    shapes: dict[str, SourceShape] = {
        "sales": SourceShape(alias="sales", stem="sales", columns=12, rows=1_000_000),
        "returns": SourceShape(alias="returns", stem="returns", columns=4, rows=7),
    }
    counts: dict[str, dict[Bucket, int]] = {
        "sales": {PeriodKey("month", 2025, month): 80_000 for month in range(1, 13)} | {UNDATED: 40_000},
        "returns": {PeriodKey("month", 2025, 1): 7},
    }
    named: tuple[NamedWorkbook, ...] = _plan(config, shapes, counts)
    per_alias: dict[str, int] = {"sales": 0, "returns": 0}
    book: NamedWorkbook
    sheet: NamedSheet
    for book in named:
        for sheet in book.sheets:
            per_alias[sheet.sheet.source_alias] += sheet.sheet.rows
    assert per_alias == {"sales": 1_000_000, "returns": 7}


def test_no_workbook_exceeds_the_cell_budget() -> None:
    config: ExportConfig = _example()
    shapes: dict[str, SourceShape] = {
        "sales": SourceShape(alias="sales", stem="sales", columns=12, rows=1_000_000),
        "returns": SourceShape(alias="returns", stem="returns", columns=4, rows=500_000),
    }
    counts: dict[str, dict[Bucket, int]] = {
        "sales": {PeriodKey("month", 2025, month): 83_333 for month in range(1, 13)},
        "returns": {PeriodKey("month", 2024, month): 41_666 for month in range(1, 13)},
    }
    named: tuple[NamedWorkbook, ...] = _plan(config, shapes, counts)
    widths: dict[str, int] = {"sales": 12, "returns": 4}
    assert len(named) > 1
    book: NamedWorkbook
    for book in named:
        cost: int = sum(widths[sheet.sheet.source_alias] * (sheet.sheet.rows + 1) for sheet in book.sheets)
        assert cost <= config.profiles[0].limits.max_cells_per_workbook


def test_every_workbook_filename_and_worksheet_name_is_distinct_where_it_must_be() -> None:
    config: ExportConfig = _example()
    shapes: dict[str, SourceShape] = {
        "sales": SourceShape(alias="sales", stem="sales", columns=12, rows=600_000),
        "returns": SourceShape(alias="returns", stem="returns", columns=4, rows=10),
    }
    counts: dict[str, dict[Bucket, int]] = {
        "sales": {PeriodKey("month", year, month): 25_000 for year in (2024, 2025) for month in range(1, 13)},
        "returns": {PeriodKey("month", 2024, 1): 10},
    }
    named: tuple[NamedWorkbook, ...] = _plan(config, shapes, counts)
    filenames: list[str] = [book.filename for book in named]
    assert len(set(filenames)) == len(filenames)
    book: NamedWorkbook
    for book in named:
        names: list[str] = [sheet.name.casefold() for sheet in book.sheets]
        assert len(set(names)) == len(names)


def test_the_shortcut_and_the_plan_agree_about_small_data() -> None:
    config: ExportConfig = _example()
    shapes: list[SourceShape] = [SourceShape(alias="sales", stem="sales", columns=12, rows=100), SourceShape(alias="returns", stem="returns", columns=4, rows=5)]
    assert fits_one_workbook(shapes, config.profiles[0].limits)
