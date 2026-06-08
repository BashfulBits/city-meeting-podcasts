<!-- Merge with a merge commit (--merge), NOT squash. See CONTRIBUTING.md. -->

## Summary

<!-- What does this change do and why? Link the review/11 catalog entry, ROADMAP item, or issue. -->

## Checklist
- [ ] Tests added/updated; `ruff check . && ruff format --check .` and `pytest` pass.
- [ ] Feed snapshots regenerated **intentionally** if output changed (`SNAPSHOT_UPDATE=1 pytest`).
- [ ] Docs updated per the **lifecycle contract** ([CONTRIBUTING.md](../CONTRIBUTING.md)): flipped the
      `review/11` catalog status, added a `CHANGELOG.md` entry, updated `ARCHITECTURE.md` if the
      architecture changed, and stamped the breakout doc "Implemented in PR #N" if applicable.
- [ ] No artifact-identity change (audio spec hash / UID derivation) without a `MIGRATION.md` note.
- [ ] Security: no provider network calls in normal CI; no secrets committed; any user-influenced URL
      goes through `validate_source_url`; no LLM/generated output overwrites official
      links/titles/dates/transcript text.
