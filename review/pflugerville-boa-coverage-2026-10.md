# Pflugerville Board of Adjustment historical coverage

**Status: Implemented in PR #1993 (merged 2026-10-03) · FROZEN**
Feed  is deployed in 
and verified by . Ten retained recording identities and official Legistar
bindings are verified. This applies the maintainer-approved missing named-body direction.

## Evidence and scope

The [official city board page](https://www.pflugervilletx.gov/812/Board-of-Adjustment) redirects
to its current Board of Adjustment page. More decisively, every retained positive has an official
Legistar meeting detail page naming **Board of Adjustment**, the same calendar date, and a Video
link whose `ID1` binds the identical Granicus clip ID. Each observed positive is therefore an
identified board proceeding, including the oldest meeting on December 14, 2016. The September 16,
2026 agenda includes variance hearings; sharing zoning subject matter does not make these
Planning and Zoning Commission or Council meetings.

The selector uses `source.body_exact: [Board of Adjustment]`, rather than a subject substring.
Normalized complete-label spelling/plural/punctuation variants and provider-duplicated complete
labels follow the existing exact machinery. Extended labels such as staff training or a differently
named appeals body do not automatically match. `remedy_policy.identity_names` assigns only this
reviewed identity and holds possible duplicates or extended labels for evidence. An unseen alias
still requires review; a different city's board requires its own official identity evidence.

## Historical replay

The provider adapter was refreshed read-only on 2026-10-03T05:51:51.775376+00:00.
It returned 205 rows across 28 labels. The publicly restored October 2 state contains 948 retained
UID rows across 38 labels. Together they reference 832 distinct provider GUIDs; retained UID
observations are not the same quantity as distinct recordings. Replay tests include **every**
current row and every retained row, not only representative body names.

The exact selector selects one currently listed recording and ten retained records, with ten
unique UIDs, ten unique Granicus clips, and ten separate meeting dates. Nine historical positives
are absent from the current provider listing. None of these ten has a duplicate retained UID for
the same clip, and no existing Council or Planning and Zoning selector claims them. The full-city
state contains other duplicate GUID observations; this batch neither changes nor resolves those.

| Official date | Clip | Preserved UID | Current player HTTP status | Identity evidence |
|---|---|---|---|---|
| 2016-12-14 | 120 | `76612e93dd76c006` | 404 | [Official meeting](https://pflugerville.legistar.com/MeetingDetail.aspx?ID=517745&GUID=B3C81AAE-64AC-4F53-9C6A-9149AF4A602D&Options=info%7C&Search=) |
| 2017-01-25 | 128 | `326fbc9dfcfa9588` | 404 | [Official meeting](https://pflugerville.legistar.com/MeetingDetail.aspx?ID=526133&GUID=11309297-403B-40E3-9CE1-41828D24DE79&Options=info%7C&Search=) |
| 2018-01-24 | 185 | `b9803eceb98e9e84` | 404 | [Official meeting](https://pflugerville.legistar.com/MeetingDetail.aspx?ID=588609&GUID=09F034AB-B7D5-4A8B-AAF1-5C3B7B80CED6&Options=info%7C&Search=) |
| 2018-08-22 | 205 | `87acad8c9e383e70` | 404 | [Official meeting](https://pflugerville.legistar.com/MeetingDetail.aspx?ID=618291&GUID=8969FC0E-625F-4B25-B645-0EE8E04ADC07&Options=info%7C&Search=) |
| 2019-04-24 | 234 | `d9cd97daff73abfe` | 404 | [Official meeting](https://pflugerville.legistar.com/MeetingDetail.aspx?ID=689855&GUID=39863264-BB85-4173-8812-F93453210AE5&Options=info%7C&Search=) |
| 2020-01-22 | 268 | `c89bdb5542bb32b6` | 404 | [Official meeting](https://pflugerville.legistar.com/MeetingDetail.aspx?ID=758887&GUID=B83E4F3E-9F5B-4DEF-B606-83C3595B2211&Options=info%7C&Search=) |
| 2021-05-12 | 379 | `a527e54968504697` | 404 | [Official meeting](https://pflugerville.legistar.com/MeetingDetail.aspx?ID=860860&GUID=0760B8C1-98CF-4B32-AFEB-A9F3770F66A0&Options=info%7C&Search=) |
| 2022-02-16 | 446 | `3aa42c1abbaf944e` | 404 | [Official meeting](https://pflugerville.legistar.com/MeetingDetail.aspx?ID=926343&GUID=73311B6D-AB63-4ECC-A5C8-16C4B5DF8EE6&Options=info%7C&Search=) |
| 2024-05-29 | 706 | `7d18895c867ef6d3` | 403 | [Official meeting](https://pflugerville.legistar.com/MeetingDetail.aspx?ID=1193221&GUID=AB1A6971-8124-4F69-948E-008CD7428506&Options=info%7C&Search=) |
| 2026-09-16 | 933 | `fcadbe135e692a1e` | 200 | [Official meeting](https://pflugerville.legistar.com/MeetingDetail.aspx?ID=1439613&GUID=8CC205FF-03E2-4963-A41C-0983094E3D70&Options=info%7C&Search=) |

**Identity coverage is distinct from media availability.** The current clip 933 player resolves
successfully. Clip 706 redirects to a permission-denied response (403), although retained state
records playable audio at its previous check. The other eight original player URLs return 404;
the official meeting pages still bind those original clip IDs. This change does not promise to
recover unavailable media or include metadata-only entries as playable RSS enclosures. Existing
availability/retention behavior remains authoritative. No media was downloaded or reprocessed,
and no archived observation is deleted or rewritten.

## Reproducibility and identity guarantees

- Available provider snapshot SHA-256: `7b2abcf0c6f6e854088be058bbaa9b01296507dfc9b2febf7a2355eadc2b36bf`.
- Retained snapshot SHA-256: `eadb6d231d9c0b09853a000d75bf9147bf58f11f7430dcfb744dc5baf99d021f`.
- Retained retrieval: `2026-10-02T22:53:02.258309+00:00`, public endpoint.
- Existing source namespace: `19c2ffe87d08`; the same feed URL and official podcast author remain.
- Replay fixtures preserve each stored UID, original body/title/date, provider GUID, and all ten
  official meeting/video-binding spans. Tests compare UID assignment under the old and new feed
  for all current rows and every historical positive, preserving provider ordering.
- No source-key, author, audio pipeline version, official metadata, or production-state changes.

The isolated `evals/remedy/policies/pflugerville-boa/` schema-v2 manifest supplies ten grounded
historical owner cases, Council/P&Z/duplicate-feed negatives, an explicitly adapted topic-label
negative, and an explicitly adapted unknown extended-identity case. Gold provenance is **seed**,
not independently verified admission truth. Evidence spans carry retrieval timestamps and canonical
JSON hashes. These cases can be re-run with the evaluation harness's custom manifest/gold options;
no model route was called to prepare this batch.

The test fixture documents HTTP observations without turning them into permanent body exclusions.
Full taxonomy completion, media recovery, and other retained duplicate-identity decisions remain
separate work.
