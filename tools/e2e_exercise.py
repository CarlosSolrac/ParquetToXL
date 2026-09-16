"""Drive the whole pipeline over deliberately awkward data, and leave the results to be inspected.

Backlog item 7 says the pipeline has never been exercised against anything but ``tmp_path``. This
is the local half of closing that: real Parquet files covering the edge cases the specifications
argue about, real configurations over them, and a real run producing sidecars, manifests, receipts,
reports and workbooks into a tree that **survives the run** so a human can open them.

It is not a test. Nothing here asserts that output is correct -- the point is to produce output
whose correctness a person can judge, which is a different job from the 1,725 tests that assert
what the code already believes. Where a case is *expected* to be refused, that expectation is
recorded and the refusal is reported as the outcome, so an unexpected success is visible too.

``docs/e2e-test-plan.md`` is the companion: what each source holds, what each profile should do
with it, and what to look for when opening the workbooks.

Usage::

    uv run python tools/e2e_exercise.py                      # into ./e2e
    uv run python tools/e2e_exercise.py --root ./somewhere    # elsewhere
    uv run python tools/e2e_exercise.py --keep                # keep a previous run's tree
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import shutil
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import polars as pl
from pqx_frame.conversion.to_excel import EXCEL_CELL_LIMIT
from pqx_pipeline.cli import main as pqx_main

RUN_INSTANT: Final[dt.datetime] = dt.datetime(2026, 9, 16, 12, 0, tzinfo=dt.UTC)
"""Pinned, so two runs of this harness produce the same run identifier and the same report names."""

OVERFLOW_ROWS: Final[int] = 2_500
"""Enough rows in one month to exceed the deliberately tiny worksheet limit in the tight profile."""

LONG_STRING: Final[str] = "x" * (EXCEL_CELL_LIMIT + 7)
"""Longer than a cell can hold, so the conversion has to do something and a human can see what."""


@dataclass(frozen=True, slots=True)
class Source:
    """One Parquet file, and why it exists.

    Attributes:
        name: The file stem and the configuration alias.
        frame: What to write.
        date_column: The column a profile partitions on.
        date_kind: The ``date_columns`` entry for it.
        covers: The edge cases this source is here to exercise.
    """

    name: str
    frame: pl.DataFrame
    date_column: str
    date_kind: dict[str, Any]
    covers: str


def _dates(start: dt.date, count: int, *, step: int = 1) -> list[dt.date]:
    """A run of dates, ``step`` days apart."""
    return [start + dt.timedelta(days=index * step) for index in range(count)]


def build_sources() -> list[Source]:
    """Every Parquet file the exercise writes, each aimed at something the specs argue about."""
    sources: list[Source] = []

    sources.append(
        Source(
            name="tiny",
            frame=pl.DataFrame({"id": [1, 2, 3], "customer": ["amy", "zed", "mel"], "booked": _dates(dt.date(2025, 3, 1), 3)}),
            date_column="booked",
            date_kind={"type": "date"},
            covers="the simplest possible export: three rows, one month, one sheet",
        ),
    )

    sources.append(
        Source(
            name="empty",
            frame=pl.DataFrame({"id": pl.Series([], dtype=pl.Int64), "customer": pl.Series([], dtype=pl.String), "booked": pl.Series([], dtype=pl.Date)}),
            date_column="booked",
            date_kind={"type": "date"},
            covers="zero rows: is_empty on the shape, and what a plan does with nothing to place",
        ),
    )

    sources.append(
        Source(
            name="single_row",
            frame=pl.DataFrame({"id": [1], "customer": ["amy"], "booked": [dt.date(2025, 7, 4)]}),
            date_column="booked",
            date_kind={"type": "date"},
            covers="one row: a header plus a single data row, the smallest non-empty sheet",
        ),
    )

    sources.append(
        Source(
            name="multi_year",
            frame=pl.DataFrame(
                {
                    "id": list(range(600)),
                    "customer": [f"c{index % 11}" for index in range(600)],
                    "amount": [round(index * 1.37, 2) for index in range(600)],
                    "booked": _dates(dt.date(2022, 1, 3), 600, step=3),
                },
            ),
            date_column="booked",
            date_kind={"type": "date"},
            covers="about five years at three-day spacing: one sheet per year, and period ordering",
        ),
    )

    sources.append(
        Source(
            name="undated",
            frame=pl.DataFrame(
                {
                    "id": list(range(30)),
                    "customer": [f"c{index % 4}" for index in range(30)],
                    "booked": [None if index % 3 == 0 else dt.date(2025, 1, 1) + dt.timedelta(days=index * 9) for index in range(30)],
                },
            ),
            date_column="booked",
            date_kind={"type": "date"},
            covers="a third of the rows undated: the UNDATED bucket, which must come last under BOTH period orders",
        ),
    )

    sources.append(
        Source(
            name="all_undated",
            frame=pl.DataFrame({"id": list(range(12)), "customer": [f"c{index}" for index in range(12)], "booked": pl.Series([None] * 12, dtype=pl.Date)}),
            date_column="booked",
            date_kind={"type": "date"},
            covers="every row undated: an export with no dated bucket at all",
        ),
    )

    # 69 -> 2069 and 70 -> 1970 under a 1970 window; 99 -> 1999, not 2099; 101 is January 2001,
    # not a three-digit value. These four are the whole reason the window is configuration.
    sources.append(
        Source(
            name="int_yymm",
            frame=pl.DataFrame({"id": [1, 2, 3, 4, 5, 6], "note": ["69 -> 2069", "70 -> 1970", "99 -> 1999", "00 -> 2000", "101 -> 2001-01", "2512 -> 2025-12"], "period": [6901, 7001, 9901, 1, 101, 2512]}),
            date_column="period",
            date_kind={"type": "int", "format": "YYMM", "two_digit_year_window_start": 1970},
            covers="the two-digit-year window at its boundaries, and leading zeros that are not truncation",
        ),
    )

    sources.append(
        Source(
            name="int_yyyymmdd",
            frame=pl.DataFrame({"id": [1, 2, 3, 4], "note": ["leap day", "new year", "year end", "ordinary"], "period": [20240229, 20250101, 20251231, 20250615]}),
            date_column="period",
            date_kind={"type": "int", "format": "YYYYMMDD"},
            covers="a leap day, and the two days a year boundary is made of",
        ),
    )

    # 2026-01-01T02:00Z is still 2025 in Mexico City. decode_datetime's own docstring names this
    # case; here it decides which workbook a row lands in.
    zoned: list[dt.datetime] = [
        dt.datetime.fromisoformat("2026-01-01T02:00:00+00:00"),
        dt.datetime.fromisoformat("2026-01-01T23:00:00+00:00"),
        dt.datetime.fromisoformat("2025-06-15T12:00:00+00:00"),
    ]
    sources.append(
        Source(
            name="zoned",
            frame=pl.DataFrame({"id": [1, 2, 3], "note": ["02:00Z -> 2025 locally", "23:00Z -> 2026 locally", "midyear"], "moment": pl.Series(zoned, dtype=pl.Datetime("us", time_zone="UTC"))}),
            date_column="moment",
            date_kind={"type": "datetime", "calendar_timezone": {"mode": "zone", "zone": "America/Mexico_City"}},
            covers="a named zone moving a row into the previous YEAR: rows 1 and 2 are the same UTC day and belong in different workbooks",
        ),
    )

    naive: list[dt.datetime] = [dt.datetime.fromisoformat(f"2025-0{index + 1}-15T08:30:00") for index in range(6)]
    sources.append(
        Source(
            name="wall_clock",
            frame=pl.DataFrame({"id": list(range(6)), "moment": pl.Series(naive, dtype=pl.Datetime("us"))}),
            date_column="moment",
            date_kind={"type": "datetime", "calendar_timezone": {"mode": "source_wall_clock"}},
            covers="naive timestamps read as a wall clock, with no zone consulted",
        ),
    )

    sources.append(
        Source(
            name="overflow",
            frame=pl.DataFrame(
                {
                    "id": list(range(OVERFLOW_ROWS)),
                    "customer": [f"c{index % 97}" for index in range(OVERFLOW_ROWS)],
                    "amount": [round(index * 0.5, 2) for index in range(OVERFLOW_ROWS)],
                    "booked": [dt.date(2025, 5, 1)] * OVERFLOW_ROWS,
                },
            ),
            date_column="booked",
            date_kind={"type": "date"},
            covers="one period with more rows than the tight profile allows per worksheet: overflow fragments and part_index",
        ),
    )

    sources.append(
        Source(
            name="dtypes",
            frame=_dtype_frame(),
            date_column="booked",
            date_kind={"type": "date"},
            covers="every scalar dtype at its boundary values: what the ToExcel conversion does to each, which is the thing worth eyeballing",
        ),
    )

    sources.append(
        Source(
            name="odd_names",
            frame=pl.DataFrame(
                {
                    "id": [1, 2, 3],
                    "*": ["a wildcard", "as a column", "name"],
                    "^date$": ["a regex", "as a column", "name"],
                    "ünïcødé": ["non", "ascii", "header"],
                    "booked": _dates(dt.date(2025, 2, 1), 3),
                },
            ),
            date_column="booked",
            date_kind={"type": "date"},
            covers="column names Polars would read as a wildcard and a regex, plus a non-ASCII header; all legal in Parquet",
        ),
    )

    return sources


def _dtype_frame() -> pl.DataFrame:
    """One column per scalar dtype, at the values that move under conversion."""
    return pl.DataFrame(
        {
            "int8": pl.Series([-128, 127, 0, None], dtype=pl.Int8),
            "int64_extremes": pl.Series([-9223372036854775808, 9223372036854775807, 9223372036854775806, None], dtype=pl.Int64),
            "uint64_max": pl.Series([0, 1, 18446744073709551615, None], dtype=pl.UInt64),
            "float_specials": pl.Series([float("inf"), float("-inf"), float("nan"), -0.0], dtype=pl.Float64),
            "boolean": pl.Series([True, False, True, None], dtype=pl.Boolean),
            "string_edges": pl.Series([LONG_STRING, "", "ordinary", None], dtype=pl.String),
            "binary": pl.Series([b"\xff\xfe", b"", b"\x00\x01", None], dtype=pl.Binary),
            "time": pl.Series([dt.time(0, 0), dt.time(23, 59, 59, 999999), dt.time(12, 0), None], dtype=pl.Time),
            "duration": pl.Series([dt.timedelta(microseconds=1), dt.timedelta(days=-100000), dt.timedelta(0), None], dtype=pl.Duration("us")),
            "decimal": pl.Series([Decimal("1.2500"), Decimal("-9.9900"), Decimal("0.0000"), None], dtype=pl.Decimal(18, 4)),
            "categorical": pl.Series(["alpha", "", "beta", None], dtype=pl.Categorical),
            "all_null": pl.Series([None, None, None, None], dtype=pl.Null),
            "booked": pl.Series([dt.date(1899, 12, 31), dt.date(9999, 12, 31), dt.date(1970, 1, 1), dt.date(2025, 8, 8)], dtype=pl.Date),
        },
    )


@dataclass(frozen=True, slots=True)
class Profile:
    """One workbook definition, and what it is meant to demonstrate.

    Attributes:
        name: The profile name and its output subdirectory.
        aliases: The sources it draws on, in sheet order.
        settings: Everything but ``name``, ``output_subdirectory`` and ``sheets``.
        expect: ``"export"`` when it should produce workbooks, ``"refusal"`` when it should not.
        demonstrates: What to look at in the result.
    """

    name: str
    aliases: tuple[str, ...]
    settings: dict[str, Any]
    expect: str
    demonstrates: str


def _limits(rows: int = 1_048_575, cells: int = 10_000_000) -> dict[str, int]:
    """Profile ceilings, defaulting to the shipped example's."""
    return {"max_data_rows_per_worksheet": rows, "max_cells_per_workbook": cells}


