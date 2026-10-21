# Google Gemma-31B: Gateway retry isolation

The maintainer authorized live replays after 31 of 46 initial Google attempts reported the
16,000 input-token-per-minute quota. Thirty-three original payloads remained available;
13 had been removed. The recovered objects predated the original attempts, but those older
attempt logs did not include request digests, limiting identity verification.

## Method

Three sequential arms used the same 33 recovered requests, original Google projects, request
shaping, output limits and order. Each waited at least 240 seconds after a settled response
before sending the next request. Both production Google Gemma-31B routes were paused and
in-flight jobs drained. Each unsuccessful first replay received one spaced confirmation.
Gateway response caching was bypassed in both Gateway arms. The third arm changed only the
per-request `cf-aig-max-attempts: 1` override relative to the default Gateway arm.

## Results

| Transport | Initial 200 / 33 | Initial 429 | Initial 500/503 | Initial timeout | All calls | All 429 | Eventual 200 / 33 |
|---|---:|---:|---:|---:|---:|---:|---:|
| Direct Google | 15 | 0 | 18 | 0 | 51 | 0 | 27 |
| Gateway default | 18 | 14 | 0 | 1 | 48 | 20 | 27 |
| Gateway one attempt | 16 | 0 | 16 | 1 | 50 | 0 | 25 |

All observed Gateway 429s reported Google's free-tier input TPM quota of 16,000, without an
explicit requested amount. Six packets returned 429 twice through default Gateway handling;
five of those packets succeeded directly. No repeated intrinsic size refusal was established.

| Original reported result | Available / original | Direct eventual 200 | Default Gateway eventual 200 | One-attempt Gateway eventual 200 | Default Gateway 429 twice |
|---|---:|---:|---:|---:|---:|
| Input TPM quota refusal | 23 / 31 | 18 | 19 | 18 | 4 |
| Request defect, 400 | 1 / 1 | 1 | 1 | 0 | 0 |
| Capacity failure, 503 | 1 / 1 | 1 | 0 | 0 | 1 |
| Successful control | 8 / 13 | 7 | 7 | 7 | 1 |

Successful-response median durations were 60.2 seconds direct, 67.9 seconds through default
Gateway handling, and 67.1 seconds with the one-attempt override. Minimum post-response waits
were 240.269, 240.323 and 240.244 seconds respectively. The final arm completed on
2026-10-21 at 08:32:35 UTC. Probe responses were not persisted as production job completions.

## Interpretation and change

The results strongly support retry amplification contributing to quota exhaustion: default
Gateway handling exposed TPM 429s while direct and single-attempt handling exposed provider
500/503 errors and timeouts. Actual Gateway internal attempts were not observed. The arms ran
at different times, so changing provider load/account state remains a confounder. These tests
neither prove 95% Google coverage nor establish schema/quote-valid completion from HTTP 200.

Set a route-level one-attempt override for the two Google Gemma-31B routes tested here, allowing
the worker to own paced retries and preserve provider failure classification. Compile route
settings ahead of provider defaults, using the existing worker header support. Other Google
models retain their defaults. Worker budgets, quotas, pacing, packing, request bodies and
deadlines stay unchanged. Existing queued work adopts the transport setting after deployment;
there is no stored-payload rewrite, artifact invalidation or pipeline version bump.

## Implementation review

CLI CodeRabbit reviewed the complete proposed diff and raised one conditional concern: compiled
retry overrides could disappear if the source YAML lacked them. Both Google Gemma-31B entries
in `config/provider_limits.yml` include the setting. Recompilation and the precedence regression
verify preservation in both generated catalogs; no source correction is required for that finding.
The outgoing-header Worker test also verifies both route credentials and the one-attempt header.


## CI runner migration repair

PR #2519's initial CI failed before lint/tests: `ubuntu-latest` selected Ubuntu 26.04.1 and
APT could not locate `libvpx9`. The checksum-pinned FFmpeg build uses the Ubuntu 24.04 ABI,
so pin only the CI test job to `ubuntu-24.04`. Binary/checksum and production runners stay
unchanged. Follow-up: other workflows installing the same runtime libraries under
`ubuntu-latest` need an independent ABI/runner audit; widening this PR would change production
execution environments beyond the Google retry fix.
