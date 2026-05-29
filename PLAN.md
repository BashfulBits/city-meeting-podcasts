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
- **CivicPlus** — *not yet characterized.* CivicPlus spans several products (CivicMedia,
  CivicClerk/CivicEngage, the acquired Swagit platform) which expose archives differently
  (RSS, JSON API, or HTML only). Phase 2 begins with investigating real target instances,
  then implements the adapter(s) that fit. The abstraction absorbs whichever shape they use.

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

### CivicPlus / CivicMedia (Phase 2 investigation seeds)
CivicPlus video is its **CivicMedia** product, typically at `tx-<city>.civicplus.com/CivicMedia`.
How CivicMedia exposes an episode list (RSS vs JSON API vs HTML-only) is the first Phase 2
investigation task — the adapter shape depends on the answer.

| City | CivicMedia URL |
|------|----------------|
| Frisco, TX | `tx-frisco.civicplus.com/CivicMedia` (FTVN) |
| Gainesville, TX | `tx-gainesville.civicplus.com/CivicMedia` |
| Haltom City, TX | CivicPlus CMS — confirm CivicMedia path |

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

### Phase 2 — Provider extensibility: CivicPlus
- Investigate real CivicPlus target instances; determine RSS vs JSON vs HTML.
- Implement CivicPlus adapter(s) behind the existing Protocol; add fixtures + snapshots.
- Finalize multi-provider city YAML validation (adapter-side `validate`).
- Add at least one real CivicPlus city to the sample set.
- *Opportunistic:* if CivicPlus has no cheap change signal either (no ETag / `updated_at`),
  build the content-hash change token here — with two providers in hand the abstraction can
  be designed correctly — rather than waiting for Phase 4.

### Phase 3 — Artwork & frontend polish
- Wikipedia seal composite + state-palette placeholder (Pillow).
- SVG seal support via optional `cairosvg`; favicon fallback (`s2/favicons`).
- Index page: live search + state filters + add-city card with Canvas artwork preview;
  per-city subscribe pages. Custom artwork via committed `docs/<slug>/artwork.jpg` (never overwritten).

### Phase 4 — Scale & audio extraction
- **Content-hash change detection** (the Phase 0 finding): when a provider exposes no ETag/
  Last-Modified, fall back to hashing the fetched body so unchanged cities skip the
  parse/render/write. Only worth it at hundreds+ of cities — negligible at the DFW scale.
- `extract_audio: true`: `ffmpeg -vn -acodec copy` → M4A; upload to R2/B2/S3 with a
  per-city `audio_manifest.json` cache. Secrets per backend.
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

### GitHub Actions secrets (Phase 4 only, when `extract_audio: true`)
R2: `CLOUDFLARE_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` ·
B2: `B2_ACCESS_KEY_ID`, `B2_SECRET_ACCESS_KEY` ·
S3: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`

---

## Known limitations & future work

- **CivicPlus is uncharacterized** — Phase 2 starts with investigation; adapter shape TBD.
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
