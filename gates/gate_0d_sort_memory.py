"""Gate 0d: how much memory does ``pl.read_parquet`` plus a global sort need, and where does 16 GB run out?

``partitioning-spec.md`` says balanced exports "sort the whole dataset before contiguous
slicing", and ``library-spec.md`` puts lazy and streaming frames out of scope. So the balanced
algorithms are an eager read of a whole source followed by an eager sort of it, on the 16 GB
machines the capacity section targets. Whether that is a ceiling worth designing around or one
reached on ordinary data is the question, and ``export-pipeline-spec.md`` treats the answer as
settled without having it.

**The escalation is the measurement.** For each width the row count doubles until the child
process dies or a cap is reached, so the number reported is the last shape that actually
completed rather than a projection from small ones. A child killed by the OOM killer is a
result, recorded as one.

**Two readings per shape, in two separate child processes.** ``ru_maxrss`` is a high-water mark
over a whole process, so the reading after ``read_parquet`` is the read's peak and the reading
after ``sort`` is the peak of the pair -- an eager sort holds its input and its output at once,
which is the cost the spec has not priced. The difference between them is the sort's marginal
cost.

A third child prices what the spec puts out of scope, because the decision it drives is whether
``balanced`` ships in v1: the same sort through ``scan_parquet`` and the streaming engine, which
does not have to hold the whole frame. It is a separate process because ``ru_maxrss`` cannot
fall -- measured after an eager sort in the same process it could only ever report the eager
sort's peak, which would read as "streaming saves nothing" whatever streaming actually did. It
is recorded as evidence about an option, not as a recommendation to take one: taking it is a
spec change, and a gate does not make those.

Generating a fixture and measuring it are **two child processes**, not one. ``ru_maxrss`` covers
a process's whole life, so a generator that built a half-million-row row group in the same
process would leave its own high-water mark standing and be read as the cost of the read. The
parent owns the temporary directory; one child writes the Parquet into it and a second, which
has allocated nothing but its imports, reads and sorts it. The parent subtracts a baseline child
that imports Polars and reads nothing.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

import polars as pl

KIBIBYTES_PER_MEBIBYTE: Final[int] = 1024
MEBIBYTES_PER_GIBIBYTE: Final[int] = 1024

SORT_TEXT_COLUMN: Final[str] = "text00"
SORT_NUMBER_COLUMN: Final[str] = "int01"
"""The two sort keys. Text first is both the realistic case and the expensive one: Polars
strings are heap-allocated, so a string comparison sort is where a global sort costs most."""

WIDTH_CYCLE: Final[tuple[str, ...]] = ("text", "int", "float", "date", "bool", "datetime")
"""Dtypes cycle in this order, so width 6 holds one of each and width 96 holds sixteen."""

TEXT_LENGTH: Final[int] = 24
"""Characters per generated text cell. Ordinary, and not free."""

DISTINCT_VALUES: Final[int] = 100_000
"""Distinct values a column draws from -- far more than a category and far fewer than one per
row, which is where a real customer name or order reference sits."""

ROW_POSITION: Final[str] = "_position"
"""The generator's working column. Every generated value is derived from it, and it is dropped
before the fixture is written, so it never reaches the measured frame."""

WIDTHS: Final[tuple[int, ...]] = (3, 12, 25, 50, 100)
"""Representative exported widths. The capacity section's own worked examples are 3 columns by
2 million rows and 100 columns by 500 thousand, so both ends are its own."""

START_ROWS: Final[int] = 500_000
CAP_ROWS: Final[int] = 64_000_000
CAP_CELLS: Final[int] = 800_000_000
"""Escalation bounds, so a run terminates even where nothing fails."""

CAP_PEAK_GIB: Final[float] = 11.0
"""Stop doubling a width once a shape peaks above this. The target machine has 16 GB and also
an operating system, a Spark JVM, and possibly an open workbook, so the last shape worth
reporting is the last one with real headroom rather than the last one that did not quite die."""


@dataclass(frozen=True)
class Shape:
    """One source shape: a row count and an exported column count."""

    rows: int
    width: int

    @property
    def cells(self) -> int:
        """Return the total cell count, the unit ``max_cells_per_workbook`` counts in."""
        return self.rows * self.width

    @property
    def label(self) -> str:
        """Return a short identifier for the report table."""
        return f"{self.width}c x {self.rows // 1000}k"


type Outcome = Literal["measured", "generation-failed", "killed"]
"""How one shape ended.

