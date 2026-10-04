# Addison body assignment audit and cross-city policy draft (2026-10-04)

Status: read-only findings and proposed policy; maintainer settlement required before further
CodeRabbit requests or readiness. No production mutation or additional classification changes.
Current Addison CPC PR #2003 head `2c11e029` has passed 4,838 offline tests and current CI, but
its already-issued 17:06:20 UTC review does not settle the wider taxonomy. Review requests are held.

## Why the inventory missed meetings and errors

The historical inventory was a provider-label/recording census, not an official all-meeting census.
All six retained CPC observations were already present and listed as a selector gap. Four additional
held early-2025 dates and scheduled July 17 came from official minutes/agenda research; no recording
was located for those dates. An agenda is not an invented audio episode.
`citypods/audit.py:collect_unexpected_bodies` skips already-matched labels and unmatched labels already
in the append-only archive. Consequently zero unexpected bodies does not prove coverage, and a
broad Council match is not tested for institutional correctness by this detector.

## Complete retained-label Addison replay

822 raw UID observations across 62 labels, using actual `record_matches_body` and current configs.
These counts prove selector eligibility, not current public RSS exposure or unique meeting counts.
CPC uses the proposed dedicated feed; all other selectors remain as currently configured.

| Provider body | Raw observations | Configured feed matches |
|---|---:|---|
| 2025 City Council Strategic Planning Session | 1 | city-council (1) |
| 2026 City Council Strategic Planning Session | 1 | city-council (1) |
| ACP Advisory Committee | 1 | comprehensive-plan-advisory-committee (1) |
| ACP Community Meeting | 1 | comprehensive-plan-advisory-committee (1) |
| ACP Vision Plan Meeting | 2 | comprehensive-plan-advisory-committee (2) |
| Addison Economic Development Luncheon | 1 | None |
| Addison Way | 1 | town-meetings (1) |
| Beckert Park Dedication Ceremony | 1 | town-meetings (1) |
| Board of Appeals | 2 | board-of-zoning-adjustment (2) |
| Board of Zoning Adjustment | 4 | board-of-zoning-adjustment (4) |
| Bond Advisory Committee | 4 | None |
| Budget Meeting | 2 | city-council (2) |
| Citizen Advisory Committee | 1 | town-meetings (1) |
| City Council Swearing-in Ceremony | 1 | city-council (1) |
| Combined Meeting | 30 | city-council (30) |
| Community Meeting #1 | 1 | town-meetings (1) |
| Community Meeting #2 | 1 | town-meetings (1) |
| Community Meeting #3 | 1 | town-meetings (1) |
| Community Partnership Committee | 6 | community-partnership-committee (6) |
| Comprehensive Plan Advisory Committee | 12 | comprehensive-plan-advisory-committee (12) |
| Council Strategic Planning Day 1 & 2 | 1 | city-council (1) |
| Destination Addison | 1 | town-meetings (1) |
| Earth Day | 1 | town-meetings (1) |
| Fiscal Year 2023-2024 Budget Workshop | 2 | city-council (2) |
| Fiscal Year 2024-2025 Budget Workshop | 1 | city-council (1) |
| Former Sam's Club Applicant Presentation | 1 | town-meetings (1) |
| Former Sam's Club Staff Presentation | 1 | town-meetings (1) |
| George Herbert Walker Bush Elementary Dedication | 1 | town-meetings (1) |
| Homelessness Education Seminar | 1 | town-meetings (1) |
| Joint CPAC, P&Z, and City Council Meeting #1 | 1 | city-council (1) |
| Joint CPAC, P&Z, and City Council Meeting #2 | 1 | city-council (1) |
| Joint City Council and Planning & Zoning Commission Meeting | 7 | city-council (7), planning-and-zoning-commission (7) |
| Joint Council & Planning & Zoning Commission Meeting | 1 | planning-and-zoning-commission (1) |
| Joint P&Z and City Council Meeting #1 | 1 | city-council (1) |
| Planning & Zoning Commission | 127 | planning-and-zoning-commission (127) |
| Planning & Zoning Commission Annual Organizational Meeting | 1 | planning-and-zoning-commission (1) |
| Planning & Zoning Commission Work Session | 47 | city-council (47), planning-and-zoning-commission (47) |
| Planning and Zoning | 52 | planning-and-zoning-commission (52) |
| Regular City Council | 111 | city-council (111) |
| Regular City Council Meeting | 1 | city-council (1) |
| Remington's Seafood Grill 30 Years Award | 1 | town-meetings (1) |
| Special Budget Meeting | 8 | city-council (8) |
| Special Community Meeting | 1 | town-meetings (1) |
| Special Council | 15 | city-council (15) |
| Special Emergency Meeting | 1 | city-council (1) |
| Special Meeting | 36 | city-council (36) |
| Special Meeting and Work Session | 15 | city-council (15) |
| Special Meeting-Tax Rate & Budget Public Hearing | 1 | city-council (1) |
| Special Work Session | 3 | city-council (3) |
| Spruill Park Dedication Ceremony | 1 | town-meetings (1) |
| TIRZ #1 Board Meeting | 1 | city-council (1) |
| Tax & Budget Public Hearing | 2 | city-council (2) |
| Tax Rate Public Hearing | 1 | city-council (1) |
| Town Hall Meeting | 22 | town-meetings (22) |
| Town Meeting | 1 | town-meetings (1) |
| Town Meetings | 16 | town-meetings (16) |
| Unified Development Code (UDC) Advisory Committee | 3 | unified-development-code-advisory-committee (3) |
| Unified Development Code (UDC) Advisory Committee Open House | 1 | town-meetings (1) |
| Vitruvian Park | 1 | town-meetings (1) |
| Work Session | 23 | city-council (23) |
| Work Session - Economic Development Strategic Plan | 1 | city-council (1) |
| Work Session and Regular Meeting | 234 | city-council (234) |

## Findings and proposed Addison dispositions

- **47 Planning & Zoning Commission Work Session observations:** Council's substring `Work Session`
  also selects commission-only labels. They already match P&Z's own feed. Remove Council eligibility
  after complete positive/negative replay; retain every UID/audio/page. Example GUID394156,
  2026-07-21, UID01960ed1399198df. This is an institution error, not evidence of a Council subcommittee.
- **One Council swearing-in ceremony:** substring `City Council` selects GUID56023, 2009-05-26.
  Under the approved ceremony exclusion, preserve its raw record but exclude it from meeting feeds.
- **TIRZ #1 board:** GUID394474 explicitly included in Council. Its separate governing capacity
  needs official agenda/recording proof and the approved city TIF-family policy, rather than treating
  identical Council membership as proof of a full Council meeting. Activation/exposure remain gated.
- **Bond Advisory Committee:** four observations match no feed. Official Town sources establish
  a 14-resident committee for the May2026 police/courts bond program. Treat as a temporary bond-family
  proceeding under the approved family policy, never as full Council. Record any missing recordings.
- **Comprehensive Plan/ACP and UDC:** separate named feeds already exist; do not reassign their
  own proceedings to Council. They are project-specific bodies; assess lifecycle and any eventual
  family migration explicitly, preserving existing URLs and all history.
- **Citizen Advisory Committee:** GUID56029 currently assigned to Town Meetings. Official identity
  and purpose unresolved; an ambiguous label alone cannot authorize either a standing feed or Council.
- **Finance Committee:** official 2018/2021 Council packets establish a monthly three-member
  advisory committee, created March10,2015 and annually reauthorized. No Finance Committee body
  labels occur in the restored source. This is a recording/census gap to investigate, not proof that
  generic Council videos are secretly Finance Committee meetings.
- **Other Town Meetings inclusions:** awards, dedications, municipal-media titles and presentations
  require recording-level disposition under the already approved non-meeting/public-input policies.
  Several named ceremonies are explicitly selected today. Do not equate every one-off with Council.
