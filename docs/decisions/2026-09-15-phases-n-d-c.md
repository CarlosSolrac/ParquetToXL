# Phases N (names), D and C — the planner and the writer

Three phases built in one session, after Phase B. Recorded together because their decisions
interlock: the name rules Phase N settled are what Phase D's writer and Phase C's planner both
validate against.

**What shipped**

| Phase | Package | Tests | State |
| --- | --- | --- | --- |
| N (first half) | `pqx_common.names` | 121 | Names done; `pqx-staging` still to build |
| D | `pqx_excel.workbook` | 32 | Done |
| C | `pqx_plan.{config,semantics,corpus,capacity,fingerprint,partition,allocate,naming}` | 469 | Planning done; manifest construction still to build |

Workspace: **1345 tests, 100% statement and branch coverage**, green under pyright strict, mypy
strict, the declaration checker and pre-commit.

---

## Four findings worth reading even if you skip the rest

These are not design preferences. Each is a case where the code or the specs were wrong, and a
test is what said so.

### 1. A naming template could walk into a value, from a configuration file

`partitioning-spec.md` requires that templates not evaluate expressions or attributes.
`str.format_map` over a restricted mapping looks like it delivers exactly that, and does not:
it bounds which **names** a template can reach and does nothing about what it reaches **through**
them.

```
"{profile.__class__}".format_map({"profile": "annual"})        -> "<class 'str'>"
"{profile.__class__.__mro__}".format_map({"profile": "annual"}) -> "(<class 'str'>, <class 'object'>)"
"{p[0]}".format_map({"p": "annual"})                            -> "a"
```

From `__class__` the usual format-string route to `__globals__` is open, and these templates are
read out of a user-maintained JSON file. **Fixed:** attribute and index access are refused
outright, by the renderer and by the semantic validator, with two corpus cases pinning it. Format
*specifications* such as `{workbook_index:03d}` stay — those are applied to a value and cannot
traverse it.

### 2. The identifier pattern reads three different ways

`^[A-Za-z0-9][A-Za-z0-9_-]*$` is the pattern both specs give. Three validators disagree about
whether it accepts the source alias `"sales\n"`:

| Reader | Verdict | Why |
| --- | --- | --- |
| ECMA-262 — what JSON Schema specifies, and what a browser-based editor runs | **refuses** | `$` matches only at end of input |
| Pydantic, via `rust-regex` | **refuses** | same |
| Python's `jsonschema` library | **accepts** | implements `pattern` with Python's `re`, whose `$` also matches *before a trailing newline* |

That alias would put a newline into a worksheet name. Nothing in this project can fix the third,
so those cases are marked as a dialect divergence in the corpus, exempted from the agreement
assertion, and given their own test pinning which side says what. A first draft of the alias check
used Python's `re` and had the same bug; it now goes through `pqx_common.names`, whose `\A`/`\Z`
anchors give Python the ECMA-262 meaning.

### 3. `format: date-time` is annotation-only

Without `rfc3339-validator` installed beside `jsonschema`, a stock validator accepts
`"config_modified_utc": "last Tuesday"`. The models refuse it, so the corpus reported a divergence
that was really a missing optional dependency. **Any consumer of this schema — the web editor
included — needs the equivalent**, or the schema is quietly weaker than it reads. It is now a
declared dependency and a test asserts the checking is really on.

### 4. `FastExcel` consumes nothing until `save()`

Gate 0a read flat peak RSS across sheet counts as constant memory, and it is — the writer adds
about 23 KiB per sheet and nothing per row. But measuring *when* it consumes shows every frame
handed over stays reachable until `save()` returns, so **peak memory is the sum of the frames**,
not one of them, and a lazy generator of `(name, frame)` pairs does not change that. The gate's
harness could not show this because it generated rows synthetically instead of slicing frames.

---

## The questions

### Phase N — names

**Q1. Where do the name rules live?** `pqx-common`, not `pqx-staging`. Putting them with the code
that moves files would make `pqx-plan` depend on that package to validate what it renders. The
diagram already draws `pqx-common -> pqx-plan`.

**Q2. Which path-length limit binds?** SMB's 260, not Azure's 1024, **even for an Azure-only
destination**.

| Option | Consequence |
| --- | --- |
| Validate against the destination actually configured | A profile repointed at a share next quarter finds a year of stable names unwritable. |
| **Always the tightest (chosen)** | A name legal today stays legal wherever the output moves. |

The Azure limit is exposed as a constant and can be passed deliberately; it is never the default.

**Q3. What happens when a suffixed sheet name would exceed 31 characters?** The suffix **displaces**
the stem rather than extending past the limit, and each candidate is checked against *every* name
already claimed — truncating two different long stems can land on one prefix. The candidates are
valid by construction, so nothing re-validates them; a test asserts that invariant instead of a
branch no input could reach.

### Phase D — the writer

**Q4. What does the multi-sheet writer take?** `Iterable[tuple[str, pl.DataFrame]]`, drained before
anything is written. Given finding 4, a lazy outer iterable buys no memory, and draining first is
what lets a bad name on the tenth sheet be refused before a half-written workbook reaches the
destination.