Three values rather than a boolean, because "the process died" is only a result when it is the
*measured* process that died. A fixture generator that ran out of disk is a failure of the
harness, and reporting it in the same column as an OOM-killed sort would put a number this gate
does not have under a heading that says it does.
"""


@dataclass(frozen=True)
class Reading:
    """What one shape's children reported, or the fact that one of them died reporting nothing."""

    shape: Shape
    outcome: Outcome
    parquet_mib: float = 0.0
    read_peak_mib: float = 0.0
    sort_peak_mib: float = 0.0
    stream_peak_mib: float | None = None
    """``None`` when the streaming child died where the eager pair survived, which is itself a
    result -- and not ``0.0``, which subtracting a baseline from would print as a negative peak."""
    read_seconds: float = 0.0
    sort_seconds: float = 0.0
    stream_seconds: float = 0.0


def column_names(width: int) -> list[str]:
    """Return the generated column names for one width, cycling the dtype mix.

    Args:
        width: How many columns, at least two.

    Returns:
        Names such as ``text00``, ``int01``, ``float02``.
    """
    return [f"{WIDTH_CYCLE[index % len(WIDTH_CYCLE)]}{index:02d}" for index in range(width)]


def build_expressions(names: list[str]) -> list[pl.Expr]:
    """Return one expression per column, each derived from the working row position.

    Everything comes from ``pl.int_range`` rather than from Python, so a thirty-million-row
    fixture is built by Polars rather than by a loop and is identical on every run.

    Args:
        names: The column names, whose prefixes select the dtypes.

    Returns:
        The expressions, in order, each aliased to its column name.
    """
    expressions: list[pl.Expr] = []
    index: int
    name: str
    for index, name in enumerate(names):
        seeded: pl.Expr = (pl.col(ROW_POSITION) * (index * 2 + 1) + index) % DISTINCT_VALUES
        if name.startswith("text"):
            expressions.append(seeded.cast(pl.String).str.pad_start(TEXT_LENGTH, "0").alias(name))
        elif name.startswith("int"):
            expressions.append(seeded.alias(name))
        elif name.startswith("float"):
            expressions.append((seeded / 7.0).alias(name))
        elif name.startswith("date"):
            expressions.append((pl.lit(10957, dtype=pl.Int32) + (seeded % 7000).cast(pl.Int32)).cast(pl.Date).alias(name))
        elif name.startswith("bool"):
            expressions.append((seeded % 2 == 0).alias(name))
        else:
            expressions.append((seeded * 1_000_000).cast(pl.Datetime("us")).alias(name))
    return expressions


def write_fixture(shape: Shape, destination: Path) -> float:
    """Write one shape's Parquet file through the streaming sink.

    Sunk rather than collected: the fixture may be larger than the memory the measurement is
    about, and building it as one frame would decide the answer before the measurement began.
    It is one file rather than a directory of parts, because reading a glob concatenates and
    that concatenation would be charged to the read this gate is pricing.

    Args:
        shape: The shape to write.
        destination: Where the file goes.

    Returns:
        The file's size in mebibytes.
    """
    names: list[str] = column_names(shape.width)
    positions: pl.LazyFrame = pl.LazyFrame({ROW_POSITION: [0]}).select(pl.int_range(0, shape.rows, dtype=pl.Int64).alias(ROW_POSITION))
    positions.select(build_expressions(names)).sink_parquet(destination, engine="streaming")
    return destination.stat().st_size / KIBIBYTES_PER_MEBIBYTE / KIBIBYTES_PER_MEBIBYTE


def _warm() -> None:
    """Build and sort a thousand rows, so Polars' first-use allocation lands in the baseline.

    Without it the smallest measured shape absorbs whatever Polars allocates on first real use,
    and is reported as costing more per cell than it does.
    """
    positions: pl.LazyFrame = pl.LazyFrame({ROW_POSITION: [0]}).select(pl.int_range(0, 1000, dtype=pl.Int64).alias(ROW_POSITION))
    positions.select(build_expressions(column_names(WIDTHS[1]))).collect().sort([SORT_TEXT_COLUMN, SORT_NUMBER_COLUMN])


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


def _generate(argument: str) -> int:
    """Write one shape's fixture and print its size, in a process that measures nothing.

    Args:
        argument: ``{"rows": int, "width": int, "path": str}``.

    Returns:
        A process exit status.
    """
    request: dict[str, Any] = json.loads(argument)
    shape: Shape = Shape(rows=int(request["rows"]), width=int(request["width"]))
    print(json.dumps({"parquet_mib": write_fixture(shape, Path(str(request["path"])))}))
    return 0