- **Joint meetings:** seven Council/P&Z observations match both feeds; additional CPAC/P&Z/Council
  labels match Council. Genuine jointly convened proceedings must be distinguished from accidental
  substring leakage and from a video containing several separately convened meetings.

Primary sources for institutional gaps:
- Bond creation/membership:
  https://www.addisontx.gov/News-articles/Council-Appoints-Community-Bond-Advisory-Committee
- Finance creation/annual reauthorization:
  https://agendas.addisontx.gov/docs/2021/CM/20210622_6809/AGENDApacket__06-22-21_0529_6804.pdf
- Finance three-member monthly meetings:
  https://agendas.addisontx.gov/docs/2018/CM/20180425_4418/4413_04-24-18_1557_AGENDApacket.pdf
- TIRZ formation (Council discussion is not itself the board recording):
  https://www.addisontx.gov/News-articles/2026-News/Council-Creates-Reinvestment-Zone-for-Addison-Junction-Area
- CPC complete minimum-known census and recording gaps:
  [CPC evidence](addison-cpc-publication-selection-2026-10-04.md).

## Proposed shared policy and onboarding acceptance

1. Classify the body that officially convened the proceeding, not its topic, membership overlap,
   provider directory path, or a committee mentioned in a Council agenda.
2. Continuing named standing bodies/subcommittees have their own feeds. Seasonal, infrequent or
   Council-member-only bodies remain continuing if their official remit and appointments continue.
3. Time-limited bond/charter/redistricting programs follow their approved city family policy.
   Existing named historical feeds are preserved until a reviewed migration explicitly replaces them.
4. Council subscriptions contain genuine full Council proceedings only. Use reviewed complete labels
   or source-bound, fully anchored aliases; `Work Session`, `Special Meeting` and shared topic words
   must not silently absorb another named institution. Generic labels require official body evidence.
5. Across all existing and future cities, genuine jointly convened meetings are distinct from
   separate proceedings bundled in one recording.
   Maintainer-approved subscription rule (2026-10-04): the same stable recording appears in each proven participating
   body's feed, with explicit participation proof and stable canonical search ownership. Separately
   convened segments require declared timeline/identity evidence; no blind clip splitting or merging.
6. Standalone public-input and public-briefing recordings use their approved family feeds. Promotions,
   ceremonies, staff training and TV programs have durable exclusions; discussing those subjects
   during a genuine government meeting never excludes that meeting.
7. Onboarding reconciles official bodies/agendas/minutes with every available provider archive view
   and retained records. Record known held/scheduled/cancelled dates and media gaps separately. A
   missing recording is visible coverage uncertainty, not an invented episode or proof of retirement.
8. Freeze an all-label/all-record replay: expected owners, approved joint overlaps, exclusions,
   unknowns and source completeness. Include negatives for neighboring committees, ceremonies and
   Council topic mentions. Existing archive presence and broad matches do not count as dispositions.
9. Maintenance detects new labels AND changed ownership/proof, unresolved archived labels and false
   inclusions. An approved exact body rule admits new unique recordings; unknown identities/aliases
   and duplicate members abstain into the existing evidence/approval gates.
10. Preserve all stored records, UIDs, official metadata and audio; publication changes require
    exposure/cache/raw-page checks and separate reviewed small PRs. No legacy remedy dispatch.

This draft does not widen executable P2 schemas or admit a model. Joint publication in each
participating feed is approved; unresolved canonical-owner/timeline choices and recording-level
ambiguous Addison cases must be settled before L3 implementation.

## Subsequent official identity/lifecycle verification

- **Finance:** July13,2021 minutes explicitly approve discontinuation by Resolution R21-035,
  vote6–1. It was a historical continuing committee, not one deemed retired by age. If standalone
  recordings are found, preserve historical Finance ownership; do not classify Council meetings
  discussing its reports/discontinuation as Finance meetings.
  https://agendas.addisontx.gov/docs/2021/CM/20210810_6813/4002_07-13-2021%20Minutes%20FINAL.pdf
- **Board of Appeals:** the actual August11,2022 packet identifies the Building/Code Board of
  Appeals as constituted from Zoning Adjustment membership. Later Town documents explicitly say
  Zoning Adjustment serves as Board of Appeals. This supports keeping its existing combined feed
  through formal institutional evidence, not merely shared members. Two retained Appeals records
  are not demonstrated Council misclassifications.
  https://agendas.addisontx.gov/docs/2022/BZA/20220811_6958/AGENDApacket__08-11-22_0519_6953.pdf
  https://agendas.addisontx.gov/docs/2025/PZ/20250129_7409/AGENDApacket__01-29-25_0333_7404.pdf
- **P&Z and TIRZ recordings:** the official indexed Swagit archive separately lists July21,2026
  P&Z work session and P&Z regular meeting, and July28 TIRZ board video (13m19s) separately from
  that day's Council video (1h31m). This confirms those labels are not simply topics inside the
  Council recording. TIRZ source calendar/board packet remains a further activation-proof task.
  https://addisontx.new.swagit.com/views/128/live
- **Citizen Advisory:** official2007budget describes approximately85 citizen members; 2009Council
  packet refers to2007NextGreatestIdeas planning process. Exact linkage to Swagit56029 (8h48m)
  remains unknown; compilation/chapter ambiguity and403video access prevent a settled identity.

## Cross-city spot check

# Retained-archive Council selector spot-check

Read-only current checkout replay; rows are archived UID observations, not unique meetings or published audio counts. Matching uses actual record_matches_body with source_body_filter/source_body_inclusions. Body-label flags identify taxonomy candidates, not proof of legal membership or actual proceedings. Explicit joint labels are separated. No production writes or reviews requested.

## arlington-tx-council — source `ecc3710ac47f`
Archive 1515 UID rows; Council selector matches 966; taxonomy candidates 16; explicit joint/luncheon candidates 2.

### Flagged labels and rule causes

- 'Budget Town Hall Meeting': 2 rows; normal body/body_any substring or glob; example UID `7e6dda2fc260f6e1`, GUID `https://arlingtontx.granicus.com/MediaPlayer.php?view_id=2&clip_id=3568`, date `2020-09-01T00:00:00+00:00`.

- 'Community and Neighborhood Development Committee': 4 rows; normal body/body_any substring or glob; example UID `237c28f30496e3e0`, GUID `https://arlingtontx.granicus.com/MediaPlayer.php?view_id=2&clip_id=3453`, date `2020-04-28T00:00:00+00:00`.

- 'Environmental Task Force': 1 rows; normal body/body_any substring or glob; example UID `1792515fdd51c6c2`, GUID `https://arlingtontx.granicus.com/MediaPlayer.php?view_id=2&clip_id=3443`, date `2020-04-14T00:00:00+00:00`.

- 'Environmental Task Force Meeting': 5 rows; normal body/body_any substring or glob; example UID `25e9b8510b0a7a66`, GUID `https://arlingtontx.granicus.com/MediaPlayer.php?view_id=2&clip_id=3494`, date `2020-06-09T00:00:00+00:00`.

- 'Special Meeting and Budget Town Hall Meeting': 1 rows; normal body/body_any substring or glob; example UID `099e977e181bf8a6`, GUID `https://arlingtontx.granicus.com/MediaPlayer.php?view_id=2&clip_id=2841`, date `2018-08-30T00:00:00+00:00`.

- 'Special Meeting and District 5 Town Hall': 1 rows; normal body/body_any substring or glob; example UID `1e33fee06ae9d526`, GUID `https://arlingtontx.granicus.com/MediaPlayer.php?view_id=2&clip_id=232`, date `2008-08-21T00:00:00+00:00`.

- 'Special and Budget Town Hall Meeting': 1 rows; normal body/body_any substring or glob; example UID `3cc0d47186eb192c`, GUID `https://arlingtontx.granicus.com/MediaPlayer.php?view_id=2&clip_id=3240`, date `2019-09-05T00:00:00+00:00`.

