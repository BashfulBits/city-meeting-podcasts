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

### Maintainer-approved JEV timestamp surrogate and replay

On November 9 the maintainer approved using the matching archived Worker result-object write time
as a **labeled, weaker substitute** for the absent JEV provider creation time when the registry
and request were also created after the fix. A provider could have returned an older cached
response. This does not establish actual physical route identity or independent qualification.

A follow-up replay checked the 20 triples on 18 distinct subjects described above, using the 34
unique retained packets. It reconstructed all 34 packet recipe hashes from the archived request
items, their evidence digests and prompt versions, using chat request revision 3. The archived chat
system messages matched the current sibling and adjudicator messages. For all 60 selected role
rows, the saved request's evidence digest and parsed answer matched the stored judgment; all 20
adjudicator quotes passed a fresh quote check. Eighteen of 20 sibling quotes passed; sibling
quote validity is not gating. All matching archived result writes were after the fix, all chat
provider creation timestamps were after it, and the JEV result writes use the approved surrogate.

The selected records matched candidates for all 18 subjects. All 20 triples had independent
logical producer, sibling and adjudicator families. Nineteen used the maintainer's agreed sibling
family: Gemma for the sampled non-Google producers. One rule-tag triple used independent Nemotron
instead, and is excluded from the agreed-panel subset. Actual provider route IDs remained unknown
in the inspected records; do not infer them from model names or response shape. These checks yield
19 **candidate triples under the approved surrogate**, not a representative calibration set:
they are chosen from 12 convenience-sampled sources, favor recent rows, contain only 18 subjects
before the panel exclusion, and share packets. They cannot support population estimates or a
threshold proposal. The existing JEV escalation band and combined rule must still be measured.

## Expanded audit and context check

The same 12 source files produced 559 deduplicated candidate questions on 475 subjects after
initial date, current candidate, logical-family, agreed-panel, and quote-validity filters. Their
441 packet references were traced against registry records and archived request/result versions.
Applying the approved JEV timestamp surrogate, exact packet recipe and evidence reconstruction,
typed-v3 chat reply parsing, independent adjudicator quote checks, and matching recorded values
left 505 candidate triples on 438 subjects. Fifty-four candidate triples could not be verified;
their exclusion reasons overlap and include missing archived request payloads, one invalid chat
contract, and an old JEV registry lineage. No failed HTTP-200 reply was resampled. Sibling quote
validity remained non-gating.

Four moment triples had the selected T1 anchor or sibling collected after the adjudicator request
was created, so they were excluded from the time-ordered cohort. The remaining 501 triples cover
435 distinct subjects: 285 tag validation questions, 74 moment validation questions, and 142
moment gate or grade questions. These numbers still describe the 12-source convenience sample,
not a population or independently sampled set. Across both samples below, 442 unique packets had
resolved registry model identities matching the stored logical model; no physical route IDs were
established by that comparison.

For a separate bounded source check, B2 listed 48 source episode objects totaling 2,167,020,792
bytes. A SHA-256 ranking of source keys using seed `review49-source-sample-2026-11-09-v1` selected
the first six; the sample size was chosen after object-size inspection and before reading those
sources' judgments. The listed universe's sorted-key SHA-256 was
`ded11cf07e7bbce9d4c8cfcdf3265c4e148b18eb0d5119826b64efb5f308a5c4`. The six objects totaled
462,339,390 bytes and were read one at a time; before/after ETags matched. Four had no qualifying
triples. The other two contributed 475 time-ordered, traced triples on 427 subjects. Two sampled
objects also belonged to the initial 12; deduplicating both sets by task, subject, question and
sample yielded 546 triples on 472 subjects, including 45 questions not in the first cohort. These
non-atomic, partially overlapping reads are not a catalog census and cannot support a general
population estimate or threshold on their own. Full key lists and raw request data stay outside Git.

### Evidence-window comparison

For the 501 time-ordered triples in the 12-source cohort, archived chat requests and responses
were replayed to check each sibling reason against its own evidence and the adjudicator's evidence.
Among 285 tag triples, 256 sibling reasons had a valid quote in their own evidence. For 113 of
those 256, no qualifying quote from the sibling reason was found in the adjudicator's evidence:
six calibration and 107 contested cases from two sources. In 101 of the 113, the adjudicator's
T2 transcript was at the 1,800-word cap. A cap being reached does not by itself prove why a
passage was absent. The code's T1 window is centered on candidate evidence while its chapter-based
T2 window begins at the chapter start and is capped; T2 is therefore not guaranteed to contain
T1. Sibling quotes remain non-gating. This is an evidence-comparability signal requiring review,
not proof that either judge was right or an estimated population error rate.

