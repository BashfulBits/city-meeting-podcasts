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
> **all** agents — any durable fact that matters must live in a committed doc.

## How to pick up the next piece of work

1. Open [`review/11`](review/11-technical-design-roadmap.md). Find an initiative in the **current
   phase** (Phase H first) whose maturity is **L3 (development-ready)**.
2. Implement it from its breakout doc (`review/12+`), which contains the file/function plan, test plan,
   sequencing, and acceptance criteria.
3. **Update the docs per the lifecycle contract** (below + in CONTRIBUTING) — this is mandatory, because
   stale design docs are how this project's earlier reviews went out of date.

## GitHub execution metadata

[`review/11`](review/11-technical-design-roadmap.md) owns scope and strategic sequence. The public
[Citypods Delivery Project](https://github.com/users/BashfulBits/projects/1) is the live execution view;
milestones are finish lines, and labels describe the work. GitHub metadata must mirror the committed
docs, never silently replace them.

When the maintainer asks an agent to **"clean up the GH issue list metadata"**, follow the normative
procedure in [CONTRIBUTING.md](CONTRIBUTING.md#github-issue-and-project-metadata):

1. Read `review/11`, then audit all open issues, PRs, Project items, milestones, and labels.
2. Reconcile Project `Status`, `Priority`, `Order`, `Phase`, and `Type` with `review/11`.
3. Keep descriptive labels namespaced (`type:*`, `area:*`, `signal:*`, `severity:*`, `needs:*`,
   `resolution:*`, `agent:*`); preserve GitHub's standard `good first issue` / `help wanted` names.
4. Put active-series development issues in the matching milestone. Operational signals and city
   requests stay outside development milestones.
5. Do **not** close issues or change strategic order merely as metadata cleanup. If reality and
   `review/11` disagree, surface the mismatch and use the roadmap-deviation gate below.
6. Verify the final live issue list and Project table, then report every mutation.

## Feature lifecycle & doc-update contract (the "change X → update Y" rule)

When you move a feature along the pipeline, update the listed docs in the **same** change:

| Stage | Update |
|---|---|
| Idea | `VISION.md` (long-horizon) or `review/11` Deferred-backlog |
| Committed to near-term | `ROADMAP.md` + `review/11` catalog (L1 sketch inline) |
| Designed (approach chosen) | break out to `review/NN`; `review/11` entry → L2 + link |
| Development-ready | mature `review/NN` to L3; cut GitHub Issue(s); `review/11` → L3 |
| **Implemented (PR merged)** | `review/11` entry → **Shipped** (+ PR link); add **CHANGELOG.md** entry; update **ARCHITECTURE.md** if the architecture changed; **freeze + stamp** the `review/NN` breakout ("Implemented in PR #N"); move the ROADMAP item to "Recently shipped"; close/narrow issues; capture any durable decision in the relevant committed doc |

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
- **Pipeline-version bumps state their backfill story.** Bumping a stage's pipeline version (e.g.
  `ASR_PIPELINE_VERSION`, `SilencePlanner.version`) changes how re-processing is triggered. The PR
  **must** state — in its description and CHANGELOG entry, matching the code — whether already-stored
  artifacts are auto-invalidated (gradually re-done) or left as-is. A silent bump that *does* invalidate
  can queue weeks of catalog rework; one that *doesn't* can leave a stale-format catalog while the docs
  claim otherwise — both have bitten this project.
- **Dependencies are pinned; adding one has a contract.** Declare `>=` floors in `pyproject.toml`,
  recompile `constraints/*.txt` (`scripts/compile_constraints.sh`), never re-declare deps in the
  external-worker image builders, and treat *output-affecting* bumps (`faster-whisper`, `ctranslate2`,
  `stable-ts`, `Pillow`, ffmpeg, base image, model revisions) as version-coupled per the rule above.
  Full policy + the light-touch update flow: [`review/22`](review/22-dependency-and-reproducibility-policy.md).
- **Branch names:** `<type>/<slug>` — `feat/`, `fix/`, `docs/`, `refactor/`, `chore/` (issue number
  in the slug when one is tracked, e.g. `feat/110-asr-transcripts`). Full convention + examples in
  [CONTRIBUTING.md](CONTRIBUTING.md).

## When a change deviates from the plan

The committed plan is [ROADMAP.md](ROADMAP.md) + [`review/11`](review/11-technical-design-roadmap.md)
(sequencing + locked decisions) and the breakouts. When a request — or a change you're about to propose —
**goes against** that plan (reorders the queue, skips a stated gate, promotes a Deferred item, tunes a
production knob past its documented ceiling, or reverses a recent decision), **do not just proceed**:

1. **Surface it** — say plainly that it deviates, and from what.
2. **Give a concise pro/con** of doing it now vs. as written — costs, risks, and what it unblocks.
3. **Get explicit confirmation, then record the rationale** in the relevant committed doc, so the
   deviation is auditable rather than silent.

This is a confirmation gate, not a veto — the maintainer decides. Its purpose is to make a deviation a
*chosen* trade-off with the trade-offs on the table, not an accident. (Cases that warrant it: a
runner-concurrency jump past a documented "hold at N" ceiling; a Deferred→Phase-R scope promotion bundled
with a pipeline-version bump.)

## Local workflow

```bash
pip install -e ".[dev]"                 # needs ffmpeg for audio
ruff check . && ruff format --check .    # lint the WHOLE repo, not just citypods/
pytest -q                                # offline; live endpoint tests are opt-in: pytest -m live
```

After an intentional change to feed output, regenerate golden snapshots: `SNAPSHOT_UPDATE=1 pytest`.

### Running the CLI locally

Use `python -m citypods.cli` rather than the `citypods` console script when running from inside the
repo directory — the console script can be flaky from script subdirectories.

**Quick offline check (seconds, no writes, no credentials):**
```bash
# Render feeds/pages from locally cached state — no live provider calls, no storage needed.
python -m citypods.cli build --phase render --city arlington-tx
```
Use this to verify template/feed-rendering changes. It reads `.citypods-state/` (or `docs/`) if
present; if that directory is empty the feeds will render empty (expected — no state synced locally).

**Full dry-run with live provider fetch (minutes, no writes):**
```bash
# Fetches live episode lists from the provider, runs all stages, writes nothing.
# Expect 1–3 min per city; the whole catalog (~85 feeds) can take 10–15 min.
python -m citypods.cli build --dry-run --city arlington-tx
```
⚠️ **`--city` takes a city slug, not a feed/body slug.** City slugs match filenames under
`config/cities/` (e.g. `arlington-tx`), not `config/feeds/` (e.g.
`arlington-tx-planning-and-zoning-commission`). Passing a feed slug silently matches nothing and
runs the full catalog.

```bash
# List valid city slugs:
ls config/cities/ | sed 's/\.yml//'
```

**What each flag actually does (important gotchas):**

| Command | Provider fetch? | Writes state/docs? | Needs B2 creds? | Duration |
|---|---|---|---|---|
| `build --phase render --city <city>` | No | Yes — renders `docs/` | No | Seconds |
| `build --dry-run --city <city>` | **Yes** — live HTTP scrape | No | No | 1–3 min |
| `build --dry-run` (no `--city`) | **Yes** — all 85+ feeds | No | No | 10–15 min |
| `build --city <city>` (no flags) | Yes | Yes — full write | Yes | Minutes–hours |

`--dry-run` skips: all object-storage uploads, `docs/` writes, state saves, and audio encoding.
It **does** call `provider.fetch_episodes()` for each city — that's a live HTTP scrape and is why
a full catalog dry-run takes 10–15 minutes across `max_workers=8` concurrent threads.

### Running a full-codebase CodeRabbit review

CodeRabbit (bot and CLI) only reviews diffs — there's no native "scan the whole repo" mode. To
review the *entire current state* of a directory instead of one PR's diff, diff `HEAD` against the
repo's root commit (real commit object, near-empty), which makes the diff cover ~every file:

```bash
root=$(git rev-list --max-parents=0 HEAD)          # 06f2244 here — 1 file, safe substitute for the empty tree
coderabbit review --agent --type committed --base-commit "$root" --dir citypods   # one source dir at a time
```

- Don't use git's empty-tree SHA (`4b825d...`) — `--base-commit` runs a three-dot symmetric diff via
  `merge-base`, which needs a real commit, not a bare tree object.
- Use `--agent` (structured JSON), not `--plain` — the CLI tells you to when it detects an agent shell.
- `coderabbit auth login` needs a real interactive terminal (browser OAuth) — run it yourself, not
  from an agent-driven shell. Install via `brew install coderabbit` (official cask).
- Scope to source dirs only (`citypods/`, `scripts/`, `tests/`, `workers/`, `templates/`, `.github/`);
  skip `config/` (per-city data) and doc dirs.
- Save raw NDJSON output (don't trust a UI preview — it truncates display only) and verify every
  finding against current code before fixing, same staleness caveat as reviewing old merged PRs.
- **Free-tier rate limit:** the CLI free tier allows ~1 review before it blocks ("wait N minutes" or
  enable billing), so 6 directory-scoped reviews back-to-back stall fast (a promotional credit can land
  mid-run and unblock some unpredictably). Retry the blocked dirs with a backgrounded
  `sleep 3600 && coderabbit review …` rather than blocking synchronously — the root-commit and
  working-tree diff both resolve at execution time, so a `git pull` in between is picked up.
- **Reconcile against overlapping audits, don't silo them.** When an audit doc already exists, cross-link
  every overlapping finding both directions by stable ID and state when one verdict supersedes another
  (e.g. code moved between the two runs); don't emit a fresh siloed list. Before finalizing a `review/NN`
  number or a large docs artifact, `git fetch && ls review/` — **other agent sessions work this repo in
  parallel** and real numbering collisions have happened (a manual audit landed as `review/21` while a
  CodeRabbit sweep was mid-run in another session; both independently caught several of the same bugs).

Full procedure and per-finding validity verdicts live in the numbered audit docs — read the **highest**
one first (`ls review/`), since new sweeps supersede: [`review/19`](review/19-coderabbit-findings-audit.md)
(2026-06-25, 129 findings), [`review/21`](review/21-manual-code-audit-2026-07.md) (manual, 20),
[`review/23`](review/23-coderabbit-findings-audit-followup.md) (2026-07-04, 115, cross-linked to both).
