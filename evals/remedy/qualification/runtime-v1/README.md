# Runtime-contract challenge baseline

The retained twelve-case spec, approved outcome decisions and budgets record the original
runtime-prompt challenge. The cohort is a reused historical challenge, not an independent holdout.
Generation/scoring instructions are maintained in [EXPERIMENTS.md](../../EXPERIMENTS.md).

`answers.json` and `continuation-answers.json` retain all actual completed/held responses and
source hashes with shared physical request profiles. `evidence_archive.load_observations` expands
profiles for the scorer; summaries and comparison tables are derived, not duplicated here.
The original expanded JSON and detailed receipts remain local/ignored and unchanged.

The maintainer approved RQ01–RQ12, allowed current manual fallback for RQ04/RQ05, confirmed RQ07's
new-feed expectation and accepted manual control for RQ12. A subsequent case-specific acceptance
covers RQ06's observed union; it does not rewrite raw gold or transfer to other answers/cases.
See `outcome-acceptance.json` and `rq06-observed-acceptance.json`.

The 48-attempt authority and bounded untouched-case continuation are historical execution records.
The completed baseline had 36 judgments and two JEV input-size holds. New runs require new reviewed
source/prompt/profile/case hashes and explicit call authority; old approvals must not be refreshed
silently. No historical measurement admits a production route or authorizes automatic feed changes.
