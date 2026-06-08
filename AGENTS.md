# Agent & contributor guide

Canonical orientation for anyone — human or AI agent (Claude, Codex, …) — picking up work on this repo.
Read this first, then the doc it points you to for your task.

## Document map (what to read for what)

| You want to… | Read |
|---|---|
| Understand the system as it exists now | [ARCHITECTURE.md](ARCHITECTURE.md) |
| Know what's planned next & priorities | [ROADMAP.md](ROADMAP.md) |
| Understand the long-horizon direction | [VISION.md](VISION.md) |
| Find the design for a feature / pick the next thing to build | **[`review/11-technical-design-roadmap.md`](review/11-technical-design-roadmap.md)** (the LIVING canonical design index) and its breakout docs `review/12+` |
| See what already shipped | [CHANGELOG.md](CHANGELOG.md) |
| Follow the contribution process & doc-update rules | [CONTRIBUTING.md](CONTRIBUTING.md) |
| Understand a past decision's rationale | [`review/00–10`](review/) (committed, point-in-time) |
| Report or reason about security | [SECURITY.md](SECURITY.md) |

> **Shared context lives in the repo.** The committed docs above are the single source of truth for
> **all** agents. Claude Code additionally keeps a private `.claude/memory/` cache of the same facts —
> it is machine-local, **not in the repo, and not visible to other agents or clones**, so never rely on
> it for shared context. Any durable fact that matters must live in a committed doc.

## How to pick up the next piece of work

1. Open [`review/11`](review/11-technical-design-roadmap.md). Find an initiative in the **current
   phase** (Phase H first) whose maturity is **L3 (development-ready)**.
2. Implement it from its breakout doc (`review/12+`), which contains the file/function plan, test plan,
   sequencing, and acceptance criteria.
3. **Update the docs per the lifecycle contract** (below + in CONTRIBUTING) — this is mandatory, because
   stale design docs are how this project's earlier reviews went out of date.

## Feature lifecycle & doc-update contract (the "change X → update Y" rule)

When you move a feature along the pipeline, update the listed docs in the **same** change:

| Stage | Update |
|---|---|
| Idea | `VISION.md` (long-horizon) or `review/11` Deferred-backlog |
| Committed to near-term | `ROADMAP.md` + `review/11` catalog (L1 sketch inline) |
| Designed (approach chosen) | break out to `review/NN`; `review/11` entry → L2 + link |
| Development-ready | mature `review/NN` to L3; cut GitHub Issue(s); `review/11` → L3 |
| **Implemented (PR merged)** | `review/11` entry → **Shipped** (+ PR link); add **CHANGELOG.md** entry; update **ARCHITECTURE.md** if the architecture changed; **freeze + stamp** the `review/NN` breakout ("Implemented in PR #N"); move the ROADMAP item to "Recently shipped"; close/narrow issues; capture any durable decision in the relevant committed doc (Claude Code also updates its local `.claude/memory`) |

`review/11` is a **living** document; `review/00–10` and frozen breakouts are point-in-time records.

## Conventions you must respect

- **Append-only records** (`records.merge_persisted`) — never drop archived meetings (#52).
- **Split hashes** — `audio_spec_hash` (bytes) vs `feed_content_hash` (RSS) invalidate independently;
  audio keys are **content-addressed**; episode **UIDs are stable** across provider migrations.
- **Stage order matters** — audio is content-addressed by its spec (including chapters/timeline), so any
  stage that affects audio bytes must run **before** `AudioStage`; feed-only stages run after.
- **Wall-clock stop budget** — gate only expensive *restartable* per-item work on `ctx.stop()`; never
  gate cheap idempotent bookkeeping; **deferred ≠ failed** (see the `stages.py` module docstring).
- **Timeline basis** — artifacts declare `served` or `source:<id>`; render emits served time.
- **LLM output is untrusted** — never overwrite official links/titles/dates/transcript text (SECURITY.md).
- **SSRF gate** — any fetch of a (potentially) user-influenced URL goes through `validate_source_url`.
- **New platform = adapter; new per-episode feature = stage.** Most features need no core change.
- **Branch names:** `<type>/<slug>` — `feat/`, `fix/`, `docs/`, `refactor/`, `chore/` (issue number
  in the slug when one is tracked, e.g. `feat/110-asr-transcripts`). An agent may prefix its own name
  instead of a type to mark provenance (e.g. `codex/reduce-enrich-concurrency`). Full convention +
  examples in [CONTRIBUTING.md](CONTRIBUTING.md).

## Local workflow

```bash
pip install -e ".[dev]"                 # needs ffmpeg for audio
ruff check . && ruff format --check .    # lint the WHOLE repo, not just citypods/
pytest -q                                # offline; live endpoint tests are opt-in: pytest -m live
python -m citypods.cli build --dry-run   # the console script can be flaky from script dirs; prefer -m
```

After an intentional change to feed output, regenerate golden snapshots: `SNAPSHOT_UPDATE=1 pytest`.