def _child(argument: str) -> int:
    """Measure the eager read and the eager sort of an already-written fixture.

    Args:
        argument: ``{"path": str}``.

    Returns:
        A process exit status.
    """
    source: Path = Path(str(json.loads(argument)["path"]))
    started: float = time.monotonic()
    frame: pl.DataFrame = pl.read_parquet(source)
    read_seconds: float = time.monotonic() - started
    read_peak: float = _peak_mib()
    started = time.monotonic()
    ordered: pl.DataFrame = frame.sort([SORT_TEXT_COLUMN, SORT_NUMBER_COLUMN])
    sort_seconds: float = time.monotonic() - started
    print(json.dumps({"read_peak_mib": read_peak, "sort_peak_mib": _peak_mib(), "read_seconds": read_seconds, "sort_seconds": sort_seconds, "height": ordered.height}))
    return 0


def _stream(argument: str) -> int:
    """Measure the same sort through the streaming engine, in a process that did nothing else.

    Args:
        argument: ``{"path": str}``.

    Returns:
        A process exit status.
    """
    source: Path = Path(str(json.loads(argument)["path"]))
    started: float = time.monotonic()
    pl.scan_parquet(source).sort([SORT_TEXT_COLUMN, SORT_NUMBER_COLUMN]).sink_parquet(source.with_name("sorted.parquet"), engine="streaming")
    print(json.dumps({"stream_peak_mib": _peak_mib(), "stream_seconds": time.monotonic() - started}))
    return 0


def _spawn(mode: str, request: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    """Run one child process in the given mode and return it without judging its status.

    Args:
        mode: ``"--generate"`` or ``"--case"``.
        request: The JSON payload for that mode.

    Returns:
        The completed process.
    """
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "gates.gate_0d_sort_memory", mode, json.dumps(request)],
        capture_output=True,
        text=True,
        check=False,
    )


def measure(shape: Shape, scratch: Path) -> Reading:
    """Generate one shape in one child, measure it in another, and collect the result.

    Args:
        shape: The shape to measure.
        scratch: Directory the fixture is written into and removed from.

    Returns:
        The reading, with ``outcome`` saying which child, if any, died rather than returning.
    """
    source: Path = scratch / f"source-{shape.width}x{shape.rows}.parquet"
    written: subprocess.CompletedProcess[str] = _spawn("--generate", {"rows": shape.rows, "width": shape.width, "path": str(source)})
    if written.returncode != 0:
        source.unlink(missing_ok=True)
        return Reading(shape=shape, outcome="generation-failed")
    parquet_mib: float = float(json.loads(written.stdout)["parquet_mib"])
    measured: subprocess.CompletedProcess[str] = _spawn("--case", {"path": str(source)})
    if measured.returncode != 0:
        source.unlink(missing_ok=True)
        return Reading(shape=shape, outcome="killed", parquet_mib=parquet_mib)
    streamed: subprocess.CompletedProcess[str] = _spawn("--stream", {"path": str(source)})
    source.unlink(missing_ok=True)
    source.with_name("sorted.parquet").unlink(missing_ok=True)
    reported: dict[str, Any] = json.loads(measured.stdout)
    streaming: dict[str, Any] | None = json.loads(streamed.stdout) if streamed.returncode == 0 else None
    return Reading(
        shape=shape,
        outcome="measured",
        parquet_mib=parquet_mib,
        read_peak_mib=float(reported["read_peak_mib"]),
        sort_peak_mib=float(reported["sort_peak_mib"]),
        stream_peak_mib=None if streaming is None else float(streaming["stream_peak_mib"]),
        read_seconds=float(reported["read_seconds"]),
        sort_seconds=float(reported["sort_seconds"]),
        stream_seconds=0.0 if streaming is None else float(streaming["stream_seconds"]),
    )


def baseline_peak() -> float:
    """Return the peak of a child that imports Polars and reads nothing.

    Returns:
        The baseline in mebibytes, subtracted from every reading below.

    Raises:
        RuntimeError: The baseline child failed, which means nothing else can be interpreted.
    """
    completed: subprocess.CompletedProcess[str] = _spawn("--baseline", {})
    if completed.returncode != 0:
        message: str = f"the baseline child failed: {completed.stderr.strip()}"
        raise RuntimeError(message)
    return float(json.loads(completed.stdout)["read_peak_mib"])