- 'Virtual Town Hall on Future Active Adult Center': 1 rows; exact GUID body_includes override; example UID `91ef155676bf9a4c`, GUID `https://arlingtontx.granicus.com/MediaPlayer.php?view_id=2&clip_id=3622`, date `2020-10-28T00:00:00+00:00`.

### Explicit joint labels (do not treat as committee-only)

- 'Special Joint Council-P&Z Meeting': 1 rows

- 'Special Joint Meeting': 1 rows

## dallas-tx-city-council — source `76869ed1994f`
Archive 5353 UID rows; Council selector matches 359; taxonomy candidates 34; explicit joint/luncheon candidates 0.

### Flagged labels and rule causes

- '2013 City Council Inauguration': 1 rows; exact GUID body_includes override; example UID `4824b62a876ea3ed`, GUID `205485`, date `2013-06-24T00:00:00+00:00`.

- '2015 City Council Inauguration': 1 rows; exact GUID body_includes override; example UID `032fc585d0663075`, GUID `205503`, date `2015-06-22T00:00:00+00:00`.

- '2016 City of Dallas Community Survey Findings': 1 rows; exact GUID body_includes override; example UID `2871a785327be96c`, GUID `201670`, date `2016-06-02T00:00:00+00:00`.

- '2017 City Council Inauguration': 1 rows; exact GUID body_includes override; example UID `06cd6ea906eb9421`, GUID `205512`, date `2017-06-19T00:00:00+00:00`.

- '2019 City Council Inauguration': 1 rows; exact GUID body_includes override; example UID `0bacdb2f6b1da49a`, GUID `205539`, date `2019-06-17T00:00:00+00:00`.

- '2021 City Council Inauguration': 1 rows; exact GUID body_includes override; example UID `451bf22c0b185839`, GUID `205569`, date `2021-06-14T00:00:00+00:00`.

- '2024 Bond CBTF and Subcommittee Chairs Meeting': 1 rows; exact GUID body_includes override; example UID `baa19208405acb22`, GUID `280220`, date `2023-11-04T00:00:00+00:00`.

- '2025 City Council Inauguration': 1 rows; exact GUID body_includes override; example UID `5ee0dc201f26502e`, GUID `345783`, date `2025-06-16T00:00:00+00:00`.

- 'Ballot Order Drawing': 3 rows; exact GUID body_includes override; example UID `70e8f310ac30b311`, GUID `209016`, date `2023-02-27T00:00:00+00:00`.

- 'City Council Inauguration': 1 rows; exact GUID body_includes override; example UID `5d7f9b290103a590`, GUID `245386`, date `2023-06-20T00:00:00+00:00`.

- 'Dallas Virtual Town Hall Meeting - District 11': 1 rows; exact GUID body_includes override; example UID `b7948a1443246062`, GUID `203148`, date `2014-08-19T00:00:00+00:00`.

- 'Dallas Virtual Town Hall Meeting - District 4': 1 rows; exact GUID body_includes override; example UID `385eaa4b1a73fda4`, GUID `203149`, date `2014-08-20T00:00:00+00:00`.

- 'Dallas Virtual Town Hall Meeting - District 5': 1 rows; exact GUID body_includes override; example UID `e36f0b32bbfbcc27`, GUID `203153`, date `2014-08-28T00:00:00+00:00`.

- 'Dallas Virtual Town Hall Meeting - District 8': 1 rows; exact GUID body_includes override; example UID `abd31dfed87bbc27`, GUID `203151`, date `2014-08-25T00:00:00+00:00`.

- 'Dallas Virtual Town Hall Meeting - Districts 13 & 14': 1 rows; exact GUID body_includes override; example UID `b331e0b983bbad52`, GUID `203152`, date `2014-08-26T00:00:00+00:00`.

- 'District 1 Tele-Budget Town Hall Meeting': 1 rows; exact GUID body_includes override; example UID `e7820770370af54c`, GUID `203201`, date `2021-08-12T00:00:00+00:00`.

- 'District 10 Tele-Budget Town Hall Meeting': 1 rows; exact GUID body_includes override; example UID `a1c9a272847dd9ff`, GUID `201678`, date `2021-08-19T00:00:00+00:00`.

- 'District 13 Tele-Budget Town Hall Meeting': 1 rows; exact GUID body_includes override; example UID `dbf151b017f84d3b`, GUID `201676`, date `2021-08-16T00:00:00+00:00`.

- 'District 4 Tele-Budget Town Hall Meeting': 1 rows; exact GUID body_includes override; example UID `6a684fdba539fb1f`, GUID `203203`, date `2021-08-26T00:00:00+00:00`.

- 'District 5 Tele-Budget Town Hall Meeting': 1 rows; exact GUID body_includes override; example UID `e5017b067760f4bd`, GUID `203200`, date `2021-08-12T00:00:00+00:00`.

- 'District 7 Tele-Budget Town Hall Meeting': 1 rows; exact GUID body_includes override; example UID `0844b0ddb99a7012`, GUID `201677`, date `2021-08-14T00:00:00+00:00`.

- 'District 8 Tele-Budget Town Hall Meeting': 1 rows; exact GUID body_includes override; example UID `8245b3b974aa56a9`, GUID `201679`, date `2021-08-19T00:00:00+00:00`.

- 'Nowitzki Way Ceremony': 1 rows; exact GUID body_includes override; example UID `5f2ddd7774de5beb`, GUID `205542`, date `2019-10-30T00:00:00+00:00`.

- 'Redistricting Town Hall': 2 rows; exact GUID body_includes override; example UID `93f5b36bb7dbabe9`, GUID `204507`, date `2021-12-16T00:00:00+00:00`.

- 'Saturday Redistricting Commission': 1 rows; exact GUID body_includes override; example UID `bd172b762dd20b63`, GUID `203320`, date `2022-01-22T00:00:00+00:00`.

- 'Special Presentation': 1 rows; exact GUID body_includes override; example UID `7ab50cd4b16dfe33`, GUID `205546`, date `2020-05-20T00:00:00+00:00`.

- 'State of the City': 1 rows; exact GUID body_includes override; example UID `7b34cc9a42c46678`, GUID `205519`, date `2017-12-05T00:00:00+00:00`.

- 'State of the City Address': 2 rows; exact GUID body_includes override; example UID `ad2ff304d8858604`, GUID `205533`, date `2018-12-04T00:00:00+00:00`.

- 'Virtual Town Hall Meeting - District 4': 1 rows; exact GUID body_includes override; example UID `2208bbc7f509d599`, GUID `203147`, date `2014-08-18T00:00:00+00:00`.

- 'Virtual Town Hall Meeting - Districts 9 & 10': 1 rows; exact GUID body_includes override; example UID `edae586dd64d4dd0`, GUID `203146`, date `2014-08-12T00:00:00+00:00`.

### Explicit joint labels (do not treat as committee-only)

## denton-tx-city-council — source `3b7588310856`
Archive 2176 UID rows; Council selector matches 770; taxonomy candidates 16; explicit joint/luncheon candidates 37.

### Flagged labels and rule causes

- 'Denton State of the City': 1 rows; exact GUID body_includes override; example UID `28ff6b1f1a1d047e`, GUID `112591`, date `2021-02-04T00:00:00+00:00`.

- 'Downtown Denton Tax Increment Financing Zone No. 1 Board on 2020-07-22 12:00 PM': 1 rows; exact GUID body_includes override; example UID `2b458f3c556df459`, GUID `73923`, date `2020-07-22T00:00:00+00:00`.

- 'Downtown Denton Tax Increment Financing Zone No. 1 Board on 2020-09-23 12:00 PM': 1 rows; exact GUID body_includes override; example UID `69d3fee77a42058c`, GUID `78064`, date `2020-09-23T00:00:00+00:00`.

- 'Downtown Denton Tax Increment Financing Zone No. 1 Board on 2020-10-21 9:00 AM (Special Called)': 1 rows; exact GUID body_includes override; example UID `5a2ce8afb18c6a82`, GUID `87440`, date `2020-10-21T00:00:00+00:00`.

