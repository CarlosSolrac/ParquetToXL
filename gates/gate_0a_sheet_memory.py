"""Gate 0a: does ``rustpy_xlsxwriter.FastExcel.sheet()`` stream, or does it buffer?

``RustpyExcelWriter`` was built around a constant-memory mode: rows are streamed with
``iter_rows`` rather than materialised, ``autofit`` is off because it measures every cell, and
``dedupe_strings`` is off because the shared-string table it builds buffers the whole sheet.
``export-pipeline-spec.md`` then plans a multi-sheet writer, which means leaving
``write_worksheet`` for ``FastExcel``. This measures whether that mode survives the move.

The question is empirical because ``FastExcel.sheet()`` does not write anything: it appends
``(name, data)`` to a list, and ``save()`` hands the whole list to Rust at once. Whether the
Rust side then consumes one sheet's iterator at a time or drains them all first is not visible
from the Python source, and it is the difference between a bounded writer and one whose
footprint is the whole export.

**Peak RSS, measured in a child process per case.** ``ru_maxrss`` is a high-water mark that
never falls, so several cases in one process would each report the largest so far. Every case
therefore runs as ``python -m gates.gate_0a_sheet_memory --case <json>``, which reports its own
peak on stdout; the parent subtracts a baseline case that imports everything and writes
nothing. Rows are generated, never held: the generator below builds each dict on demand, so any
growth the parent sees belongs to the writer rather than to the data.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import rustpy_xlsxwriter

KIBIBYTES_PER_MEBIBYTE: Final[int] = 1024
"""Linux reports ``ru_maxrss`` in kibibytes; every number below is rendered in mebibytes."""

COLUMN_COUNT: Final[int] = 8
"""Columns per generated row: two strings, three integers, two floats and a date."""

REPEATED_LABELS: Final[tuple[str, ...]] = ("alpha", "beta", "gamma", "delta", "epsilon")
"""A low-cardinality text column, so ``dedupe_strings`` has something to deduplicate."""

WIDE_COLUMN_COUNT: Final[int] = 24
WIDE_TEXT_LENGTH: Final[int] = 120
"""The wide row's shape: enough columns and enough characters for autofit to have work to do."""

EPOCH: Final[dt.date] = dt.date(2020, 1, 1)


@dataclass(frozen=True)
class Case:
    """One measurement: a shape to write and the writer options to write it with."""

    label: str
    sheets: int
    rows: int
    autofit: bool
    dedupe_strings: bool
    materialize: bool
    """Pass each sheet a ``list`` rather than a generator, to price streaming against not."""

    dedupe_first_sheet_only: bool = False
    """Set ``dedupe_strings`` on sheet zero alone, to test whether the option is really per sheet."""

    wide: bool = False
    """Write ``WIDE_COLUMN_COUNT`` long-text columns instead of the narrow mixed-type row.

    Autofit measures every cell to size a column, so its cost is in column count and text
    length, not row count. Judging it on eight short columns would flatter it.
    """

    def as_json(self) -> str:
        """Return the case as a single JSON argument for the child process."""
        return json.dumps(
            {
                "label": self.label,
                "sheets": self.sheets,
                "rows": self.rows,
                "autofit": self.autofit,
                "dedupe_strings": self.dedupe_strings,
                "materialize": self.materialize,
                "dedupe_first_sheet_only": self.dedupe_first_sheet_only,
                "wide": self.wide,
            },
        )

    @staticmethod
    def from_json(text: str) -> Case:
        """Rebuild a case from the JSON the parent passed.

        Args:
            text: The ``as_json`` output.

        Returns:
            The case.
        """
        raw: dict[str, Any] = json.loads(text)
        return Case(
            label=str(raw["label"]),
            sheets=int(raw["sheets"]),
            rows=int(raw["rows"]),
            autofit=bool(raw["autofit"]),
            dedupe_strings=bool(raw["dedupe_strings"]),
            materialize=bool(raw["materialize"]),
            dedupe_first_sheet_only=bool(raw["dedupe_first_sheet_only"]),
            wide=bool(raw["wide"]),
        )


@dataclass(frozen=True)
class Measurement:
    """What one child process reported back, or why it reported nothing."""

    label: str
    sheets: int
    rows: int
    total_rows: int
    peak_mib: float = 0.0
    seconds: float = 0.0
    output_mib: float = 0.0
    failure: str | None = None
    """The child's exit status and stderr when it did not return a result. A writer that refuses
    a shape, or is killed writing it, is a result of its own and is printed as one rather than
    raised -- raising would discard every row already measured."""


