# Judge recovery revalidation — October 23, 2026

**Current status: approved cleanup applied and verified; workflow states restored.**

Step 2's matched-panel evaluation is complete in PR #2528; its CI checks passed. The maintainer
then approved automatic resolution through the retained panels and an independent adjudicator,
longer-context JEV on disagreements, and a small audit sample. See review/53's dated amendment.
This does not authorize a production cleanup or authority-mode flip by itself.

Step 3 has begun with a fresh read-only snapshot of the 32 sources in the earlier recovery
proposal. All 32 have changed since October 20. Reads used a B2 credential verified to have no
write/delete capabilities; source snapshots and 1,601 registry records were read separately.
There were zero registry read errors and 27 missing registry records. Missing is unknown, not clean.

## Updated local proposal

| Classification | Judgments | Pending references |
|---|---:|---:|
| Sibling request predates cleanup | 6,079 | 0 |
| Adjudicator request predates cleanup | 215 | 0 |
| Newer adjudication selected using recorded old sibling votes | 371 | 42 |
| **Total** | **6,665** | **42** |

The last category is inferred from retained sibling rows present by request staging; it is not
proof of a complete historical selection trace. The audit now includes sibling votes regardless
of quote validity, matching the approved non-gating sibling quote policy. Choose-group dependencies
are not inferred. The 42 references are adjudicator pointers, not Worker jobs to cancel.

The local diffs affect 4,823 subject entries. Verification preserves every JEV row in these
sources (128,695), the two original calibration adjudications, 11,724 retained chat rows,
subject metadata, and non-judging episode content. The retained chat rows comprise 11,716
sibling rows and eight adjudicator rows (two original and six newer). All 549 pending references with unknown
registry provenance remain untouched. These are scoped snapshot counts, not whole-catalog totals.

No retained chat rows had request creation after the typed-v3 merge at 04:41:19 UTC; that timing
check alone would not certify request revision. Recovering the cohort still requires approved
cleanup followed by normal shadow collection. This report makes no population calibration claim.

## Preserved paired evidence

The wider reread found six post-October-19 sibling/adjudicator pairs, covering five moment subjects
in one adjudicator packet. Both request creation and response creation follow the October 19
cleanup. All six sibling quotes and adjudicator quotes pass their stored checks. They comprise
three calibration gate questions (two agreements), one calibration validate question (agreement),
and two contested validate questions (both agreements). These six pairs are preserved.

This supersedes the earlier zero-pair finding only for this broader targeted snapshot. It is not a
new typed-v3 cohort, a population estimate, or sufficient independent evidence for thresholds.
Details remain external in retained-pairs.json and retained-pair-summary.json.

## Review and apply boundary

The external local artifact directory is `/private/tmp/judge-recovery-2026-10-23/`:
README.md, manifest.json, complete-proposal-summary.json, complete-proposals/, revalidation.json,
and proposal-checksums.json. It retains the current source snapshots, registry reads, exact removed
rows/pointers, source ETags and hashes, entry before/after hashes and non-judging episode hashes.
Bulk source data and generated proposals are kept outside Git.

Separate approval is required before remotely archiving/removing the proposed rows and pointers.
An approved apply must archive exact removals, re-read and rebase each source-owned block, preserve
all intervening additions, and use conditional writes. Skip changed/ambiguous entries; never upload
a saved source or proposed entry wholesale. Do not delete registry records, cancel Worker jobs,
re-ask JEV, or dispatch workflows as part of this recovery proposal.

## Approved archive completed; live cleanup awaits writer coordination

The maintainer approved the cleanup and requested progress notifications for fresh collection
and calibration. All 6,665 proposed judgments and 42 pending references were archived to 32
content-addressed objects under `state/archive/judge-recovery-2026-10-23/`; every upload was
read back and hash-verified. The local approved-archive-summary.json retains exact keys/checksums.
No live source objects have been modified.

The apply review exposed an incorrect assumption in the proposal: B2 does not enforce conditional
writes (`citypods/storage/s3.py` explicitly marks it non-CAS). The existing maintenance mutex
coordinates chapter work, not all judge/source writers. Therefore the conditional-write apply
plan above is insufficient on this backend. A separate temporary maintenance window has been
requested: pause state-writing workflows, let active runs finish without cancelling them,
re-read/rebase the approved removals, verify while quiescent, and restore prior workflow states.
The archive is ready; live removal must not proceed while concurrent writes remain possible.

## Cleanup applied and verified

The maintainer approved the temporary maintenance window. The final window coordinated 39
previously active B2-access workflows, waited for source writers to be quiescent, and restored
those workflows afterward. The initial wider pause was also fully restored before source edits.
City discovery was excluded after verifying its queued run's actual code only reads episode
source state. No runs were cancelled. All 71 original workflow enabled/disabled states were
compared afterward and matched their original settings.

The Cloudflare v2 executor continued running: it writes provider replies under `results/` and
handles job payload/result storage, not `state/sources/<source>/episodes.json`. Those keys and
Worker job records were not changed by this cleanup. The maintenance guard concerned writers
to the episode source objects, rather than every writer to the B2 bucket.

All 32 source updates passed immediate remote readback verification. Exactly 6,665 judgments
and 42 pending references were removed; there were zero changed/ambiguous-entry skips. Each
update reread current source data, removed only exact approved row/reference matches, and
archived its exact delta before writing. No saved proposal entry or stale snapshot was uploaded.
An independent before/after multiset audit verified all other rows and foreign content unchanged.
It counted 128,695 preserved JEV judgments and two preserved original calibration judgments.
All six previously identified paired cases were checked and preserved as exact rows.

Per-source before/after hashes, exact-removal archive keys, counts, workflow-state restoration,
and independent verification are in the external `apply-live/` subdirectory of the recovery
artifact. The completion receipt was also uploaded and hash-verified:
under `state/archive/judge-recovery-2026-10-23/`, with filename
`completion-fe8ef2fd6d2697e0833113f866ba3f652484003568d102ef3bb228e92524e833.json`.

Normal scheduled shadow collection can now re-plan the missing chat cells using request
revision 3. This recovery re-asks no JEV cells, dispatches no manual workflow or Worker jobs,
changes no quota policy, and does not activate admission authority. The existing hourly monitor
will report meaningful fresh-collection progress and calibration evidence/proposals; cleanup
completion is not itself sufficient evidence to graduate tags or moments.
