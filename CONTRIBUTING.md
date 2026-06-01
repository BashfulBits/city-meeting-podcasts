# Contributing

Thanks for your interest! This project turns US city meeting archives into subscribable podcast
feeds and a searchable directory.

## Current status — beta, single-maintainer

The project is in active solo development through the **1.0** milestone (see [ROADMAP.md](ROADMAP.md)).
Until 1.0, the codebase and feed URLs may change, and we're **not yet actively soliciting code
contributions** — the issue tracker is kept lean and tickets are filed just-in-time for the current
working set. After 1.0, well-scoped items will be opened up and labeled **`good first issue`** /
**`help wanted`**, and this guide will be expanded.

You can help right now by:

- **Requesting a city** — open an issue describing the government entity and a link to its meeting
  archive (we support Granicus, Swagit, CivicClerk, and CivicPlus/CivicMedia).
- **Reporting a broken feed** — open an issue naming the feed/city and what's wrong (missing
  episodes, dead audio, wrong meeting body, etc.).

## Development setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"        # citypods + boto3 + pytest + ruff
# ffmpeg is required for audio materialization (apt-get install ffmpeg / brew install ffmpeg)

ruff check . && ruff format --check .
pytest -q                      # offline suite; live endpoint tests are opt-in: pytest -m live

python -m citypods.cli build --dry-run      # fetch + report, write nothing
cd docs && python -m http.server 8000       # browse generated output
```

Regenerate golden snapshots after an intentional output change with `SNAPSHOT_UPDATE=1 pytest`.

## Conventions

- **GitHub flow:** feature branch → PR → merge; `main` stays deployable. CI (ruff + pytest) and a
  per-PR site preview must pass.
- **Architecture:** new platforms are provider adapters behind the `MeetingProvider` Protocol; new
  per-episode features are enrichment **stages** (see `citypods/stages.py`). Most features need no
  core changes — see `review/02-architecture.md`.
- **Tests:** parsers are pure and tested from offline fixtures; feeds have byte-for-byte snapshot
  tests; lint + format are enforced repo-wide.

## Roadmap & priorities

See [ROADMAP.md](ROADMAP.md) for the prioritized backlog and [`review/`](review/) for the detailed
rationale, cost models, and architecture notes.
