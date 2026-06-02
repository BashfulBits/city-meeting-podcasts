# city-meeting-podcasts

Turn US city meeting archives into subscribable podcast feeds (audio + video),
hosted free on GitHub Pages, with a searchable public directory.

The system is **provider-agnostic**: each video platform (Granicus today;
CivicPlus and others planned) is a pluggable adapter behind a common interface.
See [PLAN.md](PLAN.md) for the full architecture and phased roadmap.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Build every configured city into docs/ (uses custom_domain or PAGES_BASE_URL)
citypods build

# Build a single city without writing anything
citypods build --city fort-worth-tx --dry-run

# Preview locally
PAGES_BASE_URL=http://localhost:8000 citypods build
cd docs && python -m http.server 8000   # open http://localhost:8000
```

## Adding a city

Copy [`cities/_template.yml`](cities/_template.yml) to `cities/<slug>.yml`, set the
`provider` and provider-specific `source` block, and fill in the podcast metadata.

### One feed per board/commission

Most sources list many bodies (City Council, Planning & Zoning, Board of Adjustments, …) in
one feed. Add an optional `source.body` filter (case-insensitive substring) to produce a
**single-body feed**, and make one YAML per board you want. List a source's bodies with:

```bash
citypods bodies <slug>      # meeting count + latest date per body
```

Audio that gets re-hosted (Swagit/CivicPlus, or `extract_audio`) is bounded per run by a
wall-clock window (`run_time_budget_minutes` × `budget_safety`) — and a run yields early if a
newer build is queued behind it — so splitting into many board feeds backfills safely over
successive runs.

**Providers**
- **granicus** — `source.feed_url` is a `ViewPublisherRSS.php` URL. Media is a direct MP4,
  used as the enclosure as-is.
- **civicplus** — `source.feed_url` is a CivicMedia channel RSS (`RSSFeed.aspx?ModID=92&CID=…`).
  Media is tokenized HLS, so audio is downloaded with ffmpeg, re-encoded to M4A, and hosted
  (R2/B2 in production). Requires `extract_audio: true`, ffmpeg, and a storage backend.
- **civicclerk** — `source.api_base` is a CivicClerk OData API host (e.g.
  `https://traviscotx.api.civicclerk.com`); optional `category_id` filters meeting type.
  Recorded meetings expose a direct CDN MP4 (used as the video enclosure). Set
  `extract_audio: true` to also publish a small hosted M4A audio feed.
- **swagit** — `source.list_url` is a Swagit view page and `source.body` selects one meeting
  body (substring-matched; one feed per body). Media is an expiring presigned MP4, so audio
  is always re-hosted (audio-only feed). List the bodies with
  `python scripts/discover_swagit.py <list_url>`.

## Audio hosting

CivicPlus (always) and Granicus-with-`extract_audio` re-host audio. Backend is chosen by
`defaults.audio_storage_backend` (or the `AUDIO_STORAGE_BACKEND` env override):
- `local` — writes to `docs/audio`, served from Pages. Good for dev/small sets. Needs ffmpeg.
- `b2` — Backblaze B2 (S3 API), free egress via Cloudflare CDN; set `B2_ENDPOINT`, `B2_KEY_ID`,
  `B2_APP_KEY`, `B2_BUCKET`, `B2_PUBLIC_BASE_URL` (Cloudflare-fronted domain).
- `r2` — Cloudflare R2; set `CLOUDFLARE_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`,
  `R2_BUCKET`, `R2_PUBLIC_BASE_URL`.

Both cloud backends are S3-compatible (one `S3CompatibleStorage` with presets). Set the
matching values as GitHub Actions secrets for deploys.

```bash
# Local end-to-end build that re-hosts audio under docs/audio:
AUDIO_STORAGE_BACKEND=local citypods build --city gainesville-tx   # needs ffmpeg installed
```

## Development

```bash
ruff check . && ruff format --check .
pytest                       # unit + regression-snapshot + feed-validation tests
```

Tests run fully offline against recorded fixtures in `tests/fixtures/`. After an
intentional change to feed output, regenerate the golden snapshots:

```bash
SNAPSHOT_UPDATE=1 pytest tests/test_snapshot.py
python scripts/refresh_fixtures.py   # only to re-record live feeds (rare)
```

CI (`.github/workflows/`): `ci.yml` runs ruff + pytest on every PR and push;
`preview.yml` builds a downloadable site preview per PR; `deploy.yml` builds and
deploys to GitHub Pages on push to `main` and on a 4-hour schedule.
