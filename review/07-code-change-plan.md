# Phase-3 Code-Change Plan (per-topic local branches)

Each topic is its own local branch off `main`, committed but **not pushed**. In the morning,
review and push the ones you want: `git push -u origin <branch>` then `gh pr create`. Ordered by
value/safety. I'll work top-down and stop when tokens run out; anything not reached is fully
specified in docs 02–06.

| Order | Branch | Contents | Risk | Status |
|------|--------|----------|------|--------|
| 1 | `review/fix-and-harden` | B1 stale import + scripts import smoke test; B2 alias uniqueness; S2 ffmpeg protocol whitelist; S3 defusedxml; R1 retry/backoff | low | see git log |
| 2 | `review/endpoint-tests` | live contract suite (`-m live`), `check_endpoints.py`, `contracts.yml`, recorded-fixture parser tests | low | |
| 3 | `review/run-history` | Change 1 (StageStats cost fields) + Change 2 (`run_history.jsonl` + `run_summary.json`) | low | |
| 4 | `review/projection` | `citypods/projection.py` + `citypods report` CLI + `docs/admin/` calculator page + golden test | low | |
| 5 | `review/dynamic-budgets` | Change 3 (time-bounded budgets + catch-up) — depends on #3 | med | |

Notes:
- #1–#4 are independent and low-risk; #5 depends on #3's measured `sec_per_ep` and changes runtime
  behavior, so it's last and I'll keep it conservative (opt-in via config, default off).
- Each branch keeps `ruff` + `pytest` green and updates/regenerates snapshots as needed.
- I will NOT touch B3 (stale-record GC) or S1 (SSRF gate) as code without your sign-off — they're
  design-coupled (B3 to the record-keying scheme, S1 to the Phase-5 onboarding flow). Both are fully
  specified in doc 04 for a future PR.

## Commit message discipline
- No mention of the specific custom domain in commit messages (forkability), per your standing rule.
- Co-author trailer as usual.

## Morning checklist
```
git branch --list 'review/*'           # see what got done
git log --oneline main..review/fix-and-harden   # inspect a branch
# push the ones you want:
for b in review/fix-and-harden review/endpoint-tests review/run-history review/projection; do
  git push -u origin "$b"; done
# then open PRs (titles suggested in each branch's final commit body)
```
