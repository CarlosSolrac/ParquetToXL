# Phases F, G and N (staging) — verification, reporting and file movement

The last three phases built in this session. Recorded together because each is a leaf: nothing
depends on them yet, and what they wait for is Phase E.

**What shipped**

| Phase | Package | Tests | State |
| --- | --- | --- | --- |
| F | `pqx_frame.…combine`, `pqx_verify.fragments` | 68 | Done |
| G | `pqx_report.{model,render}` | 48 | Done |
| N (second half) | `pqx_staging.{scratch,transfer,lease}` | 68 | Done |

Workspace: **1503 tests, 100% statement and branch coverage** over 2,526 statements, green under
pyright strict, mypy strict, the declaration checker and pre-commit.

---

## ⚠️ Three decisions that are yours, and one is time-sensitive

### 1. The sidecar v2 → v3 bump breaks every existing sidecar file

`export-pipeline-spec.md` requires `SidecarDocument` to gain `created_utc` (T2) and
`SIDECAR_SCHEMA_VERSION` to go 2 → 3, with **a v2 file refused rather than migrated**. That means
every sidecar on disk today stops loading and is regenerated on the next run.

**Not built, deliberately.** It is small, it is well specified, and it is the one change in the
remaining work that invalidates data that already exists. Three things make it yours to time:

| Question | Options | Recommendation |
| --- | --- | --- |
| When to land it | With Phase E, which is the first code that writes a T2 / **or** on its own now | **With Phase E.** A bump that lands before anything writes the new field means one release where every sidecar is regenerated and nothing yet reads what was added. |
| Is regeneration free | Re-hashing every source is the cost | Worth confirming against your largest profile before the release, not after. The selection rule already treats an unreadable sidecar as stale, so the mechanism is safe; the question is only how long the first run takes. |
| Where `UtcDatetime` lives | It is in `pqx-plan` today, and `pqx-sidecar` may not depend on `pqx-plan` | **Move it to `pqx-frame`**, which both already depend on. Duplicating the validator would give the project two statements of "a recorded instant must carry a zone". |

`created_utc` should be a **required keyword argument** to `SidecarStoreBase.write`, not a default
read from the clock inside it — matching `RunReport.generated_utc` and `Lease.acquired_utc`, both of
which are injected so nothing here reads a clock and no test depends on when it ran.

### 2. The vectorised date decoder — flagged for the third time

Still the one deliberate debt. It belongs in `pqx-calendar` beside the scalar path,
property-tested against it — **not** in `pqx-pipeline`, where it would become a second statement of
the century-window rule that only one of the two has a test matrix for.

### 3. Gates 0e and 0f remain unrun

Nothing built this session touches them, but two things now depend on what they would say:

- **0e (is the scratch root tmpfs?)** decides whether `pqx_staging.scratch`'s free-space precheck
  is also a memory precheck. The module says so in prose because it cannot check it.
- **0f (Azure round trip)** would confirm that there is no atomic rename, which
  `pqx_staging.transfer` already assumes and documents.

---

## The questions

### Phase F — verification

**Q1. Where does `combine` live?** On the hasher. It is a property of the aggregation, not of the
planner or the verifier, and putting it anywhere else would let a hasher change and its arithmetic
drift apart.

**Q2. What does `combine` refuse?** Records disagreeing on `identifier`, `version` or `scope` — an
aggregate from a different hasher is not a term in the same sum — and a set where *some* carry a row
digest and others do not, because summing what is there would describe fewer rows than the result
claims.

**Q3. One reader call per fragment, or per workbook?**

| Option | Consequence |
| --- | --- |
| Per workbook | The reader concatenates every sheet, and a fan-in workbook's sheets hold different sources with different columns. Also, restoring a trailing all-null run is only well defined for one sheet. |
| **Per fragment (chosen)** | Correct for fan-in, and restoration is well defined everywhere. Costs a re-parse of the workbook per sheet. |

