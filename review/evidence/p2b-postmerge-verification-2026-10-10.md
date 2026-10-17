# P2b post-merge verification — October 10, 2026 UTC

The report implementation shipped through human merge #2215 at main `80defe32`.
Verification used subsequent main `5b2009c0`, which adds #2226's feed lifecycle annotations.
The CLI was exercised twice against the same locally retained source snapshot. Provider fetch
was replaced with an empty response for this offline check; existing configured seeds were
still merged by the unchanged audit flow. This is not a live archive census. No credential,
storage synchronization, new model call, audio download, notification or GitHub reconciliation
was allowed. State-save, canonical-pull and GitHub-reconciliation calls were guarded to fail.

## Results

| Measurement | Result |
|---|---:|
| Configured feeds | 224 |
| Source namespaces with retained records | 42 |
| Retained source/UID rows | 26,555 |
| Input observations, including 11 existing seed observations | 26,566 |
| Rows with a configured selector match | 26,153 |
| Rows without a configured selector match | 402 |
| Rows with policy-resolver ownership | 345 |
| Conflicting identity rows in this retained-only run | 0 |
| Entries without selection after publication exclusions | 1,189 |
| Feed placements after publication exclusions | 28,594 |

The material hash is identical on both runs. All 42 raw source-file hashes remain unchanged.
Every source reports completeness unknown and identifies the unsynchronized local snapshot.
Policy-resolver ownership is separate from selector matches; 345 is not a claim of a fresh
independent official-document review. Projection counts precede recording/audio availability;
they do not count playable RSS episodes. Raw JSON reports are local verification artifacts;
the compact [verification record](p2b-postmerge-verification-2026-10-10.json) preserves the
hashes and all selector-versus-publication differences.

## Correcting the older P0 counting helper

The old P0 replay passed the entire site configuration to `load_city_configs` instead of its
`defaults` mapping. That omitted global body exclusions when calculating assignments. Replaying
both loader inputs reproduces the old 29,384 placements / 411 unassigned entries exactly.
Using the same defaults mapping as the real CLI yields 28,594 placements / 1,189 unassigned.

There are 790 changed source/UID placement sets. Of these, 778 bid/purchasing listings lose all
placements; 12 public-education listings retain their public-information destination while losing
an excluded aggregate placement. Matching exclusion terms overlap: bid 778, purchasing 690,
public education 12. This is a verification-helper correction, not a production configuration
change or 778 newly discovered meeting bodies. The exact per-UID comparison is retained in
[defaults reconciliation](p2b-defaults-reconciliation-2026-10-10.json).

The report's 402 unmatched selectors are a different quantity from publication exclusions.
Archive-only declarations and body exclusions explain that difference. No existing exclusion
was removed, no raw row disappeared, and P0's approved 680-pattern dispositions remain intact.
The dated P0 report remains a historical artifact; these corrected projection counts supersede
its claim to describe actual CLI defaults.

## Lifecycle and next slice

P2b is shipped and this retained-snapshot verification is complete. The final local-snapshot
warning fix passed 5,566 offline tests and all PR CI; its review thread is resolved. A fresh
substantive review of that final small fix was not completed before human merge; retain that
limited gap. Documentation/register checks pass without another code change.

The six historical cases are now accepted terminal dispositions: 169 resolved, zero open.
Their missing independent proof is preserved in the [closeout](historical-six-case-closeout-2026-10-10.md).

Next implement the scoped P2 decision-ledger foundation: remember approved assignments,
excluded formats and watch decisions, with source-local evidence and approval references.
Then integrate fresh evidence/dispositions into regular audits in report-only mode before alert
activation. The current reporting tool does not yet suppress recurring settled decisions.
P3 independent model admission/review, P4 bounded publication/recovery, and P5 onboarding/alias
qualification retain their existing gates. Search improvements remain separate.
