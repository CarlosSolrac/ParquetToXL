# Why this pipeline stays eager

`library-spec.md` lists `streaming/lazy frames` as out of scope and
`export-pipeline-spec.md` inherits it. Neither says **why** beyond one measurement about
sorting, and the question has now come up twice: once at gate 0d, and once while investigating
whether the write phase could slice a staged Parquet file instead of holding a whole frame.

This record exists so the third time costs nothing. Nothing changes here — the exclusion stands.
What is new is the reasoning behind it, and the conditions that would properly reopen it.

---

## The question

Should any part of this pipeline use `pl.LazyFrame` — `scan_parquet`, `.lazy()`, the streaming
engine — in any form?

| Option | Consequence |
| --- | --- |
| **Stay eager everywhere (chosen)** | A hard ceiling on source size, and the proposed `balanced` cell ceiling stays where gate 0d put it. |
| Go lazy where it helps | Two execution engines to keep correct against a 100% branch gate, and refusal messages that no longer name a row. Buys 20-30% on the one case that applies. |
| Go lazy end to end | The only path to sources larger than memory. A different pipeline, and every existing memory measurement invalidated. |

**Chosen: stay eager.** The reasoning below is the point of this record; the conclusion is the
least of it.

---

## 1. The additive identity removes most of what lazy is for

Polars' two headline optimisations are **predicate pushdown** (skip rows) and **projection
pushdown** (skip columns). Neither is available here, and not by accident:

> Fragment digests sum (mod 2^128) to the whole-source digest. That is why partitioning must be
> total and disjoint, why row filters and column projection are out of scope, and why a temporary
> column must never reach exported data — an extra column changes the digest.

This pipeline reads **every row and every column, always**. A lazy plan over it has nearly
nothing to optimise. Most of the case for lazy frames in a normal analytics codebase is gone
before the trade-off is even weighed, and that is the single most important thing to understand
about this decision: *the usual argument does not apply here.*

What remains is slice pushdown and streaming execution.

## 2. What lazy would actually buy, with the measurements that exist

| Use | Benefit | Measured |
| --- | --- | --- |
| Streaming sort for `balanced` | 20-30% more headroom | **Yes.** Gate 0d: peaks at 70-80% of the eager sort on large inputs. Recorded in `2026-09-15-phase-0.md` §12 and as a marked note in `partitioning-spec.md`: *"not an escape"* |
| Slice pushdown reading a staged sheet back | Touch only the row groups a sheet needs | No. An eager alternative exists — see §5 |
| Sources larger than memory | The only path there is | No. Not a v1 requirement |
| Predicate and projection pushdown | **Nothing**, see §1 | n/a |

A quarter more headroom is not a different design. It moves a ceiling; it does not remove one.

## 3. The cost that decides it: refusals stop naming a row

This is the argument that outweighs the rest.

The pipeline's value is that it refuses precisely. A bad value produces
`202513 in column 'invoice_month' at source row 41000000` — the column, the value, and the
ordinal, in a file with forty million rows. `decode_cell` takes `column_name` and `row_ordinal`
as **required** keyword arguments specifically so a caller cannot drop the half of the message
that makes it actionable, and `2026-09-15-distinct-value-bucketing.md` §4 records the trouble
taken to keep that true when decoding moved off the per-row path.

Lazy execution surfaces failures at `.collect()`, against a query plan, at whatever point the
engine got to. Errors stop belonging to a call site. Trading that for 20-30% of memory headroom
is a bad trade **for this codebase specifically**, whatever it would be worth elsewhere.

## 4. Four costs that are smaller but real

**Two engines mean two definitions of correct.** Polars' streaming engine is not feature-identical
to the eager one. Against a 100% statement-and-branch merge gate and tests that are the
specification, a second execution path roughly doubles what has to be pinned.

**Row order becomes a thing to prove, again and again.** `_slices` cuts sheets with a positional
cursor, and the source ordinal exists so the same inputs produce the same assignment every run.
Order is load-bearing in a way it is not in most pipelines — `test_staged_parquet.py` had to
assert that even a Parquet round trip preserves it. Every lazy boundary adds another such
obligation.

