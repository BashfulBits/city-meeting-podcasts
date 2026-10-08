# P0 post-merge check — 2026-10-08

This records live checks after the Dallas Bond-only change in PR #2164 and the five-row
feed-health dispositions in PR #2165. It supplements the frozen October 7 replay; it does not
rewrite its inputs or claim that the full historical replay has been regenerated.

## Merge and deployment

- PR [#2164](https://github.com/BashfulBits/city-meeting-podcasts/pull/2164) merged at
  2026-10-08 03:01:48 UTC as `8eb28289f1dea941ab22ba5a12cb23f4be1193d0`.
- PR [#2165](https://github.com/BashfulBits/city-meeting-podcasts/pull/2165) merged at
  2026-10-08 03:02:29 UTC as `2f3e26f887de1f615715894319f3edfae8608c9a`.
- Build & Deploy [run #37720810791](https://github.com/BashfulBits/city-meeting-podcasts/actions/runs/37720810791)
  passed for the #2165 merge commit; that commit includes #2164.
- PR #2165's CI test, dependency, preview and CodeQL checks passed before merge. CodeRabbit's
  substantive review identified one valid documentation gap: the required `review/11` entry was
  missing. This post-merge update records the shipped change and live check.

## Live feed and page checks

- Arlington Council archive and the permanent page for UID `a9bfe1b4bb6b6564` returned HTTP 200.
  The page retains the original Granicus `clip_id=1163` and hosted audio URL. The 2012 UID is
  absent from the current Council RSS because it is outside the feed's bounded history; it remains
  available from the archive/permalink.
- Dallas Bond Program RSS returned HTTP 200 and contains UID `aa65e2aafc90711b` with its original
  enclosure URL:
  `https://audio.citymeetings.fyi/swagit/76869ed1994f/aa65e2aafc90711b-1f714be9f4ff.m4a`.
  Dallas Public Info and dedicated Task Force RSS both returned HTTP 200 and do not contain that
  UID. A one-byte probe to the enclosure endpoint returned HTTP 200 and `audio/mp4`; the probe read
  one byte only.
- Dallas South Dallas/Fair Park Opportunity Fund RSS returned HTTP 200 and contains its
  `South Dallas Fair Park Opportunity Fund Special Called Meeting` entry under the existing board
  feed. The title does not appear in the Dallas Bond Program feed.
- `https://www.citymeetings.fyi/meta.json` reports `search_shards: 0`. `/search/` and
  `/data/search/manifest.json` return HTTP 404. Therefore public search and the archive-only search
  filter remain unverified.

## Fresh unexpected-body audit

Feed-health audit [run #37726969713](https://github.com/BashfulBits/city-meeting-podcasts/actions/runs/37726969713)
completed successfully on `cb7018eb`, after #2165 and before unrelated PR #2167 merged. Issue
[#1623](https://github.com/BashfulBits/city-meeting-podcasts/issues/1623) remains open with four
affected feed paths and five observed provider rows:

| Feed path | Observed title | Count | Recommended P0 handling |
|---|---|---:|---|
| Addison BZA | George Herbert Walker Bush Elementary Dedication | 1 | Ceremony, not a meeting; preserve the source, no meeting-feed rule. |
| Denton agenda committee | Animal Shelter Advisory Committee, dated 2021-03-24 | 1 audit row; three recordings found in cached sources | Real standing body, but only three recordings and no configured feed; below the five-UID new-feed cutoff. Keep out of feeds for this P0 pass and revisit if more recordings surface. |
| Dallas 2017 Bond Public Town Halls | Building Inspection Advisory, Examining & Appeals Boards | 2 | No matching configured feed exists. Do not misroute it to Bond or Public Info; two recordings are below the five-UID cutoff, so do not create a new feed in this P0 pass. |
| Fort Worth Emergency Medical Response | `fortworthgovMerg fortworthgovMerg` | 1 | Malformed title does not identify a body; preserve the source, no feed rule. |

These recommendations follow the maintainer's existing five-distinct-UID cutoff and no-feed
decisions. They do not establish that the rows match a selector. The current audit reports them as
unexpected because its automated check has no durable way to recognize an accepted human
not-to-pursue/watch decision. Do not add broad title selectors just to make issue #1623 disappear.
The unmatched-disposition flow in review/51 remains a proposal; its final fields and behavior have
not been implemented.

## Remaining P0 work

The frozen worksheet still contains 680 title patterns and 2,028 source/UID listings, each with a
recorded disposition. The last full replay reported 518 patterns/1,667 listings assigned, one
archive-only pattern/listing, and 161 patterns/360 listings not pursued. Those numbers predate
#2165: the exact Arlington `Empty` item changed from not-pursued to assigned, and the Dallas alias
adds a reusable rule match. Regenerate the full replay from its frozen inputs before quoting current
totals. The old CSV/JSON files remain historical snapshots.

After resolving the Dallas GUID272574 placement check, the evidence register has six open cases:
two Fort Worth UID-to-segment questions for GUID6334 and four Dallas Streets and Transportation
bond-recording/source bindings. The Fort Worth assignments remain unchanged and both cases stay open
under the maintainer's direction. The Dallas records stay in their existing feed until dated,
exact source proof is available.

P0 is not verified complete yet. Before sign-off, refresh the 680-row replay, decide/document the
five current #1623 rows so future audits can distinguish accepted exclusions from new feed errors,
and finish the six evidence cases or explicitly carry them as unresolved under the P0 exit rule.
Public search is a separate recorded limitation; it is currently unavailable, so no live search or
archive-only search-filter check can be claimed.
