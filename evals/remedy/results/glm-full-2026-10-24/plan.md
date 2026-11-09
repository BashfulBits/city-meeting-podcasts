# Full GLM comparison — authorized October23Central

Evaluate BeatAPI glm-5.3-free on the same60 frozen original-prompt cases. Compare its
exact action/feed agreements and shared errors with DeepSeek, Gemini and GLM Flash.
Provider-default reasoning, no temperature override,16384output cap,720-second hard deadline,
primary account only, single-flight and65seconds between calls. Preserve all intents/results;
retry only authorized transient upstream failures with five-minute backoff. Never repeat a
valid wrong answer, use secondary account, paid IDs or substitute models. Gold stays offline.

The live Worker does not register this candidate. Use the previously approved temporary
BeatAPI provider pause includingJEV, drain first and restore only owned provider pause on exit.
Do not falsely charge another route. Prior exact-three investigation pauses remain separately
owned. This evaluates a role candidate; no route deployment, admission or production activation.

At04:07UTC the controller still had zero intents/results and was waiting for one preexisting
DeepSeekV4 route lease. Its provider pause had expired during the long wakeup gap. Interrupted
only before requests, corrected the shared runner to renew pause during drain every240seconds,
rearmed the approved scopes, and restarted. No duplicated or unknown provider calls exist.


Maintainer explicitly approved both NVIDIA and BeatAPI provider pauses and distributed full
GLM measurement. NVIDIA catalogue verifies full `z-ai/glm-5.3`; BeatAPI uses `glm-5.3-free`.
Cases are split by original manifest index: even BeatAPI, odd NVIDIA,60distinct cases total.
Both use original messages/schema, prompt-only formatting,16384output/default reasoning and
720-second deadline. Serving identity remains separate perprovider. Reports preserve source
paths/provider partitions; pooling candidate task scores does not admit both physical routes.
`capacity.py` reports observed throughput, errors and latency under singleflight65s pacing;
it does not establish either provider's hardmaximum. Disjoint halves differ in input lengths.
The first BeatAPI request returned524 after125seconds and remains an immutable availability
failure eligible for authorized retry. NVIDIA and BeatAPI controllers have separate locks.

BeatAPI transport correction: three non-streaming api.beatapi.io calls ended524 at~125s.
Switched only its local evaluation API base to documented direct.beatapi.io/v1, which avoids
the Cloudflare proxy timeout according to provider docs. Same key/model/payload/parameters;
old failures retained. Request URL is persisted in subsequent receipts. Production untouched.

The maintainer approved32,768-token fullGLM profile. New attempts retain720-second hard
deadline, same frozen messages/schema, provider-default reasoning and no temperature override.
Keep all completed16,384-cap answers and wrong answers; do not repeat them. Previously empty
finish_reason:length responses may receive a new32,768-cap attempt with distinct provenance.
Reports must identify mixed output-cap profiles and never relabel earlier16,384 attempts.
Switch controllers only between requests through the existing safe restart helper.
