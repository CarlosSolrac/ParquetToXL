# Bucketing without a per-row decode, and converting a source once

Backlog item 1, plus a defect found while investigating it. Two commits, no new dependency and
no change to `pqx-calendar`.

**What shipped:** `ordered_by_bucket` no longer calls the date decoder once per row, and a
source is converted for Excel once per run rather than twice. Workspace total is 1,714 tests at
100% statement and branch coverage, up from 1,676.

Measured over two million rows, per shape: 70x for `YYYYMM`, 80x for `YYYYMMDD`, 54x for `date`,
53x for per-second naive timestamps, 27x for tz-aware wall clock, and **1.0x for a zoned column
of unique instants** — see question 3 for why that one is unchanged on purpose.

Six questions were decided. Each is recorded with the alternatives, so a later reader can reopen
one without re-deriving the rest.

---

## The questions

### 1. How does decoding stop costing one Python call per row?

`docs/backlog.md` and `2026-09-15-phase-b.md` §4 both assumed the answer was a Polars expression
built in `pqx-calendar` beside the scalar path.

| Option | Consequence |
| --- | --- |
| A Polars expression builder in `pqx-calendar` | What §4 named, and what it called "the one decision with a live risk attached": a second statement of the two-digit-year century window. The trap is concrete — `resolve_two_digit_year` relies on Python's **floored** modulo, where `(25 - 1970) % 100` is `55`, while Rust and therefore Polars use a **truncated** remainder giving `-45`. It also pulls Polars into a package whose stated purity is why it is separable. |
| **Decode the column's distinct values (chosen)** | A date column repeats: forty million rows hold a few hundred distinct months. One decode per distinct value, mapped back onto the frame. The scalar decoder stays the only implementation. |
| Vectorise in `pqx-pipeline` | The outcome §4 explicitly warned against, in the package without the test matrix for it. |

**Chosen: decode the distinct values.** It satisfies §4's *intent* — one statement of the
century window — more completely than the option §4 named, at a fraction of the diff, and it
needs no Polars dependency in `pqx-calendar`. The purity claim in that package's `__init__.py`
and `pyproject.toml` remains true.

### 2. What carries the partition key through the sort?

| Option | Consequence |
| --- | --- |
| A rank derived from `order_buckets` | What the code did before. No row can be assigned one until *every* bucket is known, which forbids working in chunks; and a staged file full of ranks is uninterpretable without the in-memory mapping that produced them. |
| Three `year` / `month` / `day` columns | Readable, but a staged file's schema would then change with the grid's precision — a `year-month` grid carries an all-null day column, or a different shape. |
| **One packed `Int64` (chosen)** | `year * 10000 + month * 100 + day`, absent fields zero. |

**Chosen: the packed key.** It is `PeriodKey.sort_key` written in radix 100. Month never reaches
100 and day never reaches 100, so the packing is strictly monotonic and numeric order on the key
**is** lexicographic order on `(year, month, day)` — the equivalence with `order_buckets` is
arithmetic rather than argued.

**The undated bucket is null, never zero.** Zero sorts *first*, and the rule is that it is last
under **both** period orders. `nulls_last=True` delivers that structurally. The temptation is
sharp because zero already means "absent" two fields over.

**On zero-as-absent against house style.** `2026-09-15-phase-b.md` §2 chose `day: int | None` so
a missing field cannot be forgotten, and §6 rejected `None`-as-sentinel for `Undated`. Both
govern **domain value objects** that are constructed, validated and passed around. This key is a
transient ordering device living between a join and a drop — the same category as `sort_key`,
which already packs `month or 0`.

**The cost.** Row order is now stated independently of `order_buckets`, which still orders the
counts the planner consumes. `_slices` cuts sheets with a positional cursor and *trusts* frame
order to match plan order, so a drift between them would misassign rows silently. A test asserts
the frame's bucket sequence equals the counts dict's key sequence under both period orders, with
and without an undated bucket.

### 3. Can a timestamp column be collapsed to its day before the walk?

It has to be, or the walk is no shorter: a `datetime` column can be distinct in every row. But
the day depends on a timezone conversion, and there are two databases in play.

| Option | Consequence |
| --- | --- |
| Collapse every datetime column | **Wrong, and silently.** Polars converts through its bundled chrono-tz; `decode_cell` converts through this machine's `zoneinfo`. They disagree. |
| Collapse nothing | Correct, and leaves a shape that gains nothing from this work at all. |
| **Collapse wall-clock mode only (chosen)** | Sound, for the reason below. |

The disagreement, reproduced on the installed Polars 1.44.1 and tzdata 2026.3:

```
Two UTC instants one hour apart, read as Africa/Casablanca

  Python zoneinfo (what decode_cell uses)
    2026-10-01T23:30:00+00:00  ->  day 2026-10-01
    2026-10-02T00:30:00+00:00  ->  day 2026-10-02      two days

  Polars convert_time_zone (bundled chrono-tz)
    2026-10-01T23:30:00+00:00  ->  day 2026-10-02
    2026-10-02T00:30:00+00:00  ->  day 2026-10-02      one day
```

