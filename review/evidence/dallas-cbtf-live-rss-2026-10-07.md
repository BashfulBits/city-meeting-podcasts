# Dallas CBTF feed check — 2026-10-07

This read-only check records what was publicly available before the queued deployment for PR #2116
finished. It changed no feed, record, UID, title, or audio.

## October 3, 2023 — GUID 273327

City Swagit archive page:
<https://dallastx.new.swagit.com/videos/273327>

The archived source record has stable UID `6135249a278494c5`, title “2024 Capital Bond Program
CBTF Meetings – Oct 03, 2023,” and hosted audio
`https://audio.citymeetings.fyi/swagit/76869ed1994f/6135249a278494c5-7d77e8d726f4.m4a`.

At the check time, all three public RSS URLs returned HTTP 200. Dallas Bond Program RSS contained
that exact UID, title, and audio enclosure. Dallas Public Info and the dedicated 2024 Community Bond
Task Force RSS did not contain the UID. This verifies the maintainer-directed “Bond Program only”
disposition. No separate official October 3 agenda was located; the City video archive page and the
maintainer’s explicit routing decision are retained as the basis for this disposition. The machine
transcript is unreviewed and is not treated as official minutes.

- [Dallas Bond Program RSS](https://www.citymeetings.fyi/dallas-tx-bond-program-meetings/audio_feed.xml)
- [Dallas Public Info RSS](https://www.citymeetings.fyi/dallas-tx-public-info-meetings/audio_feed.xml)
- [Dallas 2024 Community Bond Task Force RSS](https://www.citymeetings.fyi/dallas-tx-2024-community-bond-task-force/audio_feed.xml)

## September 26, 2023 — GUID 272574

The same check found stable UID `aa65e2aafc90711b` and original audio
`https://audio.citymeetings.fyi/swagit/76869ed1994f/aa65e2aafc90711b-1f714be9f4ff.m4a` in Dallas
Bond Program RSS, but not in Public Info RSS. PR #2116 has merged; its deployment was still queued
behind another build at this check. The approved dual-feed case remains open until a later live check
finds this exact UID/audio in both feeds.

## September 26, 2023 — post-#2116 deployment check, 2026-10-07 20:25 UTC

Build and Deploy run [#37672877235](https://github.com/BashfulBits/city-meeting-podcasts/actions/runs/37672877235)
completed successfully for the #2116 merge. A fresh read-only request to each of the three feeds found
UID `aa65e2aafc90711b` and its original audio in both Bond Program and Public Info; it was absent from
the dedicated Task Force feed. This reflects the then-merged dual-feed rule, which the maintainer
later superseded with Bond Program only. The corrective change is not merged or deployed yet. Keep
the case open; after the correction deploys, verify Bond Program present and Public Info/Task Force
absent before closing it. No source record or audio was changed by this check.
