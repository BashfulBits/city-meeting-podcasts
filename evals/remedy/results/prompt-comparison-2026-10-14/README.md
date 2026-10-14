# General prompt comparison

Three general prompts x60 unchanged recordings x Sol,GLM Flash,DeepSeek V4.1 Flash
=540logical jobs. Original is the earlier question; evidence_first explicitly separates
institutional ownership and broader subscription scope; self_check checks inclusions,
omissions and unsupported ownership before returning the same JSON. No committee-specific
answer mapping or examples, previous model answer, selector or gold enters the prompt.

Initial+3retries maximum per job; no SDK retries,30-minute failure cooldown and fixed
seven-day horizon. A valid wrong answer stops retries. Unmatched intents hold the job.
Normal shared CAS accounting and Worker coordination remain default; explicitly approved
bypass retains local accounting and unknown production usage. Same routes/controls and
patient900s/950s/16384 limits as prior comparison. No model activation or production changes.

The managed worktree is durable. Raw calls/intents are immutable; the controller commits
and pushes only those files before submitting the next request. This data-only checkpoint
skips optional pre-push hooks after full source verification; unrelated staged files block it.
Runtime status/locks are never committed. A push failure stops new calls without erasing results.

Run PYTHONPATH=. python evals/remedy/results/prompt-comparison-2026-10-14/run.py for an
offline plan. --execute starts only once through citypods-env; do not duplicate a controller.
--coordination-bypass binds a different experiment hash and cannot change on resume.

score.py requires explicit --gold and NEW --out, and the same coordination-mode flag.
Pairs use the same completed-case intersection per prompt and show category coverage.
Shared wrong agreement is distinct from standard feed FP(extra memberships) and
FN(missing expected memberships). Unknown-owner cases are scored as proof guardrails,
without inventing institutional negatives. Approved gold and original data remain unchanged.
Optimization on this reused dataset does not independently qualify production routes.

Remote observation checkpoints use chore/2413-prompt-observations, without a PR,
to avoid launching PR CI for every response. Source preparation is retained in
draftPR2413; completed observation snapshots will be carried into it together.
