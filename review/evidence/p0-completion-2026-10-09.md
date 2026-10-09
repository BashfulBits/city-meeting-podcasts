# P0 historical baseline complete — 2026-10-09

P0's historical baseline exit criteria are satisfied. This is completion of the reviewed frozen
baseline, not proof of all possible municipal history or completion of review/51 P2–P5.
The maintainer approved final verification and carrying the six evidence questions separately.
No feed rules, source records, audio, identifiers or publication assignments change in this report.

## Refreshed frozen catalog

Read-only replay of main `1b6f7b11048630ed6d92e4ba795b84a383cdeb69`, using the original
173-feed baseline. All 42 cached source files match the SHA-256 hashes in the October 6
[input manifest](historical-selector-replay-2026-10-06-inputs.json).

| Measure | Verified count |
|---|---:|
| Saved source/UID entries | 26,555 |
| Configured feeds | 224 |
| Added feed placements | 1,980 |
| Distinct pairs gaining placements | 1,953 |
| Incorrect Fort Worth Council placements removed | 30 |
| Total current feed placements | 29,384 |
| Entries without a feed | 411 |

The four additions since the October 8 replay are exactly #2171's Dallas Transportation and
Infrastructure placements. Unmatched totals are unchanged: 405 explicitly not pursued and six
Addison archive-only records. Entries are source/UID pairs, not unique videos or playable episodes.
The 680-pattern baseline outcomes remain 519 assigned patterns / 1,668 listings, one archive-only
pattern/listing, and 160 not-pursued patterns / 359 listings. The dated worksheets are preserved;
this report and the October 8 replay supersede their older counts.
See [machine-readable replay summary](p0-final-replay-summary-2026-10-09.json).

## Final public checks

Build & Deploy [run 37897782924](https://github.com/BashfulBits/city-meeting-podcasts/actions/runs/37897782924)
completed successfully on the main commit above, including merged #2204's browser fix.
The public index advertises 42 sources; manifest and search page return HTTP 200. Browser
verification populated 11 city choices, displayed transcript coverage, and returned 100 results
for disabilities with Austin selected. This smoke check does not establish relevance/performance
quality for every possible search.

All six approved Addison archive-only UIDs are absent from the published Addison search shard;
each direct Town Meetings archive page returns HTTP 200. These checks establish visibility,
not previously unavailable content or official-event identity proof.

All four Dallas Streets UIDs are present in the dedicated Bond Streets, Bond Program and
Transportation and Infrastructure RSS feeds. Each UID has the identical hosted enclosure URL
in all three feeds. Earlier live checks for other historical rule batches remain linked in
[p0-post-merge-check-2026-10-08.md](p0-post-merge-check-2026-10-08.md) and the deployed reports.
Full exact URLs/UID observations: [live proof](p0-final-live-verification-2026-10-09.json).

## Six carried evidence obligations

The strict register remains 169 cases: 163 resolved, six open. Do not mark these evidence questions
resolved merely because publication checks pass. P0's exit rule explicitly permits linked,
evidence-backed unresolved cases; the maintainer authorized broad publication for these records.

- Fort Worth clip6334 has two source-view UIDs. The recovered official PDF proves three consecutive
  proceedings but cannot bind each UID to a segment. Keep the current four participant feed
  assignments and both evidence cases open; preserve all raw records/audio.
- Dallas Streets GUIDs269407/270975/277827/277830 remain in all three approved feeds. Exact
  independently dated agenda/video-to-record binding remains missing. Live publication is verified;
  the four evidence cases stay open for later research, without withholding these recordings.

The register's next actions now distinguish evidence research from already completed publication.
These six cases are carried research obligations, not new categorization decisions or automatic
assignment authorization.

## Beyond this baseline

Known feed-health rows have approved no-feed/watch dispositions (ceremony, low-recording-count
bodies and a malformed title). #1623 stays open because durable accepted-disposition recognition
belongs in P2; do not add incorrect selectors to force a zero warning count. The baseline does not
claim absent source recordings were never made, and future new observations remain subject to
review. P1 evaluation tooling shipped in #1987; live model admission and P2–P5 gates remain.
Search HTTPS enforcement and hosting-independent URLs are a separate follow-up in #2205.
The retired broad scheduled task remains stopped.
