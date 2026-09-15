# ParquetToXL — working notes for Claude

Convert Parquet sources into Excel workbooks whose contents can be *proved* to match the source,
long after the source is unreachable. A uv workspace of eleven packages; every phase of
`docs/export-pipeline-spec.md` is built.

**Read `docs/backlog.md` first.** It is what is left, why, and what has already been decided about
each item.

---

## Verification — run all of it before saying anything is done

```bash
uv sync --locked --all-groups --all-packages   # fails if uv.lock drifted from a pyproject
uv run pre-commit run --all-files              # ruff, format, declarations, pyright, mypy
uv run pytest --cov --cov-report=term-missing --cov-branch --cov-fail-under=100
```

These three are exactly what `.github/workflows/ci.yml` runs — reproduce them locally rather than
predicting the outcome. CI runs on `pull_request` and on push to `main`, **not** on feature
branches, so a green local run is the only signal until a branch lands.

While iterating, narrow with `uv run pytest packages/<name> -q`; the full sweep takes ~75s.

Individual tools, if you need one alone:

```bash
uv run ruff check . && uv run ruff format .
uv run python -m tools.check_declarations packages tools   # needs explicit paths
uv run pyright
uv run mypy packages tools                                 # needs explicit paths
```

**Coverage is 100% statement *and* branch, and it is a merge gate, not a per-run one.** There is no
`fail_under` in `pyproject.toml` on purpose; `--cov-fail-under=100` is passed by CI. New code with
an uncovered branch does not land. If a branch cannot be reached, that is evidence the branch
should not exist — delete it rather than exempting it. (This has already caught two dead branches.)

---

## House rules that no linter states for you

**Every variable is annotated before its first binding.** `tools/check_declarations.py` enforces
it; no off-the-shelf linter does. Instance attributes count: `self.total = 0` needs `total: int` in
the class body. Exempt, because Python offers no syntax: comprehension and generator targets,
`except ... as`, walrus, imports, `def`/`class`, and function parameters.

```python
total: int = 0
row: Row
for row in rows:          # loop targets DO need a prior annotation
    ...
names: list[str] = [row.name for row in rows]   # the comprehension target `row` does NOT
```

**Closed vocabularies are `Literal` + `Final`, never `Enum`.** An enum member is a bare class-body
assignment, which the declaration checker rejects — and it would serialise as its member name
rather than as the value written to the file.

**Pydantic config goes in class keywords, not `model_config`.** `model_config = ConfigDict(...)` is
a bare binding the declaration checker rejects.

```python
class Thing(BaseModel, extra="forbid", frozen=True): ...
class Subclass(Thing, extra="forbid", frozen=True): ...   # repeat on EVERY subclass
```

The repetition is not redundant: pyright refuses "a non-frozen class cannot inherit from a class
that is frozen", and the keywords do not inherit.

**Regexes anchor with `\A` and `\Z`, never `^` and `$`.** Python's `$` also matches before a
trailing newline, so `^sales$` accepts `"sales\n"`. This was a real bug twice — in
`pqx_common.names` and again in `ExportConfig.check_source_aliases`.

**Clocks are injected, never read.** No library function calls `datetime.now()`. Instants arrive as
required keyword arguments (`created_utc`, `generated_utc`, `acquired_utc`, `RunInstants`). Only
`pqx_pipeline.cli.main` reads the clock, and it takes `now=` so tests can pin it. A fabricated
instant in the staleness rules is a rebuild that never happens or one that never stops.

**Paths are `UPath`, constructed with `ZPath` from `pqx_common.paths`.** The destination may be SMB
or Azure Blob. Two things are local-only and say so: the Excel writers and `fast_excel_reader`,
which hand `str(path)` to a library that opens local filenames.

**No `print`.** Ruff's `T20` allows it only under `tools/` and `gates/`. Library and CLI output
goes to an injected `TextIO`.

**Style:** PEP 8 at a 220-character line length, Google docstrings, double quotes. Everything is
documented and strictly typed — classes, methods, algorithms and variables alike. If a dependency
ships no types, add a stub under `stubs/` (one directory for the whole workspace).

