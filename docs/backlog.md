# Backlog — what is left, and what has already been decided about it

As of 2026-09-16. Every phase of `docs/export-pipeline-spec.md` is built: 1,725 tests over 3,263
statements at 100% statement and branch coverage, CI green on `main`.

Nothing here is a defect. Each item is either a deliberate debt with a recorded reason, a decision
nobody has made yet, or a gate that needs infrastructure. Read the linked decision record before
starting one — the alternatives have usually been weighed already.

---

## 1. The per-row date decode — **done and merged**

Bucketing decoded one value at a time, so a forty-million-row source paid forty million Python
calls on the run's critical path.

**It was not solved the way this entry proposed.** The entry called for a Polars expression built
in `pqx-calendar` beside the scalar path. What shipped instead decodes the column's **distinct
values** through the existing scalar decoder — a date column repeats, so forty million rows hold a
few hundred distinct months. That satisfies the *intent* of `2026-09-15-phase-b.md` §4, one
statement of the century-window rule, more completely than the expression would have: there is no
second implementation at all, and no Polars dependency in `pqx-calendar`. Measured at 50x–80x over
two million rows.

The same work removed a defect found beside it: a source was converted for Excel **twice** per run,
once to describe it and once to hand the writer a frame, with the first conversion's frame thrown
away.

Full reasoning, including the reproduction of the Polars/`zoneinfo` disagreement that decides which
timestamp columns may be collapsed, is in `docs/decisions/2026-09-15-distinct-value-bucketing.md`.

Staging the converted frame to Parquet was investigated and deliberately not built — see item 3
for the measurements, and `docs/decisions/2026-09-16-lazy-frames.md` for the scope question it ran
into, which is now settled.

---

## 2. `balanced` planning is not wired into a run — blocked on two decisions, not on code

`plan_balanced_sheets` and `minimum_balanced_workbooks` exist in `pqx_plan.partition` and are
tested. `run_profile` refuses a `balanced` profile by name (`"which is planned but not yet wired
into a run"`), which `pqx` turns into a per-profile exit 3.

**What is missing is not implementation.** `balanced` consults no date, so:

- **Naming.** `naming.worksheet` and `naming.workbook` can reference `{period_label}`, which does
  not exist for balanced output. Options: refuse a template using `{period_label}` under `balanced`
  at configuration validation (cheap, explicit, and `pqx_plan.semantics` is already the place for
  it); or substitute a fixed token. The first is recommended — a silent substitution makes two
  profiles produce identically named workbooks from different data.
- **Ordering under fan-in.** `order_profile_sheets` orders "period first, then source". With no
  period, source order is all that is left. Confirm that is intended before relying on it.

Answer both, then the wiring itself is a branch in `_plan_profile`.

---

## 3. The 150M-cell ceiling is a proposal, not an enforcement

Gate 0d measured the global-sort memory curve and decided "`balanced` ships behind a per-source
cell ceiling". `docs/partitioning-spec.md` records it as a marked note and
`docs/decisions/2026-09-15-phase-0.md` §12 records the number. **Nothing enforces it.**
`ProfileLimits` has `max_data_rows_per_worksheet` and `max_cells_per_workbook` only.

Ships with item 2 — it is the `balanced` path that sorts globally, so the ceiling is unreachable
until that path exists. When you add it: a per-source field refused at planning, with a message
that quotes the measurement (150M cells ≈ 3.5 GiB on the measured mix; ~400M is the last shape that
survives 16 GB; 800M is OOM-killed).

The number itself is a judgement, not a measurement — it is the one item on this list worth
confirming with a human before coding.

### Staging the converted frame was investigated and not built

The obvious way to bound residency is to write each source's converted frame to Parquet in scratch
and have `_slices` read each sheet back, instead of holding `Observation.frame` from planning until
the last workbook is written. It was taken far enough to measure and to prove the properties it
needs, then stopped for two reasons.

**It bounds the write phase, not the peak.** The conversion *produces* the whole converted frame in
memory; staging happens after that. So the peak is unchanged, and what staging removes is holding
the frame *while also* building workbooks, across the long part of the run. Worth having, but it is
not what makes the ceiling reachable — bounding the peak needs a streaming conversion.

**Reading a sheet back by offset needs `pl.scan_parquet(...).slice(...)`, a lazy frame**, and
`docs/decisions/2026-09-16-lazy-frames.md` settled that the exclusion stands — on grounds that have
nothing to do with sorting, so the ambiguity that raised this twice is closed. Read it before
proposing a lazy read anywhere.

That record also names the eager alternative for this case: the plan, and therefore every sheet's
row count, is known before the writer needs a slice, so a staged file whose row groups are aligned
to planned sheets can be read a row group at a time with no lazy frame at all. `pyarrow` is already
in `uv.lock`. Unbuilt and unmeasured, and `row_group_size` in Polars is a single integer, so
per-sheet groups would likely need pyarrow's writer.

Measured resident cost of a converted frame, which is what any of this is trading against:

| Shape | bytes/cell | 150M cells | 400M cells |
| --- | --- | --- | --- |
| Narrow: 4 numeric-ish columns | 7.00 | 0.98 GiB | 2.61 GiB |
| Wide: 20 columns, 11 of free text | 25.40 | 3.55 GiB | 9.46 GiB |

The second row reproduces gate 0d's recorded "150M cells ≈ 3.5 GiB on the measured mix" almost
exactly, which is worth knowing: **the ceiling is a property of the column mix, not of the cell
count.** A narrow source is nowhere near it at 150M cells; a text-heavy one passes it well before.
A single number cannot express that, so a ceiling expressed in cells will be wrong in one direction
for most sources.

