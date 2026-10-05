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

## CPAC membership clarification and selector plan refinement

CPAC is a separate community advisory body, not the combined full Council and P&Z. The
[September 19, 2023 P&Z packet](https://agendas.addisontx.gov/docs/2023/PZ/20230919_7146/AGENDApacket__09-19-23_0336_7141.pdf)
specifies up to 25 members: one P&Z representative, 21 resident/business representatives and
three legacy residents. The
[July 11, 2024 joint packet](https://agendas.addisontx.gov/docs/2024/CPAC/20240711_7339/AGENDApacket__07-11-24_1236_7334.pdf)
reports the September 26 appointments using that composition. It also provides direct dated
three-body joint evidence for GUID310072, beyond the later minutes reference.

Therefore preserve `addison-tx-comprehensive-plan-advisory-committee.yml` and its existing feed
URL/history. Standalone CPAC meetings belong there; officially joint CPAC/P&Z/Council recordings
belong in all three participating feeds. The temporary comprehensive-plan remit and current
`dormant` configuration do not erase its historical subscription. Membership is not a rule that
turns a CPAC-only meeting into Council or P&Z.

### Existing-field proposal for the work-session correction (not implemented)

Council currently uses substring `Work Session` in `source.body_any`. The full retained census
shows six labels containing those words. Replace that broad term with existing `source.body_exact`
entries for the five Council candidates below, subject to their official body verification:

| Complete retained label | Raw observations | Proposed disposition |
|---|---:|---|
| Work Session | 23 | Council exact alias, verify official convening |
| Work Session and Regular Meeting | 234 | Council exact alias, verify official convening |
| Special Meeting and Work Session | 15 | Council exact alias, verify official convening |
| Special Work Session | 3 | Council exact alias, verify official convening |
| Work Session - Economic Development Strategic Plan | 1 | Council exact alias, verify convening |
| Planning & Zoning Commission Work Session | 47 | P&Z only; remove false Council match |

The existing broad `Special Meeting` and `City Council` terms require the same full-label review:
removing only `Work Session` does not stabilize onboarding or remove ceremony inclusion. The
Council `Fiscal Year * Budget Workshop` glob also needs an anchored-label negative replay.
Do not invent a regex/exclusion config schema to solve this; existing exact selectors and
source-bound `body_includes` are sufficient for individually proved aliases. The final small
L3 slice must enumerate the complete Council alias set, not implement this provisional subset.

For the two established three-body joints, the minimal proposed addition is existing
`source.body_includes` entries binding GUID295839 and GUID310072 to their exact retained labels
in both existing P&Z and CPAC configs. Council remains selected, and its search ownership is
preserved. This intentionally avoids a general `CPAC` substring that could absorb unrelated
committee-only or public-input recordings. Tests must assert three subscription matches for
each joint UID, one canonical search entry, unchanged standalone CPAC ownership, and no
P&Z-only work-session eligibility in Council. No runtime/config edits or review request occurred.

## Settlement decision register

Already approved: continuing named bodies have dedicated subscriptions; genuine joints use the
same UID/audio in each proven participating feed; temporary programs use approved city families;
ceremonies/promotions are excluded from meeting feeds. No repeat approval is needed for those rules.

Three concrete migration decisions were presented to the maintainer in chat after the request
to finish Addison policy. Until answered, the alternatives below remain proposed:

| Decision | Recommended choice | Alternative |
|---|---|---|
| Town Meetings presentation | Preserve URL; rename Public Input; separate verified briefings | Keep existing display name |
| BZA formally serving as Appeals | One feed, display Zoning Adjustment & Appeals | Separate legal-function subscriptions |
| Unproved recording identity | Preserve current publication; explicit unresolved queue | Hold all corrections for every case |

The first recommendation preserves subscriber URLs while clarifying ownership. It is not a
license to relabel an unidentified recording. The second relies on formal institutional service,
not shared members alone. The third isolates unknown evidence without inventing a body or
quietly deleting already-published material. The public-input migration and one clearly named BZA/Appeals feed were explicitly approved
in chat on 2026-10-04. The earlier unresolved-recording boundary proposal was superseded by the maintainer’s
requirement below: historical cases must remain actively tracked and surfaced by sweeps.

### Additional proven joint correction

GUID `276855`, UID `4ef85ec6540cf95b`, October 17, 2023 currently matches Council only. The
[provider's official agenda](https://addisontx.new.swagit.com/videos/276855) explicitly calls
both Council and P&Z to order and names their presiding officers. The
[January 16, 2024 P&Z packet](https://agendas.addisontx.gov/docs/2024/PZ/20240116_7229/AGENDApacket__01-16-24_0309_7224.pdf)
contains the dated joint minutes and identifies both Mayor and Commission chair at adjournment.
This supports adding the existing recording to P&Z, retaining Council and its canonical search
ownership. It does not support adding CPAC merely because the comprehensive plan was the topic.

### Generic label boundary

The 30 `Combined Meeting` observations span October 2013 through March 2015. A date range or
regular Council calendar pattern does not prove which institutions convened. Preserve current
ownership pending official agenda binding; do not use `Combined` as evidence of a multi-body
joint. Likewise, no CPAC membership/topic rule should claim those recordings.

### Implementation completion checklist

- Record the three maintainer migration decisions above.
- Freeze complete exact Council labels and independently bound joint additions; leave unknown
  generic aliases explicitly unchanged until evidence supports a migration.
- Name the affected feed files, replay tests, exposure checks and expected UID ownership in
  [review/51](../51-unexpected-body-remedy-flow.md); mark only those concrete slices L3 and create their scoped tracking issues.
- Apply verified Addison corrections in small dependent PRs, preserving records/audio/URLs.
- Verify the 822-record, 62-label replay, standalone/joint negatives, RSS/search ownership and
  raw-page preservation; run whole-repository lint/format and offline tests for code changes.
- Stabilize the shared onboarding rules with cross-city counterexamples already recorded here.
- Report remaining evidence gaps and ask whether the classification settlement is sufficient
  to lift the explicit CodeRabbit hold. Do not infer that permission from implementation progress.

### Approved public-input migration (2026-10-04)

Maintainer explicitly approved preserving the existing Town Meetings URL while displaying
`Addison: Public Input`, limiting that subscription to verified town halls, community meetings
and open houses. Verified educational briefings use a separate Public Briefings subscription.
Verified ceremonies/promotional recordings leave podcast subscriptions; archived records and
stable UIDs remain intact. This approval settles the migration direction, not the identity of
unproved municipal-media/Citizen Advisory recordings. Exact recording dispositions, selector
contracts, RSS exposure and preservation tests must precede implementation. CodeRabbit stays held.

### Approved BZA/Appeals presentation (2026-10-04)

Maintainer chose one clearly named feed for the formally combined institutional functions.
Preserve the existing Zoning Adjustment URL and subscriptions; display
`Addison: Zoning Adjustment & Appeals`. This does not create a general rule that shared membership
merges separate institutions. Existing six BZA/Appeals retained observations keep their ownership.

## Historical uncertainty is an active coverage obligation (superseding prior proposal)

Maintainer direction: unresolved episodes generally concern apparent new bodies with insufficient
identity proof. This status must not ignore, defer indefinitely or forget older recordings.
Historical uncertainty is not permission to declare Addison coverage complete. Every such case
remains in the all-record census and the unresolved-episode sweep, including already archived
records and labels currently matched by a feed. Record age never removes that obligation.

### Citizen Advisory 56029: additional retained evidence

Source `abbf5e25e078`, stable UID `63b22f80ce9500ff`, provider GUID `56029`, provider date
June 1, 2007. Reinspection of the actual persisted record establishes ten served chapters and
ten distinct source parts totaling 31,724.448 seconds. Chapter topics are recreation/community
facilities, education, business development, environmental design, museums, performing/visual
arts, public relations, transportation, culinary, and human services. The source media paths
contain a 2009 upload-directory date; that does not supersede the official provider date.
Earlier shorthand implying unavailable chapter evidence is superseded by this retained evidence.
No official transcript or agenda text is stored. Ten parts do not establish ten meeting dates,
ten institutions, or an authority to split stable episodes. The provider index reports eleven
items; reconciliation with the ten retained parts remains a concrete completeness question.

The [FY2007–08 Town budget](https://www.addisontx.gov/files/sharedassets/main/v/1/finance/documents/fy-2007-2008-addison-annual-budget.pdf)
records approximately 85 appointed advisory participants, completion of the advisory process
in FY2007, and review of its recommendations in FY2008. The
[June 12, 2007 Council packet](https://agendas.addisontx.gov/docs/2007/CM/20070612_376/373_2007-06-12%20Agenda_Meeting%20Combined%20Meeting.pdf)
includes June 2 Council minutes discussing receipt of Citizen Advisory recommendations. The
[May 26, 2009 packet](https://agendas.addisontx.gov/docs/2009/CM/20090526_277/274_2009-05-26%20Agenda_Meeting%20Combined%20Meeting.pdf)
identifies the 2007 Next Greatest Ideas advisory process as an input to parks planning.

Recommendation: retain this recording in the existing URL's public-input subscription during
the approved rename, with the provider title/date/UID unchanged. Evidence supports historical
citizen advisory material; it does not justify full Council ownership or a new continuing-body
feed. The exact Next Greatest Ideas linkage is an inference, not established recording binding.
Do not call this an excluded municipal promotion or silently remove it for uncertainty.
Before final closure, reconcile the provider item count and use the retained source/chapter
material or an official program report to establish whether this is meetings, recommendations
or a compiled presentation. Do not spend ASR/provider quota without the relevant approval.

### Required sweep and closure behavior (policy; runtime contract still to mature)

- New apparent bodies lacking proof enter an identity-evidence queue. Historical unresolved
  recordings enter a coverage/correction queue; neither disappears because its label is archived.
- Each case identifies city/source, stable UIDs/GUIDs, observed dates, current feed ownership,
  exact missing evidence, last research result and the next concrete action.
- Every unresolved sweep highlights both queues, including known historical cases and suspected
  false inclusions. A previously matched label or old date is never a suppression criterion.
- Unchanged cases remain visible in the report without posting duplicate issues or notifications.
  Notify on new evidence, failed recovery, an overdue concrete action or a maintainer decision.
- Repeated access failure is documented evidence-access failure, not a classification verdict.
- Close only with an evidenced assignment/exclusion or a specifically approved unresolvable
  disposition. Preserve raw records in every case; do not close #1623 on zero unexpected labels.

Existing `collect_unexpected_bodies` does not meet this contract: it suppresses archived labels
and already-covered records. This is a tracked implementation gap under review/51 P2 evidence,
not a claim that the current automation already performs complete unresolved sweeps.

## #2009 local implementation and retained replay

The approved Public Input rename retains the existing slug/URL and Citizen Advisory identity.
The educational seminar GUID304981 moves to Public Briefings; explicit events56020/56021/56022/
56024/56025 leave those subscriptions. Both formal Appeals functions retain their existing feed
with a clear display title. Raw records/UIDs/audio are untouched.

The verified [seminar page](https://addisontx.new.swagit.com/videos/304981) contains educational
presentations from homelessness-service representatives, supporting briefing rather than Council
classification. Other named ceremonies/events appear individually in the official Community
Events archive; exclusion is subscription-only and does not delete the historical record.

The remaining municipal-media56026/56027/56028 and applicant/staff presentations are active
recording-evidence tasks. P&Z47 Council leakage, four missing Bond subscriptions, TIRZ ownership,
missing proven joint subscriptions and generic-label evidence remain open corrections; this
bounded implementation does not claim all Addison policy work is finished.

Read-only complete replay for #2009: all 822 raw records/62 labels were preserved. Public Input
selects 50 records versus the previous56; the six departures are the one briefing and five
explicit events listed above. Public Briefings selects the same existing seminar UID exactly
once. Every other Addison feed's selected UID set is unchanged, including BZA/Appeals.
These are selector counts, not a claim that every selected audio object is publicly available.

Outstanding historical corrections remain concrete:

| Case | Stable evidence | Next action |
|---|---|---|
| Citizen Advisory compilation | UID63b22f80ce9500ff, GUID56029, ten retained parts | Reconcile provider eleven-item listing and bind program report; keep Public Input |
| Named P&Z work sessions | 47 retained records; example394156 | Narrow Council complete-label rules, preserve P&Z |
| Three proven missing joint subscriptions | GUID295839/310072/276855 | Add exact participating feed bindings; retain canonical owners |
| Older joint variant | GUID55634, September19,2016 | Retrieve official agenda; Council currently missing |
| Bond committee recordings | GUID359742/361917/362796/371625 | Implement reviewed bond-family config; distinguish Council reports |
| TIRZ recording | GUID394474, UID757ab2a4aaf9bbca | Bind board agenda, route to TIF family rather than Council |
| Council swearing-in ceremony | GUID56023, UID1f50e2274a6dd26c | Remove ceremony eligibility with narrow Council selector replay |
| Municipal media | GUID56026/56027/56028 | Inspect actual content, decide briefing/input versus promotion |
| Applicant/staff presentations | GUID56060/56059 | Bind proceeding/public participation; choose family |
| Combined Meeting alias | Thirty2013–15 records | Establish convened institutions from dated agenda records |
| Finance/CPC media gaps | Official held dates; no located recording | Reconcile agenda census/provider views, report unavailable media |

These cases stay visible regardless of age, current matching or archive presence. #1623 remains
the historical coverage tracker; #2009 is the bounded implementation tracker. No sweep runtime
change is included, and the future sweep must consume these existing obligations rather than
only look for labels newly appearing after deployment.

#2009 validation: 4,841 offline tests passed, 14 live tests deselected; 213 targeted publication
and config-editor tests passed. Whole Ruff/format and diff checks passed. No production write,
provider media download, LLM/ASR call, audio invalidation or CodeRabbit request occurred.
Historical obligations were also posted to #1623 in comment5983390336.


Addison verified joint subscriptions (#2011, prepared/unmerged): GUID295839 and310072
now join the existing P&Z and CPAC subscriptions; GUID276855 joins P&Z only. Council
canonical search ownership, recording UIDs and audio are preserved. Complete822-record replay
adds exactly three P&Z and two CPAC memberships, with no removals or other feed changes.
Other historical assignments remain active evidence tasks; CodeRabbit requests remain held.

### Additional historical binding: May27,2014 Combined Meeting

The official dated packet at
https://agendas.addisontx.gov/docs/2014/CM/20140527_426/422_05-27-14_1507_AGENDApacket.pdf
opens with the regular meeting and work session of the City Council, with separate session
start times. Its internal agenda-item header calls this `Combined Meeting`. This establishes
Council-only ownership for that date; it is not a joint Council/P&Z merely because P&Z findings
are discussed. The remaining29 recordings still need dated corroboration. The official provider
Council archive lists the dated recording separately. Do not extrapolate one packet into proof
of every historical Combined Meeting or erase the remaining evidence obligations.


## Cached official agenda reconciliation — 2026-10-04

Read-only retrieval of the existing hosted agenda-backup artifacts establishes the convened
institution for all30 Combined Meeting recordings, not merely a sample. Each dated official
agenda opens with the City Council regular meeting, with most also naming its work session.
These30 remain Council recordings; Combined does not imply an inter-body joint meeting.
The earlier remaining29 evidence obligation is superseded by this individual-row reconciliation.
No new provider scrape, credential use, state writes or audio work occurred.

| GUID | Stored date | Official agenda bound through cached backup |
|---|---|---|
| 55599 | 2013-10-08 | [Official agenda](https://addisontx.new.swagit.com/videos/55599/agenda); [cached artifact](https://audio.citymeetings.fyi/documents/abbf5e25e078/e59549e833479e1d/backup-2a684b64e68a2c3f) |
| 55600 | 2013-10-22 | [Official agenda](https://addisontx.new.swagit.com/videos/55600/agenda); [cached artifact](https://audio.citymeetings.fyi/documents/abbf5e25e078/1e85dcff232fcba8/backup-36744e542f51dc8e) |
| 55601 | 2013-11-12 | [Official agenda](https://addisontx.new.swagit.com/videos/55601/agenda); [cached artifact](https://audio.citymeetings.fyi/documents/abbf5e25e078/78880dfd58cacebd/backup-25b2d61d6ba1f7ed) |
| 55602 | 2013-11-26 | [Official agenda](https://addisontx.new.swagit.com/videos/55602/agenda); [cached artifact](https://audio.citymeetings.fyi/documents/abbf5e25e078/08f6e5fe62fe9730/backup-5d45f764d020a7d3) |
| 55603 | 2013-12-10 | [Official agenda](https://addisontx.new.swagit.com/videos/55603/agenda); [cached artifact](https://audio.citymeetings.fyi/documents/abbf5e25e078/078e31754cb52b70/backup-05e365aa5c08584e) |
| 55604 | 2014-01-14 | [Official agenda](https://addisontx.new.swagit.com/videos/55604/agenda); [cached artifact](https://audio.citymeetings.fyi/documents/abbf5e25e078/133a684fc4fdf68e/backup-cd4514bb07330501) |
| 55605 | 2014-01-28 | [Official agenda](https://addisontx.new.swagit.com/videos/55605/agenda); [cached artifact](https://audio.citymeetings.fyi/documents/abbf5e25e078/2929e7152647c257/backup-49c9a25ff2207584) |
| 55606 | 2014-02-11 | [Official agenda](https://addisontx.new.swagit.com/videos/55606/agenda); [cached artifact](https://audio.citymeetings.fyi/documents/abbf5e25e078/de282deb731a7361/backup-a7616d4ba66e92f8) |
| 55607 | 2014-02-25 | [Official agenda](https://addisontx.new.swagit.com/videos/55607/agenda); [cached artifact](https://audio.citymeetings.fyi/documents/abbf5e25e078/3c114cb1e02ce2c4/backup-dd762db3095f1e1d) |
| 55608 | 2014-03-11 | [Official agenda](https://addisontx.new.swagit.com/videos/55608/agenda); [cached artifact](https://audio.citymeetings.fyi/documents/abbf5e25e078/5c3910b54cff845e/backup-b5f94634144137ab) |
| 55609 | 2014-03-25 | [Official agenda](https://addisontx.new.swagit.com/videos/55609/agenda); [cached artifact](https://audio.citymeetings.fyi/documents/abbf5e25e078/a306e5a33a9245c7/backup-7def730dbae6be88) |
| 55610 | 2014-04-08 | [Official agenda](https://addisontx.new.swagit.com/videos/55610/agenda); [cached artifact](https://audio.citymeetings.fyi/documents/abbf5e25e078/14f89773e59f2fd8/backup-93a35e186390f84e) |
| 55611 | 2014-04-22 | [Official agenda](https://addisontx.new.swagit.com/videos/55611/agenda); [cached artifact](https://audio.citymeetings.fyi/documents/abbf5e25e078/c18e4590c589606f/backup-0cc6e3aa036f866b) |
| 55612 | 2014-05-27 | [Official agenda](https://addisontx.new.swagit.com/videos/55612/agenda); [cached artifact](https://audio.citymeetings.fyi/documents/abbf5e25e078/6d954cb309211df9/backup-3e80a2f8aef4c911) |
| 55613 | 2014-06-10 | [Official agenda](https://addisontx.new.swagit.com/videos/55613/agenda); [cached artifact](https://audio.citymeetings.fyi/documents/abbf5e25e078/3ba7b24d44e9da7f/backup-33c2fe5ac5a95592) |
| 55614 | 2014-06-24 | [Official agenda](https://addisontx.new.swagit.com/videos/55614/agenda); [cached artifact](https://audio.citymeetings.fyi/documents/abbf5e25e078/4b0c1ed69fcbcdda/backup-77ef556929b4ac2f) |
| 55615 | 2014-07-08 | [Official agenda](https://addisontx.new.swagit.com/videos/55615/agenda); [cached artifact](https://audio.citymeetings.fyi/documents/abbf5e25e078/3cfc07ccaaa89fef/backup-5c2a5573e587f7fc) |
| 55616 | 2014-08-12 | [Official agenda](https://addisontx.new.swagit.com/videos/55616/agenda); [cached artifact](https://audio.citymeetings.fyi/documents/abbf5e25e078/6232efb99cd6894d/backup-f392751d1e0415f7) |
| 55617 | 2014-08-26 | [Official agenda](https://addisontx.new.swagit.com/videos/55617/agenda); [cached artifact](https://audio.citymeetings.fyi/documents/abbf5e25e078/f86aabec5e474882/backup-21993169dcb306bc) |
| 55618 | 2014-09-23 | [Official agenda](https://addisontx.new.swagit.com/videos/55618/agenda); [cached artifact](https://audio.citymeetings.fyi/documents/abbf5e25e078/2f9b5fd70b5514c5/backup-6392544474ffa0bf) |
| 55619 | 2014-10-14 | [Official agenda](https://addisontx.new.swagit.com/videos/55619/agenda); [cached artifact](https://audio.citymeetings.fyi/documents/abbf5e25e078/49a790f6c4d1277d/backup-045e5c9c37e568de) |
| 55620 | 2014-10-28 | [Official agenda](https://addisontx.new.swagit.com/videos/55620/agenda); [cached artifact](https://audio.citymeetings.fyi/documents/abbf5e25e078/5885503f6962565a/backup-03b85caf3d872f64) |
| 55621 | 2014-11-11 | [Official agenda](https://addisontx.new.swagit.com/videos/55621/agenda); [cached artifact](https://audio.citymeetings.fyi/documents/abbf5e25e078/1269a0f7dc0f5806/backup-a44206471ed4fc64) |
| 55622 | 2014-11-25 | [Official agenda](https://addisontx.new.swagit.com/videos/55622/agenda); [cached artifact](https://audio.citymeetings.fyi/documents/abbf5e25e078/47fc5fa46cd0d106/backup-f8c1f206dd444653) |
| 55623 | 2014-12-09 | [Official agenda](https://addisontx.new.swagit.com/videos/55623/agenda); [cached artifact](https://audio.citymeetings.fyi/documents/abbf5e25e078/0d0f97d0a5e4234f/backup-6d67251dff7782d3) |
| 55624 | 2015-01-13 | [Official agenda](https://addisontx.new.swagit.com/videos/55624/agenda); [cached artifact](https://audio.citymeetings.fyi/documents/abbf5e25e078/dcd12e177bb2d738/backup-f9c1395f0d6bfe6e) |
| 55625 | 2015-01-27 | [Official agenda](https://addisontx.new.swagit.com/videos/55625/agenda); [cached artifact](https://audio.citymeetings.fyi/documents/abbf5e25e078/9cabf11f0dab1ad5/backup-3b018cb7dc517e55) |
| 55626 | 2015-02-10 | [Official agenda](https://addisontx.new.swagit.com/videos/55626/agenda); [cached artifact](https://audio.citymeetings.fyi/documents/abbf5e25e078/f93e036b3ab247d2/backup-bca7b466d91da380) |
| 55627 | 2015-02-24 | [Official agenda](https://addisontx.new.swagit.com/videos/55627/agenda); [cached artifact](https://audio.citymeetings.fyi/documents/abbf5e25e078/87a3548cd62f20d6/backup-8cdc5b105a1a69de) |
| 55628 | 2015-03-10 | [Official agenda](https://addisontx.new.swagit.com/videos/55628/agenda); [cached artifact](https://audio.citymeetings.fyi/documents/abbf5e25e078/e3725c3d1f32d8c5/backup-81d8704d4397f94e) |

Additional individually confirmed cases:

- GUID55634, UID383dfdfed6140400: the September19,2016 official agenda explicitly
  convenes Council and P&Z jointly. Current P&Z-only subscription is incomplete. Adding Council
  must preserve the current P&Z canonical search owner; this needs a bounded ownership contract
  before implementation. [Cached official agenda](https://audio.citymeetings.fyi/documents/abbf5e25e078/383dfdfed6140400/backup-15647c606d86cda5).
- GUID394474, UID757ab2a4aaf9bbca: dated July28,2026 official agenda convenes the
  TIRZ#1 Board of Directors separately. Council-only assignment is incorrect; use the approved
  city TIF aggregate after a bounded activation contract. [Cached official agenda](https://audio.citymeetings.fyi/documents/abbf5e25e078/757ab2a4aaf9bbca/backup-9f68250e87dd4b6e).
- GUID394156, UID01960ed1399198df: dated July21,2026 official agenda explicitly
  convenes the P&Z work session. It corroborates the commission-only class behind the47
  false Council matches; Council selector narrowing still requires its full positive replay.
  [Cached official agenda](https://audio.citymeetings.fyi/documents/abbf5e25e078/01960ed1399198df/backup-dfed4ed93765ef45).


## Approved offline sweep implementation (#2013, prepared/unmerged)

The first full cached-state report contains2,104 uncovered raw recordings across restored cities.
Addison contributes10: five explicitly approved event exclusions, four verified Bond committee
recordings and Economic Development Luncheon GUID55864. The last case is now actively registered
for recording-bound briefing/event research; lack of prior inventory attention does not close it.
The register has99 cases,64 open, including47 currently matched P&Z/Council false inclusions.
There are no missing registered records or changed assignments against this prepared config
snapshot. These counts are selector eligibility, not actual deployed RSS or unique meeting counts.
The register records30 individually resolved Combined Meeting identities and five approved event
exclusions without deleting their records. Citizen Advisory and ambiguous older media remain open.
CPC/Finance official meetings with no located recording remain documented evidence obligations
here rather than fabricated UID entries. The CLI does not scrape, publish or trigger legacy remedy.


## Council positive-alias proof for bounded #2015 correction

## Evidence and positive-label proof

Official agenda text was read from previously stored agenda-backup artifacts via validate_source_url with audio.citymeetings.fyi allowed host, no redirects, no credentials or writes. Each cached header names the Council; joint headers name all participating bodies. The complete30 Combined Meeting proof is already committed in the audit and remains authoritative. The regular399077 alias is additionally identity-bound to a Council-label record sharing GUID/date/video.

| Exact label | Count | Representative GUID | Cached official agenda |
|---|---:|---|---|
| 2025 City Council Strategic Planning Session | 1 | 336861 | [cached agenda](https://audio.citymeetings.fyi/documents/abbf5e25e078/dbe3234cb18c9884/backup-6e325d83c19217eb) |
| 2026 City Council Strategic Planning Session | 1 | 374152 | [cached agenda](https://audio.citymeetings.fyi/documents/abbf5e25e078/64d910657ceb47fa/backup-912e374253bc3c03) |
| Budget Meeting | 2 | 55890 | [cached agenda](https://audio.citymeetings.fyi/documents/abbf5e25e078/9d70bea2066389f7/backup-2dd818e1c8146126) |
| Combined Meeting | 30 | 55599 | [cached agenda](https://audio.citymeetings.fyi/documents/abbf5e25e078/e59549e833479e1d/backup-2a684b64e68a2c3f) |
| Council Strategic Planning Day 1 & 2 | 1 | 297695 | [cached agenda](https://audio.citymeetings.fyi/documents/abbf5e25e078/56ff95c049d1027e/backup-371521858fa3b3e1) |
| Fiscal Year 2023-2024 Budget Workshop | 2 | 268810 | [cached agenda](https://audio.citymeetings.fyi/documents/abbf5e25e078/20209034b0e4f7b9/backup-06f47bd3b20703f0) |
| Fiscal Year 2024-2025 Budget Workshop | 1 | 311687 | [cached agenda](https://audio.citymeetings.fyi/documents/abbf5e25e078/5768282805ed4de3/backup-3b0d64aa3d463c71) |
| Joint CPAC, P&Z, and City Council Meeting #1 | 1 | 295839 | [cached agenda](https://audio.citymeetings.fyi/documents/abbf5e25e078/fcc9bde16c7f47fe/backup-0239bd2236cc0926) |
| Joint CPAC, P&Z, and City Council Meeting #2 | 1 | 310072 | [cached agenda](https://audio.citymeetings.fyi/documents/abbf5e25e078/c95af66385f6cf31/backup-659d1a2490d1fdcf) |
| Joint City Council and Planning & Zoning Commission Meeting | 7 | 55896 | [cached agenda](https://audio.citymeetings.fyi/documents/abbf5e25e078/4112b5bc5a5be0ef/backup-12e0a1f3e015890d) |
| Joint P&Z and City Council Meeting #1 | 1 | 276855 | [cached agenda](https://audio.citymeetings.fyi/documents/abbf5e25e078/4ef85ec6540cf95b/backup-8928572f440c34ec) |
| Regular City Council | 111 | 55817 | [cached agenda](https://audio.citymeetings.fyi/documents/abbf5e25e078/4977cbe848f71af9/backup-d143c56e2c71fb8e) |
| Regular City Council Meeting | 1 | 399077 | No cached agenda; see identity binding below |
| Special Budget Meeting | 8 | 55895 | [cached agenda](https://audio.citymeetings.fyi/documents/abbf5e25e078/5f9247d8b49ec33b/backup-4581448319abdcba) |
| Special Council | 15 | 55850 | [cached agenda](https://audio.citymeetings.fyi/documents/abbf5e25e078/dce4d5fc2550a32a/backup-7204471aa8470562) |
| Special Emergency Meeting | 1 | 55874 | [cached agenda](https://audio.citymeetings.fyi/documents/abbf5e25e078/276a0365c8ef32e0/backup-fb3258a08ff01fe4) |
| Special Meeting | 36 | 55854 | [cached agenda](https://audio.citymeetings.fyi/documents/abbf5e25e078/9fdab5a0104df0ef/backup-787380949ec33788) |
| Special Meeting and Work Session | 15 | 55875 | [cached agenda](https://audio.citymeetings.fyi/documents/abbf5e25e078/f4d4794bd811ca77/backup-b128bc67a413f324) |
| Special Meeting-Tax Rate & Budget Public Hearing | 1 | 55887 | [cached agenda](https://audio.citymeetings.fyi/documents/abbf5e25e078/5f830e631abdb6cc/backup-4a9f3f52c46a4de6) |
| Special Work Session | 3 | 55886 | [cached agenda](https://audio.citymeetings.fyi/documents/abbf5e25e078/e5cef2310da110c8/backup-5256db89dce9af23) |
| Tax & Budget Public Hearing | 2 | 55893 | [cached agenda](https://audio.citymeetings.fyi/documents/abbf5e25e078/b5430e76a3d766a0/backup-a05a6cd5d16193ae) |
| Tax Rate Public Hearing | 1 | 55892 | [cached agenda](https://audio.citymeetings.fyi/documents/abbf5e25e078/a946496c2bb4da38/backup-cb5f5f1bc239e128) |
| Work Session | 23 | 55902 | [cached agenda](https://audio.citymeetings.fyi/documents/abbf5e25e078/ea7bab8059305bd1/backup-791acca551da83b5) |
| Work Session - Economic Development Strategic Plan | 1 | 55913 | [cached agenda](https://audio.citymeetings.fyi/documents/abbf5e25e078/56e9ec0d67030444/backup-c57f22a9826c4b4e) |
| Work Session and Regular Meeting | 234 | 55706 | [cached agenda](https://audio.citymeetings.fyi/documents/abbf5e25e078/bb69cb17f1a3966e/backup-1d09d516d624d3c2) |

Regular City Council Meeting GUID399077: UID0901af8e58b8f0a2 and the existing Work Session and Regular Meeting UID35651c52b4a6834f share published2026-08-25 and https://addisontx.new.swagit.com/videos/399077/download. This is institutional alias corroboration, not permission to deduplicate records or audio. Both remain selected.

PZ example394156/01960ed1399198df dated2026-07-21 has a cached official header PLANNING AND ZONING COMMISSION WORK SESSION, independently establishes separate body. Citation already committed in audit. The47 exact-label-class records stay in their existing PZ feed.

Swearing-in56023/1f50e2274a6dd26c is an explicitly named ceremony. Approved global event policy excludes it from subscriptions only; raw archival pages remain.


Review hold lifted by maintainer; code reviews resume with repository-wide65-minute spacing.


Council ownership correction (#2015) prepared/unmerged:25 proved exact aliases replace broad
substrings;549->501 selector replay removes47 P&Z-only work sessions and one ceremony,
preserving all genuine Council/joint memberships, records, UIDs and hosted audio. P&Z search
attribution is corrected where Council previously matched incorrectly. TIRZ stays in Council
until its separate #2017 correction. Registered cases remain open until human merge/deployment.
No audio invalidation, pipeline changes or backfill. Full checks precede push/review.


Bond family correction (#2016) prepared/unmerged: four verified committee recordings gain
Addison: Bond Committees subscriptions with original UIDs/audio. Existing other memberships
are unchanged; canonical search attribution corrects the erroneous unmatched BZA fallback to
Bond. Council recommendations remain Council-only. No audio backfill or stage invalidation.
Cases remain open for human merge and deployed projection verification.


TIRZ ownership correction (#2017) prepared/unmerged: official dated board agenda moves only
GUID394474/UID757ab2a4aaf9bbca from Council to Addison: TIF Meetings. Canonical search
attribution changes accordingly; original raw record, UID and hosted audio remain. After the
Council narrowing,500 original Council selections remain. No audio invalidation or backfill.
The case remains open pending human merge and deployed subscription verification.


## Deployed correction verification after stack merge

A read-only fetch of public audio RSS verified all 56 implemented assignment cases:
47 P&Z work sessions are P&Z-only; four Bond recordings are Bond-only; the ceremony is
absent from the checked feeds; TIRZ is TIF-only; three genuine joints have their exact proven
participant subscriptions. Every checked entry matched the expected stable UID membership.
Feeds checked: Council, P&Z, CPAC, Bond Committees, TIF, and Public Input. URLs follow
`https://www.citymeetings.fyi/<feed-slug>/audio_feed.xml`. Main deployed commit: `1a90c62e`.
Local verification report: `/tmp/addison-deployed-verification.json` (56 passed, zero failed).
The case register now closes these assignment obligations, retaining all entries and their
verified memberships. Eight historical evidence/visibility cases remain open; no completeness
claim is made for the city or sources outside the local snapshot.

The maintainer subsequently approved Archive only for all six recordings: GUID55864,
56026, 56027, 56028, 56059, and 56060. This publication decision does not invent an institutional
identity for uncertain films or erase excerpt/source-binding obligations. Direct archives and
audio remain; public feed/search/browse omission is implemented separately under #2023.


September 2016 joint correction (#2025), prepared/unmerged: exact GUID55634 adds Council
while retaining P&Z and UID383dfdfed6140400/audio. Full 822-record ownership replay adds
exactly one Council membership (500 to 501), with no other feed changes. Existing canonical
page attribution becomes Council; participant discovery remains the #2018 redesign follow-up.
Keep the evidence case open until human merge and deployed subscription verification.

Arlington CND standing-body evidence: current Council-member assignments list the committee
(https://www.arlingtontx.gov/Government/City-Government/City-Council/City-Council-Members/Council-Member-Bowie-Hogg-District-7),
and the2026 Neighborhood Matching Grant guide distinguishes committee recommendations from
subsequent Council approval. All four cached committee GUID agendas convene that body alone.
Exactly four retained records have its exact label. All Meetings intentionally remains an aggregate.

Dallas #2035 was human-merged before its substantive CodeRabbit request; record that review gap.
The Council override correction is shipped, with deployed membership proof still pending.


Arlington CND #2038 prepared/unmerged: four verified committee recordings receive a dedicated
exact-label feed and leave Council, retaining the All Meetings aggregate and original UID/audio.
No stage invalidation or backfill. Cases remain open until deployed membership verification.
### Archive-only and 2016 joint shipped (2026-10-05)

Implemented in human-merged PR #2024 (archive visibility) and PR #2026 (joint subscription).
Their bounded implementation contracts are frozen. Main `ac0fa4bb` Build & Deploy succeeded.
CodeRabbit substantively reviewed #2024 head `56e2cb8e` with no remaining actionable issues;
#2026 was merged before its substantive review slot, an explicit review gap. No stage/audio
invalidation or backfill. Both joint feeds carry the same original UID/audio; its case is resolved.
The register now has seven open historical evidence cases, rather than eight.

Live verification: six archive UIDs absent from Public Input/Council/P&Z RSS and Public Input
browse. Five direct Public Input archive pages return 200. Luncheon UID35a78fa89c7c5c5c returns
404 at checked Public Input/Council/BZA/Briefings paths; its direct archive route remains unverified.
Public search and its expected manifest return 404; do not claim deployed search verification.
Retain these visibility/access checks and all independent historical content evidence obligations.

### Access-check diagnosis (2026-10-05)

Luncheon GUID55864 has exact stored body `Addison Economic Development Luncheon` and no
matching feed selector. Raw pages are rendered from body-filtered `raw_retained_eps`; an
archive-only declaration alone does not route unmatched records into that collection.
The five other approved records have exact Public Input inclusions and accessible direct pages.
Recommended bounded correction: add only the luncheon GUID/body to Public Input raw selection,
keeping its source-wide archive-only declaration. Routing approval is pending; no code changed.

Build & Deploy job111593502145 log at03:00:26 says the wall-clock window was spent when search
started; at03:00:31 the index build deferred, retaining the last complete search output.
This explains unavailable public search without evidence of archive identity conflicts.
Configured search is enabled with a20-minute index budget, but the shared stop signal was already
spent. Further scheduling/budget changes require a scoped contract; no production knobs changed.

### Catalog expansion authorized (2026-10-05)

Maintainer authorized access fixes and catalog-wide ownership audit/corrections. The durable
register now includes the previously enumerated Arlington, Dallas, Denton and Fort Worth UID
candidates. These are proof obligations, not automatic publication exclusions. Fort Worth joint
candidates retain Council pending agenda/segmentation proof. All current selectors are snapshotted
from cached records; the existing sweep surfaces them even when matched or historical.

Official initial institution evidence: Denton's Departments directory lists TIRZ boards as
separate bodies (https://denton-tx.legistar.com/Departments.aspx); its Development Districts page
records Ordinance2012-366 establishing TIRZ2 and its board
(https://www.cityofdenton.com/1145/Development-Districts). Arlington's official provider archive
separately labels Environmental Task Force and Community/Neighborhood Development meetings
(https://arlingtontx.granicus.com/ViewPublisher.php?view_id=9). Recording-specific agendas still
need reconciliation before config corrections. No global substring exclusion is authorized.


### Cross-city agenda retrieval checkpoint (2026-10-05)

Read-only retrieval found hosted official agenda text for 39 of 43 registered candidates.
HTTP success is availability, not a completed institutional/recording verdict. Four remaining
retrieval gaps and all joint/segmentation decisions remain tracked. The April14 Environmental
Task Force and April28 Community and Neighborhood Development agendas explicitly convene
their named bodies with their own call-to-order/adjournment and telephone participation; these
are substantive standalone-body evidence, not title-only classifications.

- `arlington-tx-1792515fdd51c6c2-ownership`: agenda status 200; [retained official agenda](https://audio.citymeetings.fyi/documents/ecc3710ac47f/1792515fdd51c6c2/agenda-1cdc5446aa8e9d8f).
- `arlington-tx-237c28f30496e3e0-ownership`: agenda status 200; [retained official agenda](https://audio.citymeetings.fyi/documents/ecc3710ac47f/237c28f30496e3e0/agenda-760478986af0cdb4).
- `arlington-tx-25e9b8510b0a7a66-ownership`: agenda status 200; [retained official agenda](https://audio.citymeetings.fyi/documents/ecc3710ac47f/25e9b8510b0a7a66/agenda-326e3957aea95c2b).
- `arlington-tx-2d5dc451df2da130-ownership`: agenda status 200; [retained official agenda](https://audio.citymeetings.fyi/documents/ecc3710ac47f/2d5dc451df2da130/agenda-048d3c873f28336b).
- `arlington-tx-30b28efc23346a6d-ownership`: agenda status 200; [retained official agenda](https://audio.citymeetings.fyi/documents/ecc3710ac47f/30b28efc23346a6d/agenda-eeb106c3db36574b).
- `arlington-tx-35b22fcc8d07967b-ownership`: agenda status 200; [retained official agenda](https://audio.citymeetings.fyi/documents/ecc3710ac47f/35b22fcc8d07967b/agenda-28a6b1309cb11254).
- `arlington-tx-4357f0c39d1ae58f-ownership`: agenda status 200; [retained official agenda](https://audio.citymeetings.fyi/documents/ecc3710ac47f/4357f0c39d1ae58f/agenda-e797bc0615bc85b2).
- `arlington-tx-9b66e98b682a47f2-ownership`: agenda status 200; [retained official agenda](https://audio.citymeetings.fyi/documents/ecc3710ac47f/9b66e98b682a47f2/agenda-d2b04dc26e89fb9c).
- `arlington-tx-a683af6e13e2bd7a-ownership`: agenda status 200; [retained official agenda](https://audio.citymeetings.fyi/documents/ecc3710ac47f/a683af6e13e2bd7a/agenda-3a5431516baaa224).
- `arlington-tx-d8f3b672b482369b-ownership`: agenda status 200; [retained official agenda](https://audio.citymeetings.fyi/documents/ecc3710ac47f/d8f3b672b482369b/agenda-b302b68ab4ef8b57).
- `dallas-tx-baa19208405acb22-ownership`: agenda status 200; [retained official agenda](https://audio.citymeetings.fyi/documents/76869ed1994f/baa19208405acb22/agenda-85a528fa8d921cf9).
- `dallas-tx-bd172b762dd20b63-ownership`: agenda status 200; [retained official agenda](https://audio.citymeetings.fyi/documents/76869ed1994f/bd172b762dd20b63/agenda-7e81fd4e35c6b9ba).
- `denton-tx-04689e3929ac104e-ownership`: agenda status 200; [retained official agenda](https://audio.citymeetings.fyi/documents/3b7588310856/04689e3929ac104e/agenda-b5f13d2a4b3df899).
- `denton-tx-1c91182b70afefb3-ownership`: agenda status 200; [retained official agenda](https://audio.citymeetings.fyi/documents/3b7588310856/1c91182b70afefb3/agenda-9c76e89344ec579b).
- `denton-tx-2493e59c0844a4b4-ownership`: agenda status 200; [retained official agenda](https://audio.citymeetings.fyi/documents/3b7588310856/2493e59c0844a4b4/agenda-f00ea12cc9662b4f).
- `denton-tx-280db52d91547dd6-ownership`: agenda status 200; [retained official agenda](https://audio.citymeetings.fyi/documents/3b7588310856/280db52d91547dd6/agenda-0a73365f4da36fd3).
- `denton-tx-2b458f3c556df459-ownership`: agenda status 200; [retained official agenda](https://audio.citymeetings.fyi/documents/3b7588310856/2b458f3c556df459/agenda-1fc469c352fec9dc).
- `denton-tx-5a2ce8afb18c6a82-ownership`: agenda status 200; [retained official agenda](https://audio.citymeetings.fyi/documents/3b7588310856/5a2ce8afb18c6a82/agenda-f0d0a8cd24db6a71).
- `denton-tx-69d3fee77a42058c-ownership`: agenda status 200; [retained official agenda](https://audio.citymeetings.fyi/documents/3b7588310856/69d3fee77a42058c/agenda-2e5a44f16a8ca995).
- `denton-tx-70a6450a1deda017-ownership`: agenda status 200; [retained official agenda](https://audio.citymeetings.fyi/documents/3b7588310856/70a6450a1deda017/agenda-6c9f1683eafa17ab).
- `denton-tx-7abd583b2ed8189a-ownership`: agenda status 200; [retained official agenda](https://audio.citymeetings.fyi/documents/3b7588310856/7abd583b2ed8189a/agenda-20fec26c8f520b20).
- `denton-tx-a1aa4af8f26e120c-ownership`: agenda status 200; [retained official agenda](https://audio.citymeetings.fyi/documents/3b7588310856/a1aa4af8f26e120c/agenda-791e2ea203a5919d).
- `denton-tx-a9089a57a1557991-ownership`: agenda status 200; [retained official agenda](https://audio.citymeetings.fyi/documents/3b7588310856/a9089a57a1557991/agenda-e4fb61bfcfd1cae7).
- `denton-tx-b50c6a018b2c08e7-ownership`: agenda status 200; [retained official agenda](https://audio.citymeetings.fyi/documents/3b7588310856/b50c6a018b2c08e7/agenda-b1a1afd7fc2c1c02).
- `denton-tx-c10f3f6cd97ac38f-ownership`: agenda status 200; [retained official agenda](https://audio.citymeetings.fyi/documents/3b7588310856/c10f3f6cd97ac38f/agenda-105b6b641b98eeca).
- `denton-tx-c26f2f99a4bdaf8f-ownership`: agenda status 200; [retained official agenda](https://audio.citymeetings.fyi/documents/3b7588310856/c26f2f99a4bdaf8f/agenda-09fac2977c403dab).
- `denton-tx-d419e8107262d0d0-ownership`: agenda status 200; [retained official agenda](https://audio.citymeetings.fyi/documents/3b7588310856/d419e8107262d0d0/agenda-c55034ec305175f8).
- `denton-tx-e7c1990b7a8e539e-ownership`: agenda status 200; [retained official agenda](https://audio.citymeetings.fyi/documents/3b7588310856/e7c1990b7a8e539e/agenda-4497295b281ff426).
- `fort-worth-tx-081d2695d7d0136a-ownership`: agenda status no artifact; locate official agenda.
- `fort-worth-tx-0c66fc968402fabd-ownership`: agenda status 200; [retained official agenda](https://audio.citymeetings.fyi/documents/6540eef2dc2e/0c66fc968402fabd/agenda-b6965cf9b8b2ec44).
- `fort-worth-tx-0d5832dbd08e6dd8-ownership`: agenda status no artifact; locate official agenda.
- `fort-worth-tx-1cbf75c664b7c63e-ownership`: agenda status 200; [retained official agenda](https://audio.citymeetings.fyi/documents/6540eef2dc2e/1cbf75c664b7c63e/agenda-dc304ac57a1bef1e).
- `fort-worth-tx-1d3f5b988918cba0-ownership`: agenda status 200; [retained official agenda](https://audio.citymeetings.fyi/documents/6540eef2dc2e/1d3f5b988918cba0/agenda-0de271fd089eee9f).
- `fort-worth-tx-27254aa1a700569f-ownership`: agenda status no artifact; locate official agenda.
- `fort-worth-tx-497b3aa05255bfb9-ownership`: agenda status 200; [retained official agenda](https://audio.citymeetings.fyi/documents/6540eef2dc2e/497b3aa05255bfb9/agenda-9d13c4c47d8e5225).
- `fort-worth-tx-6a1dd0e9e8088440-ownership`: agenda status 200; [retained official agenda](https://audio.citymeetings.fyi/documents/6540eef2dc2e/6a1dd0e9e8088440/agenda-538bf69c30bf1d42).
- `fort-worth-tx-83d6abdb749bc76f-ownership`: agenda status 200; [retained official agenda](https://audio.citymeetings.fyi/documents/6540eef2dc2e/83d6abdb749bc76f/agenda-f77d64b9f1d0bf88).
- `fort-worth-tx-99152cb9e61fa091-ownership`: agenda status 200; [retained official agenda](https://audio.citymeetings.fyi/documents/6540eef2dc2e/99152cb9e61fa091/agenda-b6965cf9b8b2ec44).
- `fort-worth-tx-b7a9ee9eae6fafc6-ownership`: agenda status 200; [retained official agenda](https://audio.citymeetings.fyi/documents/6540eef2dc2e/b7a9ee9eae6fafc6/agenda-c9785e507c29505b).
- `fort-worth-tx-c57e1210d9282ab1-ownership`: agenda status 200; [retained official agenda](https://audio.citymeetings.fyi/documents/6540eef2dc2e/c57e1210d9282ab1/agenda-2effb0b0240c0426).
- `fort-worth-tx-e0736bdd5835b1d4-ownership`: agenda status 200; [retained official agenda](https://audio.citymeetings.fyi/documents/6540eef2dc2e/e0736bdd5835b1d4/agenda-f77d64b9f1d0bf88).
- `fort-worth-tx-ead427dae0bbb284-ownership`: agenda status no artifact; locate official agenda.
- `fort-worth-tx-f28ae055829705ec-ownership`: agenda status 200; [retained official agenda](https://audio.citymeetings.fyi/documents/6540eef2dc2e/f28ae055829705ec/agenda-0de271fd089eee9f).

### Recording-specific Dallas verdict

UIDbd172b762dd20b63/GUID203320 is officially the January22,2022 Redistricting Commission
public town hall. Its retained official notice convenes that Commission at Pleasant Oaks,
with Commission chair introductions and a redistricting agenda. It does not convene Council.
Current selectors already admit it to the dedicated Redistricting Commission feed and also
Council. Recommended correction removes only its Council GUID override, retaining Commission,
raw UID/audio and historical feed. This verdict does not classify the other CBTF recording:
its available packet describes the program and subcommittees, requiring dated meeting proof.


Denton UIDa1aa4af8f26e120c/GUID13509: official March3,2014 agenda explicitly convenes
Council jointly with the Library Board. Retain Council; investigate missing Library subscription.
This is a genuine joint, not a false Council inclusion. Case remains active until participant
coverage is prepared and verified. The body-label heuristic alone would have misclassified it.

Dallas #2034 prepared ownership replay: all5353 retained records/1095 labels examined.
Council359->358 removes only UIDbd172b762dd20b63; every other feed UID set unchanged.
Raw records are unchanged. Case remains open until human merge and deployed verification.

### Denton commission institutional proof

Official current appointments and 2026 agendas corroborate a distinct continuing commission:
https://denton-tx.legistar.com/LegislationDetail.aspx?ID=7513268
https://denton-tx.legistar.com/View.ashx?ID=1355517&M=A

Four retained dated agendas explicitly convene the commission alone:
- denton-tx-280db52d91547dd6-ownership: https://audio.citymeetings.fyi/documents/3b7588310856/280db52d91547dd6/agenda-0a73365f4da36fd3
- denton-tx-7abd583b2ed8189a-ownership: https://audio.citymeetings.fyi/documents/3b7588310856/7abd583b2ed8189a/agenda-20fec26c8f520b20
- denton-tx-d419e8107262d0d0-ownership: https://audio.citymeetings.fyi/documents/3b7588310856/d419e8107262d0d0/agenda-c55034ec305175f8
- denton-tx-e7c1990b7a8e539e-ownership: https://audio.citymeetings.fyi/documents/3b7588310856/e7c1990b7a8e539e/agenda-4497295b281ff426

No case closure or runtime correction yet; exact L3 contract above precedes implementation.

Denton Health & Building Standards implementation #2044 was human-merged on 2026-10-05
at10:00:30Z, before substantive CodeRabbit review and before PR CI completed. Record this review
gap explicitly; skipped automatic review is not coverage. Full4918 offline tests passed locally.
Deployed membership verification remains pending; four cases stay open.
Denton #2042 prepared: four verified Health & Building Standards Commission records move
out of Council into their dedicated exact-GUID feed. UID/audio/raw records and Council/Library
joint are retained. Cases remain open until deployed verification; no stage invalidation/backfill.

Live2026-10-05 verification: Arlington four CND UIDs present dedicated feed/absent Council;
Dallas Redistricting UIDbd172b762dd20b63 present Commission/absent Council; six Addison archive
pages HTTP200 and no Public Input browse mentions. All Meetings RSS contains495items and omits
old CND UIDs; do not claim live aggregate historical coverage from config replay. Search404
still unverified; Denton newCommission404 while deployment77e4f31a is running.

Maintainer override: CodeRabbit reviews actual code changes only, not docs or configuration YAML.

Denton Library #2045 prepared: two standalone recordings leave Council for Library; the
Council/Library joint joins Library while retaining Council. Original UID/audio/raw records
retained, no stage invalidation/backfill. Deployment verification required before case closure.

### Denton TIRZ board dated proof

Official institutional census: https://www.cityofdenton.com/1145/Development-Districts
and https://denton-tx.legistar.com/Departments.aspx; eight retained agendas convene their
named board independently. No Council joint inferred from room name.
- denton-tx-1c91182b70afefb3-ownership: https://audio.citymeetings.fyi/documents/3b7588310856/1c91182b70afefb3/agenda-9c76e89344ec579b
- denton-tx-2493e59c0844a4b4-ownership: https://audio.citymeetings.fyi/documents/3b7588310856/2493e59c0844a4b4/agenda-f00ea12cc9662b4f
- denton-tx-2b458f3c556df459-ownership: https://audio.citymeetings.fyi/documents/3b7588310856/2b458f3c556df459/agenda-1fc469c352fec9dc
- denton-tx-5a2ce8afb18c6a82-ownership: https://audio.citymeetings.fyi/documents/3b7588310856/5a2ce8afb18c6a82/agenda-f0d0a8cd24db6a71
- denton-tx-69d3fee77a42058c-ownership: https://audio.citymeetings.fyi/documents/3b7588310856/69d3fee77a42058c/agenda-2e5a44f16a8ca995
- denton-tx-70a6450a1deda017-ownership: https://audio.citymeetings.fyi/documents/3b7588310856/70a6450a1deda017/agenda-6c9f1683eafa17ab
- denton-tx-a9089a57a1557991-ownership: https://audio.citymeetings.fyi/documents/3b7588310856/a9089a57a1557991/agenda-e4fb61bfcfd1cae7
- denton-tx-c26f2f99a4bdaf8f-ownership: https://audio.citymeetings.fyi/documents/3b7588310856/c26f2f99a4bdaf8f/agenda-09fac2977c403dab

Denton TIRZ #2048 prepared: eight standalone board recordings leave Council, six for Zone1
and two for Zone2 dedicated feeds; preserve raw UID/audio. No stage invalidation/backfill.
Cases remain open until deployed verification.

### Denton deployment ancestry clarification — 2026-10-05 12:30 UTC

PR #2044 merged into `docs/denton-health-standards-ownership`, not main. Its parent #2043
remains open against main. Successful deployment 37293889023 used main `77e4f31a`, which
has no Health Commission config file. Live Commission RSS 404 and four UIDs still in Council
are therefore expected before #2043 merges, not evidence of a render regression. Keep the
four cases open until deployment after the parent merge. Library #2047 current `1690b8e0`
has green test/deps/preview checks; its sole review finding was fixed and resolved. TIRZ
#2050 `a9fd3784` has green CI and full review requested at 12:30:52 UTC, comment 5994485501,
after repository-wide reconstruction found latest prior request #2047 at 11:00:40 UTC.

### Denton Bond contract and TIRZ review status — 2026-10-05 13:00 UTC

Issue #2051 binds the June 6, 2019 committee proceeding to retained official agenda
`https://audio.citymeetings.fyi/documents/3b7588310856/04689e3929ac104e/agenda-b5f13d2a4b3df899`.
The agenda explicitly convenes the committee; proposed 2019 Bond Program is its stated
purpose, not evidence of Council participation. Review/51 records the exact bounded L3.

TIRZ #2050 review request 5994485501 at 12:30:52 UTC was aborted by CodeRabbit reply
5994487454: "Head commit changed." Documentation push `5faac1c6` left reviewed test code
unchanged, but no substantive review completed. Current CI is green. Do not mark review
complete or duplicate the request before 13:35:52 UTC and repository-wide reconstruction.

Denton Bond committee #2051 is prepared under the committed review/51 contract: exact GUID
28865 moves from Council to its dedicated feed; raw UID/audio remain unchanged. Deployment
verification is pending; no lifecycle or broader committee-family claim is made.

### Fort Worth convening distinctions — 2026-10-05 13:30 UTC

Retained official agendas for UIDs `1cbf75c664b7c63e` (August 16, 2011) and
`b7a9ee9eae6fafc6` (November 4, 2008) explicitly name City Council Gas Drilling Workshops,
with mayor opening/call to order. Task Force recommendations are agenda subject matter;
these are not evidence of false Council ownership. Preserve their Council eligibility.

UIDs `1d3f5b988918cba0`, `c57e1210d9282ab1`, `e0736bdd5835b1d4` and
`f28ae055829705ec` have official joint Council/Crime Control and Prevention District board
agendas with a joint call to order. Council must remain eligible; participant-board feed
coverage is the outstanding question. Similar titles alone must not infer duplicate winners.
UID `0c66fc968402fabd` July 24, 2020 instead has distinct special-called Board and special
Council proceedings in one packet. Do not label that bundle a genuine joint or split its raw
identity/audio without the separately approved publication contract. Four registered records
without retained agenda artifacts remain unproven. Cases remain open pending full-source
census, exact selectors/identity checks and deployed verification where needed.

### Denton stack main merge checkpoint — 2026-10-05 14:00 UTC

Main `8a4adbeb` includes all four Denton slices. Build & Deploy 37321232154 is pending;
no new deployed ownership claim or case closure is made. The prior 77e4f31a deployment
predated the Health parent merge. Review/51 freezes/stamps the bounded contracts and records
Health, TIRZ and Bond substantive-review gaps. Library's valid test finding is fixed/resolved.
Latest repository-wide human full request remains #2050 comment5994485501 at 12:30:52 UTC,
aborted for head change; no new request was made on any closed PR.

### Denton Health verification — 2026-10-05 14:30 UTC

Successful deployment 37320977847 at `c753afa4` includes the Health parent merge. Read-only
HTTP checks of `/denton-tx-health-building-standards-commission/audio_feed.xml` and
`/denton-tx-city-council/audio_feed.xml` on `https://www.citymeetings.fyi` both return 200.
All four Commission UIDs are present in the four-item Commission feed, absent from the
493-item Council feed, and have the exact original fixture audio URLs:

| UID | Hosted audio suffix under `https://audio.citymeetings.fyi/swagit/3b7588310856/` |
|---|---|
| 280db52d91547dd6 | 280db52d91547dd6-11174e3f8f49.m4a |
| e7c1990b7a8e539e | e7c1990b7a8e539e-e2a2da60a988.m4a |
| d419e8107262d0d0 | d419e8107262d0d0-f68204d763e1.m4a |
| 7abd583b2ed8189a | 7abd583b2ed8189a-c760fcff8133.m4a |

These four exact ownership cases are resolved. Full-stack run 37321232154 at `8a4adbeb`
remains in progress. Library, TIRZ and Bond feeds returned 404 and remain unverified/open.
Council RSS is bounded: absent historical joint/TIRZ UIDs do not prove removed coverage.
The full-source config replay establishes unchanged retention separately from live RSS.

### Arlington ETF exact source census — 2026-10-05 14:30 UTC

All six registered ETF records occur in source `ecc3710ac47f`, with original MediaPlayer
GUID URLs and exact two body labels. Their dated retained agendas each contain ETF call to
order and adjournment; Council mentions concern report/recommendation context. Review/51
#2055 records the bounded preimplementation contract, preserving All Meetings. No committee
standing status or dissolution inference is made. Six cases remain open until deployed proof.

Arlington ETF #2055 implementation is prepared under the exact six-record review/51
contract: dedicated feed, only Council ETF term removed, All Meetings/raw UID/audio
preserved. Deployed verification pending; no standing or dissolution inference.

### Denton full-stack live verification — 2026-10-05 15:00 UTC

Successful deployment 37321232154 main `8a4adbeb`; all six checked feed URLs return 200.
Fourteen of sixteen exact records match expected membership and original fixture audio URLs.
Only those fourteen cases are resolved (four previously verified Health cases plus ten now).
Council RSS has 493 items; Library has two, Zone1 six, Zone2 one, Bond one, Health four.

| UID | Verified dedicated feed | Original audio filename under Swagit source 3b7588310856 |
|---|---|---|
| 280db52d91547dd6 | denton-tx-health-building-standards-commission | 280db52d91547dd6-11174e3f8f49.m4a |
| 7abd583b2ed8189a | denton-tx-health-building-standards-commission | 7abd583b2ed8189a-c760fcff8133.m4a |
| d419e8107262d0d0 | denton-tx-health-building-standards-commission | d419e8107262d0d0-f68204d763e1.m4a |
| e7c1990b7a8e539e | denton-tx-health-building-standards-commission | e7c1990b7a8e539e-e2a2da60a988.m4a |
| b50c6a018b2c08e7 | denton-tx-library-board | b50c6a018b2c08e7-a61458854406.m4a |
| c10f3f6cd97ac38f | denton-tx-library-board | c10f3f6cd97ac38f-e6ac08b8754e.m4a |
| 04689e3929ac104e | denton-tx-special-citizens-bond-advisory-committee | 04689e3929ac104e-8842e26bc64f.m4a |
| 1c91182b70afefb3 | denton-tx-tirz-2-board | 1c91182b70afefb3-f46afc15e128.m4a |
| 2493e59c0844a4b4 | denton-tx-tirz-1-board | 2493e59c0844a4b4-84c8677d0c8e.m4a |
| 2b458f3c556df459 | denton-tx-tirz-1-board | 2b458f3c556df459-48f486dbd375.m4a |
| 5a2ce8afb18c6a82 | denton-tx-tirz-1-board | 5a2ce8afb18c6a82-6f850a083849.m4a |
| 69d3fee77a42058c | denton-tx-tirz-1-board | 69d3fee77a42058c-d8ce5d81fc3b.m4a |
| 70a6450a1deda017 | denton-tx-tirz-1-board | 70a6450a1deda017-f1734abbc3e9.m4a |
| a9089a57a1557991 | denton-tx-tirz-1-board | a9089a57a1557991-f8195642ee38.m4a |

UID `a1aa4af8f26e120c` (2014 Council/Library joint) and `c26f2f99a4bdaf8f`
(2017 TIRZ No.2) are absent from all expected checked feeds. They remain open; the complete
local selector replay passes, but live membership is not verified. Do not claim truncation
as the cause: Library and Zone2 feeds themselves are only two/one items. Investigate deployed
retained-source availability and publication admission without raw state writes or backfill.

### Denton withheld-media distinction — supersedes pending two ownership cases

Further read-only evidence explains both remaining RSS omissions. Existing persisted
`media_availability.state=confirmed_empty` verdicts report successful decode near-total
silence, 24 confirmations, checked June 29, 2026. `citypods.feeds.enclosure_url` intentionally
omits withheld media from both RSS kinds. The deployed direct pages all return HTTP200:
Council and Library `/a1aa4af8f26e120c/`, and Zone2 `/c26f2f99a4bdaf8f/`. Each displays
"Recording unavailable" and the same confirmed-empty reason/check timestamp. Deployment
logs include all three exact pages and select Library three/Zone2 two episodes before RSS
admission. This is correct ownership with preserved pages, not missing retained-source data.

The two ownership cases are resolved on complete-source replay plus deployed owner-page
proof; all sixteen Denton ownership corrections are verified (fourteen RSS, two intentionally
withheld). No media verdict, raw record, UID or audio was changed. Availability review is a
separate obligation; do not claim playable podcast coverage for these two recordings.

### Catalog five ownership verification — 2026-10-05 15:30 UTC

Read-only dedicated and Council audio RSS all return HTTP200: Arlington CND four items,
Arlington Council495, Dallas Redistricting28 and Dallas Council355. Five exact UIDs occur
in their dedicated feeds with audio equal to original persisted records, absent from Council.
The five ownership cases are resolved. Arlington All Meetings retention is proven separately
by the full1,515-record replay; bounded live aggregate RSS is not historical coverage proof.

| UID | Dedicated feed | Original verified audio filename |
|---|---|---|
| 237c28f30496e3e0 | arlington-tx-community-and-neighborhood-development-committee | 237c28f30496e3e0-d2419bdee50a.m4a |
| 30b28efc23346a6d | arlington-tx-community-and-neighborhood-development-committee | 30b28efc23346a6d-07a7c67138c4.m4a |
| 4357f0c39d1ae58f | arlington-tx-community-and-neighborhood-development-committee | 4357f0c39d1ae58f-354e7dead089.m4a |
| d8f3b672b482369b | arlington-tx-community-and-neighborhood-development-committee | d8f3b672b482369b-5427e9c557a1.m4a |
| bd172b762dd20b63 | dallas-tx-redistricting-commission | bd172b762dd20b63-7cd53a44abef.m4a |

### Fort Worth eight ownership dispositions — 2026-10-05

Three official Council Gas Drilling Workshop agendas name Council and mayor call/opening.
Five official Council/CCPD joint agendas have a joint call to order. Existing source selectors
already give the correct holders; no routing changes are needed. Read-only live Council
owner pages return HTTP200 for all eight and retain original audio URLs; CCPD RSS200
(178 items) includes all five joints with exact original UID/audio. Council RSS200 has500
items and omits these older entries. Do not claim unlimited RSS history. Eight body ownership
cases resolve; source duplication/publication-winner decisions remain separate.

| UID | Proven proceeding | Retained official agenda |
|---|---|---|
| 1cbf75c664b7c63e | Council workshop | https://audio.citymeetings.fyi/documents/6540eef2dc2e/1cbf75c664b7c63e/agenda-dc304ac57a1bef1e |
| 1d3f5b988918cba0 | Council/CCPD joint | https://audio.citymeetings.fyi/documents/6540eef2dc2e/1d3f5b988918cba0/agenda-0de271fd089eee9f |
| 6a1dd0e9e8088440 | Council workshop | https://audio.citymeetings.fyi/documents/6540eef2dc2e/6a1dd0e9e8088440/agenda-538bf69c30bf1d42 |
| 83d6abdb749bc76f | Council/CCPD joint | https://audio.citymeetings.fyi/documents/6540eef2dc2e/83d6abdb749bc76f/agenda-f77d64b9f1d0bf88 |
| b7a9ee9eae6fafc6 | Council workshop | https://audio.citymeetings.fyi/documents/6540eef2dc2e/b7a9ee9eae6fafc6/agenda-c9785e507c29505b |
| c57e1210d9282ab1 | Council/CCPD joint | https://audio.citymeetings.fyi/documents/6540eef2dc2e/c57e1210d9282ab1/agenda-2effb0b0240c0426 |
| e0736bdd5835b1d4 | Council/CCPD joint | https://audio.citymeetings.fyi/documents/6540eef2dc2e/e0736bdd5835b1d4/agenda-f77d64b9f1d0bf88 |
| f28ae055829705ec | Council/CCPD joint | https://audio.citymeetings.fyi/documents/6540eef2dc2e/f28ae055829705ec/agenda-0de271fd089eee9f |

Live owner paths are `https://www.citymeetings.fyi/fort-worth-tx-city-council/<UID>/`.
Original audio keys are unchanged under Swagit/Granicus source namespaces. Separate
Board/Council packets, ISD participation and four missing-agenda cases remain open.

### Fort Worth October 14 joint corroboration — 2026-10-05

[Official October 7, 2008 Council minutes](https://fortworthgov.granicus.com/MinutesViewer.php?clip_id=94&view_id=2)
record the mayor announcing an October 14 joint Council/Gas Well Task Force meeting at
1:30 p.m. in the Will Rogers Memorial Center Stagecoach Room. This is independent evidence
that the joint event was planned. It does not yet bind the event to recording UID
`0d5832dbd08e6dd8` (clip127) or `27254aa1a700569f` (clip125). The latter listing says
Workshop of October14 despite its October21 publication date. Both cases remain open.

The two exact provider player URLs were unavailable through the web reader during this
check; unavailable reader output is not evidence that the official recordings are gone.
Do not resolve either case or add Task Force participation from this announcement alone.

### Fort Worth clip6334 recovered official packet — 2026-10-05

The live view9 clip6334 player exposes an AgendaViewer redirect to this
[official eight-page packet](https://fortworthgov.granicus.com/DocumentViewer.php?file=fortworthgov_c04c7ebca76ffa89ac6586743c0d1baa.pdf&view=1).
All eight rendered pages were visually inspected because the PDF character map makes text
extraction unreliable. Pages1–2 give the August11,2026 Council Budget Work Session at1PM,
with mayor call to order and adjournment. Pages3–5 give Property Management and Environmental
Services Committee immediately following the budget session, with its own call to order and
adjournment. Pages6–8 give Community Development Committee immediately following that
committee meeting, with chair call to order and adjournment. This is a packet of three
consecutive proceedings, not a joint convening. Shared Council members and room do not change
that conclusion.

Exact source clip6334 has registered UIDs `081d2695d7d0136a` (view9) and
`ead427dae0bbb284` (view5). The recovered link directly binds the packet to view9. A subsequent read-only view5
player check exposes its own AgendaViewer redirect to the identical document filename,
binding that packet to both registered views. The recording's actual proceeding boundaries
remain to verify. Both cases
stay open. Do not split audio, choose a duplicate winner or remove holders from packet
structure alone. Historical clips125/127 currently expose only a generic CableSchedule
default document; their player pages return HTTP200 even though the web reader could not
open them. This supplies no missing agenda proof for those two historical cases.

### Dallas November4,2023 CBTF proof — 2026-10-05

[City CBTF archive](https://dallascityhall.com/departments/bond-construction-management/2024-Bond-Dashboard/Pages/Community-Bond-Task-Force.aspx)
links [dated minutes](https://dallascityhall.com/departments/bond-construction-management/Lists/DepartmentNavigation/Attachments/219/Meeting%20Minutes%20CBTF%2011-4-23_Revised_111623.pdf).
Minutes name the Community Bond Task Force, task-force members, five visiting subcommittee
chairs, call to order and chair Agarwal's closing/adjournment. Discussion of allocations by
Council district and future Council decisions does not convene Council. Header8:24AM and
body8:24PM disagree; preserve that official inconsistency. Original stored chapter for
GUID280220 UIDbaa19208405acb22 explicitly identifies November4,2023. The retained
program handbook previously found was institutional context, not dated meeting proof.
Exact contract now recorded in review/51 #2062. Full5,353-record source census finds18
CBTF/bond-task-force rows including five subcommittee rows and three2017town halls;
remaining17 need individual proof and current ownership reconciliation.

Dallas #2062 full-source replay:5,353 records/1,095 labels, only UIDbaa19208405acb22
leaves Council358→357 and enters new CBTF0→1. Raw mapping unchanged. Offline4,929 pass.
Seventeen additional candidates:15 have no current holder; Streets/Transportation269613
and EconomicDevelopment269610 match standing Council committee feeds, requiring separate
institutional identity proof. No additional ownership change is inferred from titles alone.

### Dallas additional bond source register — 2026-10-05

Seventeen candidates are now registered with original UID/provider GUID/source namespace.
Counts are159 cases,121 resolved and38 open; expansion reveals coverage work rather than
newly introduced regressions. No ownership proof is inferred from the candidate title.

| UID | Original GUID | Source body | Current holder |
|---|---|---|---|
| 130a7ebded8e0e1d | 269608 | 2024 Bond Task Force Community Bond Task Force Meeting | none |
| 5e9fdb6ff9e8ef68 | 202453 | Citizens Bond Task Force: Town Hall Meeting | none |
| 6135249a278494c5 | 273327 | 2024 Capital Bond Program CBTF Meetings | none |
| 6a3caf1854bdd2cd | 277589 | 2024 Capital Bond CBTF and Subcommittee Chairs Meeting | none |
| 78173acda7f7efea | 269613 | 2024 Bond Task Force Streets and Transportation Subcommittee | Transportation and Infrastructure |
| 8280ec97799c999e | 269612 | 2024 Bond Task Force Flood | none |
| 84fcc0492e66906d | 233037 | 2024 Capital Bond CBTF Meeting | none |
| 8969ad6d91ed23d2 | 269610 | 2024 Bond Task Force Economic Development | Economic Development |
| 96c10f4f75706719 | 246971 | 2024 Capital Bond CBTF Meeting | none |
| 977d943cea00258e | 269611 | 2024 Bond Task Force Critical Facilities Subcommittee Meeting | none |
| aa65e2aafc90711b | 272574 | 2024 Capital Bond Program CBTF Meeting | none |
| aaf0ad8eaabe68a5 | 269278 | 2024 Capital Bond CBTF Meeting | none |
| cba1e023051d7c94 | 269916 | 2024 Capital Bond CBTF Meeting | none |
| deb5a67aa9b6e8d1 | 259822 | 2024 Capital Bond CBTF Meeting | none |
| eb1c279cc51a9b04 | 202452 | Citizens Bond Task Force: Town Hall Meeting | none |
| eeca5aeceda62d39 | 272005 | 2024 Capital Bond Program CBTF Meeting | none |
| fc9ad3b1d9def142 | 202451 | Citizens Bond Task Force: Town Hall Meeting | none |
