# P0 rule replay against the cached catalog — 2026-10-07

> **Current follow-up:** The maintainer later directed that Dallas GUID 272574 belong in Bond Program only. The original 680-pattern dispositions are unchanged, but the exact Public Info placement is removed in the current local correction. Older live-status paragraphs below describe earlier snapshots. See “Current correction replay” at the end.

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
| Feed assignments in cached catalog | 27,434 | 29,379 | +1,945 net |
| Added policy selections vs. prior baseline | — | 1,975 | 1,952 distinct source/UID pairs |
| Removed feed placements | — | 30 | 30 source/UID pairs |
| Source/UID entries with no feed under the reviewed rules | — | 411 | See the disposition below |

The 30 removals are Fort Worth “Minority Leaders and Citizens Council” civic-program recordings
that had been incorrectly included in City Council. The feed rule no longer places them in Council;
the raw source records and audio remain intact. The ten Dallas CBTF policy selections do not add ten
new live RSS items: the published Bond Program selector already routes them at runtime. The later
Bond-only correction removes one exact Public Info placement for GUID 272574.

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

The 411 unmatched entries break down as follows:

| Treatment | Entries | Meaning |
|---|---:|---|
| Not to pursue | 405 | User-approved small or non-meeting groups; keep their raw records, do not add a feed rule |
| Archive-only visibility check | 6 | Addison media already approved for archive-only treatment; confirm hidden from feeds/search and retained at the original archive page |
| **Total** | **411** | Source/UID entries, not a guarantee of 411 distinct videos |

Of the 411, 360 came from the frozen 680-pattern baseline and 51 were additional cached entries outside
that original worksheet. All 680 patterns now have a recorded user-directed outcome. In the baseline,
518 patterns (1,667 source/UID listings) are assigned to a feed by the local rules, one pattern/listing
is archive-only, and 161 patterns (360 listings) are not to pursue. The two Dallas CBTF source records
are now assigned to Dallas Bond Program by the approved recurring body-name rule. Only the September
26, 2023 recording also remains in Public Info under its separate exact approval; October 3 is Bond
Program only. Both evidence cases stay open until live RSS confirms the expected placements. The
remaining outside-baseline entries are one-off event/test media or approved archive-only media.

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
| Dallas bond-program family | 18 | 33 |
| New named-body rules | 180 | 852 |
| Additions to existing body feeds | 41 | 148 |
| Already correctly assigned to an existing feed | 9 | 32 |
| Archive-only, already handled by an approved feature | 1 | 1 |
| Not to pursue | 161 | 360 |
| **Total** | **680** | **2,028** |

New named-body rules meet the five-distinct-UID cutoff, except Denton Bond Oversight Committee, which
the maintainer had explicitly approved earlier with four known UIDs. Additions to existing bodies do
not need to meet that new-body cutoff. Public briefings, public information and public-input sessions
share one Public Info feed per city. Fort Worth Speaker’s Podium remains its own curated series, and
Dallas bond-program meetings have their own feed. Ordinary cancellations and meetings without usable
recordings continue through the existing no-recording flow; no UID-based cancellation special cases
were introduced.

## Status at the time of the local replay

**No.** The policy and local selector draft now account for all 680 inventory patterns, but this
working-tree result is not deployed. The repository’s P0 exit rule also requires every row to link to
applied coverage or exclusion, an evidence-backed open case, or a documented unavailable-source
exception. A title match alone does not prove that a historical recording is the meeting named.

At the time of this local replay, the case register had 11 open cases, including the Citizen
Advisory compilation. The maintainer later accepted that compilation's Public Input classification
for P0 on 2026-10-07; its exact historical program-to-recording link remains unknown, and the
archive's 11-item count does not establish a missing part. The current register had nine open cases before the Fort Worth #3814 check was accepted:
two Fort Worth 6334 clip-to-segment bindings, two Fort Worth 3814 bundle publication checks, one
Dallas Bond-only deployment check, and four Dallas bond Streets recording bindings. Six approved
Addison archive-only entries still need a separate visibility check, but they are not open rows in
the current case register. The October 3, 2023 Dallas CBTF recording is resolved by the maintainer's
Bond Program-only decision and a live RSS check showing the original UID/audio in Bond Program and
its absence from Public Info and the dedicated Task Force feed; see
[`dallas-cbtf-live-rss-2026-10-07.md`](dallas-cbtf-live-rss-2026-10-07.md). The September 26
recording is Bond Program only in the current local correction, but that correction is not yet
deployed. Keep that case open until Bond Program RSS shows its original UID/audio and Public Info
and the dedicated Task Force RSS omit it. The two 6334 assignments remain unchanged as the
maintainer directed. Do not close a case
because a selector matched or because an item was classified “not to pursue.”

The open-case register is not the same count as the 411 unmatched catalog entries: some open cases
are already assigned to a feed but still need live proof, while most unmatched entries are
intentionally not pursued. This replay includes the September 26 and October 3 recordings in Dallas
Bond Program only. Public comment at a meeting
does not automatically make it a Public Info session. The 680-row disposition and exact replay are
the measures for policy coverage; live deployment evidence remains a separate P0 finish gate.

### Dallas Streets case opened for exact source binding — 2026-10-07

