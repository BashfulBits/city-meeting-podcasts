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
| Feed assignments in cached catalog | 27,434 | 29,369 | +1,935 net |
| New feed placements | — | 1,965 | 1,942 distinct source/UID pairs |
| Removed feed placements | — | 30 | 30 source/UID pairs |
| Source/UID entries with no feed | — | 413 | See the disposition below |

The 30 removals are Fort Worth “Minority Leaders and Citizens Council” civic-program recordings
that had been incorrectly included in City Council. The feed rule no longer places them in Council;
the raw source records and audio remain intact.

## Follow-up: two existing feeds now cover three recordings

The Dallas **Subdivision Review Committee** is a City Plan Commission advisory committee, so its
recurring name is now included in the existing City Planning Commission feed. Dallas's [CPC annual
report](https://dallascityhall.com/government/Boards-and-Commissions/City-Plan-and-Zoning-Commission/Documents/staff-presentations/01-23-25-staffpresentations/50.%20FY2023-24%20CPC%20Annual%20Report.pdf)
describes the committee as appointed by and responsible to the commission; the [September 4, 2025
official agenda](https://dallascityhall.com/calendar/DCH%20Documents/090425_CPC_S.pdf) names the
Subdivision Review Committee meeting. The rule catches both cached recordings (June 6, 2024 and
September 4, 2025) and future entries with the same body name.

Denton's **Capital Improvement Advisory Committee** recording is now included in the existing
Planning and Zoning Commission feed. The City's [development-fees page](https://www.cityofdenton.com/1188/Development-Fees)
lists multiple historical sessions as “Planning and Zoning Commission Capital Improvement Advisory
Committee,” supporting a recurring parent-feed rule. It catches the June 25, 2025 cached recording
and future entries with that body name.

These two changes add three placements; they use body-name rules, not provider-ID exceptions, and do
not change titles, UIDs, source namespaces, or audio. The other Dallas titles from the same follow-up
are not folded into these feeds: Dallas lists the **Arts District Sign Advisory Committee** and
**Special Sign District Advisory Committee** separately on its [official sign-district page](https://www.dallascityhall.com/departments/pnv/Pages/SPSD.aspx),
and the **Building Inspection Advisory, Examining & Appeals Board** is a separate [named city board](https://dallascityhall.com/government/Boards-and-Commissions/Building-Inspection-Advisory-Examining-Appeals-Boards/Pages/default.aspx).
Neither has a matching existing feed in this checkout.

The 413 unmatched entries break down as follows:

| Treatment | Entries | Meaning |
|---|---:|---|
| Not to pursue | 405 | User-approved small or non-meeting groups; keep their raw records, do not add a feed rule |
| Exact evidence still needed | 2 | Dallas Capital Bond CBTF recordings with the same ambiguous provider title; leave assignments unchanged |
| Archive-only visibility check | 6 | Addison media already approved for archive-only treatment; confirm hidden from feeds/search and retained at the original archive page |
| **Total** | **413** | Source/UID entries, not a guarantee of 413 distinct videos |

Of the 413, 362 came from the frozen 680-pattern baseline and 51 were additional cached entries outside
that original worksheet. All 680 patterns now have a recorded user-directed outcome. In the baseline,
517 patterns (1,664 source/UID listings) are assigned to a feed by the local rules, one pattern/listing
is archive-only, and 162 patterns (363 listings) are not to pursue. Two of the unmatched CBTF entries
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
| Additions to existing body feeds | 41 | 148 |
| Already correctly assigned to an existing feed | 9 | 32 |
| Archive-only, already handled by an approved feature | 1 | 1 |
| Not to pursue | 162 | 363 |
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

The current case register has 11 open cases: the Citizen Advisory compilation binding, two Fort Worth
6334 clip-to-segment bindings, two Fort Worth 3814 bundle publication checks, two Dallas CBTF
recording bindings, and four Dallas bond Streets recording bindings. The two 6334 assignments remain
unchanged as the maintainer directed. Six approved Addison archive-only entries still need a separate
visibility check, but they are not open rows in the current case register. After this
PR is reviewed and merged by the maintainer, the remaining work is to deploy, verify the intended
feed and archive pages with original audio, and update only those cases whose evidence has actually
been satisfied. The two Dallas CBTF cases and the other source-binding cases remain open until their
specific evidence is found. Do not close a case because a selector matched or because an item was
classified “not to pursue.”

The 11-case register is not the same count as the 413 unmatched catalog entries: some open cases are
already assigned to a feed but still need proof, while most unmatched entries are intentionally not
pursued. The 680-row disposition and exact replay are therefore the correct measures for policy
coverage; deployment evidence remains the P0 finish gate.
