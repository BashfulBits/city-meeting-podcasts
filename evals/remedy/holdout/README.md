The independently approved ten-case [Grounded Denton holdout, 2026-10-10](
p1-denton-2026-10-10/README.md) is frozen under `p1-denton-2026-10-10/`.

Callers must explicitly pass `evals/remedy/holdout/p1-denton-2026-10-10/manifest.json`
and, when scoring, `evals/remedy/holdout/p1-denton-2026-10-10/gold.json`. The normal
evaluation flow does not discover nested holdout datasets automatically.

Split by source/family/recording, rather than random labels; `validate_holdout` rejects overlap
with the tuned regression seeds. Only verified real cases with approved policy can support
admission. A frozen dataset alone does not qualify a model; its evaluation must pass the gates.