def _calendar(*, algorithm: str = "calendar_greedy", base_period: str = "year", period_order: str = "ascending", null_dates: str = "separate") -> dict[str, Any]:
    """A calendar partitioning, defaulting to greedy years with undated rows kept."""
    return {
        "algorithm": algorithm,
        "base_period": base_period,
        "period_order": period_order,
        "year_split_months": [6, 4, 3, 2, 1],
        "oversized_period": "balanced_rows",
        "null_dates": null_dates,
    }


def _naming() -> dict[str, Any]:
    """The shipped example's naming templates."""
    return {
        "workbook": "{profile}_{period_label}_{workbook_index:03d}.xlsx",
        "worksheet": "{source} {period_label}",
        "single_worksheet": "{source} Data",
        "overflow_worksheet": "{source} {period_label}_p{part_index:02d}",
        "month_format": "numeric",
        "worksheet_prefix": "",
        "sheet_collision": "error",
    }


def build_profiles() -> list[Profile]:
    """Every workbook definition the exercise runs."""
    return [
        Profile(
            name="by_year",
            aliases=("tiny", "single_row", "multi_year"),
            settings={"limits": _limits(cells=800), "partitioning": _calendar(), "naming": _naming()},
            expect="export",
            demonstrates="one sheet per year, ascending, merged greedily into workbooks. The cell limit is deliberately small: see PARTITIONING ONLY ENGAGES WHEN THE EXPORT DOES NOT FIT ONE WORKBOOK in the plan",
        ),
        Profile(
            name="by_month_descending",
            aliases=("multi_year",),
            settings={"limits": _limits(cells=400), "partitioning": _calendar(algorithm="calendar_periods", base_period="year-month", period_order="descending"), "naming": _naming()},
            expect="export",
            demonstrates="one sheet per month, newest first, each period kept separate. Check the workbook names run 2026-12 downwards",
        ),
        Profile(
            name="fan_in",
            aliases=("tiny", "odd_names"),
            settings={"limits": _limits(), "partitioning": _calendar(base_period="year-month"), "naming": _naming()},
            expect="export",
            demonstrates="two sources into one workbook, small enough to take the single-workbook shortcut -- so the sheets are named by single_worksheet and carry no period label. Confirm the odd column names survived as headers",
        ),
        Profile(
            name="undated_last",
            aliases=("undated", "all_undated"),
            settings={"limits": _limits(cells=60), "partitioning": _calendar(period_order="descending"), "naming": _naming()},
            expect="export",
            demonstrates="undated rows under a DESCENDING period order. The undated sheet must still be LAST, not first -- that is the whole check",
        ),
        Profile(
            name="tight_limits",
            aliases=("overflow",),
            settings={"limits": _limits(rows=400, cells=4_000), "partitioning": _calendar(base_period="year-month"), "naming": _naming()},
            expect="export",
            demonstrates="2,500 rows in one month against a 400-row worksheet and a 4,000-cell workbook: overflow fragments and several workbooks",
        ),
        Profile(
            name="dtypes_showcase",
            aliases=("dtypes",),
            settings={"limits": _limits(), "partitioning": _calendar(base_period="year-month"), "naming": _naming()},
            expect="export",
            demonstrates="THE ONE TO OPEN FIRST. Every dtype at its boundary, in one sheet via the single-workbook shortcut. Check int64 extremes, the over-long string, inf/nan, and how booleans render",
        ),
        Profile(
            name="int_and_zoned_dates",
            aliases=("int_yymm", "int_yyyymmdd", "zoned", "wall_clock"),
            settings={"limits": _limits(cells=40), "partitioning": _calendar(), "naming": _naming()},
            expect="export",
            demonstrates="every date encoding at once. The zoned source's first two rows are the same UTC day and must land in DIFFERENT year sheets (2025 and 2026); int_yymm must spread across 1970, 1999, 2000, 2001, 2025 and 2069",
        ),
        Profile(
            name="empty_source",
            aliases=("empty",),
            settings={"limits": _limits(), "partitioning": _calendar(), "naming": _naming()},
            expect="either",
            demonstrates="a source with no rows. Whatever happens is the finding: an empty workbook, no workbook, or a refusal",
        ),
        Profile(
            name="null_dates_refused",
            aliases=("undated",),
            settings={"limits": _limits(), "partitioning": _calendar(null_dates="error"), "naming": _naming()},
            expect="refusal",
            demonstrates="null_dates 'error' over a source that has nulls. This SHOULD refuse, naming the first offending row",
        ),
    ]


