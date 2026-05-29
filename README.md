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

## Development

```bash
ruff check . && ruff format --check .
pytest          # added in Phase 1
```