- 'Downtown Denton Tax Increment Financing Zone No. 1 Board on 2021-02-05 12:00 PM': 1 rows; exact GUID body_includes override; example UID `70a6450a1deda017`, GUID `112601`, date `2021-02-05T00:00:00+00:00`.

- 'Downtown Denton Tax Increment Financing Zone No. 1 Board on 2021-03-24 12:00 PM': 1 rows; exact GUID body_includes override; example UID `2493e59c0844a4b4`, GUID `116732`, date `2021-03-24T00:00:00+00:00`.

- 'Downtown Denton Tax Increment Financing Zone No. 1 Board on 2021-05-26 12:00 PM': 1 rows; exact GUID body_includes override; example UID `a9089a57a1557991`, GUID `122084`, date `2021-05-26T00:00:00+00:00`.

- 'Health & Building Standards Commission on 2021-04-22 3:00 PM': 1 rows; exact GUID body_includes override; example UID `7abd583b2ed8189a`, GUID `119886`, date `2021-04-22T00:00:00+00:00`.

- 'Health & Building Standards Commission on 2021-05-06 3:00 PM': 1 rows; exact GUID body_includes override; example UID `d419e8107262d0d0`, GUID `120506`, date `2021-05-06T00:00:00+00:00`.

- 'Health & Building Standards Commission on 2021-05-13 12:00 PM': 1 rows; exact GUID body_includes override; example UID `e7c1990b7a8e539e`, GUID `120835`, date `2021-05-13T00:00:00+00:00`.

- 'Health & Building Standards Commission on 2021-06-03 3:00 PM': 1 rows; exact GUID body_includes override; example UID `280db52d91547dd6`, GUID `122398`, date `2021-06-03T00:00:00+00:00`.

- 'Library Board on 2021-04-08 3:30 PM': 1 rows; exact GUID body_includes override; example UID `c10f3f6cd97ac38f`, GUID `118306`, date `2021-04-08T00:00:00+00:00`.

- 'Library Board on 2021-05-13 3:30 PM': 1 rows; exact GUID body_includes override; example UID `b50c6a018b2c08e7`, GUID `120859`, date `2021-05-13T00:00:00+00:00`.

- 'Special Citizens Bond Advisory Committee on 2019-06-06 6:00 PM': 1 rows; exact GUID body_includes override; example UID `04689e3929ac104e`, GUID `28865`, date `2019-06-06T00:00:00+00:00`.

- 'Tax Increment Reinvestment Zone No. 2 Board on 2017-07-12 12:00 PM': 1 rows; exact GUID body_includes override; example UID `c26f2f99a4bdaf8f`, GUID `14400`, date `2017-07-12T00:00:00+00:00`.

- 'Tax Increment Reinvestment Zone No. 2 Board on 2021-01-13 11:00 AM (Special Called Meeting)': 1 rows; exact GUID body_includes override; example UID `1c91182b70afefb3`, GUID `111521`, date `2021-01-13T00:00:00+00:00`.

### Explicit joint labels (do not treat as committee-only)

- 'Planning and Zoning Commission on 2023-12-05 11:00 AM (Joint Meeting with City Council and Parks, Recreation and Beautification Board)': 1 rows

- 'City Council on 2026-04-07 12:00 PM (Joint Special Called Meeting with Denton ISD Board of Directors)': 1 rows

- 'City Council on 2022-11-29 11:30 AM (Joint Special Called Meeting with the Denton Independent School District Board of Directors)': 1 rows

- 'City Council on 2021-06-07 1:00 PM (JOINT MEETING WITH DENTON HOUSING AUTHORITY BOARD OF COMMISSIONERS)': 1 rows

- 'City Council Luncheon': 4 rows

- 'City Council on 2021-03-23 11:30 AM (JOINT MEETING WITH THE PUBLIC UTILITIES BOARD)': 1 rows

- 'City Council Joint Meeting': 3 rows

- 'Parks, Recreation and Beautification Board on 2023-12-05 11:00 AM (Joint Meeting with City Council and Planning and Zoning)': 1 rows

- 'City Council on 2019-12-02 11:30 AM (AMENDED -  Joint Meeting with Denton Independent School District Board of Directors)': 1 rows

- 'City Council on 2022-08-30 11:30 AM (Joint Special Called Meeting with the Denton Independent School District Board of Directors)': 1 rows

- 'City Council on 2025-04-01 11:00 AM (Joint Special Called Meeting with Denton ISD Board of Directors)': 1 rows

- 'Northeast Denton Area Plan Steering Committee on 2023-08-01 11:00 AM (Special Called Joint Meeting with the City Council & Planning and Zoning Commission)': 1 rows

- 'Public Utilities Board on 2021-04-05 11:00 AM (JOINT MEETING WITH CITY COUNCIL)': 1 rows

- 'City Council on 2024-03-19 11:00 AM (Joint Meeting with Planning and Zoning Commission)': 1 rows

- 'City Council on 2025-10-21 12:00 PM (Joint Special Called Meeting with Denton ISD Board of Directors)': 1 rows

- 'Public Utilities Board on 2021-03-23 11:30 AM (JOINT MEETING WITH THE CITY COUNCIL)': 1 rows

- 'City Council on 2023-08-01 11:00 AM (Joint with Planning & Zoning Comm. and NE Denton Area Plan Steering Cmte.)': 1 rows

- 'City Council on 2023-04-04 11:30 AM (Joint Special Called Meeting with Denton ISD Board of Directors)': 1 rows

- 'City Council on 2022-10-18 12:00 PM (Special Called Joint Meeting with the Planning and Zoning Commission)': 1 rows

- 'City Council on 2024-04-02 11:00 AM (Joint Special Called Meeting with Denton ISD Board of Directors)': 1 rows

- 'Joint Luncheon with Library Board': 1 rows

- 'Joint City Council/DISD Luncheon': 1 rows

- 'Planning and Zoning Commission on 2023-08-01 11:00 AM (Joint Meeting with City Council & Northeast Denton Area Plan Steering Committee)': 1 rows

- 'City Council on 2023-12-05 11:00 AM (Joint Meeting with Planning and Zoning and Parks, Recreation and Beautification Board)': 1 rows

- 'City Council on 2023-11-07 11:30 AM (Joint Special Called Meeting with Denton ISD Board of Directors)': 1 rows

- 'City Council/DISD Joint Meeting': 1 rows

- 'City Council on 2019-09-09 11:30 AM (Special Called/Joint Meeting with the Economic Development Partnership Board)': 1 rows

- 'City Council on 2022-02-14 10:00 AM (Special Called Joint Meeting with the Planning & Zoning Commission)': 1 rows

- 'Planning and Zoning Commission on 2022-02-14 10:00 AM (Special Called Joint Meeting with the City Council)': 1 rows

- 'Planning and Zoning Commission on 2022-10-18 12:00 PM (Special Called Joint Meeting with the Denton City Council)': 1 rows

- 'City Council on 2021-03-01 11:30 AM (Joint Meeting with the Denton Independent School District Board of Trustees)': 1 rows

- 'City Council on 2022-11-15 11:30 AM (Special Called Meeting with Denton Housing Authority Board of Commissioners)': 1 rows

## fort-worth-tx-city-council — source `6540eef2dc2e`
Archive 5896 UID rows; Council selector matches 1522; taxonomy candidates 32; explicit joint/luncheon candidates 44.

### Flagged labels and rule causes

- '"Listening Circle with Dr. J": A Discussion on Egrets "Listening Circle with Dr. J": A Discussion on Egrets': 2 rows; exact GUID body_includes override; example UID `c8381520c8366ffa`, GUID `https://fortworthgov.granicus.com/MediaPlayer.php?view_id=12&clip_id=4471`, date `2021-07-22T00:00:00+00:00`.

