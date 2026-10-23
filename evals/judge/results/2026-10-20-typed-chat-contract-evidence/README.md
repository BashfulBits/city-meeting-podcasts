# Typed judge contract evaluation, October 20–22, 2026

All 168 planned packet slots reached their first HTTP 200; ten failed the strict contract.
A failed HTTP-200 response was never retried to obtain a passing answer. See summary.json.
SambaNova slots were rerouted to NVIDIA Gemma and secondary BeatAPI slots used the primary key,
with maintainer authorization. These are repeated observations of existing routes/accounts,
not qualification of SambaNova or the secondary account.

Keep the agreed JEV + Gemma / JEV + Nemotron panels and independent GLM/NVIDIA DeepSeek
adjudication. The chat-only comparison in PAIR_RESULTS.md does not replace those panels.
Sibling quote failures are non-gating quality metrics; adjudicator evidence remains required.
The existing gold labels, including their provenance and uncertainty, are unchanged.

observations.json retains compact labeled decisions for offline re-scoring. An invalid packet
abstains even if legacy parsing recovered its answers. It does not contain raw replies and cannot
be used to re-test a parser change; use the external raw archive for that purpose.
provenance.json records frozen-input/source hashes and the raw archive checksum.
The archive is retained outside Git as judge-evidence-full-827cec48.tar.gz and delivered in the
PR/task handoff. It contains original requests/replies, all transport attempts and review logs.
It is a local artifact, not a publicly hosted download; no upload has been performed.

## Re-score saved decisions

```bash
PYTHONPATH="$PWD" python scripts/score_judge_pairs.py \
  --observations evals/judge/results/2026-10-20-typed-chat-contract-evidence/observations.json \
  --out /tmp/judge-pairs.json
```

## Evaluate new models and re-parse saved responses

```bash
PYTHONPATH="$PWD" python scripts/eval_judge_contract.py --route ROUTE_ID --out /tmp/new-judge
# Inspect the plan. Only when live calls are authorized, create fresh live output:
PYTHONPATH="$PWD" python scripts/eval_judge_contract.py --live --route ROUTE_ID \
  --out /tmp/live-judge
PYTHONPATH="$PWD" python scripts/score_judge_pairs.py --results /tmp/live-judge \
  --out /tmp/new-judge-pairs.json
```

The scorer uses current schema/parser inspection for harness outputs and never calls a model.
Each fresh harness directory preserves one response per packet; do not overwrite or resample failed
HTTP-200 replies. --gold permits re-scoring with a separately reviewed label file without changing
frozen inputs. Reports include label/input hashes and exclude uncertain labels. This selection
set is not a population estimate or disjoint holdout. Moment gate/grade/choose lack gold labels.

## Re-test parser changes against the external archive

```bash
PYTHONPATH="$PWD" python scripts/score_judge_pairs.py \
  --archive /path/to/judge-evidence-full-827cec48.tar.gz --out /tmp/reparsed-pairs.json
```

This reads original raw first-200 responses from the archive without extracting archive paths.
It reproduced all 52 historical chat-model route-pair counts using the current parser.
The archive must be retained separately; compact observations alone cannot re-test parsing.

## Review and verification

CodeRabbit reviewed the cleanup and staged scorer. Three findings were fixed: include the
explicit live-output command in the example, mark the checkpoint complete, and preserve actual
provider/account substitutions when replaying the raw archive. The latter has a regression test.
Raw review logs are external task artifacts rather than committed repository files.
All 43 removed tracked evidence files were verified byte-for-byte against the external archive.
