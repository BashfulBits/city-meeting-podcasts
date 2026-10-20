# Gemma overflow timeout investigation — 2026-10-20

Owning plan: [review/56](../56-gemma-aistudio-acceptance-plan.md), confirmed response-timeout
recovery slice. Evidence collected passively; no inference probes, live replays, or tuning.

## Runtime evidence

PR #2511 deployed version `7033a6df-93ee-49a0-823a-564f75e52dad` at 11:08:19.097985 UTC.
The recovery sample uses initial Google starts after 11:23:19.097985 UTC, with 780 seconds of
response/delivery maturity. At 12:11:11.407 UTC it contains only eight mature initial jobs:
four HTTP 200, three TPM 429, one HTTP 500. Polling found six completed and two failed.
These coordinator states do not establish producer schema/quote validity or population rates.

| Job prefix | Observed sequence | Final poll |
| --- | --- | --- |
| `c1489763` | Google secondary 429; primary 429; NVIDIA 502; NVIDIA 720-second timeout | Failed, 4 attempts |
| `c01af1cb` | Google secondary 429; NVIDIA 720-second timeout | Failed, 2 attempts |
| `c57558e5` | Google secondary 429; primary 200 | Completed, 2 attempts |

The NVIDIA route is `nvidia_gemma_4_31b_it_free`, serving the same declared 31B model. The first
packet now reaches overflow after both Google refusals, verifying the routing repair. The 502
lasted 438.832 seconds; the two no-status NVIDIA calls each lasted exactly 720 seconds. Their
failure logs carry `server_error`, not a provider quota or size refusal. Do not learn a smaller
input ceiling from them.

All eight ID-derived original payload objects predate the earlier producer cycle. Current
payload-pointer and new-producer membership limitations remain. A new producer run, 39420087578
(`a246104a`), started at 12:09 UTC and was in progress when checked. The combined-version export
contains 164 unique attempt events, matching the filtered aggregate (`sampleInterval: 1`);
agreement among stored logs does not prove provider-call completeness.

## Code diagnosis

`index.js:attemptProviderCall` starts an abort timer for the configured 720-second response
ceiling. It catches transport failures as `retryable_error`/`server_error` with no HTTP status.
`coordinator.js:completeBatch` treats a failure as a final 5xx only when its status is 500–599.
Thus a known deadline bypasses both the existing transient retry allowance and route cooldown;
it can fail immediately even when that allowance is unused. The 502 follows the normal bounded
retry path; a subsequent timeout is terminal under the old behavior.

The repair distinguishes an actual-started call aborted by the executor's own timer and preserves
quota exclusions. Following the maintainer's execution-speed observation, two deadlines on a
physical route exclude it for that packet, so fitting permitted siblings get a chance even if
they must wait for capacity. Timeout counts are independent of the ordinary 5xx allowance and
reuse its route cooldown ladder. The bound is twice per configured fitting route; token limits,
pins and paid permissions remain binding. Generic catch errors still fail.
Explicit HTTP 504/524 server errors use the same timeout route bound and retain quota charges;
other HTTP 5xx statuses keep their existing transient policy.
Timeouts retain their claim charges: no response does not establish zero upstream consumption.
This can add bounded attempts where the prior code failed immediately; it does not guarantee
NVIDIA will finish within the deadline or repair already-failed jobs.

## Request settings and unresolved causes

The gateway shaper sends non-streaming requests. A `route_max` packet can send NVIDIA's configured
32,768-token output cap rather than the payload's 2,048-token scheduling estimate. NVIDIA's
[Gemma endpoint][nvidia-gemma] documents 1–32,768 output tokens and explicit
`chat_template_kwargs.enable_thinking` controls. The route supplies no thinking override.
Neither the effective default nor generation progress was observed for these failed requests;
do not assert that excessive reasoning or the output cap caused the timeout.

NVIDIA has no explicit `ai_gateway_max_attempts` setting, so the Worker omits that header.
[Cloudflare's request-handling documentation][gateway-retries] supports configurable retries and
says its final retry waits until completion. That does not establish this gateway's effective
policy or a hidden retry count. Read-only gateway configuration and log requests both still
return 403 with the credential wrapper; Worker logs and deployment reads work.

Keep output caps, thinking mode, streaming, gateway attempts, response deadline, concurrency and
quotas unchanged in this repair. If latency persists, design a bounded comparison of request
shaping with producer answer-quality validation and request-level retry/latency evidence. A
smaller cap can truncate valid reasoning/output, so it needs a canary rather than an assumption.

[nvidia-gemma]: https://docs.api.nvidia.com/nim/reference/google-gemma-4-31b-it-infer
[gateway-retries]: https://developers.cloudflare.com/ai-gateway/configuration/request-handling/
