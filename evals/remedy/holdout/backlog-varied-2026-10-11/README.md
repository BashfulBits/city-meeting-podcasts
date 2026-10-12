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

## Entire packet v2

`packet-v2/REVIEW.md` supersedes the earlier 20-agenda preparation count. Exact retained records
for all 60 were found and bound to their source/UID/GUID; all have source media. 38 retained
agendas, additional Dallas minutes, native chapter headings, provider pages and prior manually
verified opening evidence support a proposed review packet. Five Fort Worth title-only program
records remain owner-unknown and are proposed proof-sufficiency tests, not invented exclusions.
The expected answers were approved for this evaluation before the first model call and are frozen
under `approved-v2/`. Nothing reopens P0 dispositions. The approval covers this comparison only,
not new feed changes or production model activation.

121 linked document responses and 65 supplemental responses were retained. The two unavailable
supplements are the current Addison bond-program page and FY2007 budget (403). Successful HTTP
responses include placeholders; retrieval and content adjudication are distinct. Exact raw bytes
are compressed in `packet-v2/raw-evidence.zip`, verified by per-entry hashes. To reconstruct local
working files on a new checkout, extract that inert archive into packet-v2; do not execute contents.

The proposed 113 membership/proof questions represent 60 recording/source pairs, not 113 distinct
meetings. Publication policy, actual body identity, prior controls and missing-proof behavior must
be scored separately. Thirty single-feed assignments include a policy-based citizen-input
compilation: blind-owner scoring cannot silently demand knowledge of hidden publication preferences.
Multi-feed correctness is tested with existing per-feed claim checks; no evaluator schema was widened.

Known decisions/rules would ordinarily suppress these calls in production. This is a historical
challenge comparison, not a count of new live unresolved meetings or an independent admission
holdout. Distinct event statistics group Fort Worth125/127, and prior Denton overlap remains marked.
No model calls occurred while collecting/preparing packet v2.

## Evaluation status

The maintainer approved an eight-case pilot followed by a resumable comparison of nine candidates.
The first shared eight-call attempt was not balanced: six Flash-Lite answers completed, one Gemini
3.7 call failed on the route's attempt limit, and one Gemini 3.8 call received a provider 503; the
other candidates were deferred before a provider call because their shared accounts were busy. A
later candidate-specific Kimi K3 pilot completed all eight examples. Its scored result is retained
in `../../results/varied-2026-10-11/pilot-scored-2026-10-12-v1.json`; it got seven answers right and
missed one approved Dallas Transportation and Infrastructure membership. The original observations
remain unchanged. The runner now requires one candidate per pilot invocation so the next pilots use
the same eight examples fairly. None of these regression-set scores qualifies an admission: all
owner-truth fields are null, and production remains unchanged.
