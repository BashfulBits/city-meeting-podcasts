# Changelog

All notable changes to this project are recorded here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). The project is **pre-1.0 (beta)** and does not
yet cut tagged releases, so entries are grouped by milestone (most recent first) rather than by version.
Once 1.0 ships, entries move under semver tags.

> This is the **living history of what shipped**. The forward-looking plan lives in
> [ROADMAP.md](ROADMAP.md) and [`review/11`](review/11-technical-design-roadmap.md); when an initiative
> is implemented, add it here as part of the same work (see the doc-update contract in
> [CONTRIBUTING.md](CONTRIBUTING.md)).

## Unreleased

_Work in progress toward 1.0 — see [ROADMAP.md](ROADMAP.md) Phase H (Hardening & Efficiency)._

### Added
- **Documentation architecture & handoff**: `VISION.md`, forward-looking `ROADMAP.md`, this
  `CHANGELOG.md`, `ARCHITECTURE.md`, `SECURITY.md`, `AGENTS.md` + `CLAUDE.md`; the living canonical
  design index `review/11-technical-design-roadmap.md` + breakouts `review/12–14`; the feature
  lifecycle / doc-update contract in `CONTRIBUTING.md`.
- **Contributor scaffolding (partial #57)**: PR template, feature-request + bug-report issue templates,
  and an `area:*` / `needs-*` GitHub label taxonomy.

### Fixed
- **ASR alignment fallback (H10, PR #232)**: forced alignment now uses a stable-ts faster-whisper model
  that supports `.align()`, and any alignment failure falls back to fresh transcription instead of
  skipping caption-bearing episodes.
- **Runner resource guard (H8, PR #235)**: AAC ffmpeg encodes now pin `-threads`, heavy audio/ASR
  work waits for memory/load headroom before admission, and abandoned ASR daemon inference keeps its
  worker slot until the native thread exits instead of stacking new CPU/RAM work on top.

## Timeline & content-transform foundation

### Added
- **Served↔source timeline/EDL model** (`citypods/timeline.py`) unifying silence-trim, concat, intros,
  transcripts, and clips behind one served-vs-source time map (design: `review/08`; audit: `review/09`;
  INFRA-1..9, epic #141).
- **Silence trimming** (`citypods/silence.py`, `trim_silence`) — removes long lead/trail/mid-meeting
  dead air, remapping chapters and transcripts onto the served audio (#22/#111).
- **EBU R128 loudness normalization** (`audio_loudness_profile: ebuR128:-16LUFS`) (#21).
- **Multi-segment concat** (`citypods/concat.py`) for meetings split across source segments (#122).
- **Clip / soundbite extraction** (`citypods/clips.py`) — forward-maps a served range to source cuts.
- **ASR transcripts** (`citypods/asr.py`) — reuse provider transcripts first; otherwise forced alignment
  (stable-ts) or fresh transcription (faster-whisper); `asr-bench` CLI for WER/throughput (#1/#110).
- **`<podcast:transcript>`** emission for synced hosted transcripts (#11/#154).
- **Operational status dashboard** at `/admin/status/` rendered by `citypods report` (#124).

### Changed
- **Materialization budget replaced** with a wall-clock window + graceful yield: a run processes
  recordings until a shared `stop()` predicate (time window spent, or a newer Build & Deploy run queued).
  Removed `materialize_budget_per_run` / per-source count budgets (PR #128).

## Episode-record & enrichment-stage foundation

### Added
- **Append-only archive** (`records.merge_persisted`): meetings that drop off a provider feed (Granicus
  100-item cap, Swagit windowing) are retained and rendered from the full store (#52).
- **Stable episode UID** (author+body+date), **content-addressed audio keys**, and **split hashes**
  (`audio_spec_hash` vs `feed_content_hash`) for independent re-encode vs re-render invalidation.
- **Enrichment-stage pipeline** (`citypods/stages.py`): `EnrichmentStage` Protocol + `default_stages()`;
  adding a feature = adding a stage.
- **Stable feed URLs** — aliases + `<itunes:new-feed-url>` + redirect map for provider migrations.
- **No-cost feature stages**: resource/agenda links + `content:encoded` notes; chapters across capable
  providers; universal Podcasting 2.0 `<podcast:chapters>` sidecars.
- **Durable bucket-backed state** (`statesync.py`): object storage is the source of truth; Actions cache
  is latency only.
- **Resource cost/throughput projection** (`projection.py`) + `citypods report` + static what-if admin
  page; persisted `run_history.jsonl`.

## Scale, QA & discovery foundation (Phase 5 PR-A)

### Added
- **Feed-health audit** (`citypods/audit.py`, `scripts/audit_feeds.py`, `audit.yml`): staleness,
  view-cap, enclosure liveness, empty-feed, rehost-backlog checks → idempotent GitHub issues.
- **Endpoint contract tests** (`contracts.yml`, opt-in `@pytest.mark.live`) kept out of PR CI.

### Security
- **SSRF / source-URL gate** (`validate_source_url`), per-provider host allowlists, bounded
  redirects/response-size; **ffmpeg `-protocol_whitelist`**; **defusedxml** provider parsing; fetch
  retry/backoff; alias/slug collision validation.

## Earlier foundations (Phases 0–4)

### Added
- Python `citypods/` package; provider adapters for **Granicus, CivicPlus/CivicMedia, CivicClerk,
  Swagit**; one-feed-per-board generation.
- Audio materialization pipeline (ffmpeg → M4A) with **Backblaze B2 + Cloudflare CDN** (free egress).
- Static frontend (Jinja2 → `docs/`): index with instant search + group-by-city accordion, per-city
  feed pages with inline player + subscribe links, generated cover art, custom domain.
- Offline pytest suite with byte-for-byte feed snapshots; CI (`ci.yml`), per-PR preview (`preview.yml`),
  scheduled deploy (`deploy.yml`, 4h cron); incremental builds + content-hash change detection.
