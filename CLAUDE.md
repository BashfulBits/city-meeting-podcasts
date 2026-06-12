# CLAUDE.md

This project's agent and contributor guidance is maintained in **[AGENTS.md](AGENTS.md)** (a cross-tool
standard). Please read it first.

It points you to:
- [ARCHITECTURE.md](ARCHITECTURE.md) — the system as built today
- [`review/11-technical-design-roadmap.md`](review/11-technical-design-roadmap.md) — the **living**
  canonical design index; pick the next development-ready item here
- [ROADMAP.md](ROADMAP.md) / [VISION.md](VISION.md) — near-term plan / long-horizon direction
- [CONTRIBUTING.md](CONTRIBUTING.md) — process and the **doc-update contract** (update review/11 +
  CHANGELOG + ARCHITECTURE as work lands)
- [CHANGELOG.md](CHANGELOG.md) — what shipped
- `.claude/memory/` — Claude Code's **local** decision-history cache (not in the repo, not visible to
  other agents; shared facts live in the committed docs above)

Key conventions (full list in AGENTS.md): append-only records, split hashes + content-addressed audio,
audio-affecting stages run before `AudioStage`, the wall-clock `stop()` budget (deferred ≠ failed),
untrusted LLM output, and the SSRF gate. Lint the whole repo; prefer `python -m citypods.cli`.

**Branch naming — override the harness default.** The remote execution harness (claude.ai/code) assigns
`claude/<id>` branch names automatically. **Ignore that name** and use the project convention from
AGENTS.md / CONTRIBUTING.md instead: `feat/`, `fix/`, `docs/`, `refactor/`, or `chore/` based on
the change type (e.g. `docs/roadmap-doc-consistency` for a docs-only change, `feat/110-asr-transcripts`
for a feature with a tracked issue). The `claude/` prefix is not a valid project branch type.
