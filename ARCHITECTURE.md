# Architecture

The **as-built** map of the system: how the code is laid out and how data flows today. This is the
companion to the *forward-looking* design — for where the system is **going**, see
[`review/11-technical-design-roadmap.md`](review/11-technical-design-roadmap.md) (the canonical,
living Technical Design Roadmap). For *why* past decisions were made, see [`review/`](review/).

> Convention: this file is kept **current** with the shipped code. When a change alters the shape
> described here, update this file as part of the same work (see the doc-update contract in
> [CONTRIBUTING.md](CONTRIBUTING.md)).

## What the system does

Turns US city-meeting archives into subscribable **podcast feeds** (audio + video) and a searchable
**directory**, hosted free on GitHub Pages, with derived audio/transcripts on object storage behind a
CDN. It is **provider-agnostic**: each meeting platform is a pluggable adapter behind one interface.

## Pipeline (data flow)

```
config (YAML)                       ── per-city + per-feed config
  │
  ▼
providers.fetch_episodes()          ── Granicus / CivicPlus / CivicClerk / Swagit adapters
  │
  ▼
records.assign_uids()               ── stable real-world episode UID (author+body+date)
  │
  ▼
records.merge_persisted()           ── APPEND-ONLY archive: {**persisted, **fresh}  (#52)
  │
  ▼
stages.run_stages()                 ── ordered enrichment, wall-clock budgeted:
  Chapters → Timeline → Remap → Audio → Transcript → Links
  │
  ▼
records.save_records()              ── state/sources/<key>/episodes.json
  │                                    (bucket = source of truth, via statesync)
  ▼
render / feeds / site               ── feed_content_hash skip → RSS + city pages + chapter sidecars
  │
  ▼
docs/  ──► GitHub Pages              ;   audio + transcripts + state ──► B2 (Cloudflare CDN)
```

The production deploy **splits render from enrich** (separate CLI commands, see below): Pages publishes
quickly from already-known state, then heavy enrichment runs best-effort and resumable.

## Module map (`citypods/`)

| Area | Modules |
|---|---|
| **Providers** | `providers/{base,granicus,civicplus,civicclerk,swagit}.py` — `MeetingProvider` Protocol + registry; each normalizes to the episode model. |
| **Records / identity** | `records.py` — stable `uid`, `source_key`, `audio_spec_hash`, `feed_content_hash`, append-only `merge_persisted`, content-addressed keys, orphan-GC refs. `models.py` — `Episode`/`City`. |
| **Enrichment stages** | `stages.py` — `EnrichmentStage` Protocol + `default_stages()` (`Chapters→Timeline→Remap→Audio→Transcript→Links`); `StageContext`, `StageStats`, the wall-clock `stop()` budget. |
| **Timeline / EDL** | `timeline.py` (served↔source map), `silence.py` (trim planner), `concat.py` (multi-segment), `clips.py` (clip/soundbite extraction). |
| **Media / audio** | `media.py` — ffmpeg encode, pinned AAC encode threads, loudness (EBU R128), content-addressed upload. |
| **Transcripts** | `asr.py` — forced alignment (stable-ts) / fresh transcription (faster-whisper) with align-error fallback; emits a clean **segment-cue VTT** (served via `<podcast:transcript>`) **plus a word-level JSON sidecar** (`…-asr-<recipe>.words.json`) for search/clips/diarization; version-aware re-transcribe on an `ASR_PIPELINE_VERSION` bump (provider transcripts never invalidated); both objects are content-addressed + GC-referenced. `bench.py` — `asr-bench` diagnostic. |
| **Feeds / site** | `feeds.py`, `render.py`, `site.py`, `templates/*.j2`, `artwork.py` (cover art). |
| **Orchestration** | `run.py` — `SourcePipeline`, `build()`, run history, graceful yield, resource-guard wiring. `resources.py` — process resource snapshots + memory/load admission guard for expensive native work. `cli.py` — `build / render / enrich / report / doctor / bodies / asr-bench / rebuild-audio / admin`. |
| **State** | `state.py` (build fingerprint), `statesync.py` (bucket↔local; bucket is truth), `storage/{base,local,s3}.py` (`S3CompatibleStorage` b2/r2 presets + local). |
| **Ops / QA** | `audit.py` (+ `scripts/audit_feeds.py`) feed-health; `contracts.py` endpoint contracts; `report.py` + `projection.py` cost/throughput + `/admin/status`; `validate.py` feed validation. |
| **Security** | `security.py` — SSRF gate (`validate_source_url`), host allowlists, redirect/size caps; `http.py` retry/backoff; ffmpeg protocol whitelist; defusedxml. |

