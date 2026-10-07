# Body-aware tiered retention

**Status: L3 development-ready · implementation in `feat/body-aware-tiered-retention` (2026-07-26)**

## Decision and deviation record

The prior plan deferred archive backfill: it retained a source-wide record window and materialized
only the feed-visible subset. On 2026-07-26 the maintainer explicitly promoted a bounded version of
that work. The benefit is coherent public history for every body that shares a provider listing; the
cost is gradual audio/ASR backfill and a larger retained state. The alternative—leaving the source-wide
5,000-record cap—lets a busy body evict quieter boards and makes the three public retention promises
impossible to state accurately.

## Locked policy

All configured feeds inherit these limits from `config/site_config.yml` → `defaults`; historical
per-feed `max_episodes: 25|50` overrides are removed. The loader rejects all feed-level retention
keys (`max_episodes`, `full_artifact_episodes`, `metadata_retention_episodes`, and the retired
`max_archive_items`) so an old override cannot silently create a conflicting policy.

| Per canonical body rank | Policy |
|---:|---|
| 1–300 | Publish in RSS and on the feed landing page; materialize normally. |
| 301–500 | Keep on the feed landing page and materialize normally, but omit from RSS. |
| 501–2,000 | Do not publish in RSS; gradually materialize and retain audio plus every artifact. |
| 2,001–10,000 | Retain episode/calendar metadata and non-audio artifacts, including transcripts and downloaded-document text; remove hosted-audio pointers so normal orphan GC can reclaim audio. |
| 10,001+ | Prune the durable record. |

The maintainer changed the desired public RSS window to 300 on 2026-10-07. The existing website
"Recent meetings" list remains 500 entries, and older retained rows remain eligible for the archive
and search. The current implementation still uses one `max_episodes` value for RSS, page output, and
the body-aware 500-item materialization window; therefore this split is a website implementation
requirement tracked under [review/32](32-frontend-design-accessibility-funding.md#feed-page-history-and-rss-cap-maintainer-direction-2026-10-07),
not a claim that the new behavior has shipped. Apple is not the source of this cap; its current
[RSS requirements](https://podcasters.apple.com/support/823-podcast-requirements) do not state a
300-item maximum.

The canonical store remains one `episodes.json` per `source_key`, but the retained set is the **union**
of the independent body windows. A body-less combined feed publishes its newest 300 in RSS and keeps
its newest 500 on the feed page; body-specific feeds and work selection use their own body ranks. The
source key must continue to ignore the feed's body filter: duplicating records/audio per feed would
break migration identity and content addressing.

## Implementation plan

1. Keep the site-wide `max_episodes` at 500 for the feed-page list and full-materialization window;
   add `max_rss_episodes` at 300 for RSS only. Keep `full_artifact_episodes` (2,000) and
   `metadata_retention_episodes` (10,000), validate their monotonic relationship, and reject
   feed-level overrides.
2. Replace production source-wide archive projection with a per-body merge → age-filter → rank → union
   projection. Demote only the `audio` block in the metadata-only tier; preserve non-audio blocks and
   their referenced objects.
3. Apply the same metadata tier to calendar-only rows. Keep the legacy source-wide projection callable
   only for external compatibility/tests, not production configuration.
4. Expand enrichment selection to 2,000 per body, activate `recent_archive`, and place
   `feed_visible_first` ahead of the gradual archive cohort under the existing wall-clock stop budget.
   This is a gradual backfill: there is no pipeline-version bump and no forced re-encode of already
   hosted audio.
5. Publish at most 300 newest eligible episodes in RSS; keep the 500-entry feed-page list and make
   the full retained metadata tier available through archive/search. The feed page explains the
   smaller RSS window.
   Normal object GC reclaims demoted audio because the audio key is removed from the canonical record;
   transcript/document keys remain live.
6. Update reports, architecture, config, and tests for multi-body independence, audio demotion, and
   active archive work.

## Acceptance criteria

- A source with two bodies retains 10,000 rows for each where available; one body's volume cannot evict
  the other's retained history.
- Rank 301 remains on the feed page but not RSS; rank 501 materializes eventually but appears only
  in archive/search; rank 2,001 retains transcript/document
  pointers but no audio pointer; rank 10,001 is absent.
- The 500-entry page/materialization cohort is scheduled before the 501–2,000 archive cohort.
- Existing audio below rank 2,000 keeps its content-addressed key; no pipeline-version bump or blanket
  backfill is introduced.
- Per-feed legacy 25/50 overrides no longer constrain production feeds.