`packages/pqx-pipeline/tests/test_staged_parquet.py` already proves what the approach would need:
Parquet round-trips every dtype ToExcel emits, preserves row order, preserves the digest of each
positional slice, and `DataFrameHasherBinaryAggregateHash.combine` lets `expected_whole` be taken
over chunks so the whole frame is never resident for it. Those tests stand on their own.

---

## 4. `pqx verify` cannot reach an Azure destination

`fast_excel_reader` and the Excel writers hand `str(path)` to libraries that open **local
filenames**. So `pqx verify` works against a local or SMB-mounted destination and fails against
`abfs://`. The spec already lists "re-verifying after publish" as out of scope for this reason; the
verb partially closes it, but only for filesystem destinations.

Two ways forward: give `fast_excel_reader` a remote read path (download to scratch and read, or a
real remote reader), or document the constraint in the verb's help text.

**The second is done** on `vectorised-bucketing`. `pqx --help` ends with an epilog naming it, which
is where it had to go: `cli.py` uses argparse with no subparsers, so the verb is one positional
constrained by `choices` and there is no per-verb help to hang it from. A test asserts the text is
there — the first in the repository to assert on help output.

While doing it: `CLAUDE.md` claimed "the Excel writers and `fast_excel_reader` ... say so", and
none of the three did. All three now carry the constraint in their own docstrings.

**The remote read path is untouched**, so the constraint is now stated rather than removed.

---

## 5. `--dry-run` does not print the delete set

`docs/export-pipeline-spec.md`'s negative checks ask for it. Computing a delete set needs the
manifest, which needs the plan, which needs the sources staged and read — the one thing `--dry-run`
promises not to do.

Options: let `--dry-run` stage and say so in its help; or have `plan` read the published manifest
and subtract, which gives the same answer without changing what a documented switch means. The
second is recommended. Today the workaround is `pqx plan`, which names every workbook a run would
produce — anything in the destination not in that list and not bookkeeping is what would go.

**A third option surfaced while working on item 1, and it is cheaper than either.**
`plan_calendar_sheets(shape, counts, partitioning, limits)` takes **no frame** — bucket counts plus
`SourceShape` are the entire input to planning. Both are small: a few hundred dict entries and four
numbers. Persisting them beside the export would let `plan` and `--dry-run` produce a full plan, and
therefore a delete set, **without reading a single source**. That removes the premise this item
rests on rather than working around it. Nobody has costed the staleness question it raises: a
persisted count set describes the sources as they were at some T2, so it needs the same freshness
rule the sidecars have.

Recorded in `docs/decisions/2026-09-15-phases-e-h.md`.

---

## 6. Gates 0e and 0f have never been run

| Gate | Question | Needs |
| --- | --- | --- |
| 0e | Is the scratch root tmpfs? What does `tempfile.gettempdir()` resolve to on the Spark image? Decides whether the free-space precheck is also a memory precheck, and what `default_scratch_root` should be | The Spark image |
| 0f | Azure round trip: `stat().st_mtime` on a blob, a small JSON read, an `.xlsx` copy up, a delete by listing, with production's credential path | Azure credentials |

**0f is the higher risk of the two.** The entire freshness rule rests on `adlfs` surfacing a usable
`st_mtime`; if it does not, `sidecar_stale` needs a different source of truth and that is a design
change, not a fix. Nothing built so far assumes an answer from either gate.

**The 0f harness is written and waiting for credentials**:
`gates/gate_0f_azure_round_trip.py`. It reports rather than asserts — no check says what `adlfs`
*should* return — and it exercises the production helpers (`zpath`, `copy_file`, `list_names`,
`delete_names`, `truncate_to_second`) rather than raw `adlfs`, so it answers the question the
pipeline actually asks. It refuses a destination that is not empty and deletes only the two probe
objects it creates, and it never prints a credential value, only which variables are set.

Run it with `--destination abfs://container/gate-0f`. It has been run against a local destination,
where all twelve asserted properties hold; that verifies the harness, not Azure. Note the local run
reports *sub-second* mtime resolution, which is the contrast `truncate_to_second` exists for.

0e still needs writing, and needs the Spark image to answer anything.

`gates/` holds the harnesses for 0a and 0d as a pattern to copy.

---

## 7. Never exercised against real infrastructure

Every test drives real Parquet and real workbooks through `tmp_path`. None has touched an SMB
share, a blob container, or the Spark image. The spec's end-to-end checklist and its six negative
checks each now have a verb — see the table at the end of
`docs/decisions/2026-09-15-phases-e-h.md` — but they have only ever been run locally.

---

## 8. Housekeeping

- Every package is at `0.1.0`. No tags, no release process, no CHANGELOG.
- `reports/` accumulates one set per run forever. Documented as the operator's to manage, but
  nothing prunes it, and nothing warns.
- ~~There is no `README.md`.~~ Written. `CLAUDE.md` remains the working rules; the README is the
  front door for a human, and points at the specs, the decision records and this file.
- On `vectorised-bucketing`, `ordered_by_bucket` and `extract_metadata_from_dataframe` have no
  caller left in the workspace — the run moved to `bucket_arrangement` and `describe_dataframe`.
  Both are kept and both say so in their own docstrings, because the fifty frozen tests written
  against them read as statements of behaviour, and rewriting those to gather and unpack by hand
  would bury that behind mechanism. Worth revisiting if a third entry point ever appears.

---

## Explicitly out of scope — do not build these

From `docs/export-pipeline-spec.md`: a watcher or daemon, notifications, the web configuration
editor, row filters and column projection (they break the additive identity), `polars-xlsxwriter`
as a selectable writer, atomic publication and a true mutex (neither SMB nor Blob offers
compare-and-swap), report retention, nested dtypes, Excel formatting, `.xls`, lazy/streaming
frames.