def generate_rows(count: int, sheet: int) -> Iterator[dict[str, Any]]:
    """Yield ``count`` rows, building each one on demand.

    Deliberately not a list and not a frame. The point of the measurement is the writer's
    footprint, so the data must not have one.

    Args:
        count: How many rows to yield.
        sheet: Sheet ordinal, mixed into the values so no two sheets are identical.

    Returns:
        An iterator of row dicts with ``COLUMN_COUNT`` keys.
    """
    index: int
    for index in range(count):
        yield {
            "id": index,
            "sheet": sheet,
            "label": REPEATED_LABELS[index % len(REPEATED_LABELS)],
            "code": f"{sheet:04d}-{index:09d}",
            "quantity": index % 997,
            "amount": index * 1.5,
            "ratio": (index % 101) / 7.0,
            "as_of": EPOCH + dt.timedelta(days=index % 3650),
        }


def generate_wide_rows(count: int, sheet: int) -> Iterator[dict[str, Any]]:
    """Yield ``count`` rows of long text, built on demand.

    Args:
        count: How many rows to yield.
        sheet: Sheet ordinal, mixed into the values so no two sheets are identical.

    Returns:
        An iterator of row dicts with ``WIDE_COLUMN_COUNT`` text keys.
    """
    index: int
    for index in range(count):
        yield {f"text{column:02d}": f"{sheet:04d}-{index:09d}-{column:02d}".ljust(WIDE_TEXT_LENGTH, "x") for column in range(WIDE_COLUMN_COUNT)}


def run_case(case: Case, destination: Path) -> tuple[float, float]:
    """Write one case's workbook and return its peak RSS and elapsed time.

    Args:
        case: The shape and options to write.
        destination: Where the workbook goes. Removed by the caller.

    Returns:
        Peak RSS in mebibytes, and elapsed seconds.
    """
    started: float = time.monotonic()
    if case.sheets:
        writer: rustpy_xlsxwriter.FastExcel = rustpy_xlsxwriter.FastExcel(str(destination), autofit=case.autofit)
        sheet: int
        for sheet in range(case.sheets):
            rows: Iterator[dict[str, Any]] | list[dict[str, Any]] = generate_wide_rows(case.rows, sheet) if case.wide else generate_rows(case.rows, sheet)
            if case.materialize:
                rows = list(rows)
            dedupe: bool = case.dedupe_strings or (case.dedupe_first_sheet_only and sheet == 0)
            writer.sheet(f"sheet{sheet:03d}", rows, dedupe_strings=dedupe)
        writer.save()
    return _peak_mib(), time.monotonic() - started


if sys.platform == "win32":

    def _peak_mib() -> float:
        """Refuse, because Windows has no ``resource`` module to read peak RSS from.

        These harnesses measure the Linux image the pipeline runs on. A Windows figure would not
        be comparable to the numbers already recorded in the decision records, so this refuses
        rather than quietly substituting a different measurement.

        Raises:
            RuntimeError: Always.
        """
        message: str = "peak RSS is read through resource.getrusage; run this harness on the Linux image it measures"
        raise RuntimeError(message)

else:
    import resource

    def _peak_mib() -> float:
        """Return this process's peak resident set size in mebibytes."""
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / KIBIBYTES_PER_MEBIBYTE


def _child(argument: str) -> int:
    """Run one case and print its result as JSON.

    Args:
        argument: The case, as ``Case.as_json`` produced it.

    Returns:
        A process exit status.
    """
    case: Case = Case.from_json(argument)
    directory: str
    with tempfile.TemporaryDirectory(prefix="gate0a-") as directory:
        destination: Path = Path(directory) / "out.xlsx"
        peak: float
        seconds: float
        peak, seconds = run_case(case, destination)
        size: float = destination.stat().st_size / KIBIBYTES_PER_MEBIBYTE / KIBIBYTES_PER_MEBIBYTE if destination.exists() else 0.0
    print(json.dumps({"peak_mib": peak, "seconds": seconds, "output_mib": size}))
    return 0


def measure(case: Case) -> Measurement:
    """Run one case in a fresh child process and collect what it reported.

    Args:
        case: The case to run.

    Returns:
        The measurement, or one carrying ``failure`` when the child did not return a result.
    """
    completed: subprocess.CompletedProcess[str] = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "gates.gate_0a_sheet_memory", "--case", case.as_json()],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return Measurement(label=case.label, sheets=case.sheets, rows=case.rows, total_rows=case.sheets * case.rows, failure=f"status {completed.returncode}: {completed.stderr.strip()[-200:]}")
    reported: dict[str, Any] = json.loads(completed.stdout)
    return Measurement(
        label=case.label,
        sheets=case.sheets,
        rows=case.rows,
        total_rows=case.sheets * case.rows,
        peak_mib=float(reported["peak_mib"]),
        seconds=float(reported["seconds"]),
        output_mib=float(reported["output_mib"]),
    )


