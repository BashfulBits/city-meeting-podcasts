# Final P0 rule replay against the cached catalog — 2026-10-07

This is the final local replay of the maintainer-approved historical feed rules. It compares the
current working-tree feed definitions with 26,555 cached source-record entries from 42 source
namespaces. It is read-only with respect to provider and production data: it fetched nothing and
changed no stored record, UID, title, source namespace, or audio.

The output is a proposal until reviewed, merged by the maintainer, deployed, and checked against live
feeds. “Assignment” means one source record placed in one feed. A source record can be assigned to
more than one feed when the City held a genuine joint meeting, so assignment counts are not episode
counts.

## What the full replay says

| Measure | Before these local rules | With these local rules | Change |
|---|---:|---:|---:|
| Configured feeds | 173 | 224 | +51 |
| Feed assignments in cached catalog | 27,434 | 29,366 | +1,932 net |
| New feed placements | — | 1,962 | 1,939 distinct source/UID pairs |
| Removed feed placements | — | 30 | 30 source/UID pairs |
| Source/UID entries with no feed | — | 416 | See the disposition below |

The 30 removals are Fort Worth “Minority Leaders and Citizens Council” civic-program recordings
that had been incorrectly included in City Council. The feed rule no longer places them in Council;
the raw source records and audio remain intact.

The 416 unmatched entries break down as follows:

| Treatment | Entries | Meaning |
|---|---:|---|
| Not to pursue | 408 | User-approved small or non-meeting groups; keep their raw records, do not add a feed rule |
| Exact evidence still needed | 2 | Dallas Capital Bond CBTF recordings with the same ambiguous provider title; leave assignments unchanged |
| Archive-only visibility check | 6 | Addison media already approved for archive-only treatment; confirm hidden from feeds/search and retained at the original archive page |
| **Total** | **416** | Source/UID entries, not a guarantee of 416 distinct videos |

Of the 416, 365 came from the frozen 680-pattern baseline and 51 were additional cached entries outside
that original worksheet. All 680 patterns now have a recorded user-directed outcome. In the baseline,
515 patterns (1,661 source/UID listings) are assigned to a feed by the local rules, one pattern/listing
is archive-only, and 164 patterns (366 listings) are not to pursue. Two of the unmatched CBTF entries
have evidence cases open despite the current no-rule decision; their shared title is not enough to
safely select a feed. The remaining outside-baseline entries are one-off event/test media or approved
archive-only media.

The source-scoped unmatched list and plain-language disposition for every row are in
[`p0-final-unmatched-records-2026-10-07.csv`](p0-final-unmatched-records-2026-10-07.csv). Every added
and removed feed placement is listed in
[`p0-final-added-feed-assignments-2026-10-07.csv`](p0-final-added-feed-assignments-2026-10-07.csv)
and [`p0-final-removed-feed-assignments-2026-10-07.csv`](p0-final-removed-feed-assignments-2026-10-07.csv).
The exact replay output is retained in
[`p0-final-catalog-replay-2026-10-07.json`](p0-final-catalog-replay-2026-10-07.json).

## Historical disposition

The frozen worksheet has 680 title patterns and 2,028 source/UID listings. The final local choices
cover every row:

| Outcome | Title patterns | Source/UID listings |
|---|---:|---:|
| Public Info feed (one per city) | 201 | 464 |
| Fort Worth Speaker’s Podium | 69 | 138 |
| Dallas bond-program family | 17 | 30 |
| New named-body rules | 180 | 852 |
| Additions to existing body feeds | 39 | 145 |
| Already correctly assigned to an existing feed | 9 | 32 |
| Archive-only, already handled by an approved feature | 1 | 1 |
| Not to pursue | 164 | 366 |
| **Total** | **680** | **2,028** |

New named-body rules meet the five-distinct-UID cutoff, except Denton Bond Oversight Committee, which
the maintainer had explicitly approved earlier with four known UIDs. Additions to existing bodies do
not need to meet that new-body cutoff. Public briefings, public information and public-input sessions
share one Public Info feed per city. Fort Worth Speaker’s Podium remains its own curated series, and
Dallas bond-program meetings have their own feed. Ordinary cancellations and meetings without usable
recordings continue through the existing no-recording flow; no UID-based cancellation special cases
were introduced.

## Does this finish P0?

**No.** The policy and local selector draft now account for all 680 inventory patterns, but this
working-tree result is not deployed. The repository’s P0 exit rule also requires every row to link to
applied coverage or exclusion, an evidence-backed open case, or a documented unavailable-source
exception. A title match alone does not prove that a historical recording is the meeting named.

The current case register still has 17 open cases. They include six Addison archive-visibility checks,
the Citizen Advisory compilation binding, two Fort Worth 6334 clip-to-segment bindings, two Fort Worth
3814 bundle publication checks, two Dallas CBTF recording bindings, and four Dallas bond Streets
recording bindings. The two 6334 assignments remain unchanged as the maintainer directed. After this
PR is reviewed and merged by the maintainer, the remaining work is to deploy, verify the intended
feed and archive pages with original audio, and update only those cases whose evidence has actually
been satisfied. The two Dallas CBTF cases and the other source-binding cases remain open until their
specific evidence is found. Do not close a case because a selector matched or because an item was
classified “not to pursue.”

The 17-case register is not the same count as the 416 unmatched catalog entries: some open cases are
already assigned to a feed but still need proof, while most unmatched entries are intentionally not
pursued. The 680-row disposition and exact replay are therefore the correct measures for policy
coverage; deployment evidence remains the P0 finish gate.
