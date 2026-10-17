# Arlington historical named-body coverage — 2026-10-02

**Status: Implemented in commit 5ffabb6 (merged 2026-10-02) · FROZEN**
Feeds  and  are
deployed in  and verified by .
This batch establishes coverage for the two verified independent bodies; broader Arlington historical
census and held foundation groups are tracked under review/51.

## Decisions and official evidence

| Inventory ID | Recording | Subscription owner | Grounds |
|---|---|---|---|
| `abecffac363f` | [HFC clip 3579](https://arlingtontx.granicus.com/MediaPlayer.php?view_id=2&clip_id=3579), 2020-08-25 | Arlington Housing Finance Corporation | Official dated agenda identifies a separate corporation proceeding. |
| `1d90997bebcb` | [ZBA clip 1069](https://arlingtontx.granicus.com/MediaPlayer.php?view_id=2&clip_id=1069), 2012-05-31 | Zoning Board of Adjustment | Official recording label says ZBA; the city identifies ZBA as the distinct adjustment board. |

The [official boards directory](https://www.arlingtontx.gov/Government/City-Government/Boards-Commissions)
distinguishes the Housing Finance Corporation, Planning and Zoning Commission and Zoning Board of
Adjustment. The corporation finances affordable residential housing. ZBA hears variances and
administrative appeals; Planning and Zoning recommends zoning changes and approves plats.
The [official ZBA calendar](https://www.arlingtontx.gov/Community-Calendar/Planning-and-Development-Services-Events/ZBA/Zoning-Board-of-Adjustment-Meeting)
also uses the ZBA abbreviation for that board. Shared topics and council members do not establish
that these meetings belong to Council or Planning and Zoning.

The [HFC agenda](https://arlingtontx.granicus.com/AgendaViewer.php?view_id=2&clip_id=3579)
was retrieved on 2026-10-02: its heading identifies the corporation and August 25, 2020 meeting.
The ZBA clip's agenda URL returned "not currently published"; no agenda content or recording
transcript is inferred. Its identity is supported by the official archive label and city sources.
Neither sparse recording history nor age establishes that either body has ceased meeting.

## Selector and remedy policy

Use existing normalized complete-label globs, anchored at the start of the official body name:
`Arlington Housing Finance Corporation *`, `ZBA *`, and `Zoning Board of Adjustment *`. This admits
future session suffixes without treating a Council topic reference as a corporation/board meeting.
Bare names without a session suffix, unrecognized prefixes and joint-body labels remain remedy
evidence questions. Reviewed bare identities are guarded against duplicate-feed creation. No new matching
engine or one-off GUID inclusion is needed.

`remedy_policy.identity_names` records the reviewed provider labels and official full names.
Those exact identities require their named-body owner and prohibit duplicate raw-title feeds.
`member_names` are holding clues only: an unseen abbreviation/session variant requires evidence;
a clue never proves identity or authorizes inclusion. This depends on the shared named-body
policy guard change. Policy regression seeds live in
`evals/remedy/policies/arlington-named-bodies/`; they are not model admission results.

## Replay, preservation and limits

A read-only adapter refresh on 2026-10-02 returned 1,506 provider observations, 1,506 distinct
provider GUIDs and 62 body labels from the existing Arlington Granicus source, key
`ecc3710ac47f`. `tests/fixtures/arlington_coverage/archive.json` freezes every label and GUID,
positive recording metadata and snapshot digest. All provider rows replay with exactly one
recording for each new feed and no negative-label inclusion. Existing Council and Planning and
Zoning selectors capture neither positive recording. Tests also exercise session variants and
Council-topic, cross-city and airport-board negatives.

The source state was restored read-only from the public state endpoint at
2026-10-02T22:52:31Z: 1,515 retained records, SHA-256
`34bed901ac6d6ae54c7cb6a3cfa2b726260967eb2492e2e914adf7bcf275cc15`.
Every stored record also replays against both selectors, again selecting exactly one retained UID
per feed. The provider/state union contains 1,509 distinct provider GUIDs; retained rows can share
a GUID after historical label changes, so all 1,515 UID records remain distinct in this census.
Four stored-only label variants belong to Council or Arlington Tomorrow Foundation; none is
assigned to either new body. Fixtures preserve both positive stored UIDs, and tests check them
against the unchanged identity algorithm. No recordings absent from both sources are inferred.
Three archived Foundation label rows share their provider GUIDs with three current-label
Foundation rows already covered by its existing selector. These are duplicate historical UID
identities, not three missing recordings. Adding an alias would select six UID rows for three clips;
that separate identity decision remains held. No Foundation policy or lifecycle edits are included.
The separate
`Empty` recording (inventory `4ec7e9654983`) remains unresolved; it is not silently assigned.

Both new views retain the original source URL and podcast author. Tests verify the source namespace
and episode UID inputs match the existing source. No existing feed URL, record, official title,
audio specification, pipeline version or UID override changes. Once approved coverage is rendered,
already-retained eligible episodes become visible through normal work; this does not force audio
backfill or invalidate stored artifacts.