- 'BOARD OF TRUSTEES OF EAGLE MOUNTAIN-SAGINAW ISD AND THE FORT WORTH CITY COUNCIL BOARD OF TRUSTEES OF EAGLE MOUNTAIN-SAGINAW ISD AND THE FORT WORTH CITY COUNCIL': 1 rows; normal body/body_any substring or glob; example UID `497b3aa05255bfb9`, GUID `https://fortworthgov.granicus.com/MediaPlayer.php?view_id=9&clip_id=2491`, date `2016-04-25T00:00:00+00:00`.

- 'Budget Work Session, Property Management and Environmental Services Committee, and Community Development Committee Budget Work Session, Property Management and Environmental Services Committee, and Community Development Committee': 2 rows; exact GUID body_includes override; example UID `081d2695d7d0136a`, GUID `https://fortworthgov.granicus.com/MediaPlayer.php?view_id=9&clip_id=6334`, date `2026-08-11T00:00:00+00:00`.

- 'City Council - Gas Drilling Task Force Workshop City Council - Gas Drilling Task Force Workshop': 1 rows; normal body/body_any substring or glob; example UID `0d5832dbd08e6dd8`, GUID `https://fortworthgov.granicus.com/MediaPlayer.php?view_id=12&clip_id=127`, date `2008-10-14T00:00:00+00:00`.

- 'City Council - Gas Drilling Task Force Workshop of October 14 City Council - Gas Drilling Task Force Workshop of October 14': 1 rows; normal body/body_any substring or glob; example UID `27254aa1a700569f`, GUID `https://fortworthgov.granicus.com/MediaPlayer.php?view_id=9&clip_id=125`, date `2008-10-21T00:00:00+00:00`.

- 'District 2 Town Hall District 2 Town Hall': 2 rows; exact GUID body_includes override; example UID `1b83f54ac25739e0`, GUID `https://fortworthgov.granicus.com/MediaPlayer.php?view_id=10&clip_id=3876`, date `2020-08-20T00:00:00+00:00`.

- 'District 8 Town Hall - 2021 Budget District 8 Town Hall - 2021 Budget': 2 rows; exact GUID body_includes override; example UID `3a3ff477ec35f844`, GUID `https://fortworthgov.granicus.com/MediaPlayer.php?view_id=12&clip_id=3868`, date `2020-08-19T00:00:00+00:00`.

- 'District 8 Town Hall District 8 Town Hall': 4 rows; exact GUID body_includes override; example UID `1d72f25c466aa552`, GUID `https://fortworthgov.granicus.com/MediaPlayer.php?view_id=10&clip_id=5646`, date `2024-09-26T00:00:00+00:00`.

- 'District 9 Budget Town Hall District 9 Budget Town Hall': 2 rows; exact GUID body_includes override; example UID `c135d06bf4c7952e`, GUID `https://fortworthgov.granicus.com/MediaPlayer.php?view_id=12&clip_id=3857`, date `2020-08-17T00:00:00+00:00`.

- 'District Two Town Hall District Two Town Hall': 2 rows; exact GUID body_includes override; example UID `57f734a8c9bfb489`, GUID `https://fortworthgov.granicus.com/MediaPlayer.php?view_id=12&clip_id=3780`, date `2020-07-07T00:00:00+00:00`.

- 'Evans & Rosedale Urban Village: Project Launch Meeting Evans & Rosedale Urban Village: Project Launch Meeting': 2 rows; exact GUID body_includes override; example UID `5fc98bfab9a16cda`, GUID `https://fortworthgov.granicus.com/MediaPlayer.php?view_id=12&clip_id=4543`, date `2021-09-16T00:00:00+00:00`.

- 'Pride Month Celebration of June 27, 2024 Pride Month Celebration of June 27, 2024': 2 rows; exact GUID body_includes override; example UID `9aa34fdd4daeeb32`, GUID `https://fortworthgov.granicus.com/MediaPlayer.php?view_id=10&clip_id=5568`, date `2024-06-27T00:00:00+00:00`.

- 'SPECIAL CALLED MEETING OF THE FORT WORTH CRIME CONTROL AND PREVENTION DISTRICT BOARD OF DIRECTORS AND SPECIAL MEETING OF THE FORT WORTH CITY COUNCIL SPECIAL CALLED MEETING OF THE FORT WORTH CRIME CONTROL AND PREVENTION DISTRICT BOARD OF DIRECTORS AND SPECIAL MEETING OF THE FORT WORTH CITY COUNCIL': 2 rows; normal body/body_any substring or glob; example UID `0c66fc968402fabd`, GUID `https://fortworthgov.granicus.com/MediaPlayer.php?view_id=10&clip_id=3814`, date `2020-07-24T00:00:00+00:00`.

- 'Serve and Protect...I Am NOT A Threat Seminar 08-02-20 Serve and Protect...I Am NOT A Threat Seminar': 2 rows; exact GUID body_includes override; example UID `1a239d9bedcaf5cd`, GUID `https://fortworthgov.granicus.com/MediaPlayer.php?view_id=10&clip_id=3822`, date `2020-08-03T00:00:00+00:00`.

- 'Special Called Meeting to Draw and Submit a Proposed Ten-District Map to the Redistricting Task Force Special Called Meeting to Draw and Submit a Proposed Ten-District Map to the Redistricting Task Force': 1 rows; exact GUID body_includes override; example UID `8337512cd0dbc763`, GUID `https://fortworthgov.granicus.com/MediaPlayer.php?view_id=9&clip_id=4603`, date `2021-11-16T00:00:00+00:00`.

- 'State Of The City 2024 State Of The City 2024': 2 rows; exact GUID body_includes override; example UID `629a171ac9f9ab77`, GUID `https://fortworthgov.granicus.com/MediaPlayer.php?view_id=10&clip_id=5666`, date `2024-10-24T00:00:00+00:00`.

- 'State of the City 2025 State of the City 2025': 2 rows; exact GUID body_includes override; example UID `19bb22712b099ae7`, GUID `https://fortworthgov.granicus.com/MediaPlayer.php?view_id=10&clip_id=5988`, date `2025-10-16T00:00:00+00:00`.

### Explicit joint labels (do not treat as committee-only)

- 'City Council/Plan Commission/Zoning Commission Joint Meeting City Council/Plan Commission/Zoning Commission Joint Meeting': 2 rows

- "JOINT FORT WORTH CITY COUNCIL/FORT WORTH EMPLOYEES' RETIREMENT FUND BOARD MEETING JOINT FORT WORTH CITY COUNCIL/FORT WORTH EMPLOYEES' RETIREMENT FUND BOARD MEETING": 2 rows

- 'City Council / Retirement Board Joint Meeting City Council / Retirement Board Joint Meeting': 1 rows

- 'JOINT MEETING OF FORT WORTH CITY COUNCIL AND FORT WORTH ADVISORY COMMISSION ON ENDING HOMELESSNESS JOINT MEETING OF FORT WORTH CITY COUNCIL AND FORT WORTH ADVISORY COMMISSION ON ENDING HOMELESSNESS': 1 rows

- 'Joint City Council / Retirement Board Meeting Joint City Council / Retirement Board Meeting': 2 rows

- 'JOINT WORK SESSION OF FORT WORTH CITY COUNCIL AND FORT WORTH TRANSPORTATION AUTHORITY BOARD OF DIRECTORS JOINT WORK SESSION OF FORT WORTH CITY COUNCIL AND FORT WORTH TRANSPORTATION AUTHORITY BOARD OF DIRECTORS': 1 rows

- "Joint Fort Worth City Council/Fort Worth Employees' Retirement Fund Board Meeting": 1 rows

- "Joint Fort Worth City Council/Fort Worth Employees' Retirement Fund Board Meeting Joint Fort Worth City Council/Fort Worth Employees' Retirement Fund Board Meeting": 6 rows

