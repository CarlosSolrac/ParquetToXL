# ParquetToXL

Convert Parquet sources into Excel workbooks whose contents can be **proved** to match the source,
long after the source is unreachable.

That last clause is the whole point. Anything can write a spreadsheet from a dataframe. This writes
one, records a digest of every sheet it wrote and of the source as a whole, and can later read the
workbooks back and confirm they still say what the source said — with the Parquet files deleted and
the machine that made them gone.

## How it proves it

**Fragment digests sum to the whole.** Every sheet's digest is a modular sum over its cells; the
digests of a source's sheets add, mod 2^128, to the digest of the source. So verification can read
back N workbooks, digest each, and check the source as a whole without reassembling it and without
opening the Parquet the sidecar exists to avoid re-reading.

That identity is why **partitioning must be total and disjoint** — every row in exactly one sheet —
and why row filters, column projection, and stray temporary columns are all out of scope. Each of
them would change the sum.

**Verification runs from scratch, with the sources already deleted.** Its independence is
structural rather than asserted: it needs only the workbooks, the manifest published beside them,
and the sidecar recording the dtypes to read them back with.

## Quickstart

Requires Python 3.13+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --locked --all-groups --all-packages
uv run pqx --config export.json status
```

A configuration names where output goes, where sidecars live, the Parquet sources, and one or more
*profiles* describing how to slice them into workbooks.
[docs/export-config.schema.json](docs/export-config.schema.json) is the authority on its shape, and
[docs/export-config-corpus.json](docs/export-config-corpus.json) holds worked examples that the
test suite asserts the models still accept.

## The eight verbs

In lifecycle order, which is the order they are learned in. Every verb takes the same switches and
acts on every profile unless `--profile NAME` narrows it.

| Verb | Does |
| --- | --- |
| `status` | Report what selection decides for a profile, over remote metadata only |
| `sidecar` | Re-describe a profile's sources, and publish nothing |
| `plan` | Show the workbooks and sheets a run would produce, and produce none of them |
| `write` | Write the workbooks into scratch without publishing, for inspection under `--keep-scratch` |
| `verify` | Check a published export against the manifest published beside it |
| `report` | Print the most recent published report for a profile |
| `publish` | Run a profile end to end, **regardless** of what selection says |
| `run` | Run a profile end to end, **honouring** selection |

Exit codes are `0` ok, `1` something is wrong with the data, `2` usage error, `3` refused.

⚠️ **`verify` reads workbooks through a library that opens local filenames**, so it reaches a local
or SMB-mounted destination and not an `abfs://` one. `pqx --help` says so too.

## Layout

A uv workspace of eleven packages. The graph is the **runtime** dependency direction; `pqx-testing`
is dev-only and depended on by no runtime package, which is also why it sits outside
`coverage.source`.

```text
pqx-common --+-> pqx-frame --+-> pqx-sidecar --+
             |               |                 +-> pqx-verify --+
             +-> pqx-excel --+-----------------+                |
             +-> pqx-calendar -> pqx-plan --+-------------------+-> pqx-pipeline
             +-> pqx-staging --------------+                    |
             +-------------------------------> pqx-report ------+
```

| Package | Holds |
| --- | --- |
| `pqx-common` | `ZPath`, logging, and what is a legal worksheet or file name at our destinations |
| `pqx-frame` | Hashing, column and frame metadata, the ToExcel conversion |
| `pqx-excel` | Writer registry, the multi-sheet workbook writer, the fast reader |
| `pqx-sidecar` | The sidecar document and the store that reads and writes it |
| `pqx-calendar` | Date columns, decoding, period keys, year grids, period labels |
| `pqx-plan` | Configuration, capacity, partitioning, allocation, naming, manifest, receipt |
| `pqx-verify` | Fragment, source and manifest verification over written workbooks |
| `pqx-report` | The run report and its JSON, Markdown and HTML renderers |
| `pqx-staging` | Scratch, free-space checks, file transfer, the lease |
| `pqx-pipeline` | Staleness, ingest, bucketing, write, the orchestrator, the `pqx` CLI |
| `pqx-testing` | Dev-only fixtures and the shared pytest plugin — not in the graph above |

## Where to read next

| You want | Read |
| --- | --- |
| The pipeline, its phases, and the verification checklist | [docs/export-pipeline-spec.md](docs/export-pipeline-spec.md) |
| Partitioning, naming, capacity, the ceilings | [docs/partitioning-spec.md](docs/partitioning-spec.md) |
| Inherited constraints: dtypes, round-trip safety, what is out of scope | [docs/library-spec.md](docs/library-spec.md) |
| Why something non-obvious is the way it is | [docs/decisions/](docs/decisions/) |
| What is left, and what has already been decided about it | [docs/backlog.md](docs/backlog.md) |
| The house rules no linter states | [CLAUDE.md](CLAUDE.md) |

The decision records are worth knowing about before changing anything: several of them exist
because a reasonable-looking change is wrong for a reason that is invisible from the code. Two
examples — timestamp columns read against a *named zone* are deliberately not collapsed to their
day, because Polars and Python disagree about `Africa/Casablanca`; and the row arrangement is
computed over the source's values rather than the converted ones, because the conversion writes
`True` as `-1.0` and would reverse a boolean sort key.

## Development

```bash
uv sync --locked --all-groups --all-packages   # fails if uv.lock drifted from a pyproject
uv run pre-commit run --all-files              # ruff, format, declarations, pyright, mypy
uv run pytest --cov --cov-report=term-missing --cov-branch --cov-fail-under=100
```

Those three are exactly what CI runs. Reproduce them rather than predicting them.

**Coverage is 100% statement and branch, and it is a merge gate.** New code with an uncovered
branch does not land. If a branch cannot be reached, that is evidence the branch should not exist —
delete it rather than exempting it.

Tests whose module docstring says **"Frozen tests"** are the specification. If one looks wrong,
say so rather than editing it.

## Status

Every phase of [docs/export-pipeline-spec.md](docs/export-pipeline-spec.md) is built: 1,725 tests
at 100% statement and branch coverage, CI green. It has never been run against a real SMB share,
blob container, or Spark image — see [docs/backlog.md](docs/backlog.md) items 6 and 7, which are
the honest gap between "tested" and "proven in place".
