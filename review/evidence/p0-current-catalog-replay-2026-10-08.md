# Current P0 catalog replay — 2026-10-08

Read-only replay against merged main `dabb246fc318f04d8d3fbba321209288bcefac23`.
All 42 source files match the SHA-256 hashes in the original October 6 input manifest.
No provider fetch, production writes, audio regeneration, or source-record changes occurred.

| Measure | Current result |
|---|---:|
| Cached source/UID entries | 26,555 |
| Configured feeds | 224 |
| Feed placements added relative to original 173-feed baseline | 1,976 |
| Distinct source/UID pairs gaining placements | 1953 |
| Wrong Fort Worth Council placements removed | 30 |
| Net placement increase | 1,946 |
| Total feed placements | 29,380 |
| Entries without a feed | 411 |

The sole assignment difference from the October 7 replay is Arlington clip 1163,
UID `a9bfe1b4bb6b6564`, provider title `Empty`, gaining Council placement through PR #2165.
It already belonged to Arlington's All Meetings aggregate, so the unmatched total stays 411.
The South Dallas/Fair Park alias has no additional match in this frozen snapshot; its newer
recording was separately verified live in the October 8 post-merge report.

The 411 entries remain 405 intentionally not pursued and six Addison archive-only records.
These counts describe saved provider entries, not a count of playable audio recordings.
A recording can have multiple feed placements, so placement counts exceed entry counts.

The 680-pattern worksheet's old not-to-pursue decision for `Empty` (ID `4ec7e9654983`)
is superseded by #2165's exact Council inclusion. Its updated disposition totals are
519 selected patterns / 1,668 listings, one archive-only pattern/listing, and
160 not-to-pursue patterns / 359 listings. The original worksheet remains a dated snapshot.
Six exact-recording evidence cases remain open. Public search remains unavailable;
this replay does not verify live search or resolve those evidence cases.