A descriptive replay of the combined rule used 0.5 solely as the frozen evaluation reference,
preserved T1 band escalation, used available verified T2 votes collected before adjudication,
and sent final disagreement or an in-band final JEV score to the adjudicator. It treated the
approved longer-context-on-disagreement behavior as a proposed scenario and left missing T2
results unresolved. Of 359 validation questions in the 12-source cohort, 312 had enough recorded
context for this replay and 47 remained unresolved. The 16 tag calibration questions came from
one source; in four, the panel agreed to support while the adjudicator opposed. Both model answers
are unverified against human truth; two sibling citations were absent from adjudicator evidence.
These four cases use four separate packets per role but share the source and logical GLM
adjudicator. No threshold is proposed from this small clustered subset. A local human-review
packet can be prepared from the retained requests; the underlying meeting text stays outside Git.

The weighted contested/random estimator, cluster-aware uncertainty, treatment of missing evidence,
and the retired-gate baseline still require a separate PR7 analysis. In particular, adjudicator
agreement is not human ground truth, and decisions resolved by the adjudicator agree with it by
construction. This replay must not be reported as an independent accuracy improvement.

In the four tag calibration disagreements, the current transcript and candidate records
reconstructed the exact saved T1 and T2 evidence digests in every case. For one of the two
cross-context quote mismatches, the candidate's stored evidence time precedes its assigned
chapter start. For the other, the candidate time lies inside its assigned chapter but occurs
after the segment where the chapter-first T2 window reaches 1,800 words. The latter is a
concrete truncation risk; the former requires a chapter/evidence assignment check. These are
different mechanisms and neither establishes a human truth label. A diagnostic packet showing
model answers is not a blind audit and its judgments must not be treated as independent gold.

The proposed T2 builder includes the T1 excerpt before its capped context. A chapter-scoped T2
is unbuildable if the candidate timestamp falls outside the assigned chapter; adjudication must
not fall back to T1 in that case. Old pending T2 replies are checked against their original digest
using the legacy builder. Existing rows remain stored, but changed T2 digests are new judgment
identities and can cause new JEV and adjudicator requests after deployment. Old and new evidence
revisions must be separated in calibration. This PR does not itself deploy or dispatch them.

On November 9 the maintainer reviewed these four disclosed cases in packet order and marked them
not supported, uncertain, not supported, and supported. These diagnostic judgments support
investigating the chapter assignment and truncated context; they do not establish population
accuracy, change the frozen gold labels, or resolve the uncertain case.

The maintainer's approved timestamp substitute applies only to the labeled JEV provenance
analysis above. Changing the evidence builder could change evidence digests and trigger new JEV
or chat work, so any proposed implementation must include an explicit versioning and backfill
story and must not silently re-ask preserved judgments.

## Local cache experiment (not a production run)

The November 9 judge run cached 3,388 parsed transcripts across planning and collection, with a
conservative estimated peak of 4,031,863,950 bytes. The estimate is not process RSS. The
400-episode quote-check budget does not bound the separate planning and completeness accesses.

A local experiment used 23 public holdout VTTs (4,910,484 raw bytes), parsed their transcript
segments, and replayed two sequential passes. The timing includes `tracemalloc` and per-access
serialization for output checking; it excludes HTTP transfer. Peak RSS is from independent local
processes and includes their baseline, not GitHub runner measurements. Only parsed segments were
modeled; production chapters, identity, shared definitions, failures and origin refresh semantics
were not modeled.

| Strategy | VTT parses | Disk rehydrates | Peak process RSS | Wall time |
|---|---:|---:|---:|---:|
| Unbounded memory | 23 | 0 | 105.03 MiB | 2.28 s |
| Eight-entry memory LRU | 46 | 0 | 87.91 MiB | 3.83 s |
| Eight-entry memory LRU plus temporary JSON spill | 23 | 23 | 86.98 MiB | 2.83 s |

All strategies produced identical outputs for the same access order. The spill wrote 5,783,533
bytes of temporary parsed data. A grouped-access run had more cache hits, showing why actual
access order matters. This is evidence for testing a bounded memory cache with a per-pass disk
backing, not a forecast of production throughput or a proposed quote-check limit. Any implementation
must preserve transcript snapshots, failure memoization and origin-refresh behavior, bound disk
use, clean up temporary files, and measure actual runner RSS and I/O before increasing the cap.