Casablanca sits at UTC+1 except during Ramadan, and the two databases project that differently
for late 2026. Which one is right is not the question: `decode_cell` is the authority by
construction, so no grouping may substitute the other for it. Under a daily grid the collapse
gave one bucket where the decoder gives two — wrong data, no error. A test pins it, taking its
expectation from the scalar oracle rather than writing the days down, so it still holds on a
host whose database happens to agree with Polars.

Worth knowing when reading such a value: the Polars result *reports* a `+00:00` offset while
carrying the wall clock chrono-tz computed. The object pairs one database's reading with the
other's `tzinfo`, so it does not look wrong on inspection.

**Chosen: wall-clock mode only.** Under `source_wall_clock` the decoder reads the year, month
and day straight off the value Polars rendered, so **Polars' database has already decided the
reading** and `replace_time_zone(None).dt.date()` agrees with it by construction. Under a named
zone the decoder converts the instant itself, through `zoneinfo`. So a zoned column of unique
instants still pays one decode per row. That is the price of the decoder remaining the authority
on which day an instant falls in, and it is the right price.

The collapse is a **grouping key and nothing more**: the scalar decoder still runs, on a real
value drawn out of each group, and still authors every answer and every refusal.

**Wall-clock mode on a tz-aware column therefore depends on Polars' timezone database rather
than this machine's.** That is pre-existing behaviour, not introduced here, but it is surprising
next to how carefully zone mode is specified, and nothing else says it.

### 4. How does a refusal still name the row the per-row walk would have stopped at?

`decode_cell`'s message names a row ordinal, which a distinct value does not have.

**Chosen:** one `group_by` pass carries each group's earliest source ordinal and the value from
that row, and the walk runs in that order — so the first refusal encountered is the one the
per-row walk would have reached first, and nothing after it can refuse sooner. Under
`null_dates: error` a null is also a refusal, so the earlier of the first null and the first
undecodable value wins.

The message itself is produced by **calling the scalar path again** on the offending value at
its own ordinal. That keeps the wording authored in one place, and it is a *statement* rather
than a branch: a guarded "if it somehow succeeded, raise something generic" would be unreachable
under the 100% branch gate, the same reasoning as the deliberate fall-through in
`pqx_calendar.decoding.decode`.

### 5. Why is the arrangement computed over the source rather than over the converted frame?

Converting once (question 6) means the frame that gets sorted could be either one. They are not
interchangeable to sort over.

**Chosen: decide the order on the source's own values, apply it to the converted copy.**
`bucket_arrangement` returns row positions; the run gathers. ToExcel writes `True` as `-1.0`, so
a boolean sort key sorted after conversion comes out **reversed**; an empty binary value becomes
null, which moves under `nulls_last`; and the largest integers collapse onto one float, which
makes distinct values tie. Deciding the order on the source keeps every one of those reading as
it does in the source.

Numerics are **not** turned into strings — Int, UInt, Decimal and Duration all become `Float64`.
An earlier draft of this reasoning claimed otherwise and was wrong.

### 6. Why does describing a source now hand back the frame it converted?

**This one is a defect, not a trade-off.** `extract_metadata_from_dataframe` converted the full
frame, kept `.columns_metadata` and discarded `.converted_dataframe`; `observe` then converted
the same rows again. Two full passes per source per run, one of them waste.

Nobody chose it. `test_ingest.py` says, in a comment on a frozen test, that the conversion
happens where it does *"so the digest recorded per fragment is taken over a frame already in
memory rather than by converting twice"* — describing the intent the code failed to honour.

What made the second pass unavoidable was stage order: bucketing must see **raw** values,
because ToExcel strips a timezone and decoding a converted zoned column would read the wall
clock and move rows across year boundaries. So the run converted, bucketed, and converted again.
Question 5's split removes the need.

**Chosen:** `describe_dataframe` returns the record together with every conversion's frame;
`extract_metadata_from_dataframe` remains, returning the record alone, because `pqx-frame` is a
library and a caller wanting only a description should not unpack a frame to drop it.

**What this rests on.** Fragment digests in the manifest are taken over *slices* of the arranged
frame, so if conversion were not independent of the order rows arrive in, every published digest
would shift and `pqx verify` would reject good exports. Conversion is per value and per column,
so it holds — but the cost of being wrong is silent and lands on already-published data, so
`test_conversion_row_order.py` asserts it down to the digest of each individual slice rather
than leaving it assumed.

---

## Two functions left without callers

`ordered_by_bucket` and `extract_metadata_from_dataframe` now have no caller in this workspace;
the run uses `bucket_arrangement` and `describe_dataframe`. Both are kept, and both say so in
their own docstrings with the reason. Removing them would mean rewriting the fifty frozen tests
written against them to gather and unpack by hand, which would bury the behaviour those tests
state behind mechanism.