def escalate(width: int, baseline: float, scratch: Path) -> list[Reading]:
    """Double the row count at one width until the child dies or a cap is hit.

    Args:
        width: The exported column count to hold fixed.
        baseline: The baseline child's peak, subtracted before comparing against ``CAP_PEAK_GIB``.
        scratch: Directory fixtures are written into.

    Returns:
        Every reading taken at this width, in ascending row order, the failing one last.
    """
    readings: list[Reading] = []
    rows: int = START_ROWS
    while rows <= CAP_ROWS and rows * width <= CAP_CELLS:
        reading: Reading = measure(Shape(rows=rows, width=width), scratch)
        readings.append(reading)
        if reading.outcome != "measured" or (reading.sort_peak_mib - baseline) / MEBIBYTES_PER_GIBIBYTE > CAP_PEAK_GIB:
            break
        rows *= 2
    return readings


def main() -> int:
    """Run the escalation at every width and print the table.

    Returns:
        A process exit status.
    """
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description="Gate 0d: read plus global sort memory.")
    parser.add_argument("--case", help="internal: measure the eager read and sort of one already-written fixture")
    parser.add_argument("--stream", help="internal: measure the streaming sort of one already-written fixture")
    parser.add_argument("--generate", help="internal: write one fixture and print its size")
    parser.add_argument("--baseline", help="internal: report this process's peak having read nothing")
    parsed: argparse.Namespace = parser.parse_args()
    if parsed.case is not None:
        return _child(str(parsed.case))
    if parsed.stream is not None:
        return _stream(str(parsed.stream))
    if parsed.generate is not None:
        return _generate(str(parsed.generate))
    if parsed.baseline is not None:
        _warm()
        print(json.dumps({"read_peak_mib": _peak_mib()}))
        return 0
    baseline: float = baseline_peak()
    print(f"baseline child peak: {baseline:.1f} MiB (polars imported, nothing read)")
    print(f"{'shape':>16}  {'cells':>12}  {'parquet MiB':>11}  {'read GiB':>8}  {'sort GiB':>10}  {'sort/read':>9}  {'GiB/Mcell':>9}  {'stream GiB':>10}  {'read s':>7}  {'sort s':>7}  {'stream s':>8}")
    scratch: str
    with tempfile.TemporaryDirectory(prefix="gate0d-") as scratch:
        report(baseline, Path(scratch))
    return 0


def report(baseline: float, scratch: Path) -> None:
    """Run the escalation at every width and print one line per reading.

    Args:
        baseline: The baseline child's peak, subtracted from every reading.
        scratch: Directory fixtures are written into.
    """
    width: int
    for width in WIDTHS:
        reading: Reading
        for reading in escalate(width, baseline, scratch):
            if reading.outcome != "measured":
                # OOM-KILLED is the gate's answer; GEN-FAILED is the harness admitting it could
                # not even build the fixture, which is not a statement about the sort at all.
                verdict: str = "OOM-KILLED" if reading.outcome == "killed" else "GEN-FAILED"
                print(f"{reading.shape.label:>16}  {reading.shape.cells:>12,}  {reading.parquet_mib:>11.1f}  {'':>8}  {verdict:>10}  {'':>9}  {'':>9}  {'':>10}  {'':>7}  {'':>7}  {'':>8}")
                continue
            read_gib: float = (reading.read_peak_mib - baseline) / MEBIBYTES_PER_GIBIBYTE
            sort_gib: float = (reading.sort_peak_mib - baseline) / MEBIBYTES_PER_GIBIBYTE
            stream: str = "     KILLED" if reading.stream_peak_mib is None else f"{(reading.stream_peak_mib - baseline) / MEBIBYTES_PER_GIBIBYTE:>10.2f}"
            print(
                f"{reading.shape.label:>16}  {reading.shape.cells:>12,}  {reading.parquet_mib:>11.1f}  {read_gib:>8.2f}  {sort_gib:>10.2f}  "
                f"{sort_gib / read_gib:>9.2f}  {sort_gib * 1_000_000 / reading.shape.cells:>9.3f}  {stream}  "
                f"{reading.read_seconds:>7.1f}  {reading.sort_seconds:>7.1f}  {reading.stream_seconds:>8.1f}",
            )


if __name__ == "__main__":
    sys.exit(main())