def write_sources(sources: list[Source], into: Path) -> None:
    """Write every Parquet file.

    Args:
        sources: What to write.
        into: The directory to write into.
    """
    into.mkdir(parents=True, exist_ok=True)
    source: Source
    for source in sources:
        target: Path = into / f"{source.name}.parquet"
        source.frame.write_parquet(target)
        print(f"  {source.name:<16} {source.frame.height:>6} rows x {source.frame.width:>2} cols  {target.name}")


def build_config(sources: list[Source], profile: Profile, root: Path) -> dict[str, Any]:
    """Build one configuration document for one profile.

    One configuration per profile rather than one holding all of them, so a refusal in one cannot
    hide the others and each profile's bookkeeping lands in its own directory.

    Args:
        sources: Every available source.
        profile: The profile to build for.
        root: The exercise root.

    Returns:
        The configuration document.
    """
    wanted: dict[str, Source] = {source.name: source for source in sources if source.name in profile.aliases}
    return {
        "config_version": 1,
        "config_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"e2e/{profile.name}")),
        "config_modified_utc": "2026-09-16T11:00:00Z",
        "output_directory": str(root / "destination"),
        "sidecar_location": {"kind": "directory", "path": str(root / "sidecars" / profile.name)},
        "scratch_root": str(root / "scratch"),
        "excel": {"writer": "rustpy-xlsxwriter", "options": {}},
        "sources": {alias: {"path": str(root / "sources" / f"{alias}.parquet"), "date_columns": {wanted[alias].date_column: wanted[alias].date_kind}} for alias in profile.aliases},
        "profiles": [
            {
                "name": profile.name,
                "output_subdirectory": profile.name,
                **profile.settings,
                "sheets": [{"source": alias, "partition_column": wanted[alias].date_column, "sort": []} for alias in profile.aliases],
            },
        ],
    }