## Key invariants (why it extends cleanly)

- **Provider Protocol + registry** — a new platform is a new adapter, no core change.
- **Stage Protocol + `default_stages()`** — a new per-episode feature is a new stage.
- **Split hashes** — `audio_spec_hash` (bytes) and `feed_content_hash` (RSS) invalidate independently.
- **Content-addressed audio + stable UID** — CDN cache-bust, rollback, and provider-migration safe.
- **Append-only archive** — meetings that drop off a provider feed are never lost (#52).
- **Timeline served↔source EDL** — silence-trim/concat/intro/transcripts/clips all reduce to one
  served-vs-source time map (see [`review/08`](review/08-timeline-and-content-transforms.md)).
- **Bucket-as-truth state** — derived artifacts survive Actions cache eviction.
- **Wall-clock budget + graceful yield** — heavy work runs until a time window closes or a newer run
  queues; cheap idempotent bookkeeping always finishes (see `stages.py` "stop convention").
- **Resource admission for expensive native work** — ffmpeg/ASR starts can wait for memory/load
  headroom, and abandoned ASR inference continues to occupy its worker slot until the native thread
  exits, so a stopped item does not stack unbounded CPU/RAM work.

## Hosting & CI/CD

- **Static site** → Jinja2 templates render to `docs/`; Jekyll disabled (`docs/.nojekyll`); served by
  GitHub Pages at the configured custom domain.
- **Object storage** → Backblaze B2 (S3 API) fronted by a Cloudflare Worker/CDN (free egress) for
  audio + transcripts + durable state.
- **Workflows** (`.github/workflows/`): `ci.yml` (ruff + pytest on PR/push), `preview.yml` (per-PR
  downloadable site preview), `deploy.yml` (render+deploy Pages on `main` push + 4h cron, then enrich),
  `audit.yml` (daily feed-health → GitHub issues), `contracts.yml` (weekly live endpoint contracts).
- **Tests** run fully offline against recorded fixtures; feeds have byte-for-byte snapshot tests.

## Provider notes & gotchas

Hard-won facts that bite anyone adding/debugging providers:

- **Granicus RSS is hard-capped at 100 items per view.** Cities split bodies across multiple views, so
  a single feed can be incomplete — use `source.feed_urls` (merged + deduped by guid) to combine views,
  and the feed-health `view-cap` check flags any view sitting at exactly 100.
- **Swagit serves a "Carmel, IN" placeholder page for unknown subdomains** — a false-positive trap when
  probing/discovering. Cross-check the returned locality against the requested city before trusting it.
- **Swagit `/videos/{id}/download` is broken for older meetings** (returns a keyless presigned S3 URL
  that 403s); the real media for old meetings is the per-segment files on the video page. Newer meetings
  are fine. See `.claude` history / `swagit.py`.
- **CLI console-script can fail to import the editable package from a script dir** — prefer
  `python -m citypods.cli …` (and `PYTHONPATH=. python scripts/…`).
- **Granicus `DownloadFile.php` 302-redirects to a real MP4** even when the RSS `type` says WMV — that
  legacy type is metadata, not the actual media.
- **Granicus rate-limits `DownloadFile.php` with `403` (not `429`) under concurrent access.** The adapter
  **pre-follows** the redirect to the signed `archive-video.granicus.com` URL and hands ffmpeg that signed
  URL directly (the CDN `403`s an unsigned bare path), retrying the resolve on `403` with backoff+jitter
  (PRs #245/#250/#251). Resolution stays at **fetch time** (`resolve_media_url`), never persisted, because
  the signed URL expires.
- **`Retry-After` is honored but clamped to 120s** by the shared HTTP session (`_ClampedRetry` in
  `http.py`). A Granicus 429 returning `Retry-After: 3600` once hung the whole build for an hour inside
  urllib3's retry sleep; capping keeps short legitimate backoffs without letting one header stall the run.

## Security boundary

Sources are maintainer-authored today, but the SSRF/abuse boundary is already firm for when onboarding
opens to submissions: https-only, per-provider host allowlists, private/loopback/link-local IP
rejection, bounded redirects + response size, ffmpeg `-protocol_whitelist`, defusedxml parsing. See
[SECURITY.md](SECURITY.md). **Any future LLM output is treated as untrusted** and must never overwrite
official links, titles, dates, or transcript text.
