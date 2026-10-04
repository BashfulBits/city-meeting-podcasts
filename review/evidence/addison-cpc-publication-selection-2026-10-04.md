# Addison CPC activation evidence (#1991)

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
