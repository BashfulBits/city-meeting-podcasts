# Broader-context disagreement diagnostic

The maintainer selected Sol + GLM Flash using the original prompt. This follow-up
reuses their exact eight action/feed-set disagreements, with both proposals visible
to the adjudicator. It is escalation research, not a fresh blind qualification set.

`prepare.py` does not read gold. It includes all suggested feeds plus three peers
ranked by Jaccard tokens from the recording title and proposed body names. For each,
it retrieves up to three distinct historical title patterns, excluding every UID/title
in the 60-recording study. This is Python retrieval, not LLM-selected relevance.
Historical inclusions are fallible context and cannot prove institutional ownership.
Original official URLs are fetched independently, with hashes and retrieval failures;
up to 16,000 text characters per URL are split into existing 4,000-character evidence
references. No new body-to-feed answer rule is inserted. Feed catalog/policy unchanged.

DeepSeek V4.1 Flash receives the packet and returns its own full assignment. JEV1.13
receives the same packet through existing `JevBackend`/Worker systemone transport,
with choice A, B, neither complete proposal supported, or insufficient evidence,
and separate support probabilities for A/B. Neither is essential: both judges may
omit a feed. Multiple-choice accuracy is distinct from full-assignment accuracy.
Judge names are masked in live proposals; offline provenance retains their identities.

Run scripts live under `evals/remedy/results/disagreement-2026-10-14`. Sixteen logical
submissions, no application retries, existing shared quota/production gates, no paid
fallback. JEV's queued provider retries use existing Worker bounds. DeepSeek retains
900s requests/950s cases and 16,384 output tokens. Lost receipts/intents hold; they
are never treated as permission to submit again. Deadline is 24 hours. Observations
are backed up before the next request. Original gold stays separate and unchanged.
No production model activation, feed edits, quota changes or Worker modifications.

JEV dispatch uses the existing `judge:anchor` lane and isolated evaluation recipes;
`audit-remedy` is not a registered queued purpose. Only evaluation response/deferred
objects are written; no episode judgments or production decisions are applied.

## Frozen packets

- dallas-tx: 2024 Capital Bond Streets & Transportation Subcommittee Meeting – Aug 22, 2023 (270975).
  7 evidence references; 5 feeds sampled.
  A: assign_existing; dallas-tx-2024-bond-streets-and-transportation-subcommittee, dallas-tx-bond-program-meetings.
  B: assign_existing; dallas-tx-2024-bond-streets-and-transportation-subcommittee.
- dallas-tx: 2024 Capital Bond Streets and Transportation Meeting – May 25, 2023 (233032).
  12 evidence references; 5 feeds sampled.
  A: assign_existing; dallas-tx-2024-bond-streets-and-transportation-subcommittee, dallas-tx-bond-program-meetings.
  B: assign_existing; dallas-tx-2024-bond-streets-and-transportation-subcommittee.
- fort-worth-tx: BOARD OF TRUSTEES OF EAGLE MOUNTAIN-SAGINAW ISD AND THE FORT WORTH CITY COUNCIL BOARD OF TRUSTEES OF EAGLE MOUNTAIN-SAGINAW ISD AND THE FORT WORTH CITY COUNCIL (https://fortworthgov.granicus.com/MediaPlayer.php?view_id=9&clip_id=2491).
  6 evidence references; 6 feeds sampled.
  A: assign_existing; fort-worth-tx-emsisd-board-joint-meetings, fort-worth-tx-city-council.
  B: assign_existing; fort-worth-tx-emsisd-board-joint-meetings, fort-worth-tx-city-council, fort-worth-tx.
- addison-tx: Citizen Advisory Committee – Jun 01, 2007 (56029).
  3 evidence references; 4 feeds sampled.
  A: assign_existing; addison-tx-town-meetings.
  B: unconfigured_body; no feeds.
- dallas-tx: 2024 Capital Bond Streets and Transportation Meeting – Jun 13, 2023 (259821).
  13 evidence references; 5 feeds sampled.
  A: assign_existing; dallas-tx-2024-bond-streets-and-transportation-subcommittee, dallas-tx-bond-program-meetings.
  B: assign_existing; dallas-tx-2024-bond-streets-and-transportation-subcommittee.
- arlington-tx: Environmental Task Force Meeting (https://arlingtontx.granicus.com/MediaPlayer.php?view_id=2&clip_id=3454).
  4 evidence references; 5 feeds sampled.
  A: assign_existing; arlington-tx-environmental-task-force, arlington-tx.
  B: assign_existing; arlington-tx-environmental-task-force.
- fort-worth-tx: Minority Leaders and Citizen's Council Minority Leaders and Citizen's Council (https://fortworthgov.granicus.com/MediaPlayer.php?view_id=11&clip_id=848).
  2 evidence references; 3 feeds sampled.
  A: unconfigured_body; no feeds.
  B: unresolved; no feeds.
- dallas-tx: 2024 Bond Task Force Community Bond Task Force Meeting – Aug 15, 2023 (269608).
  12 evidence references; 5 feeds sampled.
  A: assign_existing; dallas-tx-2024-community-bond-task-force.
  B: assign_existing; dallas-tx-2024-community-bond-task-force, dallas-tx-bond-program-meetings.