- 'JOINT MEETING OF FORT WORTH CITY COUNCIL AND CRIME CONTROL AND PREVENTION DISTRICT BOARD OF DIRECTORS JOINT MEETING OF FORT WORTH CITY COUNCIL AND CRIME CONTROL AND PREVENTION DISTRICT BOARD OF DIRECTORS': 4 rows

- "Joint City Council and Employees' Retirement Fund Board Joint City Council and Employees' Retirement Fund Board": 1 rows

- 'SPECIAL CALLED JOINT MEETING OF FORT WORTH CITY COUNCIL AND THE FORT WORTH HOUSING FINANCE CORPORATION BOARD OF DIRECTORS SPECIAL CALLED JOINT MEETING OF FORT WORTH CITY COUNCIL AND THE FORT WORTH HOUSING FINANCE CORPORATION BOARD OF DIRECTORS': 1 rows

- 'Special Called Joint Meeting With Redistricting Task Force Special Called Joint Meeting With Redistricting Task Force': 1 rows

- 'JOINT CITY COUNCIL AND RETIREMENT BOARD MEETING JOINT CITY COUNCIL AND RETIREMENT BOARD MEETING': 1 rows

- "Joint Fort Worth City Council/Fort Worth Employees' Retirement Fund Board Joint Fort Worth City Council/Fort Worth Employees' Retirement Fund Board": 1 rows

- 'FORT WORTH CITY COUNCIL WORKSHOP WITH TARRANT COUNTY AND INDEPENDENT SCHOOL BOARD REPRESENTATIVES FORT WORTH CITY COUNCIL WORKSHOP WITH TARRANT COUNTY AND INDEPENDENT SCHOOL BOARD REPRESENTATIVES': 1 rows

- 'JOINT CITY COUNCIL AND EMPLOYEE RETIREMENT FUND BOARD JOINT CITY COUNCIL AND EMPLOYEE RETIREMENT FUND BOARD': 1 rows

- 'JOINT MEETING OF THE CITY COUNCIL AND FORT WORTH HOUSING SOLUTIONS JOINT MEETING OF THE CITY COUNCIL AND FORT WORTH HOUSING SOLUTIONS': 1 rows

- 'JOINT MEETING OF FORT WORTH CITY COUNCIL AND FORT WORTH TRANSPORTATION AUTHORITY BOARD OF DIRECTORS JOINT MEETING OF FORT WORTH CITY COUNCIL AND FORT WORTH TRANSPORTATION AUTHORITY BOARD OF DIRECTORS': 3 rows

- "JOINT FORT WORTH CITY COUNCIL/EMPLOYEES' RETIREMENT FUND BOARD MEETING JOINT FORT WORTH CITY COUNCIL/EMPLOYEES' RETIREMENT FUND BOARD MEETING": 1 rows

- 'JOINT FORT WORTH CITY COUNCIL/FORT WORTH ADVISORY COMMISSION ON ENDING HOMELESSNESS MEETING JOINT FORT WORTH CITY COUNCIL/FORT WORTH ADVISORY COMMISSION ON ENDING HOMELESSNESS MEETING': 1 rows

- 'JOINT FORT WORTH CITY COUNCIL/FORT WORTH EMPLOYEES’ RETIREMENT FUND BOARD MEETING JOINT FORT WORTH CITY COUNCIL/FORT WORTH EMPLOYEES’ RETIREMENT FUND BOARD MEETING': 1 rows

- 'Joint City Council and Park and Recreation Advisory Board Joint City Council and Park and Recreation Advisory Board': 1 rows

- 'Joint City Council and Park and Recreation Advisory Board': 1 rows

- 'JOINT WORK SESSION OF FORT WORTH CITY COUNCIL AND HISTORIC AND CULTURAL LANDMARKS COMMISSION JOINT WORK SESSION OF FORT WORTH CITY COUNCIL AND HISTORIC AND CULTURAL LANDMARKS COMMISSION': 1 rows

- "Joint Fort Worth City Council/Fort Worth Employees' Retirement Fund Board": 1 rows

- "JOINT FORT WORTH CITY COUNCIL / FORT WORTH EMPLOYEES' RETIREMENT FUND BOARD MEETING JOINT FORT WORTH CITY COUNCIL / FORT WORTH EMPLOYEES' RETIREMENT FUND BOARD MEETING": 1 rows

- 'JOINT MEETING OF FORT WORTH CITY COUNCIL AND FORT WORTH CRIME CONTROL AND PREVENTION DISTRICT BOARD OF DIRECTORS JOINT MEETING OF FORT WORTH CITY COUNCIL AND FORT WORTH CRIME CONTROL AND PREVENTION DISTRICT BOARD OF DIRECTORS': 1 rows

- "JOINT CITY COUNCIL/FORT WORTH EMPLOYEES' RETIREMENT FUND BOARD MEETING JOINT CITY COUNCIL/FORT WORTH EMPLOYEES' RETIREMENT FUND BOARD MEETING": 1 rows

- 'JOINT MEETING OF FORT WORTH CITY COUNCIL/FORT WORTH INDEPENDENT SCHOOL BOARD OF TRUSTEES JOINT MEETING OF FORT WORTH CITY COUNCIL/FORT WORTH INDEPENDENT SCHOOL BOARD OF TRUSTEES': 1 rows

- "JOINT FORT WORTH CITY COUNCIL AND FORT WORTH EMPLOYEES' RETIREMENT FUND BOARD MEETING JOINT FORT WORTH CITY COUNCIL AND FORT WORTH EMPLOYEES' RETIREMENT FUND BOARD MEETING": 1 rows

- "JOINT FORT WORTH CITY COUNCIL AND FORT WORTH EMPLOYEES' RETIREMENT FUND BOARD JOINT FORT WORTH CITY COUNCIL AND FORT WORTH EMPLOYEES' RETIREMENT FUND BOARD": 1 rows

## pflugerville-tx-city-council — source `19c2ffe87d08`
Archive 948 UID rows; Council selector matches 481; taxonomy candidates 0; explicit joint/luncheon candidates 2.

### Flagged labels and rule causes

### Explicit joint labels (do not treat as committee-only)

- 'City Council Joint Special Meeting': 1 rows

- 'City Council Special Joint Meeting': 1 rows

## Cross-feed overlap and qualified conclusions


### arlington-tx-council

- UID `1792515fdd51c6c2` / GUID `https://arlingtontx.granicus.com/MediaPlayer.php?view_id=2&clip_id=3443`: Environmental Task Force; additional current selector matches: arlington-tx.

- UID `237c28f30496e3e0` / GUID `https://arlingtontx.granicus.com/MediaPlayer.php?view_id=2&clip_id=3453`: Community and Neighborhood Development Committee; additional current selector matches: arlington-tx.

- UID `25e9b8510b0a7a66` / GUID `https://arlingtontx.granicus.com/MediaPlayer.php?view_id=2&clip_id=3494`: Environmental Task Force Meeting; additional current selector matches: arlington-tx.

- UID `2d5dc451df2da130` / GUID `https://arlingtontx.granicus.com/MediaPlayer.php?view_id=2&clip_id=3454`: Environmental Task Force Meeting; additional current selector matches: arlington-tx.

- UID `30b28efc23346a6d` / GUID `https://arlingtontx.granicus.com/MediaPlayer.php?view_id=2&clip_id=3505`: Community and Neighborhood Development Committee; additional current selector matches: arlington-tx.

- UID `35b22fcc8d07967b` / GUID `https://arlingtontx.granicus.com/MediaPlayer.php?view_id=2&clip_id=3424`: Environmental Task Force Meeting; additional current selector matches: arlington-tx.

- UID `4357f0c39d1ae58f` / GUID `https://arlingtontx.granicus.com/MediaPlayer.php?view_id=2&clip_id=3480`: Community and Neighborhood Development Committee; additional current selector matches: arlington-tx.

- UID `9b66e98b682a47f2` / GUID `https://arlingtontx.granicus.com/MediaPlayer.php?view_id=2&clip_id=3508`: Environmental Task Force Meeting; additional current selector matches: arlington-tx.

