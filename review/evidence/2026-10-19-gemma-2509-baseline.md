# Gemma #2509: deployment and initial passive baseline

Owning plan: [review/56](../56-gemma-aistudio-acceptance-plan.md).

This is an initial diagnostic snapshot, **not** a measured acceptance improvement or completion
of the 95% coverage goal. No model calls, queue changes, or production tuning were performed.

## Cutoffs and provenance

- [PR #2509](https://github.com/BashfulBits/city-meeting-podcasts/pull/2509) merged at
  2026-10-20 04:28:53 UTC (October 19, 11:28:53 p.m. Central), commit `b98ebf7a`.
- [Deployment 39369797621][deployment] succeeded; the 100% rollout was created at
  **2026-10-20 04:29:25.127583 UTC** (October 19, 11:29:25 p.m. Central).
- Deployed version: `a8f7dd83-f671-4a89-bf67-2b83fa399014`.
- Post-rollout start cutoff: **04:44:25.127583 UTC**, allowing 15 minutes for existing leases.
- Initial collection through **04:59:55.567 UTC**. With a 780-second response/delivery allowance,
  the mature request-start window ends at **04:46:55.567 UTC**. Recent fast 429s cannot enter
  the denominator while potentially slow successes from the same start window are still pending.

[deployment]: https://github.com/BashfulBits/city-meeting-podcasts/actions/runs/39369797621

Raw exports and private payload copies are kept outside Git under `/private/tmp/`; only safe
summaries belong here. `scripts/gemma_acceptance_report.py` reads saved exports offline with
explicit version/start/end cutoffs and deduplicates overlapping exports by attempt ID.

## Initial observations

Six `gemma_provider_attempt` records were captured on the deployed version: four HTTP 200 and
two HTTP 429. Four records started before the post-rollout cutoff, so this six-record count is
descriptive rollout evidence, not the qualifying cohort. The two 429s belong to one job, tried
first on secondary and then on primary. Only its initial attempt has matured in this snapshot.

The qualifying **observed Google-first sample is one refused job**. That is insufficient to
estimate the population refusal rate or claim improvement. It also is not a newly prepared
producer cohort: the original payload object was written at 20:46:36 UTC on October 19, before
this deployment. No completed post-deploy judge producer run had been observed at collection.

Job `d5082515-3fa4-4d0f-b4de-a8b083f97643`:

| Field | Secondary first attempt | Primary second attempt |
| --- | --- | --- |
| Start UTC | 04:46:07.119 | 04:47:17.750 |
| End UTC | 04:46:23.314 | 04:48:02.740 |
| HTTP | 429 | 429 |
| Failure class | `own_tpm` | `own_tpm` |
| Raw input estimate | 13,031 | 13,031 |
| Admission estimate | 13,642 | 13,642 |
| Conservative input estimate | 16,920 | 16,920 |
| Sent output cap | 32,768 | 32,768 |
| Parsed retry delay (seconds) | 36 | 57 |

Both quota bodies explicitly name
`GenerateContentInputTokensPerModelPerMinute-FreeTier`, model `gemma-4-31b`, location `global`,
and quota value **16,000**. Neither supplies a requested amount. Both coordinator observations
show one window entry and one pending call, a 15-second margin, and the same 16,920 reservation.
This confirms the quota dimension, not an intrinsic per-request size violation or Google arrival
accounting. Gateway IDs are `01a157a1-8b23-7497-acb7-0332466dc06d` and
`01a157a2-9f07-72ba-9532-b588c8e0bf44`.

The initial read-only poll showed the job leased with two attempts. A subsequent poll during the
05:04 UTC follow-up confirmed **`failed`, `job_failed`, two attempts**. Both observed attempts
were on Google; no larger-route attempt occurred before terminal settlement.

The coordinator's `isRateLimitTerminal` predicate shares the transient counter and
`MAX_5XX_RETRIES` budget. With its configured single retry, these two generic 429s exhausted
recovery. This is a concrete recovery failure independent of whether the packet was oversized.
Prepare bounded job-local same-model fallback before terminal failure; do not teach global size
calibration that every `own_tpm` refusal means an oversized packet.

## Extended observation at 05:04:38.722 UTC

Overlapping exports now contain three mature attempts representing **two initial jobs** after
the start cutoff: the quota-refused job above and a transport failure with no HTTP status.
The latter, `d6c1eba0-8038-4c6c-9007-af34c420c005`, ran from 04:47:50.130 to 04:59:50.130 UTC
(720 seconds). This remains too small, and lacks new-producer provenance, to estimate acceptance.

A filtered aggregate over the initial export window returned seven stored attempt events with
`sampleInterval: 1`. The seventh event was the timeout ending only five seconds before the first
collection; it appeared in the later overlapping export. This supports overlapping collection
and the response-maturity allowance. It does not prove that every provider submission emitted
and delivered a log, or rule out platform loss. An hourly passive follow-up retains overlap and
stays quiet unless it finds a material result or collection blocker.

## Offline count cross-check

The previously pinned Google Gemma tokenizer counts these original B2 message contents:

| Job prefix | Content tokens | With two reference framing tokens | Google reported input |
| --- | ---: | ---: | ---: |
| `cd4f9e01` (HTTP 200) | 13,948 | 13,950 | 13,950 |
| `d5082515` (429 on both keys) | 14,537 | 14,539 | Unknown |
| `cc6f5967` (rollout-window quota feedback) | 14,695 | 14,697 | Unknown |

The success provides another exact match after the two-token framing adjustment. The refused
packet's reference count remains below 16k; do not call it proven oversize from this evidence.
The count is local, not Google's quota measurement. For failed/leased jobs the polling API hides
the active payload key; these are ID-derived original objects, so superseded-pointer uncertainty
also remains. The successful poll confirmed `cd4f9e01`'s actual referenced payload key.

## Measurement safeguards and remaining work

- Separate model, purpose, and first route. Do not count a second Google key as a second packet,
  or let a retry success repair first-attempt acceptance. Exclude ambiguous reset generations.
- Size comparisons use pre-request estimates for both successes and failures; actual full input
  is available only when reported. Never substitute uncached dashboard input for packet size.
- The report describes observed logs, not complete provider submissions. Request-start manifests,
  missing executor completions, query pagination, ingestion sampling, and delayed logs need checks.
  A source query's `abr_level` does not establish ingestion completeness; Cloudflare documents
  aggregate `sampleInterval` separately in its [query API][query-api].
- HTTP 200 and valid JSON do not establish schema/quote-valid answers. Join producer validation
  and eventual same-model fallback outcomes before reporting usable completion or coverage.
- Wait for at least 200 unique packets plus a verified new producer cycle. Existing queued work
  is useful baseline evidence but cannot establish performance of a future packing policy.
- No static quota, output cap, pacing margin, packing controller, retry budget, or dependency was
  changed. The next tuning decision remains gated on quota diagnosis and representative evidence.

[query-api]: https://developers.cloudflare.com/api/resources/workers/subresources/observability/
