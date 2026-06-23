# Migrations

## Stable episode identity (episode-record refactor)

**What changed:** the RSS `<guid>` for every episode switched from the provider's native id
(e.g. a Granicus GUID or Swagit video id) to a **stable, provider-independent uid** derived
from `author + meeting body + date` (see `citypods/records.py`).

**Why:** the provider id changes whenever a city migrates providers (we already moved Denton
from Granicus to Swagit), which makes podcast clients treat the entire back catalog as new and
re-download it. The stable uid survives provider migrations, so this churn happens **once** and
never again.

**Impact:** the first deploy after this change is a **one-time** event where existing
subscribers' clients may re-download recent episodes (clients key episodes by `<guid>`). This
was done deliberately during the beta period, before a meaningful subscriber base exists. After
this, guids are stable.

**Audio:** already-hosted audio is **not** re-encoded. `migrate_legacy_manifests` carries the
old per-slug `audio_manifest.json` entries over to the new record store by matching the
provider guid, marking them `spec_hash: "legacy"` (reused as-is until a real audio-spec change).

**State layout:** per-slug `audio_manifest.json` is superseded by a per-source record store at
`<state_dir>/sources/<source_key>/episodes.json` (restored across CI runs via `actions/cache`).

## Moving a feed (stable URLs)

Slugs are permanent. If a feed must move, **keep the old slug in the new YAML's `aliases:`
list** rather than just renaming:

```yaml
slug: denton-tx-city-council
aliases: [denton-tx]          # every former slug
```

Each alias then gets, on build:

- `docs/<alias>/audio_feed.xml` (and `video_feed.xml` if applicable) — a stub carrying
  `<itunes:new-feed-url>`, the **podcast-standard** permanent-move signal that Apple Podcasts
  and most clients honor automatically (subscribers migrate with no action).
- `docs/<alias>/index.html` — an HTML redirect (canonical + meta-refresh) for the human page.
- an entry in `docs/redirects.json` (`{from, to}` pairs).

**Real 301s (optional, for non-podcast clients):** GitHub Pages can't issue redirects, but the
site is fronted by Cloudflare. Turn `docs/redirects.json` into a **Cloudflare Bulk Redirect
list** (or a Redirect Rule) to serve true `301`s. The `itunes:new-feed-url` stubs already
cover podcast apps, so this is belt-and-suspenders.

## Durable build state

The record store and change-detection cache hold **derived, expensive-to-recompute** data
(hosted-audio provenance, and soon transcripts/summaries). They live in `state_dir`
(`.citypods-state`), but the **source of truth is the object bucket**, not `actions/cache`:

- at build start, `pull_state` (citypods/statesync.py) downloads the snapshot from the bucket's
  `state/` prefix into `state_dir` (bucket wins);
- at build end, `push_state` uploads it back.

`actions/cache` is now a pure latency optimization — if GitHub evicts it (after ~7 days idle or
at the 10 GB repo-cache limit), the next run self-heals from the bucket instead of losing the
derived state. The local dev backend has no bucket, so it just keeps its on-disk `state_dir`.

The orphan GC (`gc_audio.py`) skips anything under the `state/` prefix, so the snapshot is never
mistaken for orphaned audio.

`docs/<slug>/` directories for deleted cities or renamed slugs are pruned automatically each
build (`_prune_stale_dirs`), so removed feeds stop serving rather than lingering in the cache.

## Orphaned audio cleanup

Because audio is content-addressed, regenerating a file (e.g. adding chapters) or coalescing a
duplicate source view (GH#421) leaves the old object unreferenced.

**Scheduled (recommended).** The **Audio orphan GC** workflow (`.github/workflows/audio-gc.yml`)
runs weekly as a **dry-run**: it restores the bucket state, finds orphans, and — if any exist —
opens/updates one rolling *operations* issue with a per-city summary table (file count + size per
city, plus a grand total) and attaches the full object list (`orphans.tsv`) as a run artifact. It
never deletes on a schedule. To actually reclaim the space, trigger the workflow manually
(**Run workflow**) with **`apply = true`**.

**Manual / local.** Run the script directly:

```bash
PYTHONPATH=. python scripts/gc_audio.py --pull-state              # dry-run: list orphans
PYTHONPATH=. python scripts/gc_audio.py --pull-state --apply      # delete (skips objects < 7 days old)
PYTHONPATH=. python scripts/gc_audio.py --pull-state --out gc/    # dry-run + write the report files
```

`--pull-state` restores the durable bucket state first so the live set is current (a no-op for the
sync-less local backend). It deletes only objects not referenced by any record store and older than
`--min-age-days` (default 7), so an object written by an in-flight build is never reaped prematurely.