- UID `a683af6e13e2bd7a` / GUID `https://arlingtontx.granicus.com/MediaPlayer.php?view_id=2&clip_id=3479`: Environmental Task Force Meeting; additional current selector matches: arlington-tx.

- UID `d8f3b672b482369b` / GUID `https://arlingtontx.granicus.com/MediaPlayer.php?view_id=2&clip_id=3493`: Community and Neighborhood Development Committee; additional current selector matches: arlington-tx.

### dallas-tx-city-council

- UID `baa19208405acb22` / GUID `280220`: 2024 Bond CBTF and Subcommittee Chairs Meeting; additional current selector matches: none.

- UID `bd172b762dd20b63` / GUID `203320`: Saturday Redistricting Commission; additional current selector matches: dallas-tx-redistricting-commission.

### denton-tx-city-council

- UID `04689e3929ac104e` / GUID `28865`: Special Citizens Bond Advisory Committee on 2019-06-06 6:00 PM; additional current selector matches: none.

- UID `1c91182b70afefb3` / GUID `111521`: Tax Increment Reinvestment Zone No. 2 Board on 2021-01-13 11:00 AM (Special Called Meeting); additional current selector matches: none.

- UID `2493e59c0844a4b4` / GUID `116732`: Downtown Denton Tax Increment Financing Zone No. 1 Board on 2021-03-24 12:00 PM; additional current selector matches: none.

- UID `280db52d91547dd6` / GUID `122398`: Health & Building Standards Commission on 2021-06-03 3:00 PM; additional current selector matches: none.

- UID `2b458f3c556df459` / GUID `73923`: Downtown Denton Tax Increment Financing Zone No. 1 Board on 2020-07-22 12:00 PM; additional current selector matches: none.

- UID `5a2ce8afb18c6a82` / GUID `87440`: Downtown Denton Tax Increment Financing Zone No. 1 Board on 2020-10-21 9:00 AM (Special Called); additional current selector matches: none.

- UID `69d3fee77a42058c` / GUID `78064`: Downtown Denton Tax Increment Financing Zone No. 1 Board on 2020-09-23 12:00 PM; additional current selector matches: none.

- UID `70a6450a1deda017` / GUID `112601`: Downtown Denton Tax Increment Financing Zone No. 1 Board on 2021-02-05 12:00 PM; additional current selector matches: none.

- UID `7abd583b2ed8189a` / GUID `119886`: Health & Building Standards Commission on 2021-04-22 3:00 PM; additional current selector matches: none.

- UID `a1aa4af8f26e120c` / GUID `13509`: Joint Luncheon with Library Board; additional current selector matches: none.

- UID `a9089a57a1557991` / GUID `122084`: Downtown Denton Tax Increment Financing Zone No. 1 Board on 2021-05-26 12:00 PM; additional current selector matches: none.

- UID `b50c6a018b2c08e7` / GUID `120859`: Library Board on 2021-05-13 3:30 PM; additional current selector matches: none.

- UID `c10f3f6cd97ac38f` / GUID `118306`: Library Board on 2021-04-08 3:30 PM; additional current selector matches: none.

- UID `c26f2f99a4bdaf8f` / GUID `14400`: Tax Increment Reinvestment Zone No. 2 Board on 2017-07-12 12:00 PM; additional current selector matches: none.

- UID `d419e8107262d0d0` / GUID `120506`: Health & Building Standards Commission on 2021-05-06 3:00 PM; additional current selector matches: none.

- UID `e7c1990b7a8e539e` / GUID `120835`: Health & Building Standards Commission on 2021-05-13 12:00 PM; additional current selector matches: none.

### fort-worth-tx-city-council

- UID `081d2695d7d0136a` / GUID `https://fortworthgov.granicus.com/MediaPlayer.php?view_id=9&clip_id=6334`: Budget Work Session, Property Management and Environmental Services Committee, and Community Development Committee Budget Work Session, Property Management and Environmental Services Committee, and Community Development Committee; additional current selector matches: fort-worth-tx-city-council-worksession, fort-worth-tx-community-development-committee, fort-worth-tx-property-management-and-environmental-services-committee.

- UID `0c66fc968402fabd` / GUID `https://fortworthgov.granicus.com/MediaPlayer.php?view_id=10&clip_id=3814`: SPECIAL CALLED MEETING OF THE FORT WORTH CRIME CONTROL AND PREVENTION DISTRICT BOARD OF DIRECTORS AND SPECIAL MEETING OF THE FORT WORTH CITY COUNCIL SPECIAL CALLED MEETING OF THE FORT WORTH CRIME CONTROL AND PREVENTION DISTRICT BOARD OF DIRECTORS AND SPECIAL MEETING OF THE FORT WORTH CITY COUNCIL; additional current selector matches: fort-worth-tx-crime-control-and-prevention-district.

- UID `0d5832dbd08e6dd8` / GUID `https://fortworthgov.granicus.com/MediaPlayer.php?view_id=12&clip_id=127`: City Council - Gas Drilling Task Force Workshop City Council - Gas Drilling Task Force Workshop; additional current selector matches: none.

- UID `1cbf75c664b7c63e` / GUID `https://fortworthgov.granicus.com/MediaPlayer.php?view_id=9&clip_id=1251`: CITY COUNCIL GAS DRILLING WORKSHOP CITY COUNCIL GAS DRILLING WORKSHOP; additional current selector matches: none.

- UID `1d3f5b988918cba0` / GUID `https://fortworthgov.granicus.com/MediaPlayer.php?view_id=10&clip_id=2292`: JOINT MEETING OF FORT WORTH CITY COUNCIL AND CRIME CONTROL AND PREVENTION DISTRICT BOARD OF DIRECTORS JOINT MEETING OF FORT WORTH CITY COUNCIL AND CRIME CONTROL AND PREVENTION DISTRICT BOARD OF DIRECTORS; additional current selector matches: fort-worth-tx-crime-control-and-prevention-district.

- UID `27254aa1a700569f` / GUID `https://fortworthgov.granicus.com/MediaPlayer.php?view_id=9&clip_id=125`: City Council - Gas Drilling Task Force Workshop of October 14 City Council - Gas Drilling Task Force Workshop of October 14; additional current selector matches: none.

- UID `497b3aa05255bfb9` / GUID `https://fortworthgov.granicus.com/MediaPlayer.php?view_id=9&clip_id=2491`: BOARD OF TRUSTEES OF EAGLE MOUNTAIN-SAGINAW ISD AND THE FORT WORTH CITY COUNCIL BOARD OF TRUSTEES OF EAGLE MOUNTAIN-SAGINAW ISD AND THE FORT WORTH CITY COUNCIL; additional current selector matches: none.

- UID `6a1dd0e9e8088440` / GUID `https://fortworthgov.granicus.com/MediaPlayer.php?view_id=12&clip_id=155`: City Council Gas Drilling Workshop City Council Gas Drilling Workshop; additional current selector matches: none.

- UID `83d6abdb749bc76f` / GUID `https://fortworthgov.granicus.com/MediaPlayer.php?view_id=10&clip_id=2015`: JOINT MEETING OF FORT WORTH CITY COUNCIL AND CRIME CONTROL AND PREVENTION DISTRICT BOARD OF DIRECTORS JOINT MEETING OF FORT WORTH CITY COUNCIL AND CRIME CONTROL AND PREVENTION DISTRICT BOARD OF DIRECTORS; additional current selector matches: fort-worth-tx-crime-control-and-prevention-district.

- UID `99152cb9e61fa091` / GUID `https://fortworthgov.granicus.com/MediaPlayer.php?view_id=11&clip_id=3814`: SPECIAL CALLED MEETING OF THE FORT WORTH CRIME CONTROL AND PREVENTION DISTRICT BOARD OF DIRECTORS AND SPECIAL MEETING OF THE FORT WORTH CITY COUNCIL SPECIAL CALLED MEETING OF THE FORT WORTH CRIME CONTROL AND PREVENTION DISTRICT BOARD OF DIRECTORS AND SPECIAL MEETING OF THE FORT WORTH CITY COUNCIL; additional current selector matches: fort-worth-tx-crime-control-and-prevention-district.

