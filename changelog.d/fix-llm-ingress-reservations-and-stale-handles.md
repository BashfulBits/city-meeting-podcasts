- **Ingress reservations are released as the UTC day ends, and a spent lane stops staging.**
  `enqueueBatch`, `schemaRetry` and `ingressStatus` all admit a job against the global write budget
  minus every *other* lane's unspent `reserved_write_units`. Measured live on 2026-10-22 21:40 UTC,
  that held the tagger at exactly zero headroom (`13,240` units used plus `12,360` reserved for
  idle lanes equals the `25,600` budget) while it was the lane with work to submit —
  `ingress_write_budget_reserved` refused 2,500-4,300 jobs per six-hour sweep for that reason. A
  reserve now decays on a schedule (`_reservationBorrowFraction`): nothing while more than 12 hours
  remain, half of it from 12 hours, all of it in the last 6. An unspent reservation that late can no
  longer be used by the lane it protects, so withholding it only refuses work the rest of the day
  had budget for; the reserve still holds through the productive part of the day. On the client,
  `purpose_write_budget_exceeded` and `ingress_write_budget_reserved` now close *that purpose*
  (`_ingest_closed_purposes`) instead of leaving it open: later batches for it return retryable
  deferred handles without staging a payload or making a request. The hourly-drain sweep was
  staging ~1,000 B2 payloads (~85 s) per chunk for jobs the Worker had already refused, every six
  hours. Other purposes in the same process — one `build` runs several lanes' stages through one
  backend — keep submitting, which is why the closure is purpose-scoped rather than global.

- **Deferred records a lane-registry change made unadmissible are retired instead of retried
  forever.** `1ce2091a` dropped `gemini/gemini-3.1-flash-lite` from the `r6-moments` lane, but the
  800 deferred moment handles written before it carry their original model in an immutable policy
  capsule, so every sweep re-submitted the same 800 first, got `model_not_in_lane` for each, and
  exited non-zero — no run since 2026-10-21 16:29 UTC was green, and the records never drained.
  `enqueue_batch` now reports such rejections (`model_not_in_lane`, `purpose_not_registered`,
  `route_id_not_in_model_pool`, `backup_after_attempts_below_lane_minimum`) as the typed
  `LLMDispatchRejectedError` (still an `LLMBackendError`), and the sweep retires the record through
  the existing bounded failure marker, so the producer's next plan (a new recipe, under the lane as
  configured now) can run the work. Retirements are reported as `retired_unadmissible` and do *not*
  count as `submit_failed`, which stays reserved for a submission that queued nothing at all.

- **The moments output reservation is sized to measured use** (`citypods/moments.py`
  `MOMENTS_OUTPUT_TOKEN_BUDGET`, 16,384 → 36,864). Jobs are sent with
  `max_tokens_mode: route_max`, so this number is only the scheduling reservation — but the
  reservation can never exceed it
  (`outputReserveFor` takes `min(this, the route's calibrated forecast)`), and it is what TPM
  admission is charged for. At 16,384 it was below real use: 12 of 19 calls on OrcaRouter's GLM 5.3
  Flash wrote more than their reservation (p90 35,313) and 8 of 16 on its DeepSeek V4 Flash (p90
  25,016), so those calls under-counted the token bucket by up to 2x (the token-budget monitor's
  `reservation_too_small`, 2026-10-22). 36,864 covers the higher p90. The budget is not in the
  moments recipe hash and `route_max` still means the provider receives the route's own limit, so
  nothing re-extracts and no answer changes; the two 16,384-output BeatAPI DeepSeek routes are now
  statically ineligible for moments jobs (`routeFitsContext`), which fails them over to OrcaRouter
  instead of letting them truncate the answer, and every model in the lane keeps a capable route.