**It invalidates measurements that were expensive to take.** Gates 0a and 0d produced real
numbers for eager behaviour, and the ceilings in `partitioning-spec.md` quote them. Going lazy
means taking them again.

**Debuggability.** An eager frame can be inspected at every step. A lazy one needs `.explain()`
and reasoning about a plan, in a codebase where "measured rather than assumed" appears in the
specs repeatedly.

## 5. What the exclusion costs, stated honestly

It is not free, and pretending otherwise would make this record useless.

- Peak memory is bounded by the whole source plus its conversion. Gate 0d: **~400M cells is the
  last shape that survives 16 GB; 800M is OOM-killed.**
- The 150M-cell ceiling gate 0d proposed for `balanced` stays where it is. Note that **neither is
  live**: `ProfileLimits` has no such field, and `run_profile` refuses a `balanced` profile by name
  because the path is not wired into a run at all (backlog items 2 and 3). So this cost is
  prospective — it is what staying eager will cost when `balanced` ships, not what it costs today.
- The write phase holds each source's converted frame while also building workbooks.
- There is no answer at all if sources outgrow memory.

**An eager alternative exists for the staging case.** The plan — and therefore every sheet's row
count — is known before the writer needs a slice, because counts come from bucketing and planning
both precede `write_profile`. A staged file whose row groups are aligned to planned sheets can be
read one row group at a time, eagerly. `pyarrow` is already in `uv.lock`, so it needs no new
dependency. This is an observation, not a proposal: it has not been built or measured, and
`row_group_size` in Polars is a single integer, so per-sheet groups would likely need pyarrow's
writer.

## 6. What would properly reopen this

In order, cheapest first. **None of these is "an implementer found a use for `scan_parquet`."**

1. **Set the cell ceiling on evidence, whenever it does get set.** Cheapest in the most literal
   sense: the field does not exist yet, so nothing has to be unpicked. And the number gate 0d
   proposed stands in for a constraint it does not measure. Measured on 2026-09-16, a converted
   frame costs **7.00 bytes per cell** on a narrow numeric mix and **25.40** on a wide one with
   free text — 0.98 GiB against 3.55 GiB at the same 150M cells. The second reproduces gate 0d's
   figure almost exactly, and together they say the ceiling is a property of the **column mix**,
   not the cell count. A narrow source is nowhere near trouble where a text-heavy one is already
   past it. A limit expressed in cells alone will be wrong in one direction for most sources.
2. **Eager row-group staging**, per §5, evaluated on its own merits rather than as a workaround.
3. **Lazy at one named site, with its own decision record** — not a general lifting of the
   exclusion.
4. **Sources that genuinely exceed memory.** Then the trade in §3 has to be re-argued rather than
   assumed, because at that point the alternative is not "slower" but "impossible".

---

## How the exclusion reads today, for the next person

Two things are worth knowing before anyone argues from the text alone.

**The pipeline spec inherits it selectively, not wholesale.** `export-pipeline-spec.md` says
*"Nested dtypes, Excel formatting, `.xls`, lazy and streaming frames — inherited from
`library-spec.md`, unchanged."* That is four items chosen from a longer list. The library spec
also excludes *"a CLI (`main.py` is removed)"* — and the pipeline has `pqx`. So inheritance was a
decision about each item, and this item was inherited deliberately.

**It has already been applied to pipeline work once.** Gate 0d's option table rejects "Stream the
sort" with *"A quarter more headroom, and it takes lazy frames out of scope, which is a
`library-spec.md` decision"* (`2026-09-15-phase-0.md` §12). The strict reading is not new.

**And the code has honoured it absolutely.** There is no `scan_parquet`, no `.lazy()`, no
`LazyFrame` anywhere in any package's production code. The only mention outside
`test_staged_parquet.py` is in `pqx-verify`'s tests, which monkeypatch `pl.scan_parquet` to
*refuse*, asserting that verification never opens a Parquet file at all.

**The one thing that genuinely is ambiguous:** every recorded rationale for the exclusion is about
the *sort*, while the rule itself is unqualified. That gap is what sent this question round twice.
It is closed here — the rule stands for the reasons in §1 through §4, which have nothing to do
with sorting.
