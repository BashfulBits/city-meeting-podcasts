# City Meetings Podcast Directory — Software Plan

## Project overview

A GitHub repository that automatically converts any US city's meeting archive into
subscribable podcast feeds (audio and video), hosted free on GitHub Pages, with a
searchable public directory at a custom domain.

The system is **provider-agnostic**: each video-hosting platform (Granicus, CivicPlus,
and future platforms) is implemented as a pluggable adapter behind a common interface.
Everything downstream — RSS generation, city pages, artwork, audio extraction — operates
on a normalized episode model and never touches provider specifics.

**Live architecture:**
- `cities/*.yml` — one file per city, declaring a `provider` and a provider-specific `source` block
- `citypods/` — Python package: provider adapters, feed builders, artwork, site generation, CLI
- `docs/` — served by GitHub Pages (Jekyll disabled via `.nojekyll`); contains the index,
  per-city pages, RSS feeds, and artwork
- GitHub Actions — builds on schedule, on PRs (preview), and on push to `main` (production)

---

## Key decisions (locked)

| Decision | Choice | Rationale |
|---|---|---|
| **Language** | Python | Clean `subprocess` access to ffmpeg/AV tooling; mature feed/artwork/cloud-storage ecosystem; civic-tech contributors skew Python-literate. |
| **Frontend output** | Jinja2 templates → static HTML | Real, testable template files instead of an embedded f-string; output stays fully static. |
| **GitHub Pages / Jekyll** | Jekyll **disabled** (`docs/.nojekyll`) | Our generator produces final HTML/XML; Jekyll would otherwise mangle pre-built output. Pages just serves files as-is. |
| **Code origin** | Built fresh from this plan | No legacy code to port. |
| **First sample set** | DFW Granicus cities (~18) | First real target scale; fixtures recorded once from live feeds and committed so CI is offline. |
| **Provider model** | Adapter `Protocol` + registry | A new platform is one new file + one registry entry; multi-provider is the central design goal. |

---

## Provider abstraction (central design)

Every platform implements a small contract. Downstream code depends only on the normalized
models, never on a concrete provider.

```python
@dataclass
class Episode:
    title: str
    published: datetime
    video_url: str
    audio_url: str | None      # defaults to video_url with audio/mp4 MIME
    duration: int | None
    description: str
    guid: str

class MeetingProvider(Protocol):
    name: str                                  # "granicus", "civicplus", ...
    def validate(self, source: dict) -> None   # validate a city's source block
    def detect_change(self, source: dict) -> ChangeToken | None  # ETag/Last-Modified/hash
    def fetch_episodes(self, source: dict) -> list[Episode]
```

Providers are looked up via a registry keyed by `provider:` in the city YAML. Change
detection is provider-defined: feed-based providers use HEAD + ETag (as today), scrapers
may hash a page or a JSON payload.

### City config (new shape)

```yaml
slug: denton-tx
provider: granicus
source:
  feed_url: https://denton.granicus.com/ViewPublisherRSS.php?view_id=2
podcast_title: "Denton City Council Meetings"
podcast_author: "City of Denton, TX"
podcast_email: clerk@cityofdenton.com
podcast_description: "..."
state: TX
# optional overrides: podcast_language, podcast_category, max_episodes, extract_audio
```

### Provider notes

- **Granicus** — `ViewPublisherRSS.php` XML feed. HEAD + ETag change detection. Handles both
  `<enclosure>` and `<media:content>`. One `view_id` = one meeting type; multiple feeds per
  city = multiple city YAML files (or a future `view_ids` list).
- **Swagit** (Granicus-owned) — *characterized (2026-05).* No public API/RSS; the archive
  "view" page is server-rendered HTML (rows of `/videos/{id}` + body name + date). One view
  lists every body, so a city selects one via `body:` (substring match) — one feed per body
  (`scripts/discover_swagit.py` lists them). Media is a progressive MP4 behind an expiring
  (~1h) presigned S3 URL (`/videos/{id}/download`), so it's re-hosted as audio via the
  materialization pipeline (audio-only, like CivicPlus). Used by Dallas, Houston, and many
  large cities — one adapter, broad reach.
- **CivicClerk** — *characterized (2026-05).* Public OData JSON API at
  `<tenant>.api.civicclerk.com/v1/Events`. Recorded meetings expose `mediaSourcePathMp4` as
  an absolute progressive MP4 on CivicPlus's Azure CDN (`cpmedia.azureedge.net`) — used as a
  direct enclosure (like Granicus). `category_id` filters meeting type; non-meeting items
  (press conferences, relative streaming paths) are skipped. Source MP4s can be multi-GB, so
  `extract_audio: true` re-hosts a small M4A for the audio feed while video points at the CDN.