The official [Dallas Streets and Transportation Subcommittee page](https://dallascityhall.com/departments/bond-construction-management/Pages/Streets-and-Transportation-Subcommittee.aspx) lists Tuesday, August 22, 2023 as “Streets and Transportation Meeting #6,” matching the date and committee identity on retained provider GUID270975 / UID `16300c982b358fe9`. But the published official page’s Video button has no URL or provider GUID in its HTML. This confirms the official meeting listing, not the exact source-video binding; its case remains open and its current feed assignment is unchanged.

For the other three open Dallas Streets records, the official listing currently shows August 15 as
Meeting #5 but not an August 10 meeting. A City Manager memo dated October 27 says the bond
subcommittees were expected to reconvene by October 31, which supports the general schedule but
does not identify the October 26 or October 27 provider recordings. The source records remain in
their current Transportation feed; no case is closed from this contextual evidence alone. See the
[official memo](https://dallas.gov/government/citymanager/Documents/FY23-24%20Memos/Combined%20Friday%20Memos_%20102723.pdf).


## P0 live status — 2026-10-07

The historical rules merged in PR #2112, and Fort Worth publication notes merged in PR #2111.
Build and Deploy run #37643318454 completed successfully. Feed rendering, generated-feed
validation, the search job and Pages deployment all passed. This live check is separate from the cached local replay above.

A later read-only RSS check confirmed the October3 Dallas recording (GUID273327, UID
`6135249a278494c5`) and its original audio URL in Bond Program, and absent from Public Info and the
dedicated Task Force feed. That case is resolved. PR #2116 added September 26 GUID 272574 to Public
Info. That older deployment state is superseded by the maintainer’s Bond-only direction and the
correction described below. The full
results are in [`dallas-cbtf-live-rss-2026-10-07.md`](dallas-cbtf-live-rss-2026-10-07.md).

All 72 feeds containing proposed placements returned HTTP 200. Of 1,965 placements across 1,942
distinct source/UID pairs, 1,909 placements were present in live RSS. The remaining 56 each had a
public meeting page returning HTTP 200, but none of those pages contained a hosted audio player.
They are therefore not published podcast episodes at this time, consistent with the existing
no-audio flow; this check does not claim the city never had a source video. No feed URL failed.

For Fort Worth clip 3814, both original UIDs have public pages in Council and CCPD. All four pages
display the approved consecutive-proceedings note and the same original audio URL for each UID.
The CCPD RSS includes both noted items. The Council RSS contains its newest 500 items, from December
15, 2020 through September 29, 2026, so it does not include these July 24, 2020 items. The two
Council/CCPD evidence cases remain open pending a decision on whether the permanent page plus the
normal 500-item RSS window satisfies their publication check. No UID, title, source record, chapters
or audio were changed.

The published root `meta.json` reports `search_shards: 0`, and `/search/` plus
`/data/search/manifest.json` return HTTP 404. The search job succeeded but did not publish a search
index. Do not claim public catalog search or verify the archive-only search filter from this run.
This is a separate site-search limitation; the P0 feed rules and RSS checks above remain deployed.

The frozen 680-pattern disposition baseline is applied on the public site. The revised local
disposition has 518 assigned patterns, one archive-only pattern, and 161 not pursued. Nine
registered evidence cases remained open before the Fort Worth #3814 closure. The current register
has seven open cases: two Fort Worth 6334 segment bindings, one Dallas Bond-only deployment check,
and four Dallas Streets bond bindings. The two 3814 publication checks are resolved.
The Citizen
Advisory compilation's Public Input classification
was accepted for P0 on 2026-10-07; the exact historical binding remains unknown, and no missing part
is inferred. The register remains the source of truth for the remaining proof obligations. P0
historical dispositions are deployed;
case closure and public search availability are tracked separately and must not be inferred from
this disposition count.

### Current correction replay — Dallas GUID 272574, 2026-10-07

The maintainer directed that the September 26, 2023 recording (UID `aa65e2aafc90711b`) belong only
in Dallas Bond Program. The exact Public Info GUID inclusion has been removed locally; no broader
body rule was added. The Bond selector still accepts the recording.

The complete cached Dallas source was replayed: 5,353 records and 1,095 body labels. Compared with
the merged #2154 config, exactly one holder changed: this UID was removed from Dallas Public Info.
No other Dallas feed selection changed. The input SHA-256 and before/after config hashes are in
[`dallas-272574-replay-2026-10-07.json`](dallas-272574-replay-2026-10-07.json).

Applying that one-row change to the cached-catalog replay yields 1,975 added feed placements,
1,952 distinct source/UID pairs, 30 removed Fort Worth Council placements, and 29,379 total feed
assignments (net +1,945 from baseline). The no-feed count remains 411: 405 intentionally not
pursued and six approved Addison archive-only items. The 680 title-pattern outcomes do not change.
These are local rule results, not deployed verification.

The September 26 case remains open until a deployment check finds the original UID/audio in Bond
Program RSS and absent from Public Info and the dedicated Task Force feed.

The latest live check before this correction (Build and Deploy #37672877235, completed 2026-10-07
20:16 UTC) confirms the older dual-feed version is currently published: the original UID/audio is in
both Bond Program and Public Info and absent from the dedicated Task Force feed. The corrected local
config has not been deployed. The case therefore remains open and the current public feeds do not
yet match the maintainer's final Bond-only direction.