**Chosen: per fragment**, and the cost is designed around rather than absorbed — the reader's own
thread pool no longer helps, so parallelism moves up to `verify_source`, one `fastexcel` handle per
fragment, which is also what avoids the `Already borrowed` a shared handle raises.

**Q4. Does `verify_manifest` read the sidecars itself?** No — it takes the per-source schemas.
Locating and reading published sidecars needs the destination layout, the credentials and knowledge
of what has been staged, all of which belong to the pipeline. Putting remote path discovery inside
the package whose job is a judgement about bytes already in hand would be the wrong seam.

**Q5. Row counts.** ⚠️ Two expectations were wrong before the code was, and both are now tested in
the direction the reader actually behaves. A recorded count **above** what a sheet holds is not an
error: an all-empty row emits no `<row>` element, so the reader restores the shortfall — and
restoring rows that were never there changes the data and surfaces as a digest mismatch. A count
**below** what the sheet holds is a contradiction rather than a loss, and is refused.

### Phase G — reporting

**Q6. How many outcomes?** Four, not two. "It failed" is the least useful thing a report can say,
and the three failures reach the destination at different points: `planning-failed` means the
configuration and the data disagree, `write-failed` means the export could not be produced, and
`verification-failed` means it *was* produced and does not hold what it should.

**Q7. Is `verdict` typed?** No — a plain string rather than the `VerdictKind` literal. A report is
read long after the run, and refusing to load one because it names a verdict a later build
introduced would lose exactly the record that explains the upgrade.

**Q8. Template engine?** None. A dependency here would be inherited by everything that reports, to
save string building this shape does not need. The cost is that escaping is explicit, so every HTML
interpolation goes through `html.escape` and a test puts a `<script>` tag in a source alias.

### Phase N — staging

**Q9. Which path-length limit binds?** Answered in the earlier record: SMB's 260, always.

**Q10. How is a repeated source staged?** Once, deduplicated **by path**. Two paths that are the
same file under different spellings is a question about the store, and answering it here would mean
stat-ing everything twice to learn something the caller usually already knows. Two *different*
sources sharing a basename are refused rather than resolved — renaming one would make the staged
copy's name disagree with the manifest's record of it.

**Q11. What is the lease?** ⚠️ **Not a mutex, and the code says so.** Neither SMB nor Blob offers
compare-and-swap, so two runs starting in the same instant can both acquire. It closes the realistic
case — someone launching the job twice — not the theoretical one. A lease file this build cannot
parse is refused rather than treated as absent: it still belongs to a run that is probably still
working.

**Q12. How long is the TTL?** Six hours, and it is a **ceiling on run length**, not a heartbeat
interval — nothing renews a lease mid-run today. Worth knowing before raising it, and worth
revisiting if a profile ever takes longer than that to export.

---

## Two more things found by writing the test rather than the code

- **`tempfile.gettempdir()` caches.** It resolves once and returns that answer for the life of the
  process, so setting `TMPDIR` afterwards — including after any library has called it — does
  nothing. A docstring claiming it is read "at call time" was half wrong. A caller that needs to
  choose its scratch root at runtime passes `root` explicitly, which is why every function takes one.
- **Scratch cleanup needed a second guard.** `ignore_errors=True` covers failures met while walking
  the tree and *not* `rmtree` itself refusing — and the cleanup runs in a `finally`, so an `OSError`
  there replaced the exception the run was already raising. A test with a filesystem that refuses
  the call found it.

---

## What is left

**Phase E — `pqx-pipeline`.** `sidecar_stale` and `excel_stale`; `read_parquet` and `build_sidecar`;
`observe` producing `SourceShape`; `write_profile` producing the manifest. It is the only place that
chains stages and touches everything, and it carries the sidecar v3 bump above.

**Phase H — CLI and end to end.** `pqx status|sidecar|plan|write|verify|report|publish|run`.

**Phase C's last piece:** replacing `manifest.ResolvedConfiguration` — still a
`dict[str, JsonValue]` stand-in — with `ExportConfig`, and building the manifest from a plan. Both
belong with Phase E, where the test that guards them can be rewritten to mean something rather than
merely to compile.