---

## Layout

```
pqx-common --+-> pqx-frame --+-> pqx-sidecar --+
             |               |                 +-> pqx-verify --+
             +-> pqx-excel --+-----------------+                |
             +-> pqx-calendar -> pqx-plan --+-------------------+-> pqx-pipeline
             +-> pqx-staging --------------+                    |
             +-------------------------------> pqx-report ------+
```

| Package | Holds |
| --- | --- |
| `pqx-common` | `ZPath`, logging, and `names` — what is a legal worksheet/file name at our destinations |
| `pqx-frame` | Hashing, column/frame metadata, the ToExcel conversion, `UtcDatetime` |
| `pqx-excel` | Writer registry, the multi-sheet workbook writer, `fast_excel_reader` |
| `pqx-sidecar` | `SidecarDocument` (schema v3) and the store that reads/writes it over `UPath` |
| `pqx-calendar` | Date columns, decoding, period keys, year grids, period labels |
| `pqx-plan` | `ExportConfig` + semantics, capacity, partitioning, allocation, naming, manifest, receipt |
| `pqx-verify` | Fragment/source/manifest verification over written workbooks |
| `pqx-report` | `RunReport` and the JSON/Markdown/HTML renderers |
| `pqx-staging` | Scratch, free-space checks, file transfer, the lease |
| `pqx-pipeline` | Staleness, ingest, bucketing, write, the orchestrator, the `pqx` CLI |
| `pqx-testing` | Dev-only fixtures and the shared pytest plugin |

`pqx-testing` is deliberately outside `coverage.source`.

---

## The ideas the code assumes you know

**The additive identity.** Fragment digests sum (mod 2^128) to the whole-source digest. That is why
partitioning must be total and disjoint, why row filters and column projection are out of scope,
and why a temporary column must never reach exported data — an extra column changes the digest.

**Four timestamps, not interchangeable.** T1 the source's mtime, T2 when the sidecar was written,
T3 when the configuration was modified, T4 when the export completed. Compared with `!=` and not
`>` where a restore could move a clock backwards, and always through `truncate_to_second` because
Azure reports whole seconds and local filesystems report finer.

**Two independent markers.** The sidecar marks *described*; the receipt marks *exported*. A run
that dies between them costs the export, not the hashing. Never let one claim the other's work.

**Ownership before deletion.** Reconciliation deletes everything in a profile's output directory
the manifest does not claim. It is safe only because the receipt proves the directory is ours, the
lease is held, and `delete_names` refuses anything that is not a single path segment.

**Verify before publish, from scratch, with the sources already deleted.** Verification's
independence is structural rather than asserted.

---

## Specifications

| File | Authority for |
| --- | --- |
| `docs/export-pipeline-spec.md` | The pipeline, the phases, and the verification checklist. Each phase carries a **Built** annotation |
| `docs/partitioning-spec.md` | Partitioning, naming, capacity, the marked notes on ceilings |
| `docs/library-spec.md` | Inherited constraints: dtypes, round-trip safety, what is out of scope |
| `docs/export-config.schema.json` | The JSON Schema the Pydantic models must agree with. `tools/build_config_corpus.py` regenerates `docs/export-config-corpus.json`, which asserts they do |
| `docs/decisions/*.md` | Every non-obvious decision, its alternatives, and why one won |

**When a test proves the spec wrong, report it — do not quietly edit either.** Tests marked
"Frozen tests" in their module docstring are the specification; if one looks wrong, stop and say
so. Several real findings came out of this loop already (a format-string traversal hole, three
divergent regex dialects, two configuration serialisation bugs).

When you finish a piece of work, annotate the spec section with what was built, and add a decision
record for anything non-obvious you decided.

---

## Git

Commit messages are prose, in the imperative, explaining *why*: what the change makes true, what it
prevents, what was considered and rejected. Look at `git log` before writing one. Never put a model
identifier in a commit message, PR body, code comment, or anything else pushed to the repository.

Do not open a pull request unless asked.
