# Varied unexpected-backlog preparation set, 2026-10-11

This is a deliberately selected preparation set of 60 distinct recording/source pairs across
five cities. It is not a random catalog sample, a finalized admission holdout or approved truth.
The maintainer requested a larger, varied set after the four-meeting Denton comparison.

Selection caps each city/problem category at four registered examples, then adds sparse or
untracked records from the historical unmatched inventory. Original identifiers and titles remain
unchanged. Input file hashes and retained recording fixtures provide local provenance.

`candidate-review.json` keeps past dispositions and assignments for human evidence review.
`model-input-drafts.json` strips those answers and historical classification questions. Neither
file may be fed directly to the live evaluator: official evidence excerpts, current candidate feed
inventory, explicit approved policy and schema-v2 freezing are still required. Historical register
assignments are not gold: they can be stale or inconsistent. In particular, the retained register
contains an Addison ceremony entry pointing to a Dallas feed; no expected answer is inferred from it.

Before evaluation:

1. Retrieve retained official evidence for each recording and verify its exact source binding.
2. Draft expected answers separately; distinguish supported assignment, justified abstention,
   approved exclusion and unresolved policy. Obtain approval for new directional decisions.
3. Add plausible wrong-feed challenges to each suitable recording; include ordinary-match controls.
4. Remove overlap with prior regression and Denton cases before admission-holdout freezing.
   Existing overlap may remain in an explicitly labelled challenge/regression slice.
5. Freeze validated input and separate approved gold; run the same evidence on each model.
6. Report by unique meeting and problem category as well as question count, separating accuracy,
   abstention, transport completion and latency. Do not pool easy controls into the hard-case score.

No provider requests, production changes or new human dispositions occur when building this set.
Run `build.py` only into an empty output directory; generated files refuse overwrite.

## Evidence preparation checkpoint

Twenty official agendas were retrieved through the source-URL safety gate and retained with byte
and extracted-text hashes in `official-documents/`. `evidence-packets.json` links all 60 recordings
to retained research and exact recording identifiers; it does not promote research prose to official
source evidence. Forty recordings still need content/official evidence or a separately adjudicated
insufficient-evidence scenario. No approved gold has been manufactured.

`expected-decisions-draft.json` proposes 20 agenda-backed feed sets, including two genuine
multi-feed cases. These are review drafts. `evidence-model-inputs.json` contains agenda excerpts and
candidate feed display names, excluding historical answers, research dispositions and UID inclusion
rules. Check excerpt relevance before freezing: the first 4,000 characters are a preparation bound,
not permission to omit contradictory evidence. Three recording identities overlap the prior Denton
set; GUID matching catches alternate retained UID representations. Broader source/family overlap
still needs audit before any admission-holdout label. Character counts are not token counts.

`retrieve_evidence.py` and `prepare_packets.py` preserve preparation scaffolding. Existing aggregate
outputs refuse overwrite. Do not rerun against existing outputs. No model calls occurred.
See `smaller-model-options.md` for an evaluation-only lighter-proposer proposal and the existing
admission-floor constraint.
