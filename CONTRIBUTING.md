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
pip install -e ".[dev,llm]"    # citypods + test tools + LLM structured-output tests
# ffmpeg is required for audio materialization (apt-get install ffmpeg / brew install ffmpeg)

ruff check . && ruff format --check .
pytest -q                      # offline suite; live endpoint tests are opt-in: pytest -m live
```

Regenerate golden snapshots after an intentional output change with `SNAPSHOT_UPDATE=1 pytest`.

### Running the CLI locally

Prefer `python -m citypods.cli` over the `citypods` console script when running from the repo root.

**Quick check — no network, no writes (seconds):**
```bash
# Renders feeds/pages from locally cached state only.  No provider HTTP calls, no storage creds.
python -m citypods.cli build --phase render --city arlington-tx
cd docs && python -m http.server 8000   # browse the result
```
If `.citypods-state/` is empty the feeds render empty — that's expected when you haven't synced state
locally. Use this to check template/rendering changes.

**Dry-run with live provider fetch (1–3 min per city, no writes):**
```bash
python -m citypods.cli build --dry-run --city arlington-tx
```
This scrapes the live provider (Granicus/Swagit/CivicPlus), runs all pipeline stages, and writes
nothing — no docs, no storage uploads, no state files saved.

**Two things that commonly cause confusion:**

1. **`--city` takes a city slug, not a feed/body slug.** City slugs are the filenames under
   `config/cities/` (e.g. `arlington-tx`). Feed/body slugs are under `config/feeds/` (e.g.
   `arlington-tx-planning-and-zoning-commission`). Passing a feed slug silently matches no cities
   and processes the entire catalog instead. List valid city slugs with:
   ```bash
   ls config/cities/ | sed 's/\.yml//'
   ```

2. **`--dry-run` still makes live HTTP calls.** It skips all writes (storage, docs, state), but
   `provider.fetch_episodes()` always runs to discover the episode list. A full-catalog dry-run
   (`--dry-run` with no `--city`) hits all 85+ feeds across 8 concurrent workers and takes 10–15
   minutes. Omit `--city` only when you need to check the whole catalog.

**Reference: what each mode actually does:**

| Command | Live fetch? | Writes docs? | Writes state? | Needs B2 creds? |
|---|---|---|---|---|
| `build --phase render --city <city>` | No | Yes | Yes | No |
| `build --dry-run --city <city>` | **Yes** | No | No | No |
| `build --dry-run` | **Yes (all ~85 feeds)** | No | No | No |
| `build --city <city>` | Yes | Yes | Yes | Yes |

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

## GitHub issue and Project metadata

GitHub is the execution surface; committed docs remain the plan:

- [`review/11`](review/11-technical-design-roadmap.md) is canonical for initiative scope, phase,
  maturity, dependencies, and strategic order.
- The public [Citypods Delivery Project](https://github.com/users/BashfulBits/projects/1) shows live
  execution state and tactical ordering.
- Milestones represent concrete finish lines, currently **Phase H Complete** and
  **1.0 — Phase R Complete**.
- Issues are cut just-in-time for the active development series. Auto-managed health signals and
  individual city requests may remain open outside a development milestone.

### Project fields

| Field | Meaning | Values / rule |
|---|---|---|
| **Status** | Current execution state | `Backlog`, `Ready`, `In progress`, `Blocked`, `Review`, `Done` |
| **Priority** | Importance/urgency, independent of dependency order | `P0 urgent`, `P1 current`, `P2 next`, `P3 optional` |
| **Order** | "Do these in this order" | Numeric gaps (`10`, `20`, `30`); parallel items share a number |
| **Phase** | Owning series/surface | `H`, `R`, `Operations` |
| **Type** | Nature of the work | `Feature`, `Bug`, `Infrastructure`, `Research`, `Operations`, `Request` |

**Priority is not order.** A blocked prerequisite may have a lower `Order` but the same or lower
priority than an urgent operational signal. Strategic reordering changes `review/11` and the Project
together; tactical ordering within an already-approved initiative may update only `Order`.

Status conventions:

- `Ready`: unblocked and eligible to start now.
- `In progress`: code, investigation, or a required production observation is actively underway.
- `Blocked`: a named dependency or external condition prevents useful implementation.
- `Review`: implementation is complete and awaiting review/merge/acceptance.
- `Backlog`: valid work or signal, but not in the active execution queue.
- `Done`: completed; closed items may then be archived from the Project.

### Label taxonomy

Labels are repo-wide descriptive/search metadata. Use lowercase namespaced labels:

| Namespace | Examples | Purpose |
|---|---|---|
| `type:*` | `type:bug`, `type:feature`, `type:infrastructure`, `type:research`, `type:operations`, `type:request`, `type:docs` | What kind of work this is |
| `area:*` | `area:audio`, `area:ops`, `area:provider`, `area:frontend`, `area:search`, `area:timeline` | Code/product surface affected; multiple allowed |
| `signal:*` | `signal:feed-health`, `signal:endpoint-contract` | Automated source of an operational issue |
| `severity:*` | `severity:error`, `severity:warn` | Impact of an operational signal |
| `needs:*` | `needs:fixture`, `needs:live-verification`, `needs:human-verification` | Missing evidence or action |
| `resolution:*` | `resolution:duplicate`, `resolution:invalid`, `resolution:wontfix` | Why an issue was closed without implementation |
| `agent:*` | `agent:codex` | Agent provenance when useful |

Keep GitHub's community-discovery labels `good first issue` and `help wanted` in their familiar
unnamespaced form. Do not encode priority, status, phase, or sequence in labels; the Project fields own
those dimensions. Keep the issue's `type:*` label and Project `Type` field consistent.

### Milestone policy

- Assign active Phase-H implementation issues and their implementation PRs to **Phase H Complete**.
- Assign Phase-R implementation issues and their PRs to **1.0 — Phase R Complete** when that series is
  active.
- Do not assign auto-managed health signals or city requests to development milestones.
- Do not invent due dates. Add one only when there is a real external deadline.
- A milestone is a completion set, not a priority bucket; Project `Order` supplies sequencing.

### Agent maintenance command

The phrase **"clean up the GH issue list metadata"** means:

1. Run `gh auth status`; Project operations require the `project` scope
   (`gh auth refresh -s project` if absent).
2. Read `review/11` and the Project README before mutating GitHub.
3. Audit live state:

   ```bash
   gh issue list --repo BashfulBits/city-meeting-podcasts --state open --limit 200 \
     --json number,title,labels,milestone,url
   gh pr list --repo BashfulBits/city-meeting-podcasts --state open --limit 200 \
     --json number,title,labels,milestone,url
   gh project item-list 1 --owner BashfulBits --limit 200 --format json
   gh project field-list 1 --owner BashfulBits --format json
   gh label list --repo BashfulBits/city-meeting-podcasts --limit 200
   gh api 'repos/BashfulBits/city-meeting-podcasts/milestones?state=open&per_page=100'
   ```

4. Add missing open issues/PRs to Project 1; normalize labels; set Project fields and milestones from
   the rules above. Discover current Project/field/option IDs at runtime—never hard-code saved IDs in
   committed scripts or docs.
5. Preserve the sequence in `review/11`. If the live tracker reveals a real plan change, stop, explain
   the deviation and trade-offs, obtain maintainer confirmation, then update the committed design and
   GitHub metadata together.
6. Metadata cleanup alone does **not** authorize closing issues, rewriting scope, changing roadmap
   phase, or creating implementation tickets. Those require an explicit reconciliation/planning
   request and must preserve unique ideas in the committed reviews before closure.
7. Verify by re-running the issue, milestone, label, and Project listings. Report the final ordered
   queue, milestone membership, label changes, and any unresolved mismatch.

Saved Project views are currently configured in the web UI, not through `gh`/the public Projects API.
Their canonical recipes live in the Project README: **Do Next** (`Phase:H`, sort `Order` ascending),
**Board** (`Phase:H`, group by `Status`), and **Operations** (`Phase:Operations`, sort by `Priority`,
then `Order`).

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
| **Implemented** | PR merged | `review/11` entry → **Shipped** (+PR/issue link); **add a `CHANGELOG.md` entry**; update `ARCHITECTURE.md` if architecture changed; **freeze + stamp** the `review/NN` breakout ("Implemented in PR #N"); move the ROADMAP item to "Recently shipped"; close/narrow issues; capture any durable decision in the relevant committed doc |
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

## Adding or changing a dependency

Pins are the default (reproducible builds); an automated bot moves them forward. Full policy:
[`review/22`](review/22-dependency-and-reproducibility-policy.md). When you touch dependencies:

1. Edit **`pyproject.toml`** only — add the abstract `>=` floor. Never pin exact versions there.
2. **Recompile constraints** with `scripts/compile_constraints.sh` (runs `pip-compile` in the pinned
   linux/3.12 image; needs Docker) and commit the updated `constraints/*.txt`. In CI the `lock.yml`
   workflow does this; the `deps` job in `ci.yml` fails if they are stale.
3. **Classify it** — hygiene, or *output-affecting* (touches produced audio/transcript bytes:
   `faster-whisper`, `ctranslate2`, `stable-ts`, `Pillow`, ffmpeg, the base image, model revisions)?
   Output-affecting bumps follow the `AGENTS.md` pipeline-version-bump contract.
4. **Do not** re-declare deps in `scripts/compute/modal_app.py` / `beam_app.py` — the external workers
   install from the same constraints (a CI guard enforces this).

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
- [ ] Dependency changes follow [`review/22`](review/22-dependency-and-reproducibility-policy.md):
      `constraints/*.txt` recompiled; output-affecting bumps version-coupled; deps not re-declared in
      the external-worker image builders.
- [ ] Security checklist satisfied.