@dataclass(frozen=True, slots=True)
class Outcome:
    """What one profile's run did.

    Attributes:
        profile: Which profile.
        exit_code: What the CLI returned.
        expected: What the plan said should happen.
        surprising: Whether the outcome contradicts the expectation.
        workbooks: The workbook filenames produced.
        output: What the run printed.
        escaped: The exception that escaped the CLI, if one did. An empty string when none did.
    """

    profile: str
    exit_code: int
    expected: str
    surprising: bool
    workbooks: tuple[str, ...]
    output: str
    escaped: str


def run_profile(profile: Profile, config_path: Path, root: Path) -> Outcome:
    """Run one profile end to end and record what happened.

    Args:
        profile: The profile being run.
        config_path: Its configuration file.
        root: The exercise root.

    Returns:
        The outcome.
    """
    # The pipeline does not create a `sidecar_location: directory` target -- the sidecar store
    # writes straight into it and raises FileNotFoundError if it is not there. An operator would
    # create it; so does this. Recorded in docs/e2e-test-plan.md as a finding rather than papered
    # over, because nothing in the configuration schema says the directory must pre-exist.
    (root / "sidecars" / profile.name).mkdir(parents=True, exist_ok=True)
    stream: io.StringIO = io.StringIO()
    escaped: str = ""
    code: int = 0
    try:
        code = pqx_main(["--config", str(config_path), "run"], stream=stream, now=RUN_INSTANT)
    except Exception as exc:
        # FINDING: the CLI maps OwnershipError, StagingError, NotImplementedError and ValueError to
        # exit 3, but BucketingError and DateDecodeError subclass plain Exception and are not
        # caught, so a data-level refusal leaves `pqx` with a traceback instead of an exit code --
        # and throws away a message built specifically to name the column and the row. Caught here
        # so one profile's refusal does not end the exercise; recorded, not worked around.
        escaped = f"{type(exc).__name__}: {exc}"
        code = -1

    destination: Path = root / "destination" / profile.name
    workbooks: tuple[str, ...] = tuple(sorted(name.name for name in destination.glob("*.xlsx"))) if destination.exists() else ()

    exported: bool = bool(workbooks)
    surprising: bool = (profile.expect == "export" and not exported) or (profile.expect == "refusal" and exported)
    return Outcome(profile=profile.name, exit_code=code, expected=profile.expect, surprising=surprising, workbooks=workbooks, output=stream.getvalue(), escaped=escaped)