def cases() -> list[Case]:
    """Return every case, in the order the report reads best.

    The first is the baseline: it imports the writer and writes nothing, so subtracting it
    leaves the writer's own footprint rather than the interpreter's.

    Returns:
        The cases.
    """
    drawn: list[Case] = [Case(label="baseline", sheets=0, rows=0, autofit=False, dedupe_strings=False, materialize=False)]
    rows: int
    sheets: int
    # Constant total rows, varying sheet count: separates "per sheet" from "per row".
    for sheets in (1, 2, 4, 8, 16, 32):
        drawn.append(Case(label=f"{sheets}x{800_000 // sheets}", sheets=sheets, rows=800_000 // sheets, autofit=False, dedupe_strings=False, materialize=False))
    # Constant sheet count, varying rows per sheet: the streaming question on one axis.
    for rows in (50_000, 100_000, 200_000, 400_000, 800_000):
        drawn.append(Case(label=f"8x{rows}", sheets=8, rows=rows, autofit=False, dedupe_strings=False, materialize=False))
    # Many small sheets, which is what a calendar partition of a modest source produces. If
    # per-sheet state were the cost rather than per-row, this is where it would show.
    for sheets in (64, 128, 256):
        drawn.append(Case(label=f"{sheets}x4000", sheets=sheets, rows=4_000, autofit=False, dedupe_strings=False, materialize=False))
    # The three options that could take the writer out of constant-memory mode. The
    # single-sheet autofit case goes through ``write_worksheet``, which is the function
    # ``RustpyExcelWriter`` already calls and whose autofit its docstring warns about; the
    # eight-sheet one goes through ``write_worksheets``, which is where the new writer lands.
    drawn.append(Case(label="1x800000 autofit", sheets=1, rows=800_000, autofit=True, dedupe_strings=False, materialize=False))
    drawn.append(Case(label="8x100000 autofit", sheets=8, rows=100_000, autofit=True, dedupe_strings=False, materialize=False))
    drawn.append(Case(label="8x100000 dedupe", sheets=8, rows=100_000, autofit=False, dedupe_strings=True, materialize=False))
    drawn.append(Case(label="8x100000 list", sheets=8, rows=100_000, autofit=False, dedupe_strings=False, materialize=True))
    # dedupe_strings is documented as per sheet. If that is true, one sheet of eight opting in
    # costs about an eighth of what all eight cost, and the option is survivable; if the flag
    # is really per workbook, it costs the same as all eight and must simply be refused.
    drawn.append(Case(label="8x100000 dedupe1", sheets=8, rows=100_000, autofit=False, dedupe_strings=False, materialize=False, dedupe_first_sheet_only=True))
    # Autofit priced on data it has to work for: 24 columns of 120-character text.
    drawn.append(Case(label="wide 4x50000", sheets=4, rows=50_000, autofit=False, dedupe_strings=False, materialize=False, wide=True))
    drawn.append(Case(label="wide 4x50000 autofit", sheets=4, rows=50_000, autofit=True, dedupe_strings=False, materialize=False, wide=True))
    return drawn


def main() -> int:
    """Run every case and print the table.

    Returns:
        A process exit status.
    """
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", help="internal: run one case as a child process and print its JSON result")
    parsed: argparse.Namespace = parser.parse_args()
    if parsed.case is not None:
        return _child(str(parsed.case))
    # Printed one row at a time rather than collected first, so a child that dies on the last
    # case leaves the seventeen rows before it on the screen rather than nowhere.
    baseline: Measurement = measure(cases()[0])
    if baseline.failure is not None:
        message: str = f"the baseline case failed, so nothing else can be interpreted: {baseline.failure}"
        raise RuntimeError(message)
    print(f"{'case':>20}  {'sheets':>6}  {'rows/sheet':>10}  {'total rows':>10}  {'peak MiB':>8}  {'over baseline':>13}  {'seconds':>7}  {'xlsx MiB':>8}")
    case: Case
    for case in cases():
        result: Measurement = baseline if case.label == baseline.label else measure(case)
        if result.failure is not None:
            print(f"{result.label:>20}  {result.sheets:>6}  {result.rows:>10}  {result.total_rows:>10}  FAILED: {result.failure}")
            continue
        print(
            f"{result.label:>20}  {result.sheets:>6}  {result.rows:>10}  {result.total_rows:>10}  {result.peak_mib:>8.1f}  {result.peak_mib - baseline.peak_mib:>13.1f}  {result.seconds:>7.1f}  {result.output_mib:>8.1f}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
