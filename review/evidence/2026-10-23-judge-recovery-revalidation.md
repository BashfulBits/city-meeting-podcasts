# Judge recovery revalidation — October 23, 2026

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
