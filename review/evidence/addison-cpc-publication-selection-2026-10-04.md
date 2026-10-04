# Addison CPC activation evidence (#1991)

Owning acceptance plan: [review/51](../51-unexpected-body-remedy-flow.md#addison-cpc-dedicated-feed--l3-implementation-under-validation).

Evidence collection was read-only. Maintainer approved the July 9 winner and historical-unknown exposure in issue #1991 comment 5976979654; implementation is prepared separately. Source abb f namespace: `abbf5e25e078`.
Current local restored archive SHA-256: `dda21f5dbd7152d045b1efdad8dc90420aebb1ffb4a5c764ac05716798d2d7dc`.

## Retained observations

| UID | Provider GUID | Stored date | Fingerprint v1 |
|---|---|---|---|
| 406d87fa54437613 | 392446 | 2026-06-29T00:00:00+00:00 | a1857671298702294df61a956f8d0c3af7b57d55b06742f675e611fc452c62ad |
| 91efc2425e16e017 | 393215 | 2026-07-09T00:00:00+00:00 | 33ba50e8fccff84a745baa7545758415decb53eb7926963859ecc1f656959bd5 |
| 926cf5eb32e17f89 | 393859 | 2026-07-16T00:00:00+00:00 | d26f37bd87d5a184bf9a94864195810cd5c8f881bb615db3ea0b486503582b31 |
| c70591ce9d69600c | 393215 | 2026-07-10T00:00:00+00:00 | 5efe085bc4b84224df728bccfc477bc47a5c45a002dfef09d7166799fd26f2dd |
| d2a2a095572de394 | 347901 | 2025-06-26T00:00:00+00:00 | 4d396274f9053260415c0592cccb2915490b6b11f19546288f1994785886b204 |
| e4683d84499ede30 | 349471 | 2025-07-10T00:00:00+00:00 | 2b2a85356f3fd5c8a682ccc28e5ae3daa9934875945bc096da9f81f39549a248 |

## Preferred observation and official date

Primary official AgendaQuick agenda seq7601 labels the CPC meeting Thursday July 9, 2026. Seq7602 is the July16 agenda and references consideration of July9 minutes. Its item seq6446 explicitly states minutes for the July9 meeting were prepared for consideration. These mutually support official_date `2026-07-09`, preserving preferred UID `91efc2425e16e017`; archive observation `c70591ce9d69600c` retains its July10 date unchanged.

Direct Swagit clip393215/393859 pages and agenda endpoints all returned403 during this refresh. The official agenda date proof succeeded through AgendaQuick instead. Same-source exact provider GUID393215 establishes the two retained observations identity; date correction is publication selection, not record mutation.

## Exposure and remaining gates

Current Council audio RSS and full landing page both returned200; neither contains either duplicate UID or Community Partnership body label. Correct per-UID Council page URLs returned404. Video RSS returned404. This establishes current absence only. Status must remain `historical-unknown` until historical publication evidence is reviewed; current absence does not prove never-published. Current selectors exclude CPC and existing source identity remains unchanged.

Owner subscription direction Council subcommittee is supported by official agenda naming; broader organizational proof was recorded in #1991. No active group or selector should be added before machinery lands, approval_ref recording winner/exposure decision is captured, current full-source replay passes, retained media keys and all six observations are preserved, and current publication snapshots are refreshed for activation.

## Snapshot manifest

| URL | UTC retrieved | HTTP | SHA-256 | Local bytes |
|---|---|---|---|---|
| https://agendas.addisontx.gov/agenda_publish.cfm?dsp=ag&get_month=7&get_year=2026&id=&mt=ALL&seq=7601 | 2026-10-04T04:04:34.135712+00:00 | 200 | 50207de8568edb8e4f2bf2544ba9002235a53055243bcfea3d8e468dccb2ad67 | /tmp/addison-official-0.html |
| https://agendas.addisontx.gov/agenda_publish.cfm?dsp=ag&get_month=7&get_year=2026&id=&mt=ALL&seq=7602 | 2026-10-04T04:04:34.299765+00:00 | 200 | e9e4e05c93b34ca9fde3e7dbf074e1870290a77cec2c83f02e46926cbe208fe3 | /tmp/addison-official-1.html |
| https://www.citymeetings.fyi/addison-tx-city-council/audio_feed.xml | 2026-10-04T04:04:34.441198+00:00 | 200 | 97cad0a43ad8657ae1c6d40531d0368f6786ca1bf76d1756d4c329fd61db27f0 | /tmp/addison-official-2.html |
| https://www.citymeetings.fyi/addison-tx-city-council/video_feed.xml | 2026-10-04T04:04:34.547926+00:00 | 404 | b620507312c5e97566a3c6cfaf99144fefc18a0da7d941401dfa0f5f58fb0368 | /tmp/addison-official-3.html |
| https://www.citymeetings.fyi/addison-tx-city-council/ | 2026-10-04T04:04:34.677015+00:00 | 200 | 35062983bccf3cf9a713d92e117419b606b3fd0f38b2950af7bee14f43075f61 | /tmp/addison-official-4.html |
| https://www.citymeetings.fyi/addison-tx-city-council/episodes/91efc2425e16e017.html | 2026-10-04T04:04:34.770854+00:00 | 404 | b620507312c5e97566a3c6cfaf99144fefc18a0da7d941401dfa0f5f58fb0368 | /tmp/addison-official-5.html |
| https://www.citymeetings.fyi/addison-tx-city-council/episodes/c70591ce9d69600c.html | 2026-10-04T04:04:34.892988+00:00 | 404 | b620507312c5e97566a3c6cfaf99144fefc18a0da7d941401dfa0f5f58fb0368 | /tmp/addison-official-6.html |
| https://www.citymeetings.fyi/addison-tx-city-council/91efc2425e16e017/ | 2026-10-04T04:05:09.690331+00:00 | 404 | b620507312c5e97566a3c6cfaf99144fefc18a0da7d941401dfa0f5f58fb0368 | /tmp/addison-exposure-0.html |
| https://www.citymeetings.fyi/addison-tx-city-council/c70591ce9d69600c/ | 2026-10-04T04:05:09.801081+00:00 | 404 | b620507312c5e97566a3c6cfaf99144fefc18a0da7d941401dfa0f5f58fb0368 | /tmp/addison-exposure-1.html |
| https://agendas.addisontx.gov/agenda_publish.cfm?id=&mt=ALL&get_month=7&get_year=2026&dsp=agm&seq=6446&rev=0&ag=7602&ln=39951&nseq=6447&nrev=0&pseq=6445&prev=0 | 2026-10-04T04:05:25.291399+00:00 | 200 | 45cd6167704cf2239286be40e5d5353a9fee51dbc69fe34f5de87e43f56763f1 | /tmp/addison-exposure-2.html |
| https://addisontx.new.swagit.com/videos/393215 | 2026-10-04T04:03:31.493993+00:00 | 403 | 58bf2215b395dcac74c009aa98701854e43cbe54a1cd3a95fee6a647ca9910d4 | /tmp/addison-evidence-0.html |
| https://addisontx.new.swagit.com/videos/393215/agenda | 2026-10-04T04:03:31.718446+00:00 | 403 | 58bf2215b395dcac74c009aa98701854e43cbe54a1cd3a95fee6a647ca9910d4 | /tmp/addison-evidence-1.html |
| https://addisontx.new.swagit.com/videos/393859 | 2026-10-04T04:03:31.936509+00:00 | 403 | 58bf2215b395dcac74c009aa98701854e43cbe54a1cd3a95fee6a647ca9910d4 | /tmp/addison-evidence-2.html |
| https://addisontx.new.swagit.com/videos/393859/agenda | 2026-10-04T04:03:32.155718+00:00 | 403 | 58bf2215b395dcac74c009aa98701854e43cbe54a1cd3a95fee6a647ca9910d4 | /tmp/addison-evidence-3.html |


## Historical census and dedicated-feed policy (2026-10-04)

Read-only research on 2026-10-04. Scope: formation 2024-12-10 through 2026-10-04.
Local archive inspected: `.citypods-state/sources/abbf5e25e078/episodes.json`.

## Conclusion and confidence

The committee is an ongoing three-member Council subcommittee with annual appointments and an
annual nonprofit-budget role. A seasonal cycle does not make it temporary. It belongs in a dedicated
feed under the maintainer's explicit preference. Known evidence establishes nine held meeting dates,
one additional scheduled date (July 17, 2025), and one cancelled date. Only five distinct recordings
are present in the current official indexed Swagit archive and six raw archive UIDs (one duplicate
July 2026 GUID/date representation). Do not claim the five videos constitute every historical meeting.

## Date census

| Date | Status and proof | Recording / retained UID |
|---|---|---|
| 2024-12-10 | Council establishes CPC via R24-111. This is a Council meeting, not a CPC episode. | Council Swagit 322414; do not classify from agenda mention. |
| 2025-01-23 | Held: March 25 Council packet retrospectively lists meeting; February 4 minutes approve Jan 23 minutes. | No CPC recording found in public indexed archive or retained source. |
| 2025-02-04 | Held: draft official actions in Feb 18 cancelled packet record call to order, attendance and adjournment. Also March25 retrospective. | No CPC recording found. |
| 2025-02-18 | Cancelled: first page of amended packet explicitly states cancelled/to be rescheduled; March11 memo confirms cancellation. | Must not create a podcast episode. |
| 2025-03-11 | Held: March25 retrospective; March11 meeting packet gives exact committee heading and memo date despite stale URL directory 20250220. | No CPC recording found. |
| 2025-03-18 | Held: March25 Council packet explicitly lists this meeting among four held. | No standalone packet or CPC recording found in searches. |
| 2025-06-26 | Held: official minutes included July17 packet and Swagit archive. | GUID347901 / d2a2a095572de394. |
| 2025-07-10 | Held: official minutes included July17 packet and Swagit archive. | GUID349471 / e4683d84499ede30. |
| 2025-07-17 | Scheduled: amended official agenda at 4PM, nonprofit presentations and funding recommendations. No located completed minutes/video proof. | No recording found. Keep availability/held status unresolved; agenda alone is not an audio episode. |
| 2026-06-29 | Held: Swagit archive and minutes approved at July16 recording. | GUID392446 / 406d87fa54437613. |
| 2026-07-09 | Held: Swagit archive and minutes approved at July16 recording; approved official date reconciliation. | GUID393215 / preferred91efc2425e16e017; alternate c70591ce9d69600c labelled July10,2026 is same GUID/download. Preserve both raw UIDs. |
| 2026-07-16 | Held: Swagit archive, full recorded agenda and transcript. | GUID393859 / 926cf5eb32e17f89. |

Five distinct recorded CPC meetings become five feed entries after existing approved duplicate winner
selection. Five historical dates have no located recording (four confirmed held, one scheduled).
There may be additional unindexed meetings. The July16,2026 transcript also describes a committee
visit to nonprofit offices that week; without exact formal meeting/date/recording evidence it is not
an extra feed episode and must not be silently inferred from speech.

## Primary source URLs

- Formation, ongoing purpose, annual appointments and four held early-2025 meetings:
  https://agendas.addisontx.gov/docs/2025/CM/20250325_7396/AGENDApacket__03-25-25_0329_7391.pdf
- Dec10 minutes (formation vote), included Jan14 Council packet:
  https://agendas.addisontx.gov/docs/2025/CM/20250114_7384/AGENDApacket__01-14-25_0219_7379.pdf
- Feb18 cancelled packet and Feb4 draft official actions (Jan23 minutes approval):
  https://agendas.addisontx.gov/docs/2025/CPAC/20250213_7405/AGENDApacket__02-18-25_1248_7400.pdf
- March11 agenda/memo; cancellation confirmation (URL date is not authoritative meeting date):
  https://agendas.addisontx.gov/docs/2025/CPAC/20250220_7406/AGENDApacket__03-11-25_0324_7401.pdf
- July17 agenda and June26/July10 minutes, annual cycle and three members annually appointed:
  https://agendas.addisontx.gov/docs/2025/CPAC/20250717_7467/AGENDApacket__07-17-25_1155_7462.pdf
- Official indexed Swagit archive, Specialty–Other category, five CPC recordings:
  https://addisontx.new.swagit.com/views/128/live
- July16,2026 recording and June29/July9 minutes approvals:
  https://addisontx.new.swagit.com/videos/393859
- Current annual program explains subcommittee and June/July2026 presentation cycle:
  https://www.addisontx.gov/Government/Budget/Non-Profit-Funding-Program

## Research limitations

Swagit direct web fetch returned403; indexed archive and indexed individual recording are readable.
AgendaQuick calendar requests failed or returned application errors; one direct calendar request
returned an anti-attack/IP-capture message. No further direct calendar attempts were made. Indexed
PDFs are available. Broad official-domain searches found no additional standalone March18 or July17
recording. Absence from search is not proof a recording never existed. Historical total remains a
minimum known census, not complete authoritative all-dates coverage. Do not fabricate no-audio UIDs,
borrow Council recordings discussing CPC, or claim missing historic audio was recovered.

## Minimal classification implications

Existing trusted source body exactly `Community Partnership Committee` matches all six retained CPC
records; exact official whole-meeting alias `City Council Community Partnership Committee` may be a
reviewed second alias. Anchor whole meeting/body label, not agenda/transcript mention. Do not use
`CPAC` as alias or `/CPAC/` URL directory: Addison reuses that AgendaQuick directory for unrelated
Comprehensive Plan Advisory Committee. No member-name, date-window or nonprofit-topic heuristic.
CPC's genuine later titled recordings can be classified automatically by approved exact body rule;
previously unrecorded dates remain coverage evidence only, not fabricated episodes.
