# Judge cohort provenance pilot — 2026-11-09

This read-only pilot tests whether returned production judgments can support PR7. It is not a
population estimate or a threshold proposal. The source records were read selectively from B2;
raw episode state, request payloads, and provider responses are not included in Git.

## Candidate inventory

The first 12 source objects returned by the B2 source listing are a convenience sample, not a
random sample of the catalog. Within those files, rows recorded after October 23 included 146
calibration and 2,478 contested adjudicator rows with valid quotes and a sibling row on the same
question. A further 52 adjudicator rows had invalid quotes. Matching a question alone does not
establish an independent subject, equal evidence, usable responses, or clean provenance.

Requiring a routine anchor and sibling on the same question, prompt version, tier, and evidence
digest reduced the candidate rows to 118 tag calibration, 2,183 tag contested, 26 moment
calibration, and 249 moment contested. These are row counts, not distinct subjects or packets.
The adjudicator uses its own evidence tier; its valid quote is required. Sibling quote validity is
reported separately and is not a gating criterion under the maintainer's clarification.

## Registry and archived-object pilot

From each task and sample cell, five recent candidate triples were selected by the earliest
judgment time among their three roles. This selection is deliberately favorable to fresh results,
not representative. The 20 triples referenced 51 distinct registry objects. All three role records
for all selected triples were marked completed and had registry creation times after the October
23, 04:41:19 UTC contract fix. The sibling and adjudicator responses all carried provider
creation timestamps after that fix. None of the 20 sampled JEV response bodies had a provider
creation timestamp: their retained fields were `answers`, `id`, `model`, and `usage`. Registry
creation time and later judgment collection time cannot by themselves establish when a provider
created the response. The pilot therefore does not assert any triple is fully clean under the
existing provenance rule. The completed registry records did not preserve actual route IDs or
explicit request revisions.

For one additional recent calibration triple, B2 version history retained the prior pending
registry records and older versions of the Worker payload and result objects, despite delete
markers on the current object names. Both chat payload schemas matched the current typed-v3
schema. The JEV result object was written on October 28, after the fix, but its body still lacked
a provider creation time. The artifact write time is evidence of when the Worker saved a result;
it is not labeled as the provider's response creation time. Actual physical route IDs were not
present in the inspected registry versions and were not inferred from the response shape. One
triple does not establish that the other retained versions exist or have the same properties.

## Implications

The old six-pair inventory is no longer an adequate description of returned data. The pilot
supports building a larger provenance join using retained versions, while keeping unknown JEV
response creation times and unknown routes explicitly unresolved. Before using a surrogate for a
provider creation timestamp, document and obtain the maintainer's decision on that provenance
standard. Do not re-ask JEV, change sample rates or authority, or infer production accuracy from
this pilot. PR7 still needs its population-weighted contested and random strata, uncertainty
intervals, policy fingerprint, and a clearly defined baseline after the old gates' retirement.

### Follow-up: distinct-subject sampling and retained versions

A second selection, deduplicated within each task/sample/question cell by subject ID, produced
20 candidate triples on 18 distinct subjects across the four cells. Those triples referenced
34 unique packets: 11 JEV, 12 sibling, and 11 adjudicator. This remains a recency-favored
convenience sample; subjects can appear in multiple cells and are not population weights.

For each of those 34 packets, a prior pending registry version supplied one job reference; one
archived request payload and one archived Worker result matched the completed registry output.
Every referenced registry creation, payload write, and matching result-object write was after the
October 23 fix. All 23 recovered chat payload schemas matched the current typed-v3 schema, and
all 23 chat responses carried provider creation timestamps after the fix. These checks establish
only what the retained artifacts show: matching a schema does not by itself prove the entire prompt
or its recipe revision, and an object write time does not establish a provider response creation
time. The 11 JEV response bodies did not have a provider creation timestamp; the inspected prior
pending and completed records did not supply actual physical route IDs. No selected triple is
claimed to meet the existing strict provider-creation-time rule on this evidence alone.
