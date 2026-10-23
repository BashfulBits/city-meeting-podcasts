# Google Gemma availability and location investigation — 2026-10-23

The supplied export covers 08:28–20:19 UTC: Google AI Studio made 103 Gemma-31B attempts,
with 33 HTTP 200, 34 HTTP 500, 24 HTTP 503 and 12 HTTP 400. These are attempts, not unique
jobs. The export has no request/response bodies or project identity. Four HTTP 200 records
reported zero output tokens; that alone does not establish unusable output.

## Response bodies verified through the signed-in dashboard

- 500, 20:13:52 UTC, `01a16a65-fbd9-70a4-baff-626dee9f18aa`: INTERNAL,
  `Internal error encountered.` (890 ms).
- 500, 11:11:22 UTC, `01a16872-3ebf-70b3-b5a7-4327d7fd80bd`: same text (201,832 ms).
- 503, 20:18:04 UTC, `01a16a69-a620-73fe-aa18-e55c71be4a01`: UNAVAILABLE; message says
  the model is experiencing high demand, spikes are usually temporary, and to try later.
- 400, 17:27:19 UTC, `01a169cd-8235-776d-a1f3-a86eea605b24`: FAILED_PRECONDITION,
  `User location is not supported for the API use.`

Only these four bodies were inspected; counts by HTTP status must not be represented as counts
of these exact response texts. API detail retrieval returned 403. No exhaustive body catalog exists.

## Location: evidence and limits

The scheduled Worker executes provider calls itself; its Durable Object only coordinates claims
and settlement. Provider calls use Cloudflare AI Gateway's Google AI Studio path with our provider
key (the export has wholesale=false). No region pin or static-egress setting exists in this path.
Cloudflare documents that cron Workers run on underutilized machines, so a stable execution site
is not guaranteed. Google sees the Gateway's upstream connection, not necessarily the scheduler's
site or the user's IP. The dashboard displayed Oregon for the inspected location refusal and
both inspected 500s; that does not reveal the exact Google-facing IP or its Google geolocation.

A firsthand independent report in cloudflare/ai issue 673 describes this exact location refusal
interleaved with successes through one SIN ingress. It uses Unified Billing, unlike this BYOK
path, so it is corroboration of a possible egress issue, not proof of our root cause. There is no
evidence here that Google bans an account merely for using multiple IPs, or that our two projects
have the same failure rate. Exact upstream IP/geolocation requires provider-side tracing.

Sources:
- https://developers.cloudflare.com/workers/configuration/cron-triggers/
- https://ai.google.dev/gemini-api/docs/available-regions
- https://github.com/cloudflare/ai/issues/673

Recommended location follow-up: correlate the verified request ID with a successful request's
route/project and Gateway trace; ask Cloudflare to identify upstream egress and Google geolocation.
Do not change keys or prompt size on the strength of this error. A narrow location-availability
classifier with bounded same-model fallback is a separate follow-up; generic 400s remain defects.
No support message was sent, egress pinned, Gateway bypassed, or paid route enabled.

## Retry changes and rollout

The approved policy is recorded in [review/56](../56-gemma-aistudio-acceptance-plan.md).
Before this change Google Gemma had Gateway max attempts=1, despite an obsolete tuning comment
saying Gateway made five fast attempts. Only 429s could retry within an invocation; 500/503s
settled durably for later cron claims. Generic 500s ordinarily had one durable retry, while
high-demand 503s had eight but route cooldown could grow to 24 hours.

The patch gives the observed Google INTERNAL 500 the upstream retry budget, uses three early
30–60-second availability cooldowns then a five-minute cap, and passes provider Retry-After to
settlement. It adds no provider calls within an existing lease and therefore keeps each call
inside normal admission/accounting. Other providers retain the day-scale saturation ladder.

Deployment is still required. Existing long cooldowns need explicit operational reconciliation;
otherwise a route already blocked for a day will remain blocked until that timestamp. Monitor
validated completions/hour, attempts/completion, 400/500/503 signatures and quota refusals by
project. Roll back the Google-specific classifier/cooldown if attempts rise without useful output.

## Local validation

- Worker suite: 567 tests passed, including classifier scoping, continued durable requeue after
  earlier failures, early retry timing, capped cooldown, and provider Retry-After floors.
- Dispatch compiler suite: 80 tests passed; full Python suite: 6,888 passed, 17 deselected.
- Whole-repository Ruff lint and format checks passed. No live inference calls were made.

## Follow-up: IP and location telemetry requested by the maintainer

Every `gemma_provider_attempt` log now includes `bundle_id`, the configured `provider_account_id`,
`gateway_cf_ray`, `gateway_colo`, and fixed `provider_error_status` / `provider_error_signature`
fields. The signature distinguishes unsupported location, INTERNAL and high demand without
logging arbitrary provider messages. The existing Gateway log ID and job ID remain available.
The configured account ID is a local catalog alias, not a key or Google project number.

A `gemma_execution_network` event joins by bundle ID and reports `worker_probe_ip`,
`worker_probe_colo`, and `worker_probe_country`. Its fixed endpoint is Cloudflare's public
`/cdn-cgi/trace`; it reports Worker-to-Cloudflare trace facts, NOT Gateway-to-Google egress. Different
outbound connections may take different paths. Neither the probe IP nor Gateway Ray colo proves
which IP or country Google saw. Cloudflare still needs the Gateway log ID to trace that hop.

There is at most one probe per claimed Google Gemma bundle, not per job or retry. It has a two-second
timeout, a 4 KiB response bound, no redirects, and runs concurrently with inference. Cleanup ticks
and unknown scheduled times skip the probe. A conservative per-bundle subrequest calculation also
skips it if provider calls/retries and B2 traffic would exceed 50 requests. No R2/DO writes or
provider inference calls are added by the observation. Probe failure is logged as a fixed status
and does not fail the jobs; API keys, prompts, outputs and arbitrary trace fields are excluded.

Compare failures and successes by provider account, Gateway colo, Worker probe location and bundle.
Repeated location failures from one observation group can establish a useful correlation, not
proof of the Google-facing source IP. Include the exact Gateway log IDs in a support trace.

Cloudflare can substitute a fixed Worker IP in cross-zone traffic. The observation therefore
labels `worker_probe_ip_kind=worker_placeholder` for `2a06:98c0:3600::103`; other values are
`trace_reported`, not verified Google egress addresses. The country is likewise trace-reported.
See https://developers.cloudflare.com/fundamentals/reference/http-headers/ for this limitation.

CodeRabbit CLI review found no initial issues; the telemetry follow-up found one changelog-title
format nit. Both entry titles now follow the documented period-inside-bold format.