- **CivicPlus / CivicMedia** — *characterized (2026-05).* The CivicMedia module exposes a
  per-channel RSS feed at `/RSSFeed.aspx?ModID=92&CID=<channel>` listing meetings (title,
  pubDate, guid, and a `?VID=<n>` page link) — **but no enclosure/direct media URL**. Each
  `VID` resolves to a TikiLive `videoId` (e.g. via the page's `civplus.tikiliveapi.com/embed?
  ...&videoId=<id>` reference), whose only media URL is a **tokenized, time-limited, IP-bound
  HLS manifest** (`.m3u8`). Consequences:
  - HLS can't be a podcast `<enclosure>` (players need progressive MP4/M4A), and the URL
    expires/IP-binds so it can't live in a feed regardless.
  - Therefore CivicPlus feeds **require materialization**: download the HLS, demux audio with
    ffmpeg → M4A, host it ourselves (R2), and point the enclosure at the hosted file. This is
    the shared media pipeline (see Phase 2, re-scoped). Audio-only is the product scope
    (video re-hosting is storage-heavy and deferred).
  - The HLS token is ~24h-valid and bound to the requester's IP; the same CI runner fetches
    the token and runs ffmpeg, so the download stays within the valid window/IP.

---

## DFW target cities (initial sample set)

Researched DFW-metroplex candidates. **Confirmed** = a valid Granicus podcast RSS feed was
fetched and verified. **Subdomain known** = the Granicus host is identified but the correct
`view_id` still needs verification (Granicus returns HTTP 404 for a wrong `view_id`, so each
must be probed individually in Phase 0). **CivicPlus** targets feed Phase 2.

> ⚠️ Verify every entry before committing a fixture — subdomains are deceptively similar.
> e.g. `mesquitenv.granicus.com` is Mesquite **Nevada**, not Mesquite TX. Always confirm the
> feed `<title>` names the right city/state.

### Granicus (Phase 0 / Phase 1 fixtures)
| City | Status | Host / feed |
|------|--------|-------------|
| Fort Worth, TX | ✅ Confirmed | `fortworthgov.granicus.com` — `ViewPublisherRSS.php?view_id=5&mode=vpodcast` |
| Arlington, TX | ✅ Confirmed | `arlingtontx.granicus.com` — `ViewPublisherRSS.php?view_id=2&mode=vpodcast` |
| Denton County, TX | ✅ Confirmed | `dentoncounty.granicus.com` — `ViewPublisherRSS.php?view_id=26&mode=vpodcast` |
| Denton, TX | 🔎 Subdomain known | `denton-tx.granicus.com` — verify `view_id` |
| Garland, TX | 🔎 Likely Granicus | confirm host (CGTV broadcasts) + `view_id` |
| Mesquite, TX | 🔎 To verify | NOT `mesquitenv` (that's NV); find the TX host |
| Plano, TX | 🔎 To verify | confirm host + `view_id` |
| Irving, TX | 🔎 To verify | confirm host + `view_id` |
| Richardson, TX | 🔎 To verify | confirm host + `view_id` |
| McKinney, TX | 🔎 Swagit (Granicus-owned) | `swagit-attachments.granicus.com` seen; confirm feed path |
| Grand Prairie, TX | 🔎 To verify | confirm host + `view_id` |
| Carrollton, TX | 🔎 To verify | confirm host + `view_id` |
| Lewisville, TX | 🔎 To verify | confirm host + `view_id` |

Goal is ~18 cities; the confirmed three are enough to build Phase 0/1 fixtures, and the rest
get verified and added incrementally (each new city is a small PR).

### CivicPlus / CivicMedia (characterized 2026-05)
CivicPlus video is its **CivicMedia** product, at `<site>/CivicMedia`. Episode list comes
from the per-channel RSS `<site>/RSSFeed.aspx?ModID=92&CID=<channel>`; media is tokenized
TikiLive HLS requiring materialization (see Provider notes). Adapter built in Phase 2.

| City | CivicMedia | Channel RSS (ModID=92) |
|------|------------|------------------------|
| Gainesville, TX | `gainesville.tx.us/CivicMedia` | `?CID=City-Council-1` (✅ verified seed city) |
| Frisco, TX | `tx-frisco.civicplus.com/CivicMedia` (FTVN) | confirm channel CID |
| Haltom City, TX | confirm CivicMedia path | confirm channel CID |

(Note: Dallas itself cablecasts and streams from `dallascityhall.com`; its archive platform
still needs identification.)

---

## Repository structure

```
city-meeting-podcasts/
├── pyproject.toml                  # package metadata, deps, ruff config
├── site_config.yml                 # Global: domain, title, schedule, rate limiting, defaults
├── PLAN.md
├── README.md
├── .gitignore
├── citypods/                       # Main package
│   ├── __init__.py
│   ├── cli.py                      # `citypods build [--city SLUG] [--dry-run]`
│   ├── config.py                   # load_site_config, load_city_configs
│   ├── models.py                   # Episode, City, ChangeToken
│   ├── providers/
│   │   ├── __init__.py             # registry: name -> provider
│   │   ├── base.py                 # MeetingProvider Protocol
│   │   ├── granicus.py
│   │   └── civicplus.py            # Phase 2
│   ├── feeds.py                    # build_rss (audio + video, iTunes namespace)
│   ├── artwork.py                  # Wikipedia seal composite / placeholder
│   ├── site.py                     # index + per-city pages via Jinja2
│   └── run.py                      # orchestration: ThreadPoolExecutor, etag cache
├── templates/
│   ├── index.html.j2
│   └── city.html.j2
├── cities/
│   ├── _template.yml
│   └── *.yml                       # DFW set seeded in Phase 0/1
├── tests/
│   ├── fixtures/                   # recorded provider responses + golden RSS
│   └── test_*.py
├── .github/
│   ├── ISSUE_TEMPLATE/add-city.yml
│   └── workflows/
│       ├── ci.yml                  # lint, tests, snapshot, feed validation (PRs + push)
│       ├── preview.yml             # PR preview deploy of docs/
│       └── deploy.yml              # scheduled + main-push production deploy to Pages
└── docs/                           # GitHub Pages root (auto-generated)
    ├── .nojekyll
    ├── CNAME
    ├── index.html
    ├── .feed_etags.json            # change-detection cache (committed)
    ├── meta.json
    └── <slug>/
        ├── index.html
        ├── audio_feed.xml
        ├── video_feed.xml
        ├── artwork.jpg
        ├── audio_manifest.json     # only if extract_audio: true
        └── meta.json
```

---

## Phased delivery (GitHub flow: one feature branch + PR per phase; `main` stays deployable)

### Phase 0 — Repo skeleton & restructure
Thin foundation so later phases have real code to test. No new product features.
- Package layout above; `pyproject.toml`; `.gitignore`; `docs/.nojekyll`.
- Normalized models + `MeetingProvider` Protocol + registry.
- **Granicus adapter** implemented fresh from this plan (change detection, parsing, both
  enclosure styles).
- `feeds.py` (audio + video RSS) and a minimal `site.py` rendering Jinja2 templates.
- CLI: `citypods build [--city SLUG] [--dry-run]`.
- Seed `cities/` with confirmed DFW Granicus cities (grow toward ~18).

> **Finding (Phase 0):** Granicus HEAD responses carry no ETag/Last-Modified, so HEAD-based
> change detection never skips — every run fetches. This is harmless at ~18 cities and is a
> *scale* concern, so the content-hash fallback is deferred to Phase 4 (see below), not
> Phase 1. The `ChangeToken.content_hash` field already exists as the drop-in hook.

### Phase 1 — Testing & CI/CD foundation *(first priority)*
- **pytest + recorded fixtures** (VCR-style): provider responses captured once, committed;
  CI never hits the network.
- **Regression snapshot tests**: golden RSS for the DFW sample; CI fails if existing feed
  entries change (the primary safety net — most future changes are usability/new cities).
- **Feed validation in CI**: every generated `*.xml` validated; build fails if invalid.
- **Lint/format gates**: `ruff` + `ruff format` as required status checks.
- **Workflows**: `ci.yml` (PRs + push), `preview.yml` (per-PR preview deploy of `docs/`),
  `deploy.yml` (scheduled `0 */4 * * *` + main-push production deploy to Pages).

### Phase 2 — Media materialization pipeline + CivicPlus *(re-scoped)*
Investigation (done) showed CivicMedia serves only tokenized HLS, so CivicPlus can't be a
plain feed-based adapter — it needs media re-hosting. The materialization pipeline is the
same machinery Granicus needs for `extract_audio: true`, so it's built here as a shared
dependency rather than deferred.
- **Media model**: extend `Episode` with a `MediaSource` (`direct` | `hls`) plus a
  pipeline-populated `hosted_audio_url`. Granicus default stays "point at the source MP4";
  `extract_audio`/HLS sources go through materialization.
- **Storage backends**: `storage/` package — `local` filesystem (dev/tests, no creds) and
  **Cloudflare R2** (zero egress; production). Pluggable for B2/S3 later.
- **Audio pipeline** (`media.py`): resolve media → `ffmpeg` demux/encode to M4A → upload →
  `audio_manifest.json` cache (skip already-hosted episodes). A per-run episode budget keeps
  large backfills under the Actions 6-hour job limit (catch up over successive runs).
- **CivicPlus adapter**: parse the CivicMedia RSS list; resolve `VID` → TikiLive `videoId` →
  HLS manifest; emit `Episode`s with `MediaSource(kind="hls")`. Audio-only feeds.
- Finalize multi-provider city YAML validation (adapter-side `validate`); seed a real
  CivicPlus city (`gainesville-tx`); add fixtures + snapshots; wire R2 secrets into deploy.
- *Opportunistic:* if a content-hash change token is needed for CivicMedia's RSS, build it
  here (two providers now inform the abstraction) rather than waiting for Phase 4.

### Phase 3 — Artwork & frontend polish
- Wikipedia seal composite + state-palette placeholder (Pillow).
- SVG seal support via optional `cairosvg`; favicon fallback (`s2/favicons`).
- Index page: live search + state filters + add-city card with Canvas artwork preview;
  per-city subscribe pages. Custom artwork via committed `docs/<slug>/artwork.jpg` (never overwritten).

### Phase 4 — Scale & video re-hosting
- **Content-hash change detection** (the Phase 0 finding): when a provider exposes no ETag/
  Last-Modified, fall back to hashing the fetched body so unchanged cities skip the
  parse/render/write. Only worth it at hundreds+ of cities — negligible at the DFW scale.
- **Video re-hosting** (optional, storage-heavy): remux full MP4 alongside the audio M4A for
  providers like CivicPlus that only serve HLS; additional storage backends (B2/S3).
  (Audio-only materialization + R2 already shipped in Phase 2.)
- Index pagination/virtualization for 1,000+ cities.
- Scheduled discovery workflow (`discover_cities.py`-style) opening review Issues for new subdomains.

---

## Cross-cutting design (carried from original plan)

- **Feed format**: valid iTunes/Apple Podcasts RSS with full `<itunes:*>` tags;
  `<enclosure length="0">` intentional (players re-check size at download; per-episode HEAD
  is too expensive at scale).
- **Audio strategy**: default audio feed points at the same MP4 (MIME `audio/mp4`); optional
  lossless M4A demux + cloud upload (Phase 4).
- **Change detection**: provider-defined; feed providers attempt one HEAD + ETag/Last-Modified
  compare against `docs/.feed_etags.json` to skip unchanged cities (critical at scale). Where a
  provider returns no validators (e.g. Granicus HEAD), the build simply always fetches; a
  content-hash fallback is a Phase 4 scale optimization.
- **Concurrency**: `ThreadPoolExecutor(max_workers=20)` over I/O-bound provider calls.
- **Rate limiting**: `request_delay_seconds` (default 0.1s) per worker thread.
- **Actions budget**: ETag caching + concurrency keep all target scales (DFW ~18, TX ~70,
  TX+4 ~150, USA ~1,000) within the free 2,000 min/month at 4-hour cadence.

---

## Configuration reference

### site_config.yml
| Key | Default | Description |
|-----|---------|-------------|
| `custom_domain` | `""` | Custom domain; blank = github.io URL |
| `site_title` | `"City Council Podcast Directory"` | Index page title |
| `site_description` | ... | Index page subtitle |
| `site_author` / `site_author_url` | `""` | Footer attribution |
| `github_repo` | `""` | `"user/repo"` for Issue links |
| `schedule_cron` | `"0 */4 * * *"` | Production deploy cadence |
| `request_delay_seconds` | `0.1` | Politeness sleep per worker thread |
| `defaults.podcast_language` | `"en-us"` | Inherited by all cities |
| `defaults.podcast_category` | `"Government"` | Inherited by all cities |
| `defaults.max_episodes` | `50` | Max episodes per feed |
| `defaults.extract_audio` | `false` | Enable M4A extraction (Phase 4) |
| `defaults.audio_storage_backend` | `"r2"` | `"r2"`, `"b2"`, or `"s3"` |

### City config (cities/*.yml)
| Key | Required | Description |
|-----|----------|-------------|
| `slug` | ✅ | URL path segment, e.g. `denton-tx` |
| `provider` | ✅ | Adapter name: `granicus`, `civicplus`, ... |
| `source` | ✅ | Provider-specific block, validated by the adapter |
| `podcast_title` / `podcast_author` / `podcast_email` / `podcast_description` | ✅ | Feed metadata |
| `state` | recommended | Two-letter abbreviation for state filters |
| `city_website` | optional | Artwork hint (favicon fallback) |
| `podcast_language` / `podcast_category` / `max_episodes` / `extract_audio` | optional | Override site defaults |

### GitHub Actions secrets (audio hosting — CivicPlus always, Granicus when `extract_audio`)
Storage is S3-compatible; set the secrets for the `audio_storage_backend` in use:
- **B2** (+ Cloudflare CDN for free egress): `B2_ENDPOINT`, `B2_KEY_ID`, `B2_APP_KEY`,
  `B2_BUCKET`, `B2_PUBLIC_BASE_URL` (the Cloudflare-fronted domain)
- **R2**: `CLOUDFLARE_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`,
  `R2_PUBLIC_BASE_URL`

Secrets live in repo Settings → Secrets and variables → Actions (encrypted, masked in logs,
never in the repo, not exposed to fork PRs). The public repo means Actions minutes are free;
the per-job 6-hour cap is managed by `materialize_budget_per_city`.

---

## Status (2026-05-31)

Phases 0–4 shipped. Since then, also merged to `main`:

- **Episode-record refactor (R1–R3):** stable provider-independent `uid` as RSS `<guid>`, split
  invalidation (`audio_spec_hash` for bytes vs `feed_content_hash` for RSS), content-addressed
  audio, per-source record store, stable feed URLs (`aliases` + `itunes:new-feed-url` + redirects),
  orphan-audio GC.
- **Durable build state:** the object bucket (not `actions/cache`) is the source of truth for
  derived artifacts (`citypods/statesync.py`); stale `docs/<slug>` dirs are pruned each build.
- **Enrichment-stage pipeline:** `default_stages() = [chapters, audio, links]`. Shipped no-cost
  stages: resource links + `content:encoded` show notes; agenda links (Granicus AgendaViewer,
  CivicClerk publishedFiles); chapters (Swagit `/videos` page, Granicus `JSON.php`, CivicClerk
  `EventsMedia`) embedded in AAC *and* surfaced as Podcasting 2.0 `<podcast:chapters>` sidecars;
  transcript links (Swagit, CivicClerk).

**Next:** the paid stages (ASR transcripts → then summaries/search/votes/translation), and Phase 5
discovery/onboarding. See **`review/`** (overnight review, 2026-05-31) for: a 50-feature brainstorm,
required architecture changes, a parametric **resource cost/time projection** proposal, a
bug/security audit (incl. a **broken `generate_board_cities.py` import** and an **SSRF gate required
before Phase 5**), and an endpoint contract-test plan.

## Known limitations & future work

- **CivicMedia RSS exposes only recent items** — the `ModID=92` channel feed returns just the
  latest few meetings, not the full archive, so CivicPlus feeds are limited to what the RSS
  currently lists (older meetings drop off and are never materialized). A future enhancement
  could scrape the CivicMedia channel page (`?CID=…`) for the full `VID` list.
- **CivicPlus audio is re-hosted** — feeds depend on our R2 bucket; if it's emptied or the
  manifest/object is lost, enclosures 404 until the next run re-materializes. Audio-only:
  no video feed for HLS-sourced providers (deferred to Phase 4).
- **Granicus rate limiting** at large scale is untested; mitigate via `request_delay_seconds`/cadence.
- **Wikipedia/seal coverage** is incomplete; SVG seals need `cairosvg` (system `libcairo`); placeholder fallback always works.
- **`podcast_email`** required by RSS spec but not validated; blank passes through.
- **State color palettes** approximate flag colors, not official standards.
- **`docs/` is committed** — repo size grows with city count; keep `max_episodes` reasonable.
- **Index DOM size** at 1,000+ cities — pagination/virtualization in Phase 4.
- **Multiple meeting types per city** — currently one feed per city YAML; a `view_ids`/multi-source list is future work.

---

## Local development

```bash
pip install -e .            # installs citypods + deps
# optional, for SVG seals: pip install cairosvg  (needs libcairo)

citypods build                       # build all configured cities -> docs/
citypods build --city denton-tx      # single city
citypods build --dry-run             # no writes; report what would change

ruff check . && ruff format --check .
pytest                               # unit + snapshot + feed-validation tests

cd docs && python -m http.server 8000   # serve locally
```
