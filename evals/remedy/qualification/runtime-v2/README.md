# Concise runtime-prompt reevaluation

The Python generator sends source evidence once and uses the same prompt/schema factories as the
runtime. It preserves full taxonomy, all source evidence, legacy prompts and scoring expectations.
Native JEV receives one instruction/evidence object; Gemini receives one blinded A/B proposal pair.
The frozen run used actual new initial responses for judges, not prior example answers.

The 48-attempt approval and one delayed retry are preserved in `run-approval.json` and
`retry-approval.json`. The original Flash 429 remains in `observed-hold.json`; one explicitly approved
retry succeeded. These are completed historical authorities, not permission for a new run.

The run completed 38 judgments in 39 attempts, including that failure/retry, on all twelve cases.
No input holds remained. Both owned pauses were restored, there were no unknown requests, and the
continuation automation was removed. `answers.json` preserves every normalized observed response,
shared request profiles, usage and original source hashes. `conclusions.json` records compact
completion facts. Expanded observations and detailed score snapshots remain local and ignored.

Use [EXPERIMENTS.md](../../EXPERIMENTS.md) for clean-checkout comparison and new-model preparation.
The generator/executor require fresh exact source hashes and authority for another live run.
Do not bypass historical hash mismatches. This hard-case challenge is not independent qualification;
its outcomes and the later human-reviewed production authorization are distinct decisions.
