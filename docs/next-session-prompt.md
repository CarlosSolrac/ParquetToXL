# Prompt for the next session

Paste the block below into a fresh session. It assumes nothing from the conversation that built the
pipeline — `CLAUDE.md` and `docs/backlog.md` carry everything it needs.

---

```
Read CLAUDE.md and docs/backlog.md before doing anything.

Every phase of docs/export-pipeline-spec.md is built and merged to main; 1,676 tests at
100% statement and branch coverage, CI green. What is left is in docs/backlog.md, in
priority order, each item with the decisions already taken about it.

Start with backlog item 1, the vectorised date decoder. It is the only pending item that
changes whether the pipeline is usable at the scale it was designed for: bucketing decodes
one value at a time and now sits on the run's critical path.

Build it in pqx-calendar beside the scalar path -- NOT in pqx-pipeline, where it would
become a second statement of the two-digit-year century window. Property-test it against
decode_cell over every dtype, nulls, and the window boundaries, then swap the single call
site in ordered_by_bucket. Keep the scalar path; it is the oracle.

Then take backlog item 4's second half (say in the CLI's help that `verify` cannot reach an
Abfs destination), since it is small and the constraint is currently silent.

Items 2 and 3 are blocked on decisions rather than on code -- bring me the options rather
than choosing for me. Item 6 needs infrastructure neither of us has here.

House rules that no linter will tell you, all of them in CLAUDE.md: every variable annotated
before its first binding, Literal and never Enum, Pydantic config in class keywords repeated
on every subclass, \A and \Z instead of ^ and $, injected clocks, 220-column lines, and 100%
branch coverage as a merge gate. Run the three CI commands in CLAUDE.md before telling me
anything is done -- reproduce them, do not predict them.

Work on a branch, commit in the repository's prose style, and do not open a pull request
unless I ask. If a test proves the specification wrong, tell me rather than editing either.
```

---

## If you want a different starting point

Swap the third and fourth paragraphs for one of these:

- **Decisions first** — "Start with backlog items 2 and 3. Do not write the wiring; put the two
  naming and ordering questions to me with your recommendation, and the 150M-cell ceiling with the
  measurement behind it."
- **Infrastructure** — "Start with backlog item 6. Write the gate 0f harness under `gates/`
  following `gate_0d_sort_memory.py` as the pattern, so it is ready to run the moment credentials
  exist. Do not guess what `adlfs` returns."
- **Hardening** — "Start with backlog item 7. Run the spec's end-to-end checklist and its six
  negative checks by hand against a real destination, and report what the commands actually did
  against what the specification says they should."
