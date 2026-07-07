<!-- Merge with a merge commit (--merge), NOT squash. See CONTRIBUTING.md. -->
<!-- Open as DRAFT and iterate here; flip to "Ready for review" only when you want CodeRabbit's
     review (it skips drafts). Saves throttled review-events on self-directed churn. See AGENTS.md
     § Working with CodeRabbit on a PR. -->

## Summary

<!-- What does this change do and why? Link the review/11 catalog entry, ROADMAP item, or issue. -->

## Checklist
- [ ] Tests added/updated; `ruff check . && ruff format --check .` and `pytest` pass.
- [ ] Feed snapshots regenerated **intentionally** if output changed (`SNAPSHOT_UPDATE=1 pytest`).
- [ ] Docs updated per the **lifecycle contract** ([CONTRIBUTING.md](../CONTRIBUTING.md)): flipped the
      `review/11` catalog status, added a `CHANGELOG.md` entry, updated `ARCHITECTURE.md` if the
      architecture changed, and stamped the breakout doc "Implemented in PR #N" if applicable.
- [ ] No artifact-identity change (audio spec hash / UID derivation) without a `MIGRATION.md` note.
- [ ] Dependency changes follow [`review/22`](../review/22-dependency-and-reproducibility-policy.md):
      `constraints/*.txt` recompiled; deps not re-declared in the external-worker image builders.
- [ ] CodeRabbit findings resolved (fixed, fixed-and-expanded, or pushed back with a stated reason)
      and CI green — see [AGENTS.md § Working with CodeRabbit on a PR](../AGENTS.md#working-with-coderabbit-on-a-pr).

<!-- OUTPUT-AFFECTING dependency bump (faster-whisper/ctranslate2/stable-ts/Pillow, ffmpeg, base
     image, or an HF model revision)? Then also: -->
- [ ] `dep-bump-smoke` per-source table reviewed (granicus/swagit/civicplus/civicclerk) — states
      whether produced bytes/WER changed. If output **changed**, this PR either bumps the relevant
      pipeline version (`ASR_PIPELINE_VERSION` / `SilencePlanner.version`) and states the backfill
      story, or holds. Pure re-pinning of the current version does **not** bump any version.
- [ ] Security: no provider network calls in normal CI; no secrets committed; any user-influenced URL
      goes through `validate_source_url`; no LLM/generated output overwrites official
      links/titles/dates/transcript text.
