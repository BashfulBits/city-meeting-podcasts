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

**Providers**
- **granicus** — `source.feed_url` is a `ViewPublisherRSS.php` URL. Media is a direct MP4,
  used as the enclosure as-is.
- **civicplus** — `source.feed_url` is a CivicMedia channel RSS (`RSSFeed.aspx?ModID=92&CID=…`).
  Media is tokenized HLS, so audio is downloaded with ffmpeg, re-encoded to M4A, and hosted
  (R2 in production). Requires `extract_audio: true`, ffmpeg, and a storage backend.

## Audio hosting

CivicPlus (always) and Granicus-with-`extract_audio` re-host audio. Backend is chosen by
`defaults.audio_storage_backend` (or the `AUDIO_STORAGE_BACKEND` env override):
- `local` — writes to `docs/audio`, served from Pages. Good for dev/small sets. Needs ffmpeg.
- `r2` — Cloudflare R2; set `CLOUDFLARE_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`,
  `R2_BUCKET`, `R2_PUBLIC_BASE_URL` (as GitHub Actions secrets for deploys).

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
