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
  per-PR site preview must pass. **Merge with a merge commit (`--merge`), not squash** — preserve
  per-commit history.
- **Branch names:** `<type>/<short-kebab-slug>`, where `<type>` is one of:
  - `feat/` — new features or capabilities (include the tracking issue number when there is one,
    e.g. `feat/110-asr-transcripts`, `feat/151-loudness-normalization`)
  - `fix/` — bug fixes (e.g. `fix/scheduled-run-graceful-yield`)
  - `docs/` — documentation-only changes (e.g. `docs/roadmap-doc-consistency`)
  - `refactor/` — internal restructuring with no behavior change
  - `chore/` — maintenance / housekeeping (deps, CI config, repo hygiene)

  An agent acting on the maintainer's behalf may use its own name as the prefix instead of a type,
  to make provenance visible in the branch list and PR history (e.g. `codex/reduce-enrich-concurrency`).
- **Architecture:** new platforms are provider adapters behind the `MeetingProvider` Protocol; new
  per-episode features are enrichment **stages** (see `citypods/stages.py`). Most features need no
  core changes — see `review/02-architecture.md`.
- **Tests:** parsers are pure and tested from offline fixtures; feeds have byte-for-byte snapshot
  tests; lint + format are enforced repo-wide.

## Roadmap & priorities

See [ROADMAP.md](ROADMAP.md) for the near-term prioritized backlog, [VISION.md](VISION.md) for the
long-horizon direction, and **[`review/11-technical-design-roadmap.md`](review/11-technical-design-roadmap.md)**
— the living canonical design index — to find the design for a feature and pick the next
development-ready item. [`review/00–10`](review/) hold point-in-time rationale, cost models, and
architecture history.

## Documentation map

| To understand… | Read |
|---|---|
| The system as built | [ARCHITECTURE.md](ARCHITECTURE.md) |
| Near-term plan / long-horizon vision | [ROADMAP.md](ROADMAP.md) / [VISION.md](VISION.md) |
| Forward design, pick next work | [`review/11`](review/11-technical-design-roadmap.md) + breakouts `review/12+` |
| What shipped | [CHANGELOG.md](CHANGELOG.md) |
| Agent/AI orientation | [AGENTS.md](AGENTS.md) (and [CLAUDE.md](CLAUDE.md)) |
| Security posture & reporting | [SECURITY.md](SECURITY.md) |

## Feature lifecycle & doc-update contract (normative)

This is the **single normative copy** of the contract (mirrored for convenience in
[`review/11`](review/11-technical-design-roadmap.md) §2 and [AGENTS.md](AGENTS.md)). Every feature
travels this pipeline; when you move it forward, update the listed docs **in the same change** so the
design docs never go stale:

| Stage | Trigger | Update (change X → update Y) |
|---|---|---|
| **Idea** | captured | `VISION.md` (long-horizon) **or** `review/11` Deferred-backlog entry |
| **Committed** | promoted to near-term | `ROADMAP.md` + `review/11` catalog (L1) + write the L1 sketch inline in `review/11` |
| **Designed** | approach chosen / next-up | **break out** to a new `review/NN`; `review/11` entry → L2 + link |
| **Dev-ready** | full design done | mature `review/NN` to L3; **cut GitHub Issue(s)** from it; `review/11` → L3 |
| **Implemented** | PR merged | `review/11` entry → **Shipped** (+PR/issue link); **add a `CHANGELOG.md` entry**; update `ARCHITECTURE.md` if architecture changed; **freeze + stamp** the `review/NN` breakout ("Implemented in PR #N"); move the ROADMAP item to "Recently shipped"; close/narrow issues; capture any durable decision in the relevant committed doc (Claude Code also updates its local `.claude/memory` cache, which is not in the repo) |
| **Superseded** | abandoned/replaced | mark the `review/11` entry; note in `CHANGELOG.md` if ever partially shipped |

## How to add an enrichment stage

A new per-episode feature is almost always a **stage**, not a core change.
1. Implement `process(provider, city, episodes, ctx) -> StageStats` in `citypods/stages.py` (or a new
   `citypods/stages/<name>.py` if the module is getting large).
2. Insert it in `default_stages()` **in the right order**: anything that changes audio bytes
   (chapters/timeline/loudness/trim) must precede `AudioStage`, since audio is content-addressed by its
   spec; feed-only stages (transcript/summary/links/tags) run after.
3. Gate only expensive *restartable* work on `ctx.stop()`; let cheap idempotent bookkeeping always run.
4. If it produces a durable artifact, give it a **spec hash** + **content-addressed key** (mirror audio).
5. Add tests in `tests/test_stages.py`; if it changes feed output, regenerate snapshots.

## How to add a provider

1. Implement the `MeetingProvider` Protocol in `citypods/providers/<name>.py` and register it.
2. Normalize to the episode model (`body`, dates, media URL, optional chapters/agenda/transcript).
3. Add the host to the **SSRF allowlist** (`citypods/security.py`).
4. Record offline fixtures (`tests/fixtures/`) — **no live network in default CI**; add parser unit
   tests and a feed snapshot. Add a live contract test under `@pytest.mark.live` (runs in `contracts.yml`,
   not PR CI).
5. Document it in [README.md](README.md) and [`.github/ADD_CITY.md`](.github/ADD_CITY.md).

## Updating feeds & snapshots

Feed output is snapshot-tested byte-for-byte. After an **intentional** change, regenerate with
`SNAPSHOT_UPDATE=1 pytest` and review the diff in the PR. Never change artifact **identity** (audio
spec hash inputs, UID derivation) without a migration note in [MIGRATION.md](MIGRATION.md).

## Security checklist (per PR)

- No provider network calls in normal CI; no secrets committed (env-only).
- Any new fetch of a user-influenced URL goes through `validate_source_url`.
- No LLM/generated output overwrites official links/titles/dates/transcript text.
- See [SECURITY.md](SECURITY.md) for the full posture.

## PR checklist

- [ ] Tests added/updated; `ruff check . && ruff format --check .` and `pytest` pass.
- [ ] Feed snapshots regenerated intentionally (if output changed).
- [ ] Docs updated per the lifecycle contract (review/11 + CHANGELOG + ARCHITECTURE as applicable).
- [ ] No artifact-identity change without a migration note.
- [ ] Security checklist satisfied.
