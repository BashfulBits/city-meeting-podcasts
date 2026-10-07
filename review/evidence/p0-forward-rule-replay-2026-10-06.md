> **Superseded:** This is the earlier candidate replay, not the final P0 result. The final current-selector replay and disposition are in the [2026-10-07 report](p0-forward-rule-replay-2026-10-07.md), with exact row-level outputs in its linked CSVs.

# P0 rule replay against the cached catalog — 2026-10-06

This was a read-only comparison against the pre-final P0 rule set. It loaded the configured feed rules and archive-only settings,
then tested the proposed P0 destinations against **26,555 cached source-record entries** from
42 provider sources and 173 configured feeds. It did not fetch providers, change a feed, edit a
record, delete audio, or write production state.

**Current-state note (2026-10-07):** the maintainer subsequently approved and drafted additional
reusable named-body rules and exact aliases described in the final section of
[`p0-final-disposition-2026-10-06.csv`](p0-final-disposition-2026-10-06.csv). The 771 candidate count
below predates those rules. Repeat the full-catalog replay against the final selectors before using
that count as current or treating P0 as complete.

The row-by-row candidate changes are in [`p0-forward-rule-replay-2026-10-06.csv`](p0-forward-rule-replay-2026-10-06.csv).
The blanket-exclusion collisions are in
[`p0-forward-exclusion-impact-2026-10-06.csv`](p0-forward-exclusion-impact-2026-10-06.csv).
All 680 historical pattern decisions are in
[`p0-final-disposition-2026-10-06.csv`](p0-final-disposition-2026-10-06.csv).

## What the replay found

The currently approved rule candidates would add **771 feed assignments** across the cached catalog:

| Destination/rule | Added assignments | Assessment |
|---|---:|---|
| Public Information Meetings | 550 | Includes 439 older worksheet records, 76 additional clear-title candidates, and 35 community/project or named-Commission cases that need a dated notice or recording check. Do not auto-route those 35 by wording alone. |
| Fort Worth Speaker’s Podium | 140 | Exact curated-series prefix; good rule match. Keep other speaker or podium programs out. |
| Dallas bond-program feed | 53 | 40 worksheet records plus 13 further 2024 bond-family records; good family match. Preserve existing subcommittee feed ownership as well. |
| Fort Worth Transportation Impact Fees CIAC | 24 | Dated/officially named CIAC family. 19 cached entries match the selected inventory families and one is “City Plan Commission & CIAC” (clip 4931), for which the existing City Plan Commission assignment remains. The two generic clip-4363 records are not included. |
| Denton Bond Oversight Committee | 4 | Denton-only match on the full continuing committee name. Its feed definition and selector tests now exist in the working tree, but are not merged or deployed. The separate 14-title Special Citizens Bond Advisory cycle is reopened as a pending suggestion and is not included in this replay. |
| **Total** | **771** | Candidate changes only; not applied to config. |

For Public Information Meetings, the additional title scan found 112 candidate records. One Addison
“ACP Community Meeting” was rejected because it is currently a Comprehensive Plan Advisory Committee
record; “community meeting” alone would be an unsafe match. The remaining 111 produce candidate
assignments: 76 title-level candidates and 35 that need additional event-format proof. The 35 are
32 generic project/community-session records and 3 “Charter Review Commission: Community Meeting”
records. Those charter items should go into the aggregate only if the dated notice confirms a separate
public session rather than a Commission proceeding.

The Dallas bond scan found 13 more records across six exact title families, including “2024 Capital
Bond Streets and Transportation,” “2024 Bond Task Force Economic Development,” and “2024 Bond CBTF and
Subcommittee Chairs Meeting.” These are part of the approved Dallas bond family; a bond public-input
title does not get diverted to the general Public Information feed.

Fort Worth uses **two** Capital Improvements Advisory Committees: Transportation Impact Fees and
Water/Sewer Impact Fees. The approved candidate is the Transportation committee. “CIAC” alone is not
a safe future rule. Prefer the full Transportation committee name; for dated shorthand records, require
a match to the official City schedule. A different city, Pflugerville, has an existing CIAC feed; it
does not receive Fort Worth records.

## What blanket exclusion would break

A naive “exclude everything outside the chosen groups” pass would touch 26 currently assigned records.
**Twenty-five should stay where they are**: they belong to six already-working Board of Adjustment,
Zoning Board of Adjustment, Housing Finance, Bond Advisory, UDC Advisory, or Community Partnership
feeds. Removing them would be an error. The remaining one is Arlington’s “Empty” record; its public
feed assignment should be removed because no meeting title or identity is present. Keep its raw record
and UID in the archive. See the full row list in the exclusion-impact CSV.

## Limits

The CSV shows source/UID pairs, not unique videos; different source views can list the same recording.
The 771 are potential routing changes based on the local cached catalog, not proof that every old
recording is the event named by its title. Exact dated source-to-event checks remain necessary for the
35 flagged Public Information cases, dated CIAC shorthand, and any future live selector activation.
The user-directed exclusions and Denton committee assignment are classification decisions only; this replay did not change production
feeds or remove historical media.
