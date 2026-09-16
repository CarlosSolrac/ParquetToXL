# Prompt for the next session

Paste the block below into a fresh session. It assumes nothing from the conversation that built the
pipeline — `CLAUDE.md` and `docs/backlog.md` carry everything it needs.

---

```
Read CLAUDE.md and docs/backlog.md before doing anything.

Every phase of docs/export-pipeline-spec.md is built and merged to main. Work is in
progress on the branch `vectorised-bucketing`, which has two commits on it and is not
merged. Read docs/decisions/2026-09-15-distinct-value-bucketing.md before touching
either area it covers -- it records six decisions with their alternatives, and two of
them look wrong until you read why.

What those two commits did: bucketing decodes a date column's DISTINCT values through
the existing scalar decoder rather than calling it once per row, which is 50x-80x over
two million rows and leaves pqx-calendar's decoder the only statement of the
century-window rule. And a source is now converted for Excel once per run instead of
twice -- describing it converted the whole frame and threw the result away.

Two traps are written down there and are easy to reintroduce. Zoned timestamp columns
are deliberately NOT collapsed to their day, because Polars converts timezones through
its bundled database and decode_cell converts through this machine's, and they disagree
(Africa/Casablanca, October 2026) by enough to put a row in the wrong bucket silently.
And the row arrangement is computed over the source's values, never the converted ones,
because ToExcel writes True as -1.0 and would reverse a boolean sort key.

What is left on the branch, in order:

1. Stage the converted frame to Parquet per source in scratch and have _slices read each
   sheet back instead of slicing a resident frame. Observation.frame is held through
   planning and the whole write phase, which is the residency backlog item 3's ceiling
   is about. Two things must be tested BEFORE _slices is rewired, and if either fails,
   stop and report rather than working around it: that a Parquet round trip preserves
   every dtype ToExcel emits, and that it preserves row order exactly -- _slices cuts
   with a positional cursor, so order is load-bearing. Also check first whether
   DataFrameHasherBinaryAggregateHash can be fed in chunks; expected_whole hashes the
   entire frame, and if it cannot stream, say so rather than inventing an incremental
   hasher. Do NOT derive expected_whole by summing the fragment digests: it is a
   cross-check on them, and deriving it from them would make it always agree.

2. Backlog item 4's second half. `pqx verify` works against a local or SMB-mounted
   destination and fails against abfs://, and nothing says so. cli.py uses argparse with
   no subparsers -- the verb is one positional constrained by choices -- so the text goes
   in an epilog rather than per-verb help. fast_excel_reader's docstring should say it
   too; CLAUDE.md already claims it does and it does not.

Items 2 and 3 are blocked on decisions rather than on code -- bring me the options rather
than choosing for me. Item 6 needs infrastructure neither of us has here.

House rules that no linter will tell you, all of them in CLAUDE.md: every variable
annotated before its first binding, Literal and never Enum, Pydantic config in class
keywords repeated on every subclass, \A and \Z instead of ^ and $, injected clocks,
220-column lines, and 100% branch coverage as a merge gate. Run the three CI commands in
CLAUDE.md before telling me anything is done -- reproduce them, do not predict them. Note
that `pre-commit run --all-files` fails on Windows for a reason that predates this work:
gates/gate_0a_sheet_memory.py and gates/gate_0d_sort_memory.py use resource.getrusage,
which is POSIX-only. Confirm it against a clean tree rather than assuming, and do not fix
it.

Commit in the repository's prose style, explaining why. Do not open a pull request unless
I ask. If a test proves the specification wrong, tell me rather than editing either.
```

---

## If you want a different starting point

Swap the numbered list for one of these:

- **Merge what is there** — "The branch is at a sensible stopping point. Run the three CI commands,
  show me the diff against main, and tell me what you would want reviewed before it merges."
- **Decisions first** — "Start with backlog items 2 and 3. Do not write the wiring; put the two
  naming and ordering questions to me with your recommendation, and the 150M-cell ceiling with the
  measurement behind it."
- **Infrastructure** — "Start with backlog item 6. Write the gate 0f harness under `gates/`
  following `gate_0d_sort_memory.py` as the pattern, so it is ready to run the moment credentials
  exist. Do not guess what `adlfs` returns."
- **Hardening** — "Start with backlog item 7. Run the spec's end-to-end checklist and its six
  negative checks by hand against a real destination, and report what the commands actually did
  against what the specification says they should."
