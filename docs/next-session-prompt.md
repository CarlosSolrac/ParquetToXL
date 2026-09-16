# Prompt for the next session

Paste the block below into a fresh session. It assumes nothing from the conversation that built the
pipeline — `CLAUDE.md` and `docs/backlog.md` carry everything it needs.

---

```
Read CLAUDE.md and docs/backlog.md before doing anything.

Every phase of docs/export-pipeline-spec.md is built and merged to main. The branch
`vectorised-bucketing` is finished, green and NOT merged. Read
docs/decisions/2026-09-15-distinct-value-bucketing.md before touching either area it
covers -- it records six decisions with their alternatives, and two of them look wrong
until you read why.

What the branch did: bucketing decodes a date column's DISTINCT values through the
existing scalar decoder rather than calling it once per row, which is 50x-80x over two
million rows and leaves pqx-calendar's decoder the only statement of the century-window
rule. A source is now converted for Excel once per run instead of twice -- describing it
converted the whole frame and threw the result away. And `pqx --help` finally says that
verify cannot reach an abfs:// destination.

Two traps are written down there and are easy to reintroduce. Zoned timestamp columns
are deliberately NOT collapsed to their day, because Polars converts timezones through
its bundled database and decode_cell converts through this machine's, and they disagree
(Africa/Casablanca, October 2026) by enough to put a row in the wrong bucket silently.
And the row arrangement is computed over the source's values, never the converted ones,
because ToExcel writes True as -1.0 and would reverse a boolean sort key.

The branch is finished and green: six commits, 1,725 tests, 100% statement and branch
coverage. Nothing is half-done on it. Ask me whether to merge it before starting
anything new.

Before proposing any lazy frame -- scan_parquet, .lazy(), the streaming engine --
read docs/decisions/2026-09-16-lazy-frames.md. The pipeline is eager everywhere on
purpose, and the reasons are not the ones the specs give: the additive identity makes
predicate and projection pushdown unavailable, so a lazy plan here has almost nothing
to optimise, and lazy execution surfaces failures at collect() rather than at the row
that caused them, which is what the refusal messages are for. That record lists what
would properly reopen it, and none of the conditions is "an implementer found a use".

Backlog item 3 carries the related work: staging each source's converted frame to
Parquet so the write phase slices it from disk. Investigated, measured, deliberately
not built -- it bounds the write phase but not the peak, since the conversion produces
the whole frame in memory before staging could happen. An eager row-group alternative
is described there and is unbuilt.
packages/pqx-pipeline/tests/test_staged_parquet.py already proves every property the
approach would need.

Backlog items 2 and 3 are blocked on decisions rather than on code -- bring me the
options rather than choosing for me. Item 6 needs infrastructure neither of us has here.

House rules that no linter will tell you, all of them in CLAUDE.md: every variable
annotated before its first binding, Literal and never Enum, Pydantic config in class
keywords repeated on every subclass, \A and \Z instead of ^ and $, injected clocks,
220-column lines, and 100% branch coverage as a merge gate. Run the three CI commands in
CLAUDE.md before telling me anything is done -- reproduce them, do not predict them, and
read the hook status lines rather than the tail of their output. All three are green on
Windows and on Linux; if pre-commit fails for you on a clean tree, say so rather than
working around it.

Commit in the repository's prose style, explaining why. Do not open a pull request unless
I ask. If a test proves the specification wrong, tell me rather than editing either.
```

---

## If you want a different starting point

Swap the numbered list for one of these:

- **Merge it** — "Run the three CI commands, show me the diff against main, and tell me what you
  would want a reviewer to look at hardest before it merges."
- **Cost the eager staging path** — "Read backlog item 3 and 2026-09-16-lazy-frames.md §5, then
  tell me what aligning a staged file's row groups to planned sheets would actually take, and what
  it would save. Measure before proposing; do not write the wiring."
- **Decisions first** — "Start with backlog items 2 and 3. Do not write the wiring; put the two
  naming and ordering questions to me with your recommendation, and the 150M-cell ceiling with the
  measurement behind it."
- **Infrastructure** — "Start with backlog item 6. Write the gate 0f harness under `gates/`
  following `gate_0d_sort_memory.py` as the pattern, so it is ready to run the moment credentials
  exist. Do not guess what `adlfs` returns."
- **Hardening** — "Start with backlog item 7. Run the spec's end-to-end checklist and its six
  negative checks by hand against a real destination, and report what the commands actually did
  against what the specification says they should."