- UID `b7a9ee9eae6fafc6` / GUID `https://fortworthgov.granicus.com/MediaPlayer.php?view_id=9&clip_id=153`: City Council Gas Drilling Workshop City Council Gas Drilling Workshop; additional current selector matches: none.

- UID `c57e1210d9282ab1` / GUID `https://fortworthgov.granicus.com/MediaPlayer.php?view_id=9&clip_id=1761`: JOINT MEETING OF FORT WORTH CITY COUNCIL AND FORT WORTH CRIME CONTROL AND PREVENTION DISTRICT BOARD OF DIRECTORS JOINT MEETING OF FORT WORTH CITY COUNCIL AND FORT WORTH CRIME CONTROL AND PREVENTION DISTRICT BOARD OF DIRECTORS; additional current selector matches: fort-worth-tx-crime-control-and-prevention-district.

- UID `e0736bdd5835b1d4` / GUID `https://fortworthgov.granicus.com/MediaPlayer.php?view_id=11&clip_id=2015`: JOINT MEETING OF FORT WORTH CITY COUNCIL AND CRIME CONTROL AND PREVENTION DISTRICT BOARD OF DIRECTORS JOINT MEETING OF FORT WORTH CITY COUNCIL AND CRIME CONTROL AND PREVENTION DISTRICT BOARD OF DIRECTORS; additional current selector matches: fort-worth-tx-crime-control-and-prevention-district.

- UID `ead427dae0bbb284` / GUID `https://fortworthgov.granicus.com/MediaPlayer.php?view_id=5&clip_id=6334`: Budget Work Session, Property Management and Environmental Services Committee, and Community Development Committee Budget Work Session, Property Management and Environmental Services Committee, and Community Development Committee; additional current selector matches: fort-worth-tx-city-council-worksession, fort-worth-tx-community-development-committee, fort-worth-tx-property-management-and-environmental-services-committee.

- UID `f28ae055829705ec` / GUID `https://fortworthgov.granicus.com/MediaPlayer.php?view_id=11&clip_id=2292`: JOINT MEETING OF FORT WORTH CITY COUNCIL AND CRIME CONTROL AND PREVENTION DISTRICT BOARD OF DIRECTORS JOINT MEETING OF FORT WORTH CITY COUNCIL AND CRIME CONTROL AND PREVENTION DISTRICT BOARD OF DIRECTORS; additional current selector matches: fort-worth-tx-crime-control-and-prevention-district.


Arlington: 10 distinct archived UID observations have committee/task-force-only labels, accepted by named body_any rules. Whether Environmental Task Force was continuing versus temporary still needs official evidence; that does not make its proceedings full Council.

Denton: 15 board/commission/committee-only labeled observations accepted solely by exact GUID overrides. One State of the City event adds the sixteenth broad taxonomy candidate. No demonstrated current dedicated feeds for these labels.

Dallas: 2 committee/commission-only observations plus 32 event/town-hall candidates, accepted by exact GUID overrides. Redistricting Commission already has its own configured feed; its GUID also matches Council (demonstrated cross-feed leakage). CBTF/chairs may be temporary and requires official institutional evidence.

Fort Worth: reported 32 taxonomy candidates are NOT 32 committee-only false inclusions. Four observations explicitly name two governing bodies, two combine Budget Work Session with two committees, two name Council task-force workshops. These eight require recording/agenda segmentation proof; the other 24 are event/town-hall/ceremony candidates. Never remove explicit full Council joint proceedings merely because board or committee appears in label.

Pflugerville: no committee/event labels detected among 481 selected UID rows; two explicit joint meeting labels. This is a limited retained-source/label spot-check, not a city-wide guarantee.

Common root cause: mixed provider namespace is intentionally archived broadly; config added exact exceptional GUID inclusions and family body_any terms to avoid coverage gaps, without a sufficiently narrow institution-versus-event taxonomy gate. Arlington, Dallas, Denton, and Fort Worth Council files were last touched by merged #1590 (566df01a5cfbef39cb74a2b345bda6f934a299fd); Pflugerville was last touched by a9ca7722 (R6 admission and clip safeguards). This finding distinguishes archive presence from feed assignment; selectors prove eligibility, not current public RSS exposure.

## Recording-bound follow-up verification (2026-10-04)

These findings narrow the next config contracts; they do not lift the review hold or change feeds.

### Bond committee: official listed recording census reconciled

The Town's [2026 bond program page](https://www.addisontx.gov/Government/Departments/Finance/Bond-Election-History/2026-Bond-Election)
separates committee meetings from later Council consideration. Its four listed committee dates
match all four retained `Bond Advisory Committee` observations:

| Official listed date | Retained GUID | Stable UID |
|---|---|---|
| October 30, 2025 | 359742 | `31b194e3b25f0bf8` |
| November 20, 2025 | 361917 | `1ef623b5f2bc909a` |
| December 4, 2025 | 362796 | `cb34bf9c6001c32a` |
| January 8, 2026 | 371625 | `902400566a6637b5` |

This reconciles the official page's listed recordings, not every possible committee gathering.
The January 13 recommendation was presented during a Council work session; it is not evidence
for relabeling that Council recording as a standalone committee meeting. The separate October 30
[provider recording](https://addisontx.new.swagit.com/videos/359742) names the committee's charge.
This is a bounded bond-program body, with a proposed city bond-family assignment. No config
activation, new audio work, or assumption of continuing standing status follows from this table.
The official page was available through the search index; direct retrieval returned HTTP 403.

### Three-body joints: two institution/date bindings established

- January 24, 2024: GUID `295839`, UID `fcc9bde16c7f47fe`, retained label
  `Joint CPAC, P&Z, and City Council Meeting #1`. The
  [February 21 P&Z packet](https://agendas.addisontx.gov/docs/2024/PZ/20240221_7244/AGENDApacket__02-21-24_0625_7239.pdf)
  identifies minutes of the January 24 joint CPAC, P&Z and Council meeting.
- July 11, 2024: GUID `310072`, UID `c95af66385f6cf31`, retained label
  `Joint CPAC, P&Z, and City Council Meeting #2`. The
  [August 8 CPAC packet](https://agendas.addisontx.gov/docs/2024/CPAC/20240808_7343/AGENDApacket__08-08-24_1136_7338.pdf)
  identifies minutes prepared for consideration for the July 11 joint meeting of all three bodies.

These official references establish joint identity independently of provider substring matching;
placing minutes on an approval agenda does not itself prove an approval vote. Both recordings
currently match Council only in the complete retained-label replay. Under the approved global
joint rule, the proposed subscription correction adds the same stable recording to the existing
P&Z and CPAC feeds, retains Council, and preserves the current canonical search owner. It does
not create three records or three audio objects. Before implementation, the scoped L3 contract
must name exact existing selector fields, replay all retained positives/negatives, and check
feed exposure and canonical ownership. Remaining joint aliases still need their own binding.

### Narrow next slices and unresolved boundaries

1. Council/P&Z ownership: eliminate the 47 commission-only work-session false Council matches
   while preserving genuine Council work sessions and all proven joint participation.
2. Proven three-body joints: add missing subscriptions for the two bindings above; do not
   broaden generic `Joint` matching to unknown participants.
3. Temporary bond-family coverage: use the four reconciled recordings and exact committee label;
   keep subsequent Council deliberations in Council.
4. TIRZ, ceremonies/public input, Citizen Advisory and remaining generic/joint aliases retain
   recording-level proof/exposure gates. Unknown cases remain visible in the census.

These slices require documented L3 file/function/test plans before runtime/config edits.
CodeRabbit remains held until the wider Addison and onboarding settlement is complete.