def main() -> int:
    """Write the sources, run every profile, and say what to look at.

    Returns:
        ``0`` always. Nothing here is a pass or a fail; a surprising outcome is reported, not
        exited on, because judging correctness is what the human inspection is for.
    """
    parser: argparse.ArgumentParser = argparse.ArgumentParser(prog="e2e_exercise", description="Drive the pipeline over awkward data and leave the results to be inspected.")
    parser.add_argument("--root", default="./e2e", help="where to build the exercise tree")
    parser.add_argument("--keep", action="store_true", help="add to an existing tree instead of replacing it")
    arguments: argparse.Namespace = parser.parse_args()

    root: Path = Path(str(arguments.root)).resolve()
    if root.exists() and not arguments.keep:
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)

    sources: list[Source] = build_sources()
    profiles: list[Profile] = build_profiles()

    print(f"exercise root: {root}\n\nsources:")
    write_sources(sources, root / "sources")

    print("\nprofiles:")
    outcomes: list[Outcome] = []
    profile: Profile
    for profile in profiles:
        config_path: Path = root / "config" / f"{profile.name}.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(build_config(sources, profile, root), indent=2), encoding="utf-8")
        outcome: Outcome = run_profile(profile, config_path, root)
        outcomes.append(outcome)
        marker: str = "  <-- UNEXPECTED" if outcome.surprising else ""
        escaped_note: str = f"  escaped: {outcome.escaped}" if outcome.escaped else ""
        print(f"  {profile.name:<22} exit {outcome.exit_code:>2}  {len(outcome.workbooks):>2} workbooks  (expected {profile.expect}){marker}{escaped_note}")

    summary: Path = root / "RESULTS.json"
    summary.write_text(
        json.dumps(
            {
                "run_instant": RUN_INSTANT.isoformat(),
                "sources": [{"name": item.name, "rows": item.frame.height, "columns": item.frame.width, "covers": item.covers} for item in sources],
                "profiles": [
                    {"name": item.profile, "exit_code": item.exit_code, "expected": item.expected, "surprising": item.surprising, "workbooks": list(item.workbooks), "escaped": item.escaped, "output": item.output}
                    for item in outcomes
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    result: Outcome
    surprises: list[Outcome] = [result for result in outcomes if result.surprising]
    print(f"\nwrote {summary}")
    if surprises:
        print(f"\n{len(surprises)} profile(s) did not do what the plan expected -- these are the interesting ones:")
        for result in surprises:
            print(f"  {result.profile}: expected {result.expected}, exit {result.exit_code}, {len(result.workbooks)} workbooks")
    print("\nOpen these first:")
    print(f"  {root / 'destination' / 'dtypes_showcase'}     every dtype at its boundary")
    print(f"  {root / 'destination' / 'int_and_zoned_dates'} the zoned rows that cross a year")
    print(f"  {root / 'destination' / 'tight_limits'}        overflow fragments across workbooks")
    print(f"\nSidecars are under {root / 'sidecars'}, manifests and receipts beside each profile's workbooks.")
    print("docs/e2e-test-plan.md says what each one is for and what to look for.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