**Q5. Is `rustpy_xlsxwriter.validate_sheet_name` enough?** No, and measured rather than assumed: it
accepts `History`, a name wrapped in apostrophes, and names holding a tab, a newline or a NUL. The
project's own rule runs first — it produces the message that names the offending character — and is
a **strict superset** of the library's over a character corpus, which a test asserts rather than
assumes. The library's check stays as a backstop for the release that tightens its rule, and a
monkeypatched test drives that branch rather than leaving a line no input reaches.

**Q6. Why is `autofit` off?** Not memory. Gate 0a measured that at nothing detectable, correcting
the docstring that had said so. It is off for **determinism**: autofit sizes columns from the cells
actually present, so the same rows split differently produce different column widths, and a
verified export whose bytes depend on how it was partitioned is worse to explain than a narrow
column. `dedupe_strings` *is* off for memory — ~1.2 KiB per row, charged per sheet.

### Phase C — the planner

**Q7. How are the schema and the models kept in step?** A shared corpus of 96 documents both must
classify identically — not a generated-and-diffed comparison, which would test the representation
where the two legitimately differ in spelling (`unevaluatedProperties` against
`additionalProperties`, `if`/`then` against a `model_validator`). It caught three real divergences
before it was finished (findings 2 and 3, plus the models typing `year_split_months` as a bare
tuple of ints where the schema constrains members and uniqueness).

**Q8. Where does the corpus live?** In the package, not beside the tests, because
`export-pipeline-spec.md` makes it the fixture set for the web configuration editor — a separate
tool in another language. `tools/build_config_corpus.py` exports it to
`docs/export-config-corpus.json`, and a test fails when that file goes stale.

**Q9. What precision are bucket counts keyed at?** ⚠️ **Not the base period.** A `year` base asks for
**month**-precision counts, because subdividing an oversized year needs the months inside it and a
count keyed by year cannot produce one. `required_count_precision` states the rule; the planner
rolls the fine counts up itself and keeps the finer numbers for the moment a year turns out not to
fit. A first draft took the base period's own precision and silently double-counted when a caller
supplied both grains. Counts at the wrong precision are now refused.

**Q10. In what order do a profile's sheets go, across sources?** **Period first, then source.** The
spec does not say so outright; it states the consequence only this ordering produces — that a bucket
too expensive for one workbook makes *the period* span workbooks, and that a workbook may hold only
some of the profile's sources for a period. Grouping by source would make each *source* span
workbooks and put no two sources' 2025 near each other.

**Q11. How is a workbook labelled when its sheets cover calendar at different grains?** At the
**coarsest grain present**. Fan-in makes mixed precision ordinary — one source's 2025 subdivided
into quarters, another's a single year sheet, both in one workbook. A first draft built a span from
the two endpoints and raised, because a span may not mix precisions. The coarsest grain is the only
one every span can honestly be stated at, and exact coverage belongs in the manifest.

**Q12. What does the fingerprint cover?** Everything, deliberately. Adding a configuration field
rebuilds the export once, which is cheaper than maintaining a list of digest-relevant fields and
much cheaper than being wrong about one entry in it: a field wrongly excluded produces a stale
export that every check calls current. SHA-256 rather than the dataframe hasher, which is versioned
and part of the digest contract the sidecar and manifest share — sharing it would make a hasher
upgrade look like a configuration change.

**Q13. What happens to an oversized year whose monthly buckets are still too large?** It does **not**
stay whole. The finest declared grid is used anyway, the months that fit are emitted, and
`oversized_period` applies to the ones that do not — which is what `partitioning-spec.md` says, and
what a first reading ("no grid fits, so keep the year") got wrong.

**Q14. Do subdivided year buckets merge?** No, twice over. They never merge with a neighbouring year
(the spec forbids it) and never merge back with each other inside their own year, which would undo
the split that was needed to make them fit. They are marked *sealed* and greedy merging flushes
around them.

---

## Not decided — still yours

- **Gates 0e and 0f** remain unrun. Nothing built here touches them.
- **`manifest.ResolvedConfiguration`** is still `dict[str, JsonValue]`. Swapping it for
  `ExportConfig` is a one-line type change, but gate 0c's round-trip test deliberately feeds it an
  arbitrary JSON object with mixed numbers and a null, and narrowing that proves less. The swap
  belongs with the code that actually builds a manifest from a plan, where the test can be rewritten
  to mean something rather than merely to compile.
- **The vectorised date decoder** (flagged in the Phase B record) is still the one deliberate debt.
  It belongs in `pqx-calendar` beside the scalar path, property-tested against it, not in
  `pqx-pipeline` where it would become a second statement of the century-window rule.
- **`balanced` planning is built but not wired.** `plan_balanced_sheets` and
  `minimum_balanced_workbooks` exist and are tested; nothing yet chooses between them and the
  calendar path, because that choice belongs to the code that builds a manifest.
- **The 150M-cell ceiling** is still a proposal, unchanged from the Phase 0 record.
