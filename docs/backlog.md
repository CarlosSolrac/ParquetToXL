# Backlog — what is left, and what has already been decided about it

As of 2026-09-16. Every phase of `docs/export-pipeline-spec.md` is built. On `main` at `bdb5fbd`
that was 1,676 tests over 3,208 statements; on the `vectorised-bucketing` branch it is 1,714 over
3,263, both at 100% statement and branch coverage.

Nothing here is a defect. Each item is either a deliberate debt with a recorded reason, a decision
nobody has made yet, or a gate that needs infrastructure. Read the linked decision record before
starting one — the alternatives have usually been weighed already.

---

## 1. The per-row date decode — ~~pending~~ **done on `vectorised-bucketing`, not yet merged**

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

**What is left on that branch:** staging the converted frame to Parquet and slicing it from disk
(which is what bounds item 3's memory ceiling), and item 4's help-text half.

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

---

## 4. `pqx verify` cannot reach an Azure destination

`fast_excel_reader` and the Excel writers hand `str(path)` to libraries that open **local
filenames**. So `pqx verify` works against a local or SMB-mounted destination and fails against
`abfs://`. The spec already lists "re-verifying after publish" as out of scope for this reason; the
verb partially closes it, but only for filesystem destinations.

Two ways forward, neither started: give `fast_excel_reader` a remote read path (download to scratch
and read, or a real remote reader), or document the constraint in the verb's help text. **Do the
second now regardless** — a verb that silently only works on some destinations is worse than one
that says which.

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
- There is no `README.md`. `CLAUDE.md` covers the working rules; a human arriving at the repository
  still has to start from `docs/export-pipeline-spec.md`.
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
