# Changelog

All notable changes to this project are recorded here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). The project is **pre-1.0 (beta)** and does not
yet cut tagged releases, so entries are grouped by milestone (most recent first) rather than by version.
Once 1.0 ships, entries move under semver tags.

> This is the **living history of what shipped**. The forward-looking plan lives in
> [ROADMAP.md](ROADMAP.md) and [`review/11`](review/11-technical-design-roadmap.md); when an initiative
> is implemented, add it here as part of the same work (see the doc-update contract in
> [CONTRIBUTING.md](CONTRIBUTING.md)).

## Unreleased

_Work in progress toward 1.0 — see [ROADMAP.md](ROADMAP.md) Phase H (Hardening & Efficiency) and
Phase R (Research-Tool Surface)._

### Added

- **`NativeDiarizeStage` registers with `PROGRESS` and logs per-attempt start/done/error lines
  (`citypods/stages.py`).** Investigating a live run (denton-tx, 2026-09-05) that appeared to sit
  at `no tracked work active` for hours turned out to be a genuinely busy pyannote pipeline call
  with no progress instrumentation at all — the diarize lane was the one long-running caller that
  `citypods/progress.py`'s `PROGRESS` registry didn't cover, so a healthy multi-hour attempt was
  indistinguishable from a hung one until the runner was killed. The single blocking
  `backend.run_inference(...)` call for each candidate now runs inside `PROGRESS.track(source=
  <city entity>, uid=<episode>, phase="diarize")`, so the heartbeat's `active work:` line shows it
  busy with an elapsed time instead of reporting nothing. Each attempt also prints an
  `[enrich] diarize start uid=... body=... recording_s=... estimate_s=...` line before starting
  and a matching `diarize done`/`diarize error` line with `elapsed_s`/`ratio` afterward, naming the
  episode's own `body` so a shared-source run (per-body feeds sharing one `source_key`, e.g.
  `denton-tx-city-council` and `denton-tx-board-of-ethics`) is never ambiguous about which meeting
  type is actually running. If the run is killed before the first candidate finishes, the last
  heartbeat's `active work:` line names the stuck uid and its elapsed time directly, instead of
  requiring a `thread activity:` stack-sample read to infer that the run was busy at all.

### Fixed

- **Reject roster entries that are not name-shaped, and strip leading titles (review/31 §B.3a).**
  Measured against seven realistic minutes shapes, four produced junk that entered the person
  registry as members: `"a quorum of the Council was established at 6:02 p.m"`, a run-on line
  fused into one six-word "name", OCR noise, and `"Yes"`/`"Absent: None"`. Junk is strictly worse
  than silence here — a roster name enrols a person in the body registry, carries forward as
  standing membership, tiers as a **member**, and through `roster_person_ids` acts as a correction
  constraint (`allowed_ids &= roster_ids`), so a roster of junk resolves to zero known people,
  intersects `allowed_ids` to empty, and **suppresses every correct voice match for that meeting**
  while `confirmed=` simultaneously goes true. An empty roster has no such path: it yields `None`,
  which already means "make no correction". `_clean_roster_name` now rejects spans with digits,
  over five words, over 60 characters, or containing parliamentary/prose tokens, and strips
  leading honorifics longest-first (`Mayor Pro Tem` before `Mayor`). Stripping is not cosmetic:
  corroboration compares the roster name to the *spoken* name, so a stored "Mayor Gerard
  Hudspeth" could never corroborate a chair cue proposing "Gerard Hudspeth" — failing for exactly
  the people who speak most. `MinutesTextStage` also now counts `minutes-roster-parsed` vs
  `minutes-roster-empty`, because "no minutes yet" and "minutes present but unparseable" were
  previously indistinguishable downstream though only the second is a defect.

- **Bound and sandbox the R7 diarize decode, and refuse unknown-length episodes (CodeRabbit
  review of #1484).** Three real defects, each verified against the code before acting.
  `_load_waveform`'s `subprocess.run` had no `timeout`, and it runs inside a worker *before* the
  next `ctx.stop()` check — malformed media could hold its admission slot until the job's own
  330-minute timeout, so it now bounds the decode and reports a stuck one as an actionable
  per-episode error. It was also the only ffmpeg call site in the repo without a
  `-protocol_whitelist` (10+ others pin one); it now pins the narrowest form, `file,crypto,data`,
  since the diarize input is always a local temp file — without it a downloaded artifact that is
  really a manifest could make ffmpeg fetch the URLs it names. ffmpeg's stderr, which reaches logs
  and a stored `speakers_error`, is now credential-redacted and length-bounded. Separately,
  `NativeDiarizeStage` converted a missing served duration to `0.0`, which estimates as 0s of
  runtime and so "fits" any remaining budget while reserving only the base memory footprint — and
  because admission sorts longest-first, such an episode lands late, exactly when the budget is
  tightest. That is run 51's failure mode (an unbounded item admitted because its cost was
  unknown) with the cost hidden behind a default instead of a slow model; those episodes now defer
  as `unknown-duration` until the duration lands.


- **Make `/remedy` direct and bounded (issue #1231).** Classify in the Actions process with local
  schema/evidence correction, shared quota accounting, and no dispatch/deferred cache access.
  Version the remedy recipe by prompt/schema and preserve every finding in the evidence artifact;
  compact batches use local evidence IDs, explicit manual-review outcomes, and action-specific
  validation. Report partial failures honestly, install pinned verification dependencies, enforce
  a 55-minute process deadline with fallback issue reporting, and include findings in the generated
  PR without closing the audit issue on merge. Old remedy cache records are bypassed, not bulk
  deleted; existing audio/enrichment artifacts are unchanged and no catalog backfill is triggered.

- **Resolve R7 speaker diarization pilot city entity scoping and generalize allowlisting
  (GH#1274).** The scheduled R7 diarization workflow (`r7-diarization.yml`) completed in 30–40ms
  reporting `0 ran, 0 errors` because `stages.py` passed feed slugs (e.g.
  `denton-tx-board-of-ethics` or `denton-tx-city-council`) to `pilot_selected()`, while
  `config/site_config.yml` scopes pilot bodies by city entity (`denton-tx`), resulting in every
  episode being skipped as `pilot-not-selected`. Updated `stages.py` (`NativeDiarizeStage`,
  `ProviderTranscriptDiarizeStage`, `SpeakerIdentityStage`, and `_mark_stage_complete`) to use
  canonical entity slugs (`city.city_entity or city.slug`), and enhanced `citypods/speakers.py`
  (`pilot_selected` and `pilot_capture_context`) to support entity-prefixed feed slugs, wildcard
  city selectors (`city: "*"`), wildcard bodies (`body: "*"` or `all_bodies: true`), and global
  allowlisting (`allow_all_cities: true`). Unconfigured capture contexts under wildcard rules
  gracefully fall back to `{city_slug}-audio-v1` while preserving fail-closed security for empty
  pilot lists. No pipeline version change; existing content-addressed artifacts remain valid and
  unprocessed pilot episodes will be picked up on subsequent runs.

- **Chunk the deferred sweep's queue-only v2 submission at the Worker's batch limit
  (`scripts/llm_deferred_sweep.py`).** The sweep handed `LiteLLMBackend.enqueue_batch` its entire
  queue-only backlog in one call; the ingress Worker caps a single enqueue-batch request at
  `_WORKER_BATCH_LIMIT` (1000) jobs and rejects an oversized request with HTTP 400 for the whole
  batch, not just the overflow. Once the backlog grew past that limit, every queue-only record in
  the run failed to submit at once and the workflow went red (`llm-deferred-sweep.yml` failed on
  every run once the pending backlog exceeded 1000). The sweep now chunks the same way
  `BatchingDispatchBackend.flush()` already does — `_WORKER_BATCH_LIMIT`-sized chunks submitted via
  `_enqueue_batch_with_retry` — and treats a chunk not yet attempted when the sweep's deadline hits
  as still pending rather than failed, consistent with "deferred ≠ failed".

- **ASR align/diarization workflows broken by a fleet-wide Python 3.14 Renovate bump.** The
  `github-actions` PR that pinned Actions SHAs (#1353) also bumped every `setup-python`
  step's `python-version` from `3.12`/`3.13` to `3.14`, Renovate's default behavior for that
  input. `constraints/asr.txt`/`constraints/prod.txt` are compiled specifically for CPython 3.12
  (`scripts/compile_constraints.sh` resolves inside `python:3.12-slim-bookworm`), and
  `whisperx==3.8.6` has no distribution satisfying pip's resolver on 3.14 — `asr.yml`'s align
  lane, `asr-bench.yml`, `asr-quality-eval.yml`, and `r7-diarization.yml` all install extras that
  pull in whisperx, and failed deterministically (`ResolutionImpossible`) on every run since the
  merge, while `asr.yml`'s transcribe lane (no whisperx) stayed green. Reverted `python-version`
  to `"3.12"` in the four affected workflows (including `asr.yml`'s `reconcile` job, which shares
  `constraints/prod.txt`), with a comment pointing at the constraints compile target so a future
  blanket Python bump doesn't silently re-break these. No pipeline version change; no stored
  artifacts affected.

- **Active stop and runner budget boundaries for the LLM topic tags workflow.** Added
  `tag_run_time_budget_minutes: 140` configuration and wired `StopSignal` in `citypods/run.py`
  to actively stop `tag` lane runs as soon as tagging producer quotas (tagger and prelabeler)
  are exhausted. Wired `_run_bounded` to honor `ctx.stop` immediately across enrich passes and
  capped global candidate submissions to the purpose allowance window with headroom. In
  `citypods/tags.py`, eliminated duplicate transcript downloads between episode and chapter
  tag input generation.

### Changed

- **Minutes attendance lines carry member/staff sections, and `speaker_identity` skips episodes
  with no new work (review/31 §B.3, §C.5.7).** `parse_roster`'s status word was anchored to the
  start of the line, so `MEMBERS PRESENT:` / `STAFF PRESENT:` matched nothing at all — the cities
  that helpfully separate a council member from the City Attorney were exactly the ones whose
  rosters parsed as empty. An optional qualifier is now captured and mapped to a canonical
  `members`/`staff` section (staff wins a mixed "Staff Members Present", since "members" is filler
  there and misfiling a staffer as a member would hand them the cross-meeting speaker page the
  tier policy withholds), and the section is stored on the registry person so `body_membership`
  carries the distinction through the weeks before the next minutes publish. Widening the pattern
  created a new requirement it also satisfies: `Others/Guests/Public/Visitors Present:` lines are
  now excluded explicitly, because the old anchor had been excluding them by accident and the
  roster seeds the person registry — an audience member landing there would be enrolled as a
  probable official and, since an unsectioned roster hit tiers as `member`, could be offered a
  speaker page. Separately, `speaker_identity` stays always-revisit (a human decision must never
  wait on a media mutation) but now does no per-episode I/O when nothing feeding a naming decision
  moved: a coarse run-scoped digest over profiles/verdicts/thresholds, plus a per-episode digest
  over the diarization keys, roster, and **current pull-quote attributions** — that last part so a
  lost or rolled-back record push re-dirties the episode instead of being skipped forever on a
  marker that outlived its own output. Measured on the stage's own fixture: two object reads on
  the first run, zero on the second.

- **R7 naming closes its feedback loop, and covers the minutes-lag window (review/31 §C.5).** Three
  fixes that together make the adaptive gate able to actually learn. (1) `naming_candidates` never
  reached the weekly review queue — `speaker_review.package` built its pool from `candidates` and
  `reference_candidates` only — so no verdict could ever reach the precision table and no signal
  combination could ever become trusted. The three candidate classes are now a table keyed by the
  ledger that holds them, which also fixes a second break of the same shape: `self-introduction`
  rows live in `reference_candidates`, but only the literal `"chair-reference"` kind was
  recognized, so a self-introduction rendered as a shadow-match issue and then failed ingest
  against a ledger it was never in. (2) The queue was ordered by `candidate_id` hash; with a
  weekly limit of 8 that ordering, not the backlog, decides how fast the gate learns, so it now
  ranks references (one approval mints a voice profile that names its subject in every past and
  future meeting) above naming verdicts above shadow matches, better-corroborated first within
  each class. (3) A member speaking in last night's meeting has no roster for weeks and so had
  exactly one signal and never reached review; `body_membership()` now supplies the standing
  "who sits on this body" that `observe_attendance` already accumulates and
  `refresh_membership_status` already decays — no "last N meetings" window to pick or to get wrong
  across an election. It is a **distinct** signal from `SIGNAL_ROSTER` (a roster says someone
  attended *this* meeting; membership says they sit on the body — sharing a precision bucket would
  blend two different-quality signals), it yields once real minutes arrive, and it never reaches
  `roster_person_ids`, which uses a real roster to *remove* names. `UNTIMED_SIGNALS` now names the
  invariant that roster and membership can never name anyone in any combination, however many
  agree — previously true only as a side effect of the originating-signal rule. Also scopes
  `allowed_ids` to the episode's own body, which review/31 §C.4.3 required and which was invisible
  while a single body was piloted.

- **R7 speaker naming is a tiered, self-tuning gate instead of a flat per-cell threshold
  (review/31 §C.4; new `citypods/naming.py`).** `auto_publish_allowed`'s policy — 30 reviews × 30
  days × 95% precision per `(city, body, engine_recipe, capture_context)` cell, plus a private
  gold-set benchmark for that cell — is deleted. It multiplied as `30 × cities × bodies`, so no
  amount of better detection could scale past it, and it produced one publish/don't-publish flag
  for a whole episode, unable to express "confirm this member but auto-name that staffer". The
  replacement normalizes every signal (voice print, chair-recognition cue, self-introduction cue,
  roster corroboration, spoken title) to one claim shape — *signal S proposes name N for cluster
  C* — fuses agreeing signals into a single reviewable candidate, and decides per candidate by
  tier: council/board **members** always require human confirmation, **staff** publish
  unattended once their signal combination has earned it (≥20 verdicts at ≥95% agreement, no
  calendar element), and **everyone else** is never named. Precision is tracked per *signal
  combination* and pooled globally, so city #2 inherits the trust city #1 earned instead of
  re-earning it, with a per-city divergence guardrail that returns a city with genuinely worse
  audio to human review. Cold start is fail-closed. Three consequences worth knowing: the
  precision table is **derived** from the append-only review ledger on every run rather than
  persisted beside it (no second source of truth to drift, and pre-gate verdicts are inert by
  construction instead of needing a destructive migration); a cleared candidate now names its
  cluster **directly** via `cue_identity(method="cue-fusion")`, because `assign_turn` can only
  name a cluster that already matches a stored voice print and a staff presenter appearing once
  never acquires one; and `chair_reference_candidates` now reports a `title_cue` separately from
  `cue_kind`, since "the chair recognizes *Council Member* Jane Doe" carries an elected title
  inside a recognition cue and dropping it would have tiered a council member as `other`.
  `calibration_cell()` survives as the reviewer-facing scope label on ledger rows.

- **R7 diarization runs a concurrent, admission-controlled worker pool (review/31 §A.4).**
  `NativeDiarizeStage.process()` was a strictly serial `for ep in episodes:` loop, so one long
  meeting consumed a whole run and the backlog cleared one item at a time. It now collects every
  candidate up front (admission needs the full eligible set to pick a best fit), then runs a pool
  of `speakers.workers` single-threaded worker processes — measured across three different GH
  Actions runner CPUs (AMD Zen4, Intel Xeon 6973P-C, AMD Zen3), N single-threaded processes beat
  every other split on aggregate throughput by 65-78%, including the same 2-thread single-job
  latency optimum run N-wide. Admission is **best-fit-decreasing**: each freed worker claims the
  largest candidate whose estimated runtime still fits before the start cutoff, which with runway
  means the longest meeting (so long meetings are never starved by a queue of short ones) and,
  as the budget shrinks, narrows to progressively shorter ones on its own — no phase threshold to
  tune. Estimates are re-read per claim, so samples from items finishing this run sharpen later
  decisions. Memory is a second, independent constraint: each worker reserves its predicted peak
  RSS (~350MB + ~650MB per hour of audio, measured near-identically on all three CPUs) against
  `speakers.memory_budget_mb` via the same `MemoryReservation` accountant H8 already uses for
  audio encodes, so several long meetings running at once cannot OOM the runner. A new
  `diarize_backstop_minutes` (default 320) adds ASR's second tier: the start cutoff bounds what
  may *begin*, the backstop bounds how long an in-flight item may keep the job alive, marking it
  deferred and closing admission (it does not kill the subprocess — see review/31 §A.4 for that
  residual and why admission, not the backstop, is the real control). The pool uses a `spawn`
  context, never `fork`: the enrich run always has a heartbeat thread alive, and forking a
  multi-threaded process is the documented CPython deadlock hazard. Model files are prefetched
  once in the parent so spawned workers don't each re-download the same ~46MB, and a
  `BrokenProcessPool` (usually an entry point that runs work at import time, which `spawn`
  re-executes in every child) closes admission with an actionable message instead of failing
  every remaining candidate with the same opaque one. Candidates deliberately hold their
  timed-words *key* rather than its bytes: every candidate is now live at once, so retaining
  each sidecar would have turned a bounded per-item read into a whole-backlog memory spike.
  Also wires `self_introduction_candidates` into `SpeakerIdentityStage` -- it shipped defined
  but never called -- and adds that stage's first tests, which is what surfaced the gap.

- **R7 native diarization engine: pyannote-audio → sherpa-onnx + NeMo TitaNet-Small
  (review/31 §A.1a).** Run 51 (denton-tx, 2026-09-05) exposed pyannote's real CPU cost — ~2.2s
  of compute per second of audio, capping a single diarizable meeting at roughly 2h40m before
  the `diarize_start_cutoff` admission window (285m) runs out, a real problem for this project's
  many longer meetings. A same-day offline trial (two real, transcript-matched Denton City
  Council excerpts, a purpose-built gold-labeling tool, and `citypods/speaker_benchmark.py`'s
  own scoring extended past its prior hardcoded two-engine limit) found NeMo TitaNet-Small —
  run through `sherpa-onnx`'s CPU-only ONNX pipeline — matches pyannote's measured accuracy
  (turn_cluster_accuracy 0.947/0.937 vs. 0.962/0.956) at 8-13x its CPU speed, removing the
  ceiling outright with no GPU/external dispatch. `citypods/diarize.py` now runs
  `sherpa_onnx.OfflineSpeakerDiarization` (pyannote-segmentation-3.0 for VAD/segmentation +
  a swappable, per-recipe-calibrated embedding model, default TitaNet-Small) instead of
  `pyannote.audio.Pipeline`; **neither model is Hugging-Face-gated**, so `HF_TOKEN` is no longer
  needed for this lane (`scripts/preflight_diarization.py` rewritten to validate the new
  models are downloadable instead of checking HF auth; `r7-diarization.yml` drops the
  `HF_TOKEN`/`HUGGINGFACE_HUB_TOKEN` secrets and the torchcodec/shared-ffmpeg workaround
  pyannote needed). `pyproject.toml`'s `diarize` extra is now `sherpa-onnx` + `numpy` in place
  of `pyannote-audio`. TitaNet-Large and naive INT8-quantized TitaNet-Small were both evaluated
  and rejected (no real accuracy gain and clustering collapse respectively — see review/31);
  WeSpeaker-ResNet34/CAM++ were also evaluated and rejected on accuracy despite being fast
  (identity scrambling within continuous single-speaker speech). Also new:
  `citypods.speakers.self_introduction_candidates` — a second automatic naming-evidence signal
  (alongside the existing `chair_reference_candidates`) that scans the first ~10s of a
  speaker's own turn for a self-identification ("MY NAME IS...") or name-then-staff-title
  ("Matt Bodine, Assistant Planner") pattern, for the same identify-then-human-confirm
  pipeline, never assigning a name directly (wired into `SpeakerIdentityStage` by the
  worker-pool change above, which also added the first tests for that stage). Also closes the run-51 cold-start hole the swap would
  otherwise have reopened: `DiarizeRuntimeLog.estimate_seconds` now falls back to a seeded
  `DIARIZE_DEFAULT_RUNTIME_RATIO` (0.2 s/s, rounding up the worst measured single-threaded RTF)
  instead of returning `None`, so a recipe with no samples yet is *bounded* rather than admitted
  with no cap — changing the engine discards every measured sample, which is exactly when that
  matters. The worker pool, best-fit-decreasing admission ordering, and memory-pressure cap (also
  designed in this pass, review/31 §A.4) land separately. New diarization-pipeline recipe hash — existing pyannote-produced `speakers.json`
  artifacts remain valid or reachable; new/changed episodes re-diarize under the new recipe.

- **Preserve external-compute spend during lease cleanup (GH#1329).** Settlement and release
  use the existing reservation's provider cycle when callers omit it, rather than resetting the
  balance to a bare calendar-month key. Explicit stale-cycle callbacks and unknown owners are
  no-ops; admission still rolls provider cycles normally. This prevents Modal/Beam reconciliation
  from erasing settled spend and sibling reservations. Existing incorrect balances require an
  operator correction; the fix does not infer historical charges. No pipeline version changes or
  artifact backfill. The configured provider caps and reserves are unchanged.
  
- **Reproducible Worker deployments and documented shim-token rotation (GH#1328).** All five
  Wrangler action inputs now pin `4.129.0`, with a Renovate npm regex tracker on the weekly
  hygiene cadence and reviewed upgrades. Wrangler is excluded from the output-affecting custom
  manager rule. The provider shim README documents coordinated secret/Base URL rotation for
  z.ai and OpenCode, including maintenance, validation, and rollback. No pipeline version changes
  or stored-artifact invalidation; existing artifacts are left as-is.

- **One canonical registry for every LLM dispatch lane.** `config/site_config.yml` gains an
  `llm_lanes` block keyed by the exact `LLMRequestPolicy.purpose` string, carrying both that lane's
  models and its Cloudflare Dispatch v2 ingress write budget. `scripts/compile_llm_lanes.py`
  compiles it into the Worker's `ingress_reservations.json`, drift-checked at deploy the same way
  `dispatch_limits.json` already is. This replaces a hand-maintained `INGRESS_PURPOSE_RESERVATIONS`
  var whose keys had drifted to name purposes no client sends — it reserved capacity under
  `topic-tags` and `moments` while the client sends `topic-tags:tagger`, `topic-tags:prelabeler`,
  `r6-moments`, and `r6-judge` — which withheld 10,000 of 30,000 daily ingress write units from
  every real lane while being unusable by the lanes it named. An unregistered purpose is now
  rejected at ingress (`purpose_not_registered`) instead of drawing on shared headroom, so a new
  verb or task requires a new lane entry and a sub-purpose does not inherit its prefix's budget.
  `tagging.llm_model(s)`, `tagging.prelabeler.model`, `moments.llm_model(s)` and
  `moments.judges.models` are removed, and the chapter, tournament, and R5-benchmark routes move
  out of Python constants into the same block. **Model values are unchanged, so no recipe hash
  changes and no stored artifact is invalidated**; `tests/test_llm_lanes.py` pins the
  recipe-affecting strings so a future edit surfaces its backfill cost instead of silently
  queueing catalog rework. The lane's route list is enforced, not merely documented: a job whose
  `allowed_models` name a route its own lane never declared is rejected at ingress
  (`model_not_in_lane`), and both gates also cover the supersession path, which consumes no
  admission budget and so must not be a way around registration. The registry has no per-run
  override by design — the Worker's reservation map is compiled from the committed
  `config/site_config.yml` alone, so lanes resolved from any other config would look like they
  applied in the producer and simply not exist at ingress.

- **Batched research-lane dispatch and a higher dispatch ceiling.** `citypods/tournament.py` and
  `citypods/r5_benchmark.py` built a fresh `LiteLLMBackend` inside their innermost loops, so every
  candidate generation and every pairwise comparison was its own Worker request and the backend's
  per-instance ingest-throttle state was discarded on each call. Both now share a run-scoped
  `PerModelBatchingBackends` collector and submit one bounded `enqueue_batch` per model. The
  tournament's per-run budget is derived from its two lane budgets (candidate jobs and comparison
  jobs bill to different purposes) instead of a hard two-sample clamp that had capped it near 32
  jobs/day, giving 46 samples per run. A lane whose per-run cap exceeds what its daily write
  budget funds is now rejected outright — four lanes shipped with that skew, and the surplus was
  simply rejected at ingress. Worker knobs are retuned against the measured binding
  constraint — the shared 100,000/day Durable Object row-write budget, not the per-invocation
  subrequest ceiling: `MAX_BUNDLES_PER_UTC_DAY` 1,000 → 1,400 and
  `MAX_INGRESS_WRITE_UNITS_PER_UTC_DAY` 30,000 → 24,000 lift the dispatch ceiling from 4,000 to
  5,600 LLM calls/day at 69% of the write ceiling. `MAX_BUNDLE_JOBS` stays at 4 because raising it
  is what breaches that budget. No recipe, artifact, or pipeline version changes.

### Fixed

- **Gemini free-tier 429s on oversized single requests, and a silently-deferred `/remedy` path.**
  Live-tested against the real Gemini API this session: `tpm` on the free tier is a hard
  per-request ceiling with no burst room above it — a single request over the usable window fails
  outright no matter how idle the account is — and the real usable ceiling can sit well below both
  `tpm` and the model's advertised context window (confirmed on an otherwise-idle model: ~125,000
  actual, vs. the `250000` both our config and Google's own quota error report). Confirmed live
  this is *not* universal: NVIDIA's free tier accepted a real request nearly 3x its configured
  `tpm` outright, so a blanket scheduler rule would have broken working capacity elsewhere.
  `LLMRoute` gains an opt-in `hard_input_ceiling` field (`citypods/compute/llm_policy.py`), set
  only on the 14 Gemini/Gemma routes in `config/provider_limits.yml` (120,000 for the
  250k-tpm flash family, 8,000 for the 16k-tpm Gemma routes — conservative interim values pending
  per-model calibration), enforced as a second, tighter gate in `select_route`
  (`citypods/compute/llm_scheduler.py`) and in the Cloudflare Worker's token-bucket pacing
  (`workers/llm-dispatch-v2/src/pacing.js`, which previously assumed every route could burst up to
  5 windows deep). `COUNCIL_MOMENT_MODELS` (`citypods/moments.py`) and the `chapter-locator` lane
  (`config/site_config.yml`) gain `deepseek/deepseek-v4-pro` and `moonshotai/kimi-k3` (free NVIDIA
  routes, no hard cap) as overflow for a full-transcript job too large for Gemini's real ceiling.
  Separately, `citypods/audit_remedy.py`'s `/remedy`-issue-comment classification never set
  `purpose` on its `LLMRequestPolicy`; a capacity miss silently persisted a deferred handle to the
  shared registry (`LiteLLMBackend.run_inference` writes it before the caller's own exception path
  runs) for the unrelated `llm-deferred-sweep` cron to retry hours later, disconnected from the
  original request — the opposite of the real-time turnaround `/remedy` promises. It now sets
  `purpose="audit-remedy"` and explicitly discards that handle on a capacity miss instead
  (`discard_deferred`), and its evidence-bundle assembly is now also capped by a token budget
  (`EVIDENCE_TOKEN_BUDGET = 100_000`) as an additional backstop alongside the existing fixed
  `MAX_ARCHIVED_BODIES` body-count cap, so it reliably fits under the tightest `REMEDY_MODELS`
  route's ceiling rather than depending on a fallback model. Trimming escalates in three passes so
  a bundle made entirely of findings already at the minimum-episode floor (nothing left for the
  first pass to touch) can still be brought under budget: strip each episode's free-text `body`
  next, and only as a last resort drop whole findings outright (least-evidenced first, one always
  kept so the model never gets an empty bundle), recording the count on
  `evidence["findings_omitted_count"]`. The Cloudflare Worker's mirror of the same hard-ceiling gate
  (`workers/llm-dispatch-v2/src/pacing.js`) now compares it against the input-token estimate alone,
  matching `select_route`'s contract, instead of the combined input+output `reservation` — the
  Worker was previously rejecting jobs the Python scheduler had already accepted whenever the
  output estimate alone pushed the combined total over the ceiling. `mistral/mistral-large-2512` (`403 tier_not_allowed`
  live) and NVIDIA's `nemotron-3-ultra-550b-a55b` route (`404` live at its configured model id) are
  both currently broken independent of this fix — flagged, not fixed here; Mistral is deliberately
  not depended on for the new overflow paths, since it separately carries its own account-wide
  monthly budget cap. No recipe hash or pipeline version changes: `models[0]` is unchanged on both
  affected lanes, and `hard_input_ceiling`/`EVIDENCE_TOKEN_BUDGET` are scheduler/runner-side only.

- **R6 accepted jobs no longer report false submission failures.** Meeting moments run #13
  accepted all 14 jobs, then treated four immediate poll errors as failed submissions. Shared
  run-scoped collectors now flush bounded enqueue batches only; durable handles retain execution
  and schema failures for normal reconciliation and its existing bounded retry/correction policy.
  Rejected submissions still fail the producer. This also fixes the same collector contract for
  tags, chapters, and research lanes. No recipe/version change or artifact backfill is required.

- **Remedy unexpected bodies workflow runtime, state integrity, and structured output schema.**
  `.github/workflows/remedy-unexpected-bodies.yml` previously took ~30 minutes per run due to
  several compounding issues:
  - Step 1 (`Collect unexpected-body evidence`) lacked Cloudflare R2 credentials, causing
    `pull_canonical_state` to fail manifest resolution and fall back to listing only 424 B2 files.
    Missing historical meeting records caused hundreds of existing meetings in Dallas, Denton,
    and Fort Worth to be falsely flagged as unexpected bodies, producing massive prompts that
    exceeded context budgets and triggered Gemini 429 rate limits. R2 secrets are now wired into
    step 1.
  - Step 1 previously ran against the entire catalog across all cities and feeds, refetching
    sibling feeds on the same source view dozens of times (e.g. Dallas was scraped 36 times).
    `scripts/audit_feeds.py` now accepts `--issue` to scope evidence collection strictly to
    feeds sharing the source keys of affected feeds parsed from the issue state marker, skips
    sibling fetches when collecting evidence, and bypasses issue reconciliation on dry-run
    evidence generation.
  - `classify_unexpected_bodies` now specifies `structured_output` (`unexpected-body-remedy`
    contract bound to `RemedyOutput`) in `InferenceJob.inputs`, preventing Pydantic validation
    failures from unstructured model responses.
  - Removed an unnecessary `apt-get install -y ffmpeg` step and eliminated a redundant second
    `pull_canonical_state` invocation in Step 2.

- **Chapter agenda extraction and boundary locator runtime bottlenecks eliminated.** Eliminates
  redundant 7m 29s replay loop for deferred `JobHandle` items by recording pending status and
  job refs directly in memory after batch flush. Short-circuits agenda text and transcript
  artifact downloads from B2 storage when deferred jobs are already pending. Wires
  `max_dispatches_per_run` from `config/site_config.yml` into runner `StageContext` for
  `chapter-agenda` and `chapter-locator` producer dispatch caps (1,000 max dispatches), avoiding
  quota breaches and Worker write rejections. Pre-filters global queue candidate episodes in
  `_run_enrich_global_queue` down to eligible episodes needing work (dropping candidate queue
  from 25,031 to ~1,777 items). In batch-prepare pass: enabled stats accumulation and deducted
  provisional `llm-pending` counts for replayed `JobResult` items or submission errors to eliminate
  double-counting while preserving accurate stage totals. No recipe, artifact, or pipeline
  version changes.
  
- **Granicus Worker fallback respects slice download caps on truncated probes.**
  `download_verified` in `citypods/granicus_chunked.py` previously treated `max_bytes` solely as a
  remote media cap (`total > max_bytes`), causing truncated media-fetch probes with an 8 MB cap
  to immediately fail against large meeting video files (>1 GB) with
  `ChunkedDownloadError("Worker object exceeds the configured media cap")` and trigger spurious
  `RateLimitedMediaFetchError` contract probe failures (#1241). Added a `max_download_bytes`
  parameter to `download_verified` to bound byte ranges and stream limits without rejecting large
  remote objects, and preserved captured FFmpeg diagnostic logs when contract checks encounter
  exceptions.

- **Topic-tag dispatches use the registered v2 lane purpose.** `llm_tag_suggestions()` previously
  defaulted to the storage feature name `topic-tags`, while the v2 ingress registry requires
  `topic-tags:tagger`. The tagger and pre-labeler now share explicit purpose constants, preventing
  rejected queue submissions without changing recipes, artifacts, or backfill behavior.

- **Endpoint Contracts workflow installs dev dependencies for AI Gateway probe.** `contracts.yml`
  runs `tests/live/test_ai_gateway_contract.py` via `python -m pytest` to probe Cloudflare AI Gateway
  custom-provider routing, but its install step previously locked only `constraints/prod.txt`,
  failing scheduled runs with `No module named pytest`. The install step now installs `.[dev]`
  constrained by `constraints/dev.txt`.

- **Topic-tag checkpoints no longer spend most of their time in serial storage I/O.** Dispatch
  batches now stage independent B2 payloads and persist accepted/deferred handles with bounded
  concurrency. An ambiguous transport failure retries the exact same prepared envelope, retaining
  job IDs and B2 keys rather than creating an avoidable second upload set. Per-purpose ingress
  budget rejections are now durable deferrals (with reason-count telemetry), not failed
  submissions, and do not trigger a meaningless status poll. Independent source-record
  fetch/merge/upload checkpoints also run with bounded concurrency while retaining the existing
  per-source foreign-block merge. No dispatch quota, recipe, artifact, pipeline version, or
  backfill behavior changes.

- **Topic-tag ingress now reaches the Worker before the runner timeout.** The tag lane formerly
  retained every new queue-only job in a process-local `BatchingDispatchBackend`; its only flush
  was in the final epilogue, so a 165-minute step timeout could cancel the run with zero accepted
  LLM jobs. The collector now flushes before each durable checkpoint, and the pre-labeler has its
  own 1,250-dispatch producer cap rather than continuing after tagger capacity has filled. The
  Worker remains authoritative for ingress and daily quotas; this changes no recipe, artifact,
  pipeline version, or backfill behavior.

- **Tag/moments lanes no longer spend a whole run checkpointing.** The mid-pass checkpoint
  re-anchored its interval timer *before* doing its work, so a checkpoint slower than the 180s
  interval came due the instant it returned and the lane checkpointed continuously; and
  `_run_bounded` refilled its submission window *after* `on_progress`, so the worker pool sat empty
  for that whole window. Measured on an instrumented run: the checkpoint takes **272s then 589s**,
  dominated by the durable state push (225s/555s) rather than the local record persist (47s/34s).
  Together these had the tag lane completing **one item per ~250s cycle** with eight workers
  available — one `stage start`, one `stage done`, then 229 seconds of nothing but heartbeats,
  repeatedly — and being cancelled at GitHub's 180-minute job timeout on three consecutive runs
  (2026-09-03/04). The interval now re-anchors after the checkpoint, scales so checkpoint cost stays
  under ~25% of wall clock, and the window is refilled before the callback so workers keep going
  across it. Adds a thread-activity sampler to the heartbeat, since its existing `active work:` line
  only covers one instrumented call and reported "no tracked work active" for a busy run.

  Verified on three instrumented production runs of the same lane:

  | | `main` | + interval re-anchor | + refill & adaptive interval |
  |---|---:|---:|---:|
  | stage completions / min | 2.6 | 23.6 | **50.6** |
  | worker parallelism | ~1 item / 250s | 2.9x | **5.7x** |
  | distinct sources touched | 3 | 11 | 11 |
  | checkpoint overhead | ~continuous | — | **15% of wall clock** |

  A 261s checkpoint now sets `next_interval=1043s` instead of coming due again after 180s, and
  9 stage completions land *during* that checkpoint where previously the pool sat empty. At 19.5x
  the original rate, a backlog that took the full 180-minute job timeout finishes in roughly ten
  minutes of the same work.

- **The R5 benchmark no longer bills the catalog's prelabel lane, or prelabels an incomplete
  candidate set.** `llm_prelabel_candidates` hard-coded `purpose="topic-tags:prelabeler"`, so the
  shadow benchmark spent the production catalog's ingress write budget while its own
  `r5-benchmark:judge` lane went unused; the purpose is now a parameter that keeps the production
  default. Separately, a prelabel entry that reaches `resolved` is never recomputed, and under
  Dispatch v2 the whole first tagger pass comes back deferred — so the pre-labeler assessed the
  rule engine's candidates alone and permanently froze that verdict, never looking at the LLM
  candidates it exists to evaluate. It now waits for every tagger to resolve an example before
  spending a job on it.

- **LLM producer lanes no longer fail silently.** A failed batch submission is not a deferral —
  nothing is queued, so no later run picks it up — but the enrichment, tournament, and benchmark
  entry points counted the failures, printed a line, and exited 0. All four now report them and
  exit non-zero. `scripts/llm_deferred_sweep.py` had the same defect and is fixed the same way,
  with one distinction its reconciler role requires: submission rejections are counted separately
  from reconcile failures (`submit_failed` in the end summary), and only the former set the exit
  status — a bad payload or a provider error is an ordinary per-record outcome for a sweep and
  must not turn that workflow permanently red. The Worker's new registration gate makes this
  reachable in production: a legacy record carrying the bare `topic-tags` purpose is upgraded to
  queue-only by the sweep and rejected at ingress as `purpose_not_registered`. The four lane workflow steps also gain a step-level timeout below their job
  timeout: a job-level timeout *cancels* the run, which GitHub shows as a grey "cancelled"
  indistinguishable from a manual cancellation and which skips every later step, so nothing is
  persisted or reported. `tag.yml` hit exactly that on eight consecutive scheduled runs between
  2026-08-26 and 2026-09-02. `llm-tournament.yml` gets the same guard for a new reason: its
  sampling step now sizes itself from the lane budget (~46 samples, up from a hard-coded 2), so
  overrunning the 30-minute job would spend all that work and skip the champion-ticket step.

- **Record-first, quota-filling LLM producer lanes.** Record-backed enrichment now prepares from
  the restored append-only episode archives instead of first scraping every live provider and only
  falling back to records on an error. Queue-only topic-tag and R6 moments runs are bounded by their
  committed dispatch-count quotas rather than an earlier runner deadline, allowing each daily run
  to enqueue its full quota when enough pending work exists; Cloudflare Dispatch v2 still owns
  provider pacing, admission, and quota enforcement. No recipe, artifact, taxonomy, or pipeline
  version changes, so existing artifacts are retained and no backfill is auto-invalidated.

- **Serialize Airforce and add a request-start safety margin.** Restores provider and route
  concurrency to one for Airforce's one-RPM Mistral Medium route, and spaces starts by 62 seconds
  (the documented minute plus a two-second clock-skew margin). This is intentionally not
  response-time pacing; it changes scheduling only and requires no recipe, artifact, pipeline
  version, or catalog backfill.

- **Increase Airforce provider and route concurrency.** Raises `concurrency` from 1 to 4 for the
  `airforce` provider and `airforce_mistral_medium_3_5_primary` route in
  `config/provider_limits.yml` and recompiled catalogs (`llm_routes.json`, `dispatch_limits.json`).
  Permits up to 4 concurrent in-flight requests against `api.airforce` to improve throughput now
  that failure modes other than global rate limits are isolated.

### Added

- **System architecture evolution & refactoring program (review/45), detailed to L3.** Records a
  reviewed umbrella for future throughput, reliability, observability, LLM dispatch, and
  modularity work; hardened from an initial "L3 dev-ready" overclaim into a gated L2 program, then
  detailed back to **L3 dev-ready per workstream** the same day — every one of its 18 initiatives
  plus state-store partitioning (§2) and v1 retirement (§3) now carries exact file/function
  references, algorithms, schemas, and test plans grounded directly in the codebase (several
  premises in the earlier draft were corrected against the real code in the process: Initiative
  8's stage-signature claim was factually wrong, Initiative 11's LOC estimate was off by ~10x,
  Initiative 14's "priority already in `llm_lanes`" was false). It does not itself activate a
  state-store cutover, retire v1, or ship an 11-PR schedule — those still-destructive steps carry
  their own explicit maintainer-approval checkpoints inside the now-detailed design; review/26
  duration normalization and review/34 tournament work remain their own accepted designs.

- **review/45 gains Initiative 19, folding in [GH#1463](https://github.com/BashfulBits/city-meeting-podcasts/issues/1463).**
  GH#1463's dirty-source-tracking ask (its item 3) is already GH#1458/Initiative 4 — cross-referenced,
  not duplicated. Its remaining scope (an atomic/expiring v2 admission reservation before B2 payload
  staging, hardening the deferred-job registry's crash-recovery guarantee with a proof obligation
  rather than an assumption, and extended per-purpose admission/rejection stats) is specified as a
  new L3 initiative sequenced after Initiative 4, grounded against the real `enqueue_batch`/
  `coordinator.js` admission flow (confirmed: `write_deferred` already persists accepted jobs
  independently of episode-record persistence, but recipe-hash determinism across a crash/restart is
  currently unproven; rejections are not persisted anywhere today).

- **Provider-chapter fallback gate and bounded v2 ingress/reaping.** Generated agenda and boundary
  extraction now runs only for episodes without `source_chapters`; provider markers clear any stale
  generated overlay and batch-cancel queued v2 fallback jobs. Dispatch v2 now budgets ingress in
  pessimistic Durable Object write units with purpose-scoped reservations, raising the former 5k
  job ceiling without removing its runaway-workflow guard. The deferred sweep consumes v2 terminal
  jobs through a cursor feed and job-reference pointers after one explicit index repair, instead of
  downloading every pending v2 record each cadence. No chapter recipe or pipeline-version bump is
  introduced, so existing no-provider-chapter fallback artifacts are retained.

- **Airforce provider, Mistral Medium latest migration, and 429 throttle fix.** Adds
  `airforce` provider in `config/provider_limits.yml` routing through Cloudflare AI Gateway
  (`custom-airforce/v1/chat/completions`, `AIRFORCE_API_KEY`) to serve `mistral-medium-3.5`.
  Standardizes Mistral Medium routes on `mistral/mistral-medium-latest` (50 RPM / 0.83 RPS,
  25k TPM) with legacy aliases `mistral/mistral-medium-2508`, `mistral/mistral-medium-2505`,
  and `mistral/mistral-medium-3-5` normalized automatically. Clears cross-model overflow routing
  so Mistral Medium traffic stays isolated to official Mistral and Airforce endpoints. Fixes a
  deadlock in LLM Dispatch v2 where transient 429 route buffer penalties were checked as static
  duration thresholds in `_capacityFraction`, permanently excluding throttled routes from
  candidate ranking.

- **Cloudflare AI Gateway rate-limit resilience and Groq free-tier limits update.** Implements
  aggregate provider-level TPM rate limiting across Python (`citypods/compute/llm_budget.py`,
  `llm_policy.py`) and Cloudflare Workers V2 (`workers/llm-dispatch-v2`), sharing token buckets
  across multiple routes under the same provider account (e.g. `nvidia`). Adds per-route and
  provider concurrency limits (`concurrency: 1` on SambaNova, OpenCode, Kilo; `concurrency: 2` on
  NVIDIA) to prevent free-tier concurrency exhaustion. Adds `parseRetryAfterSeconds` in Workers V2
  gateway to capture and honor upstream `Retry-After` and reset headers (`x-ratelimit-reset-*`),
  updating route cooldown buffers dynamically. Adds full randomized jitter (`0.5 + Math.random()`)
  to 429 and transient 5xx retries to break thundering-herd storms. Updates the Groq free-tier
  catalog in `config/provider_limits.yml`, removing deprecated Llama 3.3 routes and adding
  `gpt-oss-120b`, `qwen/qwen3.6-27b`, and `qwen/qwen3.8-27b` routes meeting the Gemma-4 floor.

- **Groq Qwen 3.8 27B free route.** Registers `qwen/qwen3.8-27b` with its published 30 RPM, 1K
  RPD, 8K TPM, 131,072-token context, and 65,536-token output limits. Groq's separate 2M TPD
  ceiling is documented in the provider registry but is not yet enforceable because the route-ledger
  schema has no daily-token field. The route is available for explicit Qwen requests; Gemma routing
  and the production judge remain unchanged pending task-specific evaluation.

- **NVIDIA build.nvidia.com provider and free-capacity pooling for Mistral Medium overflow.** New
  `nvidia` provider in `config/provider_limits.yml` (OpenAI-compatible NIM gateway, `NVIDIA_API_KEY`)
  with a conservative, explicitly self-imposed cap: 12 RPM provider-wide (30% of the ~40 RPM
  community-reported, unpublished baseline) and 100k TPM per route (not a true provider-level TPM
  ledger — see the provider block's comment). Adds direct free routes for `moonshotai/kimi-k3`,
  `deepseek-ai/deepseek-v4-pro-0813`, `google/gemma-4-31b-it`, `openai/gpt-oss-120b`,
  `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning`, and `nvidia/riva-translate-4b-instruct-v2`
  (reserved for future translation work), and pools with existing routes for DeepSeek V4 Flash and
  both Nemotron 3 Ultra and Super — the last two previously reachable only through OpenRouter/Kilo/
  OpenCode brokers, whose free Nemotron slots return 429/503 "upstream provider has no capacity"
  under load. Mistral Medium's quota-exhaustion overflow (`model_routing`, both 2508 and 2505) now
  targets DeepSeek V4 Pro and Nemotron 3 Super ahead of Gemini 3.5 Flash Lite, reflecting Mistral's
  own account moving from a rate limit to a $10/mo credit cap. NVIDIA's embedding
  (`nemotron-3-embed-1b`) and TTS (`magpie-tts-zeroshot`) models, and Gemini's embedding
  (`gemini-embedding-1/2`) and TTS (`gemini-3.1-flash-tts`) models, are intentionally **not** wired
  in here — they need a distinct non-chat-completions request shape this schema doesn't support yet.

- **Shared weekly review adapter and merge-gated tournament tickets.** H15, R5, R6, R7, and H16 now use
  one typed issue envelope, publisher, native-child batch surface, trust-gated resolver, and scheduled
  finalizer. Empty batches are not published; blocked capacity is distinct from no candidates; bodies are
  UTF-8 byte bounded with their full workflow artifact retained. H16 evidence children now record durable
  Confirm empty / Restore media overrides and unresolved evidence re-surface. The weekly tag tournament
  publishes one rolling ticket per verb at a strict `>60%` gate. A checked route selection opens a
  configuration-only PR; merge is the maintainer-selected approval gate, and retained-catalog selection
  starts a resumable bounded tag backfill. No output pipeline-version bump is introduced.

### Fixed

- **Debounced weekly review resolution with cohort-wide batch sweep.** Resolves an issue where
  review sub-issues (R5, H16, H15, R6, R7) were not closing when multiple checkboxes were checked
  in rapid succession. GitHub Actions concurrency queue collapsing (`cancel-in-progress: false`)
  previously dropped intermediate runs and individual runs only resolved `$EVENT_ISSUE`. The
  workflow now runs a dedicated `weekly-review-debounce` job that pends until review edits have
  ceased for 5 minutes (resetting whenever a new review edit arrives), followed by an uninterrupted
  `resolve` job that sweeps all open review children together, pre-filters issues with checked
  decisions, and finalizes cleared batches without mid-run cancellation risk.

- **Gemini direct AI Gateway routing and remedy workflow resilience.** Fixes 404 client errors when
  routing direct Gemini calls through Cloudflare AI Gateway by mapping Gemini's path prefix to
  `/v1beta` instead of `/v1beta/openai`, matching LiteLLM's native Google AI Studio adapter
  (`VertexLLM`) which calls `{api_base}/models/{model}:generateContent`. Preserves the
  `/v1beta/openai` base in `config/provider_limits.yml` and worker dispatch payloads for Cloudflare
  Workers compatibility. Broadens exception handling in `classify_unexpected_bodies` and
  `scripts/remedy_unexpected_bodies.py` to catch all inference exceptions, preventing workflow
  crashes and guaranteeing report generation on classification failures.

- **Airforce 429 retry-after guarantees are now authoritative in LLM Dispatch v2.** A parsed
  provider delay is no longer capped or jittered downward; it becomes a route-level `blocked_until`
  floor, including when it exceeds the current executor window or adaptive 429-buffer ceiling.
  Retried work may add only positive post-deadline jitter. This changes scheduling only: no recipe,
  artifact schema, pipeline version, or catalog backfill is involved.

- **Legacy v1 LLM completions can now be recovered without polling the Worker.** The temporary,
  manually dispatched recovery importer scans legacy R2 request records directly. It first joins
  exact v1 references still persisted by the resumable agenda-extraction and chapter-location
  stages, then reconstructs only unfinished agenda/locator prompts from their durable source bytes
  and accepts an R2 record only when one owner matches its exact normalized prompt and recorded
  response-schema shape. It validates the structured response before writing a completed B2
  deferred record, and reports direct and reconstructed candidates, unavailable inputs, pending,
  failed, invalid, unowned, many-owner, and many-record matches separately. It never guesses an
  owner, creates no B2 pending handles, calls no Worker endpoint, and retains R2 records for a
  later verified cleanup decision. This is a temporary migration aid for draining v1; no recipe,
  artifact schema,
  pipeline version, or backfill changes.

- **The temporary deferred sweep did not leave enough reaping time for legacy v1 handles.** The
  scheduled pass now runs for 90 minutes inside a 105-minute Actions timeout, retaining a
  15-minute teardown margin. This allows the B2 deferred registry's finished v1 entries to be
  verified and persisted after snapshot and maintenance work. It does not enumerate unregistered
  legacy R2 requests, alter dispatch rate, change any artifact schema, or trigger a backfill; the
  longer budget is temporary until the registered v1 queue drains.

- **Lifting the v1/v2 split-cap rate-limit multiplier to 1.0 (review/44 Phase 3 coexistence exit).**
  Following complete draining and purging of the legacy v1 LLM queue in Cloudflare R2
  (`citypods-llm-dispatch`), `split_cap_multiplier` in `config/provider_limits.yml` is restored from
  `0.50` back to `1.0`. All provider and route limits (RPM, RPD, TPM, monthly TPM) across Python and
  the Cloudflare Workers (`workers/llm-dispatch-v2`, `workers/llm-dispatch-proxy`,
  `citypods/compute/llm_routes.json`) are now unscaled to full 1.0x capacity.

- **The LLM deferred sweep could turn a failed v2 batch read into a singleton-poll storm.** An
  unresolved v2 handle fell through from the bulk poll into `reconcile()`, so a partial or failed
  recovery could make one Worker invocation per remaining job and consume the full four-hour sweep
  budget without reaping work. V2 now uses one initial and at most one recovery batch, then leaves
  unknown jobs for the next six-hour cadence; legacy queue-only capsules are submitted in bounded
  v2 batches as well. The sweep emits client-owned v1/v2 counts plus one v2 scheduler snapshot and
  runs for 30 minutes within a 40-minute Actions timeout. Both authenticated dispatch endpoints
  now require HTTPS, so a configuration error cannot send their bearer token over cleartext. V1
  remains on its existing temporary R2-backed reap path—no new ledger or endpoint. No artifact
  schema, recipe, pipeline version, or backfill behavior changes. A follow-up after the first
  bounded production run found that its initial registry snapshot still read every B2 record
  serially before the deadline check, so the sweep now emits a flushed snapshot-start event and
  loads at most 16 records concurrently. Snapshot reports include listed/loaded/omitted counts and
  stop admitting reads at the wall-clock deadline, leaving the untouched tail for the next cadence.

- **SambaNova's Free-tier routes were admitting an incorrect daily quota.** The two physical
  SambaNova routes now use the documented 20 RPM / 20 RPD source ceiling, with the existing
  v1/v2 split-cap compiling that to 10 RPM / 10 RPD per dispatcher while both transports coexist.
  The shared provider ledger also carries a 20 RPM safety ceiling, and SambaNova calls override
  AI Gateway's five-attempt retry series with one attempt so a known 429 is not immediately
  re-sent four more times. SambaNova's documented 200k TPD allowance is not modeled because the
  catalog has no daily-token field; this change therefore uses the conservative request cap rather
  than inventing a token parser. No pipeline version or artifact backfill is involved.

- **NVIDIA's per-model rate limit was generalised to every NVIDIA route, throttling the ones doing
  the work.** `deepseek-v4-pro`'s measured ~30/hr quota was applied as `rpm: 0.5` across all nine
  NVIDIA routes. NVIDIA's limits are per model, and `nemotron-3-super` has never returned a single
  429 — but it inherited one request per four minutes per Worker, and it was the route actually
  carrying Mistral Medium's overflow. Combined with `concurrency: 1` and nemotron's 127–140s median
  latency, throughput collapsed. Only `deepseek-v4-pro` keeps 0.5 rpm now; the rest return to 4.
- **The 402 backoff ladder could stop escalating, depending on the day of the week.** The rungs are
  calendar dates — tomorrow, next UTC Monday, start of next month — and those are not inherently
  ordered. On a Sunday "next Monday" *is* tomorrow, so a second 402 bought no additional cooldown;
  near a month end, "start of next month" can fall before next Monday, so a third 402 could regress
  below the second. The rungs are now combined into a strictly increasing ladder, each at least a
  day beyond the previous. Found because the Sunday collision failed the v2 suite on 2026-08-30.

- **Mistral Medium's overflow now prefers the model that was actually measured on the task.**
  `review/40`'s frozen-gold scoring (2026-07-31, 322 source-backed positives over 24 agendas) is the
  only evaluation of agenda-chapter extraction, and DeepSeek V4 **Flash** won it — F1 .734 against
  Mistral Medium's .643. The overflow chain nonetheless led with DeepSeek V4 **Pro**, chosen on a
  general "higher quality tier" argument that never referenced that evaluation; Pro has never been
  scored on this task, and neither has Nemotron 3 Super or Gemini 3.5 Flash Lite. Measurement has
  since undercut the tier argument as well: Pro runs ~42s median with multi-hour 429 lockouts, and
  Nemotron spends ~10k completion tokens per job, mostly hidden reasoning. Flash now leads (two free
  legs, OpenCode and NVIDIA), Pro drops to last, and the config records which orderings rest on
  evidence and which are merely operational.
- **The 429 route cooldown ceiling is four hours, up from thirty minutes.** NVIDIA's developer forum
  reports that each request made while blocked extends the lockout, so a 30-minute ceiling meant ~48
  probes a day, each potentially re-extending it — matching production, where `deepseek-v4-pro` sat
  pinned at the cap and still 429'd 5.5 hours in. The change is also the cheap test of that theory:
  if lockouts shorten once we probe roughly hourly, probing was the cause. Guessing high costs an
  idle route until the next probe, and any success clears the streak immediately; guessing low, if
  the theory holds, costs a lockout that never ends.

- **`llm-dispatch-v2` reset daily quotas on a rolling 24-hour window instead of the provider's
  calendar day, hiding whole models from the scheduler.** `reset_timezone` appeared only in v2's
  compiled catalog and in no v2 source file — v1 has `zonedDateKey`/`routeResetTimezone`, v2 had
  neither. v2 anchored `rpd` at the first request of a window and only refreshed 24 hours later, so
  a route exhausted at (say) 14:00 local stayed exhausted until 14:00 the *next* day, well past the
  provider's real reset. Gemini's free tier rolls at midnight `America/Los_Angeles`, giving up to
  ~14 hours of needless starvation. The consequence is worse than slowness: `_capacityFraction`
  scores an exhausted route 0, and `_rankModelsByCapacity` drops any model whose weighted score is
  0, so an affected model disappears from the ranking entirely and its queued jobs go unclaimed.
  **28 routes carry an `rpd`**, 14 of them Gemini/Gemma on Pacific time. Daily accounting is now
  keyed on `zonedDateKey(now, reset_timezone)` across all three places that touch it — the
  reservation stamp, the capacity score, and `earliestSafeStart`'s readiness — with a new
  `rpd_day_key` column and the usual `_ensureColumn` migration. `rpd <= 0` keeps its pre-existing
  pacing meaning rather than being quietly redefined while fixing timezones.

- **Route rates below one request per minute are now expressible, and NVIDIA is paced to its actual
  per-model quota.** Three separate places clamped a fractional rate *up* to 1 — `_scale_rate_limit`
  (`0.25 x 0.5` became `1`, four times the configured rate), `routeAvailable`'s per-minute bucket,
  and the v1 test harness's split-cap un-scaling. The middle one was the dangerous one: a route
  paced below 1 rpm was refused its first request on a fresh ledger (`0 + 1 > 0.25`), and because
  that refusal is what prevents `requests_available_at` from ever being written, the route would
  never admit anything again — silently dead rather than slow. Recompiling the existing catalog
  changed **zero** routes, so the change only enables new sub-1 configurations.
  **NVIDIA's quota is per model, not per account** — verified on one key by interleaved calls:
  `deepseek-v4-pro` returned 429 nine times out of nine while `nemotron-3-super` returned 200 three
  times, twice inside the same minute. Each route is therefore paced at `0.25 rpm` (15/hr per model,
  against observed lockout onsets at 28 and 37 successes/hr), with the provider-wide cap raised to
  6 so it stays a safety net rather than making nine models share one model's budget. This costs no
  throughput — the quota caps us either way — it spends the budget evenly instead of burning it in
  half an hour and then sitting locked out for 22+ minutes.
- **A 402/429 route block could stay stuck forever when two dispatches landed in the same
  millisecond.** The stale-guard compared `requestStartMs > blockedAtMs`, so a success starting in
  the same millisecond as the rejection was treated as predating it and never cleared the block.
  Now `>=`: a call starting in that millisecond did not begin *before* the rejection.

- **A 429 now stands the whole route down, not just the job that hit it** (`llm-dispatch-proxy`).
  Retrying only the job is not enough against a provider that enforces a quota *window*: it answers
  every request with 429 until the window rolls, so each remaining queued job was still admitted at
  the route's normal rpm and rejected in turn. NVIDIA on 2026-08-29 showed the shape exactly — two
  lockouts of 26.1 and 27.0 minutes absorbing 54 and 47 **consecutive** 429s, with no successes
  interleaved, roughly 50 wasted dispatches per cycle that could not have succeeded. The route now
  takes an escalating cooldown (`throttled_until`), honouring `Retry-After` when the provider sends
  one and otherwise doubling from a minute to a half-hour cap, converging on a ~26-minute lockout in
  about six occurrences rather than fifty. A success clears it, subject to the same stale-guard as
  the 402 block: only a call that *began* after the 429 proves recovery. Both cooldowns are
  measured from the instant the rejection arrived rather than from the batch's start time —
  the latter shortened the window by the upstream call's duration and stamped the stale-guard
  early enough that a sibling starting mid-batch could still clear a cooldown it never saw,
  which also corrects the 402 guard shipped moments earlier. `Retry-After` is parsed in both
  RFC 9110 forms, so an HTTP-date deadline is honoured instead of silently discarded. `llm-dispatch-v2` already
  had an equivalent via `throttle_streak`/`buffer_seconds`, so this closes the gap between them.
  Also corrects a comment that claimed 429 was "already handled by requests_available_at/Retry-After"
  — it was not; `Retry-After` only ever fed the per-job retry delay.

- **A 402 requeue in `llm-dispatch-v2` would have stranded the job permanently.** Claiming a job
  deletes its `job_models` index rows, and the requeue introduced above fell through to a generic
  `UPDATE jobs SET state=...`, leaving the job `queued` while still holding a stale lease and with
  no index row — `claimDispatchWindow` could never select it again, and v1's operator requeue
  script only reads R2, so it would have been invisible there too. It now takes the same branch as
  a durable 5xx retry (clearing the lease and re-indexing) and shares the 5xx retry budget, so a
  route that stays 402 across every cooldown cannot requeue a job forever. Caught in review before
  reaching production.
- **A stale success could reopen a 402-blocked route.** Without a per-route concurrency cap a batch
  runs several requests on one route, and a sibling that *started before* a 402 landed would clear
  the block on completion, proving nothing about the account's balance. The block now records when
  it was set, and only a call that began after that clears it.

- **NVIDIA 429s were a concurrency problem, not a rate problem.** Roughly 57% of post-fix NVIDIA
  calls came back 429 while we were averaging just **1.15 requests/min** — some 35x under NVIDIA's
  ~40 RPM community-reported free baseline — and burning ~1,865 tokens/min against a 100,000 TPM
  cap. Neither limit was binding. What binds is overlap: successful calls run **41s median, 77s
  p90, 399s max**, while 429s return fast and tightly clustered at 8.3–9.1s, the signature of an
  admission rejection rather than a rate window. With v1 at `BATCH_CONCURRENCY: 2` and v2 at
  `MAX_JOBS_PER_ROUTE_PER_BUNDLE: 4`, up to six 41-second calls could pile onto one model. Every
  NVIDIA route now sets `concurrency: 1` (the knob both Workers already honour, as OpenRouter's
  free legs do), with `rpm` 12 → 4 and `tpm` 100000 → 40000 as secondary guards that align the
  config with what the dispatchers could ever exercise. Note the ceiling is per-Worker-ledger, so
  v1 and v2 each keep one in flight while both transports are live — the same coexistence caveat as
  `split_cap_multiplier`.

- **`llm-dispatch-v2` billed for work a free route could have taken.** Its route comparator ranked
  purely by available capacity, with `free` appearing nowhere except the hard exclusion applied when
  a job does *not* set `allow_paid`. So whenever a job did allow paid, a paid route with more
  headroom outranked a partly-consumed free one — the opposite of v1, which tries every free route
  first and elevates to paid only when no free route exists or waiting for one would miss the job's
  deadline. Measured on a mixed pool, 3 of 4 jobs went to the paid route while a free route had
  capacity. Free now sorts ahead of paid before any capacity signal, so paid is reached only once
  every free route is exhausted. Rows read did not regress (35 → 31 on the same probe; the fix adds
  no SQL, only reordering iteration over the already-cached per-window route ledger).

- **A 402 no longer consumes the job that discovered the exhausted budget** (both dispatch
  Workers). The escalating 402 backoff blocks the whole route, but the triggering job was still
  marked terminally `failed` — so every cooldown lapse burned exactly one recoverable job per
  route, recoverable only by an operator requeue. Production showed the pattern precisely: 2
  failures per Mistral route on 2026-08-26, matching `payment_required_streak=2` in the ledger,
  versus 1,260 on 2026-08-22 before the backoff existed. A 402 is a route-level budget signal, not
  a defect in the job, and the dispatcher already has enough information to say so — it blocks the
  route in the same branch. The job is now requeued instead: admission keeps it off the blocked
  route, so it runs on an overflow route or once the cooldown clears, still bounded by `attempts`.

- **Requeued 971 dispatch jobs stranded by the AI Gateway routing bug, and gave the recovery script
  an `--error-status` filter.** The gateway 404s left `mistral/mistral-medium-2508` jobs terminally
  `failed` — 404 is in neither dispatch Worker's `retryableStatus` set and gets no `blocked_until`,
  so each one hard-failed on its first attempt with no failover and was never revisited. They sat
  alongside 1,264 records of the *same logical model* failed with 402 payment-required, which
  `requeue_failed_llm_dispatch.py` could not tell apart: it selected on model prefix alone, so
  recovering the routing failures would have re-submitted the payment failures too — re-failing all
  of them and, since the 2026-08-25 402 backoff, driving an escalating route-wide `blocked_until`
  that keeps other jobs off Mistral as well. The new filter selects on the terminal error's upstream
  status; a dry run and an independent classification pass agreed on 971 exactly, and the apply
  moved `failed` 2,243 → 1,272 with zero conflicts.

- **`workers/llm-provider-shim`: a thin shim for providers AI Gateway cannot address.** The gateway
  rewrites the *last path segment* of a Custom Provider's Base URL to a hardcoded `v1` (undocumented;
  established by registering a throwaway custom provider against an echo service). Kilo's
  `/api/gateway` became `/api/v1` and z.ai's `/api/paas/**v4**` became `/api/paas/**v1**` — each
  reproducing, on a direct curl, the exact 404 the gateway returned. Kilo is fixable by registering
  `https://api.kilo.ai/api/gateway/v1` (it serves that path); z.ai is not expressible under the rule
  at all, so the shim restores its real prefix and keeps it inside AI Gateway's logging instead of
  bypassing the gateway. The shim pins its destinations to an allowlist, fails closed without its
  secret, and forwards only `authorization`/`content-type`/`accept`, because it relays third-party
  API keys. OpenCode is routed through it as well, for a cause never identified from outside: its gateway
  URL was already correct and returned a real 400 directly, yet 404'd through the gateway, and
  replaying the gateway's full header set directly did not reproduce it. The shim resolves it,
  which also disproved the leading theory that opencode.ai rejects Cloudflare-edge traffic.
  Verified 2026-08-29 end to end through the gateway: z.ai 200, Kilo 200, OpenCode a genuine
  upstream 429 (free-tier quota) in place of the routing 404 — and unlike 404, 429 *is* in
  `retryableStatus`, so it fails over properly.
- **Live contract tests for the gateway's undocumented URL join** (`tests/live/`, `pytest -m live`,
  wired into the weekly `contracts.yml`). The deviation lives in Cloudflare's edge, so no offline
  test can see it: these assert every custom provider's configured URL actually reaches its
  provider API (rejecting routing-404 fingerprints, empty-body 404s, and Cloudflare edge blocks
  that would otherwise pass as ordinary 4xx), plus a canary asserting NVIDIA's bare path still
  fails — if it ever starts working, Cloudflare changed the join and the compensating prefixes
  have become double-prefixes.

- **Cloudflare AI Gateway dropped the Base URL path for Custom Providers, 404-ing every NVIDIA and
  SambaNova route.** The gateway joins the caller-supplied path at the provider's *origin root*,
  discarding the path component of the registered Base URL — the opposite of what
  [its documentation](https://developers.cloudflare.com/ai-gateway/configuration/custom-providers/)
  describes (`{base_url}/{provider-path}`). NVIDIA's routes were therefore dispatching to
  `https://integrate.api.nvidia.com/chat/completions` and receiving an empty-body `text/plain` 404
  from NVIDIA's AWS load balancer; SambaNova's were hitting `https://api.sambanova.ai/chat/completions`
  and receiving a plain-text `404 page not found`. Because 404 is not in either dispatch Worker's
  `retryableStatus` set, these hard-failed with no failover. Fixed by carrying the base path in each
  provider's `ai_gateway_chat_path` (`/v1/chat/completions`), verified live against the gateway:
  all nine NVIDIA route models returned HTTP 200 completions, and SambaNova returned a genuine
  upstream 429 in place of a routing 404. A new guard test asserts the invariant for every
  `custom-*` route so a provider added with a root-relative path fails in CI rather than in
  production.
- **Re-enabled `nvidia_deepseek_v4_pro_0813_free` and `nvidia_deepseek_v4_flash_0731_free`,
  retiring a misdiagnosis.** Both were disabled on the theory that NVIDIA gated them behind a
  per-key "Public API Endpoints" entitlement. They were failing for the Base-URL-path reason above,
  and both return HTTP 200 with the path corrected — no entitlement request was needed or filed.
  `deepseek/deepseek-v4-flash` is back to four provider legs.

- **NVIDIA build DeepSeek routes disabled: both 404, root cause not a naming bug.** Both
  `nvidia_deepseek_v4_pro_0813_free` and `nvidia_deepseek_v4_flash_0731_free` return 404 with no
  JSON body. An initial fix changed the pro route's `upstream_model` from
  `deepseek-ai/deepseek-v4-pro-0813` to `deepseek-ai/deepseek-v4-pro`, reasoning the `-0813` suffix
  was a marketing label rather than the API identifier; that reasoning was wrong -- NVIDIA's own
  official sample code for this model uses the `-0813`-suffixed string verbatim, confirmed against
  the real failing request payload, and reverted. Both routes use their correct, docs-matching
  model strings and the correct `https://integrate.api.nvidia.com/v1/chat/completions` URL
  (confirmed against NVIDIA's own OpenAI-SDK sample), yet both still 404. This matches NVIDIA
  Developer Forum reports from multiple unrelated developers of the identical symptom on these
  exact models (`GET /v1/models` lists them, the playground works, `POST /v1/chat/completions`
  404s "Function not found for account" before reaching a serving container) -- most likely
  explanation: NVIDIA gates some models behind a "Public API Endpoints" entitlement that must be
  explicitly requested/granted per API key (see NVIDIA Developer Forum threads "Request Public API
  Endpoints access for my API key" and "Request for access to DeepSeek V4 Pro NIM public API
  endpoint"), which this account's key likely hasn't been granted for these two models yet. Since
  404 gets no automatic block/retry treatment in either dispatch Worker the way a real 429 does,
  both routes are commented out (not just documented) rather than left live to hard-fail every
  attempt.
  `deepseek/deepseek-v4-flash` still has three working legs (SiliconFlow, DeepSeek Direct,
  OpenCode); `deepseek/deepseek-v4-pro`'s only route while this is disabled is DeepSeek Direct's
  paid leg, so Mistral Medium's `model_routing` overflow onto it is not free right now.
- **R7 pilot scope and timed-word artifact integrity (GH#1274).** Denton’s native diarization pilot
  now matches the provider’s explicit `City Council` label family instead of requiring a raw body
  string that Swagit does not persist. Transcript, provider-alignment, native-provider, and external
  worker adoption paths now inspect word-sidecar JSON and require at least one finite, positive-length
  timed word; invalid sidecars are re-routed to alignment or fresh ASR, and fresh outputs cannot be
  marked complete until validation succeeds. Dispatch lease reconciliation uses the same check, so an
  empty `.words.json` cannot settle work as done. Existing valid transcript/ASR bytes are reused; the
  validation fingerprint causes a gradual normal-run audit, and only recordings with missing or invalid
  word timing are regenerated. No output schema or ASR pipeline-version bump is introduced.

- **R7 sidecar review hardening.** Malformed timed-word rows now reject boolean, null, and
  overflowing timestamps without raising; joint and section-only City Council labels stay outside
  the pilot; a provider-linked episode cannot reuse an invalid ASR sidecar; and repeated external
  ASR outputs without usable timed words use a persisted exponential cooldown. The roadmap index and
  regression coverage now record these completion semantics. No output schema or ASR pipeline-version
  bump is introduced.

- **LLM dispatch V2 mixed-result recovery (GH#1318).** Enqueue and poll batches now preserve
  accepted, replayed, pending, completed, rejected, and failed per-job outcomes instead of
  converting one item into a whole-batch failure. Unknown outcomes use one bounded recovery round:
  sets larger than five are retried as a batch, while smaller sets use isolated requests. The
  deferred sweep originally applied the same threshold. Failed poll chunks remain isolated so later
  chunks still run; retry diagnostics omit response bodies. Payload-free structured counters expose
  batch, retry, singleton, and schema-correction request counts. The later batch-only sweep fix
  above supersedes its small-set singleton fallback. No artifact schema, recipe, pipeline version,
  or backfill behavior changes.

- **Unexpected-body remediation evidence collection.** The remediation workflow now passes its
  GitHub token to the audit's issue-reconciliation read and runs that collection in dry-run mode;
  this prevents step 7 from failing with an unset `GH_TOKEN` and prevents evidence collection from
  mutating issues or dispatching a second remediation run.

- **Blank optional R2 endpoint handling.** Storage helpers and the R2 maintenance scripts now use
  the standard account endpoint when `R2_ENDPOINT` is unset, empty, or whitespace-only, while
  preserving explicit jurisdiction-specific endpoint overrides.

- **Observable exhausted storage reads.** S3-compatible object reads now preserve the affected key
  and original cause when bounded transient retries are exhausted. Deferred LLM snapshot and index
  repair paths report and skip only unavailable objects, retain existing pointers during uncertain
  repair, leave migration incomplete until every canonical read succeeds, and continue reconciling
  independent records; downloads use unique per-call staging files; strict reads still surface
  authentication, configuration, and other non-transient errors.
  
- **LLM dispatch V2 deferred schema corrections and moments reconciliation.** The standalone
  deferred sweep now registers the `moment-extraction` response contract. It also stages one
  corrected v2 payload and submits it through a durable schema-retry endpoint that clones the
  completed job's routing policy into a new idempotency namespace, then consumption-acks the
  malformed source after the replacement record and correction marker are persisted. This replaces
  the v1-only correction path that produced schema-correction failures for v2 jobs and leaves failed
  corrections retryable; uncertain HTTP failures retain the deterministic staged payload for a
  safe idempotent retry.

- **Unexpected-body remediation now catches new table rows automatically.** An audit run dispatches
  the remediation workflow when it creates the consolidated issue or adds a newly affected feed
  row to an existing one. Cosmetic refreshes and changed detail on an existing row do not re-run
  it; `/remedy` remains available for an explicit retry. The rolling consolidated issue is
  preserved, avoiding duplicate tickets for the same feed-health check.

- **Unexpected-body issue guidance now exposes the catch-up command.** Consolidated issues explain
  that a merged feed-config PR does not close the issue until a later audit observes zero current
  unmatched rows. The existing `/remedy` command is documented for explicit retries, and the
  recovery path is discoverable in the affected-row tables.

- **LLM dispatch V2 consumption ack, parallel result resolution, and a 6-hourly sweep.** A v2 job's
  DO row and B2 objects were held for `COMPLETED_RETENTION_DAYS` (38) after completion even though
  the client had fetched, validated and durably persisted the result minutes later --
  `delete_dispatched_ref` is a v1-only path and v2 had no equivalent, so nothing communicated
  consumption. `POST /v2/jobs:ack-batch` now does, called once per poll chunk (not per job) after
  `write_deferred` succeeds, reducing post-ack retention to roughly one hourly cleanup tick --
  completion-to-release still spans the six-hour observation cadence for a job the sweep hasn't
  yet polled. A result that failed structured-output validation remains unacked while the sweep's
  schema-correction path re-reads it, then is acked after the corrective clone is accepted. A failed
  ack is harmless -- the result is already durable client-side -- and never fails the poll.

  Fixed a latent stranding bug found while wiring this up: `purgePendingBatch` selected only
  `completed`/`failed`, so a row already in `purge_pending` was never re-listed. A cleanup run that
  died between the B2 deletes and `confirmPurge` therefore orphaned its B2 objects permanently,
  contradicting the method's own documented idempotency contract. It now re-lists carried-over rows
  first, then tops up to the limit with newly-eligible ones.

  `poll_batch` resolves completed results through a bounded thread pool instead of serially. Each
  completion costs several sequential B2 round trips (the result GET plus `write_deferred`'s own
  reads and writes) which are pure I/O wait, so the sweep's runtime scaled with the completion
  count; measured ~7.9x faster at 16 completions. The deferred sweep moves from once daily to every
  six hours, keeping the 17:30 UTC DeepSeek off-peak run exactly -- a job completed minutes after
  dispatch was previously only *observed* up to ~23h later, which kept both the deferred registry
  and the coordinator's `jobs` table near their maximum between runs.

- **LLM dispatch V2 Durable Object rows-read overage.** `claimDispatchWindow` full-scanned the
  `bundles` table twice on every cron tick -- once for the lease-expiry sweep, once for the
  active-bundle count -- because `bundles` carried only its `bundle_id` primary key and nothing in
  the codebase ever deleted from it. Instrumenting the real coordinator measured **6,400 of a
  tick's 6,424 rows read (99.6%)** from those two statements at 3,200 accumulated bundles, which
  exhausted the Workers free tier's 5M daily Durable Objects rows-read budget on 2026-08-27; the
  cost grew with every bundle ever claimed, which is why reads climbed steadily while writes
  stayed flat. Adds `bundles (state, created_at)`, turning both into index seeks over the
  `active` rows only -- a population `MAX_ACTIVE_BUNDLES` already bounds. Same tick, same table
  sizes: **6,424 -> 30 rows read**.

  Also adds retention for the two append-only bookkeeping tables that had none. `bundles` and
  `attempts` have no B2 counterpart and no client-side reader, so both now age out inside the DO
  (`BUNDLE_RETENTION_DAYS`/`ATTEMPT_RETENTION_DAYS`, default 7) on a bounded per-tick delete
  budget; a bundle is only ever removed once terminal, past retention, and past its lease, so no
  late `completeBatch` can lose one it could still settle. `attempts` gains a `created_at` index
  so that pruning is itself a bounded seek rather than the scan it exists to prevent.

  Finally, wires up `purgePendingBatch`/`confirmPurge`. Both shipped with Phase 2 with tests but
  were called from nothing, so terminal `jobs` rows and their B2 payload/result objects were never
  released. They now run from `scheduled()` on an hourly cadence (`CLEANUP_INTERVAL_MINUTES`),
  deleting B2 objects before confirming the row drop so a crash mid-way simply retries. This
  required a second new index, `jobs (state, updated_at)`: the existing
  `(state, priority, created_at)` index could not serve the purge query's age filter, so wiring it
  up naively would have read every completed job on each run (**60,189 -> 367** VDBE ops at 6,000
  terminal jobs) -- reintroducing the same unbounded-scan shape.

  Adds `test/rows-read.test.js`, a standing guard against this whole bug class rather than
  against these four queries. It exercises every RPC entry point and asserts two invariants: no
  statement may full-scan a table that grows with traffic, and the same operations against 10x
  the accumulated history must read about the same number of rows. The second catches what the
  first cannot -- an index seek constrained only on a low-cardinality `state` column still walks
  every row in that state, which is exactly how the purge-query trap above hides behind a plan
  that reads as `SEARCH`. Verified by mutation testing: removing any of the four indexes, or
  reverting the candidate lookup to a queue-head scan, fails at least one invariant.

- **LLM dispatch V2 rollout compatibility migrations retired.** Both the `job_models` backfill and
  legacy `retryable` recovery migrations (below) completed in production -- confirmed 2026-08-28 via
  a Data Studio query against the live coordinator showing both `scheduler.*_complete` flags at `1`
  -- so their code, config knobs (`MAX_QUEUED_JOB_MODEL_BACKFILL_PER_CLAIM`,
  `MAX_LEGACY_RETRYABLE_RECOVERY_PER_CLAIM`, `MAX_MIGRATION_WRITE_UNITS_PER_UTC_DAY`,
  `MAX_MIGRATION_ROWS_SCANNED_PER_UTC_DAY`), and per-tick state threading were removed from
  `coordinator.js`/`index.js`/`wrangler.jsonc`, along with the tests that only existed to exercise
  them. The `scheduler` columns those migrations wrote are left in place on already-migrated
  instances rather than dropped -- an unused column on a single-row table costs nothing, and
  `ALTER TABLE ... DROP COLUMN` against live production data is an unnecessary risk for zero
  benefit; a fresh DO instance's schema simply no longer declares them. See review/44's "Retired:
  the rollout compatibility migrations".

- **LLM dispatch V2 linear, budgeted compatibility migrations.** Historical queued-job model
  indexing now captures a rollout-time SQLite row-id high-water mark and advances a durable cursor,
  so it reads the finite old population once instead of repeatedly searching the queue head. Legacy
  `retryable` recovery follows the existing state/priority/created index. The migrations share
  conservative daily row-read and write-unit budgets (`250,000` and `5,000`) and cautious per-cron
  caps (four backfill rows, one recovery row); a zero cap or daily budget remains an emergency
  pause. A job whose mappings exceed a daily budget resumes from a durable model offset; partial
  backfill jobs remain out of admission and a `retryable` job is queued only after all mappings
  exist. Each job retains every explicitly allowed model -- there is no allowed-model cap. This is
  scheduler-only: no model fallback, pipeline version, stored artifact, or record format changes.

- **LLM dispatch final-5xx recovery.** AI Gateway now performs the configured short retry series
  before either Worker sees a final response. V1 retains a final 500/502/503/504 as one durable
  pending retry (rather than failing on its former one-attempt production setting). V2 now returns
  a final 5xx to the indexed queue for a later cron, with one bounded outer retry and a short route
  cooldown so other models/accounts can continue draining. A final 5xx can no longer leave
  a V2 record forever in the previously unclaimable `retryable` state: each cron recovers a
  bounded batch of such historical rows, while the V1 offline reindexer has an explicit
  `--recover-retryable` recovery mode. Timeout, transport, and result-write ambiguity is surfaced
  as `failed`, not silently parked. No model fallback, pipeline version, or stored artifact is
  changed.

- **V2 dispatch admission is capacity-ranked, rather than globally FIFO.** The scheduler DO keeps
  an indexed queued-model membership for every explicitly allowed model on each job, ranks model
  pools by their routes' live free capacity, treats 402-blocked, paused, and capacity-exhausted
  routes as zero, and reads only four candidates from a model before trying the next pool. The
  exact per-job token and lane gate remains authoritative. This improves use of independent
  free-tier capacity without adding V2 model routing: V2 still does not use the V1 Mistral →
  Gemini 3.5 Flash Lite overflow fallback. A job's index membership omits any model whose every
  configured route is structurally too small for its own token estimate, so a handful of
  oversized jobs can no longer occupy a model's bounded candidate window forever and starve
  smaller jobs behind them; a SQLite trigger keeps the index's admission-order priority in sync
  with a direct `jobs.priority` recovery edit; and the one-time pre-index backfill cursor latches
  off after its fixed rollout population, rather than re-scanning the queued backlog on every cron
  tick.

- **Pipeline and GitHub Actions reliability hardening.**
  - **Runner timeout bounds:** Reduced `chapter-locator.yml` timeout to 45 minutes to prevent
    hung runner orphan processes from holding runner quota.
  - **Review issue overflow protection:** Added a 60,000-character ceiling guard in
    `llm_evaluation.py` and `llm-tag-review.yml` before creating/updating GitHub review digest
    issues, preventing GraphQL `Body is too long` failures.
  - **Granicus media probe fallback:** Enabled Cloudflare Worker chunked fallback with an 8 MB
    cap for truncated probe fetches in `media.py` when Granicus CDN returns HTTP 403 to ffmpeg.
  - **Auxiliary discovery error handling:** Caught upstream LLM provider exceptions (quota, auth,
    rate limits) in `scripts/city_discovery.py` to return `DEFERRED_EXIT` (75) and added fallback
    provider secrets to `city-discovery.yml`.
  - **ASR quality webhook filtering:** Filtered issue webhook events to `"H15 sample "` in
    `asr-quality-ingest.yml` and gracefully handled missing metadata in `transcript_quality.py`.
  - **Diarization setup ordering:** Placed FFmpeg shared library installation before python
    dependencies in `r7-diarization.yml`.
  - **LLM deferred sweep fault tolerance:** Wrapped individual record reads in `llm_deferred.py`
    so transient storage retries don't abort entire snapshot sweeps.


- **R7 diarization model-access diagnostics.** The preflight now verifies `HF_TOKEN` before
  loading pyannote and reports invalid credentials, unaccepted gated-model terms, unavailable
  configured models, Hub availability, and post-access pyannote runtime failures separately. This
  changes no diarization recipe, artifact schema, or `DIARIZE_PIPELINE_VERSION`; no backfill is
  triggered.

- **R7 diarization runner provisioning.** Explicitly install FFmpeg's shared runtime before the
  pyannote preflight. Pyannote 4 decodes through torchcodec, which needs the dynamically loaded
  `libav*` libraries rather than merely an `ffmpeg` executable. This does not bump
  `DIARIZE_PIPELINE_VERSION`: the shadow pilot failed during runtime preflight, so it produced no
  stored diarization artifacts that need invalidation or backfill.

- **Dynamic `model_routing` expansion and bounded requeues in `workers/llm-dispatch-proxy`.**
  `selectRoute` and `nextCapacityRetryAt` now dynamically expand `model_routing` at dispatch
  time, matching the Python scheduler and ensuring resident R2 records enqueued before an
  overflow route was configured (e.g. Mistral Medium → Gemini 3.5 Flash Lite) immediately
  benefit from overflow capacity without requiring record migration. In addition, `dispatchBatch`
  now defers `no_capacity` heads in memory during the candidate preparation loop and relocates at
  most one blocked head per idle tick, eliminating multi-minute sequential R2 write loops and
  preventing `exceededCpu` runtime terminations under the Workers Free 10 ms CPU budget.

### Added

- **R9 runtime and dependency maintenance automation (Shipped).** Implemented the full
  dependency-pinning and automated maintenance policy from `review/22`. Synchronized
  `.github/renovate.json5` with complete two-lane rule coverage across all Python runtime,
  hygiene, output-affecting packages, and Cloudflare Worker manifests. Added a static CI
  guard (`scripts/check_dependency_policy.py`) that fails if any declared `pyproject.toml`
  dependency escapes Renovate package rule classification, and expanded `ci.yml` to execute
  test suites across all 5 Cloudflare Workers (`granicus-media-proxy`, `swagit-list-proxy`,
  `llm-dispatch-proxy`, `llm-dispatch-v2`, `city-request-intake`).


- **R7 calibrated speaker attribution (in progress).** Added pyannote-backed native speaker-turn
  artifacts, private city/body membership and golden-voice registries, the 30-day/30-review/95%-precision
  public-identity gate, minutes-backed silent correction, R6 single-speaker pull-quote attribution, and
  static speaker pages. Diarization/version backfills are gradual: only episodes with an eligible retained
  transcript/audio artifact are reconsidered; audio and transcript bytes are never regenerated.
  The pilot now serializes its private state per city/source, uses a recipe-specific measured runtime
  profile, scopes profiles/calibration to the active embedding and explicit capture context, requires
  a recorded private benchmark decision before public naming, and confirms valid roster-backed names.
  A bounded weekly GitHub-review issue workflow now harvests authenticated Correct/Incorrect shadow
  labels into the private calibration ledger; the pyannote-vs-WeSpeaker gold-set comparator remains an
  explicit offline run because the gold references are private. The same weekly parent/sub-issue
  batch now reviews conservative transcript cues such as “Commissioner X” and “Council Member X”
  as possible golden voice references without assigning names automatically. The Denton pilot now has
  a scheduled/manual `r7-diarization.yml` lane that runs native diarization followed by identity
  projection, with a shared `HF_TOKEN` model-access preflight and cached Community-1 runtime. The
  diarization model recipe is intentionally changed content-addressably, so existing artifacts are
  retained and reprocessed gradually by the recurring lane rather than invalidated in one backfill.

- **R6 calibrated moments and shareable clips.** Added council-only free Gemini 3.6/3.5 routing,
  immutable Good/Borderline/Reject review records, background independent judges, deterministic
  admission gates, RSS soundbites, and grounded captioned MP4 clips. Clips preserve source audio,
  use a versioned face/mouth-motion speaker crop when confident, and otherwise retain a safe group
  composition. The dedicated daily R6 lane carries the v2 dispatch credentials and batches its
  shared 40-job extraction/judge allowance into one bounded ingress request; existing records stay
  unchanged until normal staged processing reaches them.

- **Run-batched v2 topic-tag and chapter dispatch (review/44 Phase 4).**
  `BatchingDispatchBackend` collects new `queue_only` tagger/prelabeler jobs across the tag lane's
  concurrent per-episode work, including recursively split chapter windows, and now also collects
  the chapter-agenda and chapter-locator lanes' jobs across their full global queues. Each chapter
  stage replays only its accepted jobs after the bounded 1,000-job `enqueue_batch`/`poll_batch`
  flush, retaining its established artifact finalizer and real durable reference; a failed batch
  submission is recorded on that episode and remains retryable next run. `poll_batch` partitions
  all v2 status requests at the same 1,000-handle limit, and the deferred sweep no longer follows
  a successful bulk poll with one singleton poll for every still-pending v2 handle. Cached results,
  direct/v1 calls, and terminal-error recovery remain unchanged. No recipe, candidate/artifact
  schema, pipeline version, or backfill behavior changes.

- **Cross-model overflow routing (`model_routing`) and a Mistral Medium → Gemini 3.5 Flash Lite
  route.** Added an optional `model_routing` map to `config/provider_limits.yml`: a job pinned to
  one model (`allowed_models=(source,)`) becomes eligible for its configured target model(s) too,
  once the source's own routes are exhausted or paused — extra daily capacity for a job without
  editing the job or caller. Compiled into both `citypods/compute/llm_routes.json` and
  `workers/llm-dispatch-proxy/src/dispatch_limits.json`; the Python scheduler
  (`llm_scheduler.select_route`) and the `llm-dispatch-proxy` Worker
  (`normalizeChatRequest`) both expand a request's allowed models the same way, so it applies
  uniformly across transports. Ties still prefer the caller's own requested model over an
  overflow target when both are equally eligible. Routed Mistral Medium (2508/2505) to Gemini 3.5
  Flash Lite's independent free-tier pool as the first use, to help drain the queued-request
  backlog built up during Mistral's monthly-quota pause (see the secondary-capacity entry above)
  without touching the queued jobs themselves.

- **Explicit LLM alternates now precede config-injected overflow routes.** The v1 dispatch Worker
  now keeps every caller-supplied `allowed_models` entry ahead of models added by `model_routing`.
  Previously, expansion interleaved Mistral Medium's Gemini overflow before its explicit Llama
  3.3 70B peer, so queued agenda extraction always selected Gemini whenever it had capacity and
  never reached SambaNova. This is a dispatch-selection fix only: no pipeline version changes and
  already-queued durable requests pick up the corrected ordering dynamically on their next tick.

- **V2 dispatch now separates job discovery from route choice.** Capacity-ranked model indexes
  remain the bounded mechanism for finding queued work, but after a job is found the Durable
  Object ranks every route explicitly allowed by that job. Previously the alphabetically first
  tied model pool (commonly Gemini) both found and claimed the job, so later Llama/SambaNova peers
  were never examined even when a cron tick returned fewer than four jobs. Equal-capacity routes
  now retain the caller's `allowed_models` order. Existing queued jobs adopt the fix on their next
  claim; there is no pipeline-version change or artifact backfill.

- **Secondary Mistral dispatch capacity and deeper v1 queue lookahead.** Added independent
  secondary-account routes for every native Mistral model, using `MISTRAL_API_KEY_SECONDARY` and
  the same RPM/TPM limits each primary route had before its temporary `rpd: 0` quota pause. The
  primary routes remain paused. Increased v1's ready-marker lookahead from 16 to 500 so a run of
  jobs blocked on the primary account is less likely to hide work eligible for another route.

- **Phase 2 of bounded bundled LLM dispatch (review/44): DO-driven paced dispatch, implemented in
  [PR #1254](https://github.com/BashfulBits/city-meeting-podcasts/pull/1254).** Brings
  `workers/llm-dispatch-v2/` online as a real dispatcher, draining jobs Phase 1 ingests. Adds to
  `src/coordinator.js`: `claimDispatchWindow` (fenced admission — bundle/active-bundle/in-flight/
  daily caps, priority-then-distinct-route-then-aging-then-size ordering, capped at
  `MAX_CONCURRENT_ROUTE_LANES` concurrent lanes), `attemptStarted`/`authorizeRetry` (fenced attempt
  tracking and bounded 429 retry authorization), `completeBatch` (settlement with calibration
  folded in — margin_tokens only ever increases), and bounded B2 cleanup RPCs
  (`purgePendingBatch`/`confirmPurge`/`confirmNeverAccepted`). Adds a route-capacity/pacing model
  in the new `src/pacing.js` (fixed-window RPM/RPD counters plus a refillable TPM token bucket,
  independently verified by 16 pure-function tests) and route-catalog selection in the new
  `src/routes.js`, both extracted from `workers/llm-dispatch-proxy`'s existing patterns per Phase
  1's own instruction to reuse rather than fork provider logic. Implements the `scheduled()`
  executor in `src/index.js`: claims one paced window per cron tick, runs each route lane
  independently and just-in-time (no B2 access before a job's own `wait_ms` elapses), and reports
  every attempt in one `completeBatch` call. Adds a from-scratch AWS SigV4 client
  (`src/b2.js`, independently cross-verified against a separate Node-crypto re-derivation of the
  same signatures) for the executor's B2 reads/writes — the ingress Worker still never touches B2.
  Adds `src/gateway.js` for AI Gateway request construction, adapted from
  `workers/llm-dispatch-proxy`'s `resolveProviderCredentials`/`upstreamRequestForRoute`.
  `scripts/compile_llm_limits.py` now also compiles `workers/llm-dispatch-v2/src/dispatch_limits.json`
  (same catalog shape v1 already gets, kept in sync automatically). The per-minute cron trigger is
  back in `wrangler.jsonc` now that `scheduled()` does real work. v2 still carries no live traffic
  in production: no GitHub workflow has been cut over to `enqueue_batch` yet (a separate deployment
  decision, tracked with Phase 1's dominant-call-site cutover) and no compiled route yet advertises
  `transport: "llm-dispatch-v2"` (Phase 4) — this phase makes the dispatch pipeline itself real and
  tested, ready for the shadow-mode validation gate Phase 2's own plan requires before any of that.

- **Phase 1 of bounded bundled LLM dispatch (review/44), implemented in
  [PR #1253](https://github.com/BashfulBits/city-meeting-podcasts/pull/1253).** Implemented the
  initial parallel `workers/llm-dispatch-v2/` deployment with SQLite-backed `LLMSchedulerDO`
  Durable Object coordinator, pure validate-then-DO pass-through with zero Worker-side B2 I/O on
  ingress, and batch ingress/polling endpoints (`/v2/jobs:enqueue-batch`, `/v2/jobs:poll-batch`,
  `/v2/jobs:resolve-unknown-batch`, `/v2/jobs/{id}:schema-retry` — the last two land as stubs;
  `schema-retry` returns `501` until Phase 2's dispatch machinery exists to back it). Added
  `enqueue_batch` and `poll_batch` methods to `LiteLLMBackend` in `citypods/compute/llm.py` with
  direct client-side B2 payload staging (the ingress Worker itself never touches B2 — see
  review/44's connection/subrequest-limit revision) and client-side throttling to self-limit
  before making HTTP requests, updated `llm_deferred_sweep.py` to batch-poll v2 handles before
  its existing per-handle reconciliation loop, and updated `citypods/compute/llm_policy.py`.
  v2 stays inert until `dispatch_v2_url` is configured (`CITYPODS_LLM_DISPATCH_V2_URL` /
  `LLM_DISPATCH_V2_URL`, alongside `CITYPODS_LLM_DISPATCH_V2_AUTH_TOKEN` /
  `LLM_DISPATCH_V2_AUTH_TOKEN` and `CITYPODS_LLM_DAILY_INGEST_CAP` / `LLM_DAILY_INGEST_CAP`), since
  `LiteLLMBackend._available_transports()` and `_enqueue_durable_policy_job` both gate on it; the
  Worker deploy itself additionally needs `CLOUDFLARE_LLM_API_TOKEN` as a deploy-time secret.

- **Batch LLM job dispatch at every `queue_only=True` call site (review/44 follow-up), implemented
  in [PR #1262](https://github.com/BashfulBits/city-meeting-podcasts/pull/1262).** Production
  Cloudflare Worker logs showed every `enqueue-batch` POST from the chapter-agenda/chapter-locator
  lane carried exactly one job — despite the v2 protocol, DO, and `LiteLLMBackend.enqueue_batch`/
  `poll_batch` all supporting up to 1000 jobs/call since Phase 1, no call site ever actually
  accumulated more than one job before submitting, so the dominant lane was still making one Worker
  request per episode. Added `dispatch_job_batch()` to `citypods/compute/llm.py` — one
  `enqueue_batch` call for a whole run's jobs (falling back to per-job retry only if the batch call
  itself raises, since `enqueue_batch` raises for the whole call on a single job's
  `idempotency_conflict`), plus one `poll_batch` reconcile pass for any still-pending v2 handles —
  and restructured `AgendaChapterCandidatesStage`/`ChapterBoundaryLocatorStage`
  (`citypods/stages.py`) and `citypods/tournament.py`'s pairwise-judge comparison phase to build
  every job first, dispatch the whole set in one call, then finalize each result against its own
  context. `citypods/tags.py`'s own model-generation dispatch (`llm_tag_suggestions`) and the
  transcribe/align call sites remain one-job-at-a-time, deferred to review/44 Phase 4 — not because
  structured-output validation is hard (that's ordinary finalize-step work, same as the sites
  above), but because `llm_tag_suggestions` recursively splits one episode into a variable number
  of jobs internally and `TagsStage` runs episodes through a worker-thread pool sharing a live,
  incrementally updated per-run dispatch budget; see review/44 Phase 4 for the detail.

- **Read-only v1 dispatch queue-order report (`scripts/report_pending_dispatch_queue.py`,
  `Report LLM dispatch queue` workflow).** Operator diagnostic for the Mistral pause above: lists
  the v1 Worker's pending `ready/` markers in their real R2 lexicographic dispatch order, resolves
  each job's candidate routes via the same `model_aliases`/`model_routes_map` lookup
  `workers/llm-dispatch-proxy/src/index.js` uses, and checks each candidate against
  `state/dispatch_budget.json` and its compiled `rpd` to flag routes paused (`rpd: 0`),
  reactively blocked (`blocked_until`), or reporting capacity. Never writes to R2 — no marker
  relocation, requeue, or ledger mutation. Flags whether a job is only reachable through
  currently-paused routes ("STUCK") vs. has an open alternative ("ELIGIBLE"), and separately
  whether it sits within the Worker's 500-marker (`DEFAULT_READY_LOOKAHEAD`) lookahead window —
  since `dispatchBatch`'s per-tick scan already skips a `no_capacity` head in place (or relocates
  it once blocked past `DEFER_IN_PLACE_SECONDS`) rather than stalling on it, a run of STUCK jobs
  at the queue head only actually blocks a later job once it fills that whole lookahead window.

- **Configurable global token estimate buffer (`token_estimate_buffer`).** Added a new top-level
  setting `token_estimate_buffer` (e.g. `0.90`) in `config/provider_limits.yml` that applies a
  scaling multiplier to all compiled token rate budgets (route `tpm`, provider `monthly_tpm`,
  and provider `tpm`) across `llm_routes.json` and `dispatch_limits.json`. This provides a
  zero-runtime-overhead calibration knob against token estimation drift and rate limit 429s.

### Fixed

- **Transient provider HTTP error handling in sharded enrich lanes.** Updated
  `is_transient_provider_error` and `ProviderError` to recognize retryable HTTP status codes
  (`500..599`, `429`, `408`, `425`) across exception attributes, responses, causes, and status
  messages. Prevents transient upstream provider 5xx outages (e.g. Granicus archive index 500s)
  from failing sharded matrix jobs when all other assigned sources and audio materialization work
  succeeded.

- **Downstream enrich lane error recovery and thread-safe contract registration (run #313 fix).**
  Fixed two issues that caused Chapter Agenda extraction (workflow run 313) to exit with code 1.
  First, `_run_enrich_global_queue` now treats all record-backed downstream enrichment lanes
  (`transcribe`, `align`, `diarize`, `speaker-identity`, `tag`, `moments`, `chapter-agenda`,
  `chapter-locator`, `chapter`) as secondary enrichers: when an upstream provider fetch fails
  (such as an external Granicus HTTP 500 outage), the lane falls back to
  `pipeline.fetch_merge_from_records` to continue processing existing locally persisted records,
  and reports unrecoverable source fetch failures as `skipped` rather than `error`. Second,
  `citypods.compute.structured` contract registration was made thread-safe and idempotent with a
  global lock, eliminating race conditions in multi-threaded worker pools where concurrent initial
  invocations caused `ValueError: duplicate or empty structured-output contract`. Incompatible
  schemas still fail closed, rather than silently reusing the wrong response contract.
  
- **ASR Quality Eval MMS_FA model caching and dependency cascade.** Added
  `scripts/prepare_mms_fa.py` to provide a robust local cache → B2 mirror → upstream Meta CDN
  download cascade for the L2 CTC aligner checkpoint (`model.pt`), eliminating CI failures on
  Actions cache misses. The existing MMS_FA bytes are now SHA256-verified and all model cache/B2
  identities are exact, so a future model revision cannot reuse old bytes. This verifies the
  already-used model and does not change the H15 evaluation recipe or trigger re-scoring.
  Hardened `.github/workflows/asr-bench.yml` to use the project's SHA256-verified static ffmpeg
  pin (replacing unpinned `apt-get install ffmpeg`), prepare models after installing its storage
  dependency, and preserve its selected model matrix. Added aligner model caching to the `align`
  matrix lane in `.github/workflows/asr.yml`, and updated
  `review/22-dependency-and-reproducibility-policy.md`.
  
- **Separate per-lane chapter maintenance leases and key-by-key candidate merge.** Separated the
  shared chapter maintenance mutex into independent per-lane R2 CAS objects
  (`maintenance-leases/chapter-agenda.json` for `chapter-agenda.yml` and
  `maintenance-leases/chapter-locator.json` for `chapter-locator.yml`), and made
  `generated_agenda_candidates` a composite field merged key-by-key in `merge_preserving_foreign`.
  Previously, both workflows shared `maintenance-leases/agenda-chapter-reset.json`, causing the
  chapter locator workflow to fail with `MaintenanceLeaseBusy` whenever schedule delays or longer
  extraction runs overlapped their execution times. `scripts/reset_agenda_chapter_state.py` now
  claims both leases as a composite transaction before mutating state during manual recovery. A
  short shared `maintenance-leases/chapter-record-write.json` lease now serializes only the
  non-CAS B2 read/merge/upload commit, preventing overlapping lanes from losing each other's
  changes while preserving concurrent extraction work. The merge also honors the chapter reset's
  explicit deletion of stale `generated_agenda_candidates` state.

- **Free-model alternates for jobs that dispatched only to Mistral (2026-08-18 follow-up to the
  Mistral pause below).** Agenda-chapter extraction (`chapter_titles.AGENDA_PRODUCTION_MODEL`) now
  has `meta-llama/llama-3.3-70b-instruct` as a same-priority alternate
  (`AGENDA_PRODUCTION_MODELS`), with `finalize_agenda_job`/`ChapterAgendaCandidatesStage` now
  recording whichever model the scheduler actually dispatched to (`result.model`) instead of
  always labeling the artifact with the pinned constant. The shadow-only locator/title-equivalence
  selector (`chapter_locator.select_locator_model`) is now `select_locator_models`, returning every
  same-priority candidate for a request's size instead of one: Mistral is supplemented with the
  free `deepseek/deepseek-v4-flash` (OpenCode Zen tier) and `nvidia/nemotron-3-ultra-550b-a55b:free`
  routes below their respective context ceilings, with Gemini kept as a last-resort escalation
  beyond every free tier (its free quota is tiny). `LocatorRequest`/`AgendaItemExtractionRequest`/
  `TitleEquivalenceRequest` gained an additive `models: tuple[str, ...]` field alongside the
  existing single `model: str` (still the primary candidate), so the research scripts that build a
  direct single-model backend from `.model` are unaffected. The weekly topic-tag tournament
  (`tournament.py`, `.github/workflows/llm-tournament.yml`) replaced its Mistral contestant with
  `zai/glm-4.7-flash` and added `google/gemma-4-26b-a4b-it` as a 4th contestant, growing
  `CONTESTS` to the full 6-pair round robin. Removed the now-dead `mistral/mistral-small-2603`
  entries from `r5_benchmark.OPTIONAL_TAGGER_MODEL` and `audit_remedy.REMEDY_MODELS`.

- **Mistral provider paused: monthly token budget exhausted (2026-08-18 hotfix).** Mistral moved
  to account-wide monthly token metering; the current cycle's allowance is used up (resets
  2026-08-31). Pinned `rpd: 0` on all seven Mistral routes in `config/provider_limits.yml`
  (`mistral_codestral_2508_primary`, `mistral_devstral_2512_primary`,
  `mistral_small_2603_primary`, `mistral_medium_2508_primary`, `mistral_medium_2505_primary`,
  `mistral_large_2512_primary`, `mistral_large_3_primary`,
  `mistral_labs_leanstral_1_5_1_primary`), recompiled via `scripts/compile_llm_limits.py`. This
  routes through the existing quota-exhaustion path (`LLMBudget.available()`/`select_route()`)
  rather than the route's `rpm`/`tpm` fields, so dispatch is deferred and retried (at each local
  midnight, via `_next_quota_reset`) instead of hard-failing callers like
  `AgendaChapterCandidatesStage` that dispatch to Mistral (`AGENDA_PRODUCTION_MODEL`)
  unconditionally, and avoids the `ZeroDivisionError` a literal `rpm`/`tpm` of `0` would hit in
  `LLMBudget.reserve()`'s rate-schedule math. `providers.mistral.monthly_tpm` was also set to `0`
  as a documentation-of-record value, though it is not read by the scheduler or Worker today.
  Remove the `rpd: 0` overrides once the monthly allowance resets and a real budget is sized.

- **LLM dispatch v2's `enqueue_batch`/`poll_batch` payload I/O against production storage
  (2026-08-18 incident follow-up).** Production wires `LiteLLMBackend`'s `storage=` to
  `citypods.storage.routing.RoutingStorage` (B2 primary + R2 coordination), whose
  `COORDINATION_PREFIXES` deliberately excludes `payloads/`/`results/` — v2 job payloads are
  B2-resident by design (see `workers/llm-dispatch-v2`'s own SigV4 client), not R2 coordination
  state. `_storage_client()` (used by both `enqueue_batch`'s payload write and `poll_batch`'s
  result read, `citypods/compute/llm.py`) previously returned the router itself, so every
  `payloads/…` `put_cas`/`get_bytes` call routed to the B2 primary and then correctly hit
  `RoutingStorage`'s own safety gate — B2 is deliberately marked non-`cas_capable` there, since it
  doesn't enforce real If-Match/If-None-Match — raising `NotImplementedError` before the batch
  ever reached the dispatch Worker over HTTP. This is why wiring `LLM_DISPATCH_V2_URL`/
  `LLM_DISPATCH_V2_AUTH_TOKEN` into the six GitHub workflows alone did not stop the ingest flood:
  `citypods-llm-dispatch-v2`/its Durable Object never saw a single request, while the exception
  was swallowed per-item by `TagsStage`'s existing error handling and every call kept falling
  through to `llm-dispatch-proxy` (v1). Fixed by having `_storage_client()` reach past the router
  to its `.primary` B2 backend directly when `self.storage` is a `RoutingStorage` (whose own
  `put_cas`/`get_bytes` never gated on the `cas_capable` flag to begin with — only the router did)
  — an unconditional write is exactly right here, since `enqueue_batch` already only ever calls
  `put_cas` with no `if_match`/`if_none_match` (`job_id` is a fresh UUID per call, so there is no
  CAS race to protect). Added a regression test in `tests/test_compute_llm_dispatch_v2.py` using
  the real `RoutingStorage` (not the flat mock the rest of the suite uses, which doesn't
  reproduce the prefix-routing gate and is why this shipped untested against the real topology).

- **Stale v1-backlog reconcile storm in `_chapter_job_result` (2026-08-18 incident, continued).**
  `citypods/stages.py`'s `_chapter_job_result` called `backend.reconcile()` on every `JobHandle`
  `run_inference` returned, including a pre-existing deferred handle from before
  `LLM_DISPATCH_V2_URL` was configured (`backend != "llm-dispatch-v2"`). v1 never had a
  poll-batch endpoint, so reconciling one meant one `GET /v1/requests/{id}` Worker invocation per
  stale episode on every chapter-agenda/chapter-locator run — measured at ~1980 such polls in
  under 10 minutes, all returning `202`, zero progress. Fixed by only reconciling v2-backed
  handles; a stale v1 handle is now left untouched and still reported pending. Also declares
  `"observability": { "enabled": true }` in `workers/llm-dispatch-v2/wrangler.jsonc` — Workers
  Logs, enabled by hand in the dashboard to diagnose this incident, kept reverting to disabled on
  the next deploy because nothing in source declared the setting.

- **`dispatch_v2_url`/`dispatch_v2_auth_token` never wired into the tag/tournament/r5-benchmark
  backends (2026-08-18 incident, continued).** `LLMBackendConfig` has no env-reading
  `__post_init__`, so a field omitted from a manual construction is `None` regardless of the
  environment. `citypods/run.py`'s `tag_backend` (the tag lane's actual backend) and
  `citypods/tournament.py`'s `_backend()` hand-rolled only `dispatch_url`/`dispatch_auth_token`
  and were never updated when v2 shipped — every `queue_only=True` policy from those two fell
  straight through to the legacy v1 branch no matter what was configured. Fixed by building all
  three from `LLMBackendConfig.from_env()` via `dataclasses.replace()` instead of hand-copying
  env vars, closing the bug class for any future field added there too.

- **`console.error`'s second argument silently dropped by Cloudflare Workers Logs (2026-08-18
  incident, continued).** `console.error("x failed", err)` in `workers/llm-dispatch-v2/src/index.js`
  never surfaced the actual `Error` in exported logs — only the literal call-site string did,
  even with a custom Logs field added in the dashboard. Added `describeError()`, rendering any
  thrown value into a string used both in `console.error` and the HTTP response body's `detail`
  field (previously a generic `"Coordinator request failed"`), so the real cause now also shows
  up in the Python client's own error output.

- **`LLMSchedulerDO` never extended `DurableObject` — the actual root cause of the whole 2026-08-18
  incident.** `workers/llm-dispatch-v2/src/index.js`'s `getCoordinator()` calls
  `env.LLM_SCHEDULER.getByName("global-v2")`, Cloudflare's named-Durable-Object RPC binding style,
  which requires the class itself to `extend DurableObject` (from `"cloudflare:workers"`) to
  support RPC calls at all. `LLMSchedulerDO` never did, from Phase 1's first deploy — every
  `enqueueBatch`/`pollBatch`/`resolveUnknownBatch` call failed with `TypeError: The receiving
  Durable Object does not support RPC, ...`, invisible to this repo's test suite because it
  constructs `new LLMSchedulerDO(...)` directly and calls its methods directly, bypassing the
  real binding/RPC layer entirely. None of the four fixes above could ever have mattered until a
  request reached a working RPC call, and none of them ever did. Fixed with a dynamic
  `import("cloudflare:workers")` (falling back to a plain class under the Node-based test suite,
  where that module doesn't exist) and a regression test guarding the `extends` clause. Full
  retrospective with guards for Phase 2–4 work in review/44's "Rollout incident retrospective
  (2026-08-18)".

- **Legacy agenda records with partial state can now be reset and rebuilt safely.** Added the
  dry-run-first `reset-agenda-chapter-state.yml` recovery workflow and
  `scripts/reset_agenda_chapter_state.py`, which targets only records missing
  `links["agenda_text_artifact_key"]`, preserves official provider links, clears stale derived
  agenda/chapter state, and pushes audio/chapter-owned blocks through the race-safe merge path.
  The repair and chapter lanes now share a CAS-backed R2 maintenance mutex, checked immediately
  before durable chapter pushes so a concurrent chapter run cannot restore reset state.
  `AgendaTextStage` now requires the artifact key for its accepted-document reuse fast path, so a
  missing pointer cannot permanently prevent chapter dispatch. This is a metadata repair only:
  it does not bump the agenda pipeline version or invalidate completed agenda documents globally.
  
- **`/remedy` command to re-run remediation on an issue that grew new rows.** `audit.yml`
  dispatches `remedy-unexpected-bodies.yml` automatically, but only on the run that *creates* a
  consolidated `unexpected-body` issue — not on a later run that adds or changes rows on one
  still open, so nothing kicks off remediation for those newer findings by itself. Commenting
  `/remedy` on that issue re-dispatches it. `remedy-commands.yml` follows `stale-commands.yml`'s
  shape exactly: the same `citypods.github_permissions` write-or-higher check (not the possibly
  stale `author_association` alone) via `scripts/remedy_commands.py`, which also confirms the
  commented-on issue actually carries the audit's own `unexpected-body` marker before dispatching
  — so `/remedy` on an unrelated issue, or from a non-collaborator, does nothing but post an
  explanation. No `pip install` needed for the check itself: `citypods/github_permissions.py`
  has zero third-party imports.

- **Automated remediation for `unexpected-body` audit findings.** The daily audit reports provider
  labels no feed selector covers; classifying one is a taxonomy call, and this wires an LLM into
  that step under a strict trust boundary. The response schema carries no path and no YAML — the
  model returns a feed slug, an action, and provider GUIDs, and every value is re-derived from the
  audit's own evidence before anything is written: the label must have been observed for that
  source, target slugs must be feeds on that same source, GUIDs must belong to episodes carrying
  the label, and a new slug must be well-formed and unused. Anything unverifiable is rejected with
  a reason and reported rather than applied, and the applier resolves slugs to paths through a map
  built by scanning `config/feeds`, so no write path originates from model output. Feed edits are
  line-level insertions (`citypods/feed_yaml_edit.py`) that preserve the hand-written comments a
  `safe_dump` round-trip would erase, each re-parsed and diffed before the write. Evidence is
  collected during the audit's existing fetch (`audit_feeds.py --unexpected-body-evidence`, reusing
  the new `collect_unexpected_bodies`), so there is no second provider fetch and no second
  definition of "unmatched". Applied changes are gated on config reload plus repo-wide Ruff lint,
  Ruff format, and the full `pytest -q`, reverting the tree on failure.

  Runs automatically: `audit.yml` dispatches `remedy-unexpected-bodies.yml` (its own
  `workflow_dispatch`, invoked via `gh workflow run` with `audit.yml`'s narrowly-scoped
  `actions: write`) the moment `reconcile()` *creates* a new consolidated `unexpected-body`
  issue — never on a later run that only updates an already-open one, so this fires once per
  fresh finding, not once per day it stays open. Deliberately not an `issues: opened` listener:
  that fires for any issue any GitHub user opens on this public repo with an attacker-controlled
  body, forcing the remedy workflow to re-verify the triggering issue's authorship and content
  before trusting it. Dispatching from `audit.yml`'s own job needs none of that — only something
  already holding `actions: write` on the repo can reach `workflow_dispatch` at all. Every
  terminal outcome (opened or reused PR, nothing to change, or verification failure) posts one
  comment back on the issue with the full classification and a link to the PR; re-runs over
  unchanged findings reuse the same digest-named branch and PR instead of erroring on a
  duplicate `gh pr create`. Still runnable manually from the Actions tab.

- **Direct LLM calls now route through the Cloudflare AI Gateway, and do so with the right URL.**
  A direct provider call is proxied through the gateway whenever it's enabled and configured, so
  runner-side requests land in the same analytics surface as the Worker's. The gateway is a
  transparent proxy, so this is an observability change, not a routing one — it is on by default,
  with `LLM_AI_GATEWAY=0` as the
  kill switch. Two things make it safe: the rewrite is scoped to the **direct** transport
  (`_provider_options(..., direct=…)`), leaving the `llm-dispatch` payload untouched — the Worker
  already applies its own gateway, and handing it an `api_base` would double-proxy the call; and
  each route's generated `ai_gateway_chat_path` now actually contributes its prefix to `api_base`.
  LiteLLM appends `/chat/completions` on its own, so a gateway `api_base` of
  `…/google-ai-studio` resolves to a 404 — Gemini's OpenAI-compat endpoint lives under
  `/v1beta/openai`, and Mistral's under `/v1`. The field was previously plumbed through
  `LLMRoute` and the compiled catalog but never read. Routing tests now assert the full request
  URL for every gateway provider rather than `api_base` alone, cover the kill switch and the
  unconfigured-gateway fallback, and pin that the dispatch payload carries neither `api_base` nor
  `cf-aig-authorization`. `AI_GATEWAY_AUTH_TOKEN` is wired into the four workflows that make
  direct provider calls, with a workflow contract test so a new one cannot silently miss it.

- **Resolve 7 unexpected meeting bodies from the feed-health audit (#1231).** The daily audit
  flagged seven provider labels no feed selector covered. Recurring series were unioned onto the
  owning feed's `body_any`: Addison's bare `Special Meeting` (35 rows) onto City Council, and Fort
  Worth's provider-duplicated `Audit and Finance Committee Audit and Finance Committee` (14 rows)
  onto the Audit Committee feed — the duplicated label matches by substring, so the single
  un-duplicated selector covers it. Pflugerville's bare `TIRZ` label (5 rows) got a dedicated
  `pflugerville-tx-tirz-board` feed. True one-offs were pinned by exact provider GUID under
  `body_includes`: Arlington's `Virtual Town Hall on Future Active Adult Center`, Dallas's
  `Purchasing Bids 7-23-2015` (alongside the existing `7-30-2015` row), Denton's `Joint Luncheon
  with Library Board` (Council is the only configured body of the pair), and Waco's `Texas Ranger
  Hall of Fame & Museum Advisory Board Meeting` — routed to the Boards and Commissions feed rather
  than City Council, since an advisory board is not a Council session.

- **Cross-lane `links` clobber silently dropped `agenda_text_artifact_key` (and any other
  `links` entry) on push.** `links` was the one derived field never added to `ARTIFACT_BLOCKS` —
  every other artifact (`agenda_text`, `tags`, `generated_agenda_candidates`, …) is protected
  during a scoped lane's push via `protected_blocks_for_lane`/`merge_preserving_foreign`, so a
  sibling lane's stale local snapshot can never regress it; `links` had no equivalent, so
  `chapter-agenda`/`chapter-locator`/`tag` (none of which write any `links` key, but which still
  merge the whole record for every source they touch) could push their own necessarily-stale
  `links` snapshot over a fresher key an interleaved `audio` run had just written moments earlier.
  Confirmed live via temporary diagnostic instrumentation (still on the PR branch pending
  production verification): an audio run's own post-push readback showed
  `links["agenda_text_artifact_key"]` gone again within seconds, specifically for the one source
  (Fort Worth, ~20 board feeds sharing one `source_key`) also touched by two other lanes' 15-minute
  crons — this is why `chapter_agenda`'s `missing-agenda-artifact` defer reason covered 100% of its
  pool despite `agenda_text` reporting the same episodes `accepted`.
  `merge_preserving_foreign` now merges `links` per-key, mirroring the identical pattern already
  used for `stage_completion`: start from remote, let a lane overwrite only the specific keys it
  actually owns (`_owned_link_keys` — audio owns everything except `transcript`; `transcribe`/
  `align` own only `transcript`; every other scoped lane owns nothing and may only add a key
  remote doesn't have yet), and never let an unscoped run drop a remote-only key it didn't touch.

- **`tag.yml` job timeout was cancelling `LLM topic tags` runs mid-batch.** The GH Actions job
  timeout (30m) sat just above the lane's internal wall-clock budget (`tag_run_time_budget_minutes`,
  20m), leaving almost no margin once source-prepare scraping and the graceful-stop tail were
  accounted for; two scheduled runs during the Aug-16 LLM dispatch incident were hard-cancelled by
  GitHub before the run's own budget ever got a chance to stop it cleanly, needlessly truncating a
  run's LLM dispatch submissions. `timeout-minutes` widened 30 → 180 and
  `tag_run_time_budget_minutes` 20 → 160 (site_config.yml), mirroring the margin pattern already
  used by `llm-deferred-sweep.yml`'s 240m job timeout.

- **LLM dispatch retry path R2 budget (Finding 1, audit of PR #1229).** `saveRetry` now accepts
  an optional `pendingMarkerDeletes` array; when provided by `dispatchBatch`, the old ready-marker
  DELETE is deferred into the end-of-batch `unmarkReadyBatch` call rather than fired immediately.
  This collapses the retry path from 9 R2 ops to 8 (matching the success and failure paths).
  `replaceReadyMarker` received the same `pendingDeletes` hook so the deferral is transparent to
  other callers.

- **LLM dispatch permanent-failure DELETE flood (Finding 2, audit of PR #1229).** When multiple
  heads in the ready lookahead window carry permanent failures (`no_configured_route`, `context_limit`,
  `credential_resolution_failed`), each `saveFailure` call in the candidate-prep loop previously
  issued an individual R2 DELETE for the stale marker, producing 3N ops for N failures. The
  `pendingMarkerDeletes` array is now declared before the prep loop, permanent-failure `saveFailure`
  calls push to it instead of deleting immediately, and a single `unmarkReadyBatch` flush fires in
  the early-exit path (or the existing end-of-batch flush fires when candidates were also dispatched).

- **LLM dispatch stagger sleep timing (Finding 3, audit of PR #1229).** The stagger delay for
  a second in-batch candidate was computed as `requests_available_at − now` before the CAS commit,
  then the full `delayMs` was slept *after* the commit. This meant CAS latency (~100–500 ms) was
  added to the intended inter-request gap rather than subtracted. The sleep now computes
  `remainingMs = max(0, dispatchAtMs − Date.now())` at the moment the sleep starts, so the request
  is dispatched at the absolute `dispatchAtMs` target regardless of CAS overhead. `dispatchAtMs`
  is destructured from the candidate item in the `upstreamTasks.map` closure to support this.

### Added

- **Staggered in-batch concurrency and coordinator consolidation in LLM dispatch.**
  `citypods-llm-dispatch-proxy` now supports admitting multiple requests for the same route or
  provider within a single scheduled batch (e.g. 2 Gemma-4 or 2 Mistral jobs per cron tick)
  via in-batch stagger pacing (`MAX_IN_BATCH_STAGGER_SECONDS`, default 20s). Admitted requests
  pre-load payloads into memory and compute pacing delays from RPM/TPM intervals, sleeping until
  the absolute `dispatchAtMs` target before opening upstream TCP connections. The cron lease and
  rate ledger are consolidated from `locks/cron.json` and `state/dispatch_budget.json` into a single
  object `state/dispatch_coordinator.json`, reducing R2 operations by 2 per scheduled dispatch
  invocation. Ready marker lookahead evaluates route capacity directly against metadata in V8 memory
  with zero R2 reads for deferred heads.

- **Cloudflare AI Gateway observability integration for LLM dispatch.** `citypods-llm-dispatch-proxy`
  now supports optional routing through Cloudflare AI Gateway, enabling time-series request/response
  charts, status code breakdown (200, 429, 400, 500), upstream error payload inspection, and CSV log
  export. Provider blocks in `config/provider_limits.yml` define `ai_gateway_slug` mappings (`google-ai-studio`,
  `mistral`, `groq`, `deepseek`, `openrouter`, and custom provider slugs). When `CLOUDFLARE_ACCOUNT_ID` is
  set (a one-time manual Worker secret, `wrangler secret put CLOUDFLARE_ACCOUNT_ID` — see
  `workers/llm-dispatch-proxy/README.md`) or `AI_GATEWAY_BASE_URL` is set, outbound calls route through the
  gateway; when unconfigured, calls default directly to provider endpoints with zero breaking changes.
  `.github/workflows/llm-dispatch-worker-deploy.yml`'s `accountId` input to `wrangler-action` only
  configures the deploy CLI — it does not expose `CLOUDFLARE_ACCOUNT_ID` to the deployed Worker, so an
  earlier revision of this change had the deploy workflow inject it via `wrangler-action`'s `secrets`/`env`
  inputs instead. That broke every subsequent deploy: `secrets`/`env` makes `wrangler-action` run `wrangler
  secret bulk` *before* deploying any code, and Cloudflare's secret-modification API rejects that call with
  error 10215 ("the latest version of your Worker isn't currently deployed") once this Worker's latest
  uploaded version and its currently-deployed version drift even slightly — permanently wedging the deploy
  at that step, since the plain `wrangler deploy` that would otherwise resolve the drift never gets to run.
  `CLOUDFLARE_ACCOUNT_ID` doesn't change, so it doesn't need re-uploading on every deploy: it's a one-time
  manual secret like every other credential this Worker uses. Separately, a live
  probe against the `citypods-dispatch` gateway found its "Authenticated Gateway" setting on, which
  rejects any proxied call lacking a `cf-aig-authorization` header with a non-retryable `401` — before
  this fix that would have turned "invisible in the dashboard" into "every dispatch fails outright" the
  moment `CLOUDFLARE_ACCOUNT_ID` started working. The Worker now attaches `cf-aig-authorization` from a
  new optional `AI_GATEWAY_AUTH_TOKEN` secret whenever a route is actually going through the gateway (see
  `workers/llm-dispatch-proxy/README.md`).

- **Pre-push lint verification script and explicit line-length guidance.** Added `.githooks/pre-push`
  and `scripts/pre-push.sh` to run `ruff check .`, `ruff format --check .`, and `pytest -q` before
  pushing. Updated `AGENTS.md` and `CONTRIBUTING.md` with explicit instructions on handling
  non-autofixable `E501` (line-too-long) violations so agents manually wrap long docstrings,
  comments, and literals prior to opening pull requests.

### Changed

- **The compact Worker route catalog was silently dropping `structured_output_schema_strip_keys`.**
  The catalog's fixed-position array format (`COMPACT_ROUTE_FIELDS` in `index.js`,
  `_WORKER_ROUTE_FIELDS` in `scripts/compile_llm_limits.py`) predates the Worker's structured-output
  schema relaxation and never listed that field. `routeFromCatalog` has no way to signal a missing
  field — it just reads as `undefined` — so every configured route (Gemini, Gemma, and any other
  route declaring strip keys) dispatched an *unstripped* schema against the real compiled catalog,
  while a hand-built test fixture using a full route object still passed. Both field lists now
  include it, kept in sync by a comment in each pointing at the other. Caught by a new test that
  exercises the actual compiled `dispatch_limits.json` rather than a fixture route object.

- **A no-candidate dispatch batch never persisted its ledger changes.** The guard compared
  `JSON.stringify(budget)` against a second stringify of the same object, so the strings always
  matched and the write could not fire — while still paying for two whole-ledger serializations on
  every no-capacity invocation. The write now fires when an abandoned reservation was reaped;
  minute/day window rollover is recomputed from the current time on every load and never needed
  persisting.

- **The LLM dispatch Worker now ships a compact startup catalog.** The generated Worker JSON removes
  the duplicate route list, unused direct structured-output metadata, and provider discovery data;
  fixed-position route records and numeric model indexes avoid repeating route property names and
  IDs in the bundle. Legacy selectors are folded into the model-alias map, while the richer route
  catalog used by Python remains unchanged.

- **LLM dispatch now batches R2 reservation cleanup.** A dispatch batch removes all of its route
  reservations with one conditional budget read/write cycle and overlaps that cleanup with the
  independent canonical result persistence. Existing `BATCH_CONCURRENCY` and `MAX_TOTAL_REQUESTS`
  controls and defaults are unchanged.

- **Pre-labeler dispatch now keys off its YAML-owned LLM schema version.**
  `tagging.prelabeler.llm_schema_version: "2"` is part of every pre-labeler recipe, batched
  durable-handle identity, and persisted candidate assessment. Existing version-1 assessments are
  therefore stale and the tag lane schedules fresh version-2 work gradually; it does not rewrite
  episode artifacts in place. The manual recovery action can dry-run or retire only the bounded,
  older Gemma `assessments` requests (retaining their R2 audit records), after which the deferred
  sweep clears their handles as terminal and the normal tag workflow creates the replacement jobs.

- **LLM dispatch Worker observability is now sampled at 100%.** All proxy invocations retain their
  Workers logs while the existing payload-redaction boundaries remain unchanged.

- **Resolved untracked meeting bodies and expanded feed coverage across Texas cities (#1165).**
  Addressed `unexpected-body` findings across 7 cities:
  - Added new feeds for `arlington-tx-economic-development-committee` (historical series), `waco-tx-plan-commission` (active bi-weekly meetings), and `waco-tx-boards-and-commissions-committee`.
  - Unified board aliases in `pflugerville-tx-equity-advisory-board` (`Equity Commission`, `Equity`).
  - Added one-off provider UID inclusions for date-embedded clerk typos and council special sessions in `dallas-tx-bid-purchasing` (`Purchasing Bids 7-30-2015`), `denton-tx-city-council` (`2nd Tuesday Session`), `addison-tx-city-council` (`TIRZ #1 Board Meeting`), `fort-worth-tx-city-council-worksession` & `fort-worth-tx-city-council` (`Budget Work Session Budget Work Session`), and `waco-tx-city-council` (`Budget & Audit Committee Meeting`, `Special City Council`).

- **Granicus source-cache downloads now reject truncated zero-exit responses.** The standard direct
  audio path still runs first; only a failed or locally short canonical archive fetch uses the
  authenticated Worker, where the runner assembles and byte-validates sequential ranges (with a
  verified full-GET fallback for origins that ignore `Range`). Pages, metadata, documents, and
  non-audio media retain the existing general Worker proxy behavior. This is transport validation
  only: no audio/spec pipeline version changed, no stored artifacts are invalidated, and no catalog
  backfill is required. The new `chunked-canary` Granicus probe compares the chunked bytes with a
  standard direct download when the runner permits it, otherwise with a non-ranged Worker download.

- **Topic-tag dispatch now versions its structured-output schema.** Bumping the dedicated tag LLM
  schema version invalidates old recipe/fingerprint identities, so reruns create fresh queue
  requests after a provider-compatibility schema change. Existing R2 request bodies are not
  rewritten or requeued automatically.

- **Oversized terminal LLM error bodies now retain bounded diagnostics.** The private R2 record keeps
  the first 8 KiB, observed byte count, and `truncated` marker instead of discarding the diagnostic
  entirely; retryable responses continue to avoid repeated body previews.

- **CodeRabbit automatic and incremental reviews are disabled by default.** Maintainers can request
  a review explicitly with `@coderabbitai review`, avoiding unsolicited free-tier review runs while
  preserving the repository's review configuration and guidance.

- **LLM dispatch logs now include bounded stage profiling.** Normal scheduled batch records expose
  wall-clock milliseconds for queue preparation, budget/ledger work, upstream fetch and response
  parsing, R2 persistence, reservation release, and total dispatch time. The profile stays out of
  provider-facing completion responses and contains no request or response content.

- **LLM provider diagnostics now retain bounded response shape and body previews.** Terminal private
  R2 failure records include the upstream content type, response byte length, bounded JSON field
  names, nested error path, and an 8 KiB body preview. Retryable responses retain the structural
  metadata without repeated body previews unless their retry budget is exhausted; Worker logs and
  client responses remain redacted.

- **LLM dispatch now preserves bounded provider diagnostics for non-2xx responses.** Private R2
  request records retain structured provider error code/status and a truncated JSON error message,
  while scheduled logs expose only request/route/status identifiers and never prompts, API keys, or
  raw provider bodies. This makes future Google/Gemma failures diagnosable without changing the
  asynchronous response contract.
  
- **LLM pricing is now effective-dated and YAML-driven.** `config/provider_limits.yml` can define
  input/output rates and UTC peak windows per physical route; the compiler carries those periods to
  both the Python scheduler and the dispatch Worker. DeepSeek V4 Flash and Pro include the August 16,
  2026 rate-card cutover. Cache-hit pricing is intentionally not modeled because its hit ratio is not
  predictable or controllable. Flexible deferred work waits for the route's next cheapest pricing
  window, while a deadline can authorize the currently active price. No batch protocol was added because
  DeepSeek does not provide a batch API. No LLM artifacts are invalidated and no backfill is required;
  this changes admission and cost accounting only.

- **Ready-marker routing metadata now travels with the R2 list result.** The dispatcher requests
  compact marker metadata during its bounded `ready/` listing and falls back to the marker body for
  legacy objects, eliminating up to 16 marker reads and JSON decodes per scheduled invocation.
  Reindex and failed-request recovery writes now populate the metadata; existing queue records and
  request payloads are unchanged.

- **Free-plan LLM dispatch now defaults to one request per scheduled run.** Production CPU
  telemetry rose above the 10 ms Cron Trigger allowance after the bounded dispatcher was increased
  to four concurrent requests; `BATCH_CONCURRENCY` and `MAX_TOTAL_REQUESTS` are back to `1` while
  the queue-index and multi-route selection behavior remain unchanged. This changes dispatch
  throughput only and does not alter stored requests, responses, or pipeline artifacts.

- **Provider-align now permits a roughly 4 GiB CTC section envelope.** The safe section limit is
  increased from five to ten minutes using the observed WhisperX allocation slope, and oversized
  sections produce a visible GitHub Actions warning before they are routed to full ASR.
  Provider-align version 7 reopens prior version-6 ineligible items for gradual recomputation;
  full-ASR artifacts are not invalidated.

- **The transcript recovery action can now requeue failed LLM dispatch records.** The new
  `requeue-failed-llm-dispatch` mode is dry-run by default, targets the Gemma 4 model prefix unless
  overridden, resets terminal request state with an R2 ETag guard, and restores the Worker’s
  compact ready marker. It uses the dedicated dispatch-bucket credentials and does not change
  unrelated failed models.

- **Groq’s Llama 3.3 route now uses JSON-object structured output.** The live endpoint accepts
  `response_format: {"type":"json_object"}` but rejects JSON Schema; the compiled route profile
  now reflects that capability.

- **LLM structured-output behavior now follows compiled capability profiles.** Provider and route
  YAML profiles select JSON Schema versus JSON-object mode, native versus Instructor handling,
  prompt-schema embedding, and provider-specific schema relaxation; direct and queued requests use
  the same materialized route capability instead of inferring behavior from model names. Google
  routes now strip the size/range keywords rejected by the live API, Gemma 4 26B uses the available
  `gemma-4-26b-a4b-it` identifier, and retired Gemini 2.5 routes are removed. No stored artifacts
  or pipeline versions change.

- **Queued Google structured-output dispatch now applies the compiled schema relaxation upstream.**
  The Worker previously forwarded the caller's full Pydantic JSON Schema despite carrying the
  route's `strip_schema_keys` profile, causing Gemma 4 requests to fail with Google's generic
  `400 INVALID_ARGUMENT`. Only the provider-bound copy is relaxed; the canonical R2 request keeps
  the original schema for local validation and schema-retry behavior. No pipeline version or
  stored catalog artifact changes; existing terminal failures can be requeued after deployment.

- **Provider-align workers now preserve bounded timing windows through the local backend.** The
  internal process previously dropped the provider's coarse served-time segments, causing
  WhisperX to feed an entire meeting into one CTC convolution and request roughly 40 GB for a
  1.7-hour recording. Timed segments now reach WhisperX on both worker paths; any remaining
  section over five minutes is rejected before inference so the item can follow the normal ASR
  route instead of exhausting runner memory. Provider-align version 6 reopens prior provider
  alignments for gradual recomputation; full-ASR artifacts are not invalidated. Per-file logs now
  report audio seconds, elapsed alignment seconds, and realtime throughput.

- **Conditional R2 coordination writes now retry transient backend errors.** CAS-backed active-lease
  index maintenance now uses the same bounded retry path as ordinary uploads, so a temporary R2
  `PutObject/InternalError` does not leave an index prune or update to a later sweep. No pipeline
  version, stored artifact, or backfill behavior changes.

- **LLM dispatch queue reindex now backs off on R2 throttling.** The one-time migration uses four
  concurrent object operations and retries transient R2 429/5xx responses with jittered exponential
  backoff, so a temporary bucket read-pressure response does not abort an otherwise healthy scan.

- **Provider-align workers now load the configured WhisperX CTC model.** The internal worker no
  longer passes the faster-whisper `large-v3-turbo` transcription model to WhisperX; alignment
  recipes and both worker backends use `asr_alignment_model` (`WAV2VEC2_ASR_BASE_960H`). The ASR
  workflow also skips faster-whisper cache/download steps for align runners. `provider-align`
  version 5 reopens all prior provider-align work for gradual recomputation; full-ASR artifacts are
  not invalidated.

- **Free-plan LLM dispatch cron now has a queue-depth-independent ready index and bounded parallel
  routing.** Pending R2 requests write date-ordered compact `ready/` markers, so a scheduled
  invocation inspects a fixed lookahead of 16 marker bodies and reads canonical prompts only for
  viable candidates instead of scanning and parsing up to 1,000 queue records. A provider/model at
  capacity no longer blocks a later eligible route, and up to four independently paced requests are
  dispatched concurrently (`BATCH_CONCURRENCY=4`, `MAX_TOTAL_REQUESTS=4`). Provider/account ledgers
  remain authoritative. The historical Worker reindex and exact-estimate endpoints are retired to
  prevent unbounded scans. Existing R2 pending requests require the one-time
  `scripts/reindex_llm_dispatch_queue.py` marker migration, available as a dry-run-first manual
  GitHub Action; canonical prompts/results are unchanged, and no Citypods pipeline version or
  artifact backfill is involved.

- **Durable state restores now retry B2 ETag races.** `s3transfer`'s protective `If-Match` check can
  observe an object changing between its metadata request and ranged download; that specific
  download failure is now treated as transient and retried with a fresh ETag. No pipeline version,
  stored artifact, or backfill behavior changes.

- **Known-text provider alignment now uses WhisperX with a separate artifact lane.** Untimed Swagit
  and similar provider documents are cleaned of bracketed source-time markers, remapped to served
  time, and aligned with the configurable `WAV2VEC2_ASR_BASE_960H` model. The 90% gate is measured
  before interpolation; below-gate candidates are marked ineligible and deferred to full
  `large-v3-turbo` ASR on a later pass. Provider-aligned VTT/word sidecars no longer share full-ASR
  keys, and the existing H15 provider-align/asr-challenger evaluation records now measure the new
  output with the independent CTC judge. `provider-align` version 4 causes existing provider
  alignments to be reprocessed gradually; full-ASR artifacts are not invalidated by this change.
  Terminal work leases now carry the provider-align recipe version and reopen automatically on a
  later provider-align version, so the promised gradual backfill is not blocked by an earlier
  stable-ts success or failure; active unexpired claims remain protected.

- **Topic tagging lane now optimizes candidate scheduling and removes dispatch deadlines.** In the
  dedicated `tag` lane (`enrich --lane tag`, `tag.yml`), `_run_enrich_global_queue` pre-filters candidate
  episodes in memory so the thread pool only evaluates items that genuinely need rules derivation or LLM
  tagging, while preserving the full retained episode list for append-only records persistence. Tag LLM
  dispatches are submitted without an artificial wall-clock deadline (`deadline_at=None`), eliminating
  runner-side sleep pacing and ensuring queued jobs do not timeout while waiting on the Cloudflare Worker.
  A configurable per-run dispatch cap (`max_dispatches_per_run: 2000`) prevents worker queue flooding,
  and the tag workflow budget is reduced to 20 minutes (30-minute job timeout). This changes only runner
  orchestration and scheduling: no stored artifacts are invalidated, and no catalog backfill is required.

- **Swagit plain-text transcripts now use coarse constrained alignment when possible.** Standalone
  ``[HH:MM:SS]`` anchors are parsed into monotonic source-time windows, structural bracket labels
  are excluded from the alignment text, and the windows are remapped to served time before
  stable-ts ``align_words()`` runs. Files with too few, invalid, or unmappable anchors retain the
  full-alignment fallback. ``PROVIDER_ALIGN_PIPELINE_VERSION`` is bumped from 2 to 3; existing
  TXT provider-align artifacts without the new recipe marker are re-evaluated gradually, while
  existing VTT/SRT provider-align artifacts are not invalidated.

- **Swagit transcript discovery now fills the provider-link gap.** For Swagit episodes whose video
  page does not advertise a transcript, the transcript lane derives and probes
  `/videos/{id}/transcript`, stores non-empty VTT/SRT/TXT responses in the provider registry, and
  attaches the discovered link to the episode. Probe state is persisted: available endpoints are
  rechecked after 30 days, confirmed misses use 7-day-to-90-day exponential backoff, transient
  failures use 1-day-to-7-day backoff, and no more than 500 Swagit transcript requests are made per
  source per run. This is metadata/provider-source backfill only; no ASR or provider-align pipeline
  version changed and no stored transcript artifacts are invalidated.

- **Hosted-audio ASR downloads now retry HTTP 429 responses with a capped `Retry-After` delay.**
  The scheduled ASR matrix is also reduced to three transcribe workers and two provider-align
  workers to lower concurrent CDN pressure. This changes transport behavior and worker capacity
  only; no ASR or provider-align pipeline version changed, and no stored artifacts are invalidated
  or backfilled. Exhausted 429 attempts now defer rather than terminally wedge a lease; reconcile
  covers all transcript work classes, and an explicit class-scoped recovery command reopens failed
  leases left by earlier incidents. The manual Transcript Recovery workflow exposes that command
  with the same B2/R2 credentials used by production Actions.

- **Feed-health stale alerts now allow five median cadence intervals with a 45-day floor.**
  This replaces the previous 3×/30-day rule, reducing alerts during ordinary multi-week recesses
  while still flagging prolonged outages. It changes audit classification only; no catalog
  artifacts, pipeline versions, or backfill behavior are changed.

- **Provider transcript alignment now preserves word-boundary provenance.** Provider endpoints are
  probed for every discovered episode. A VTT with inline word timestamps is served as
  `provider-native`; cue-only VTT/SRT/TXT is aligned with stable-ts and served as
  `provider-aligned`; episodes without provider text use fresh ASR. Active records and the H15
  report now distinguish provider text/provider timing, provider text/computed timing, and ASR.
  H15 routing is source/body policy rather than an episode-level publication gate, so a changed
  route dynamically changes the served transcript. `PROVIDER_ALIGN_PIPELINE_VERSION` was bumped
  from 1 to 2; existing provider-align artifacts are re-evaluated/adopted under the new semantics,
  while ASR artifacts are not invalidated. Provider-selected episodes then enter a separate
  `transcript-asr-comparison` queue only after the ordinary ASR queue drains; it retains full ASR
  artifacts for H15 without replacing the served provider route.
- **Equivalent provider model selectors now share canonical logical keys.** The limits compiler
  emits a `model_aliases` map, coalesces selector-only duplicates for one physical provider/account
  quota bucket, and normalizes route entries before generating the Python and Worker
  catalogs. DeepSeek V4 Flash 0731 aliases now share one logical candidate pool across DeepSeek,
  SiliconFlow, and OpenCode; the equivalent OpenRouter/Kilo/OpenCode Nemotron free routes are
  likewise unified. Physical `route_id` entries remain separate, so provider/account quotas and ledger
  reservations are not merged. Existing provider-qualified selectors remain accepted through the
  alias map; no stored LLM result or pipeline artifact is invalidated, and no backfill is required.

- **Topic tagging is now pinned to Gemini 3.1 Flash Lite only.** Gemini 3.5 Flash Lite remains
  reserved for production chapter locating, preserving its independent free-tier capacity for the
  long-context locator workload. This changes the tag route allowlist only; tag prompts, recipe
  hashes, visibility calibration, and stored artifacts are unchanged, so no pipeline-version bump
  or catalog backfill is required.

- **Topic tagging now uses the asynchronous LLM dispatch Worker.** The tag workflow enqueues Gemini
  requests instead of holding a GitHub Actions runner while waiting for local quota windows; the
  Worker owns provider credentials, pacing, retries, and completion, and the deferred sweep makes
  results available to a later tag run. This is a transport/configuration change only: prompts,
  recipe hashes, tag visibility gates, and stored artifacts are unchanged, so no pipeline-version
  bump or catalog backfill is required. Existing direct deferred records remain compatible with the
  sweep.

- **LLM TPM admission now models average throughput instead of a hard one-minute request ceiling.**
  The Python CAS ledger and LLM dispatch Worker admit requests larger than one minute's declared
  TPM and persist an oversized-request cooldown proportional to `tokens / TPM`; ordinary smaller
  requests retain their normal token-rate burst. Rollover-only RPM/RPD/token bookkeeping is now
  persisted even when selection finds no route, so quota state does not remain on an old day key.
  This changes only ephemeral coordination state (`state/llm_budget.json` and the dispatch Worker
  budget); no durable catalog artifact is invalidated or backfilled.
  
- **External GPU-worker memory and billing telemetry now match the deployed resource model.** Modal
  settlement uses `Workspace.from_context().billing.report()` instead of the deprecated billing
  helper, with an explicit fallback when the report cannot be queried or has no matching function
  call. Modal/Beam workers sample process RSS once per second around claims, including the final
  sample before settlement, while Beam's scheduled and canary entrypoints now request 1 CPU and 4 GiB
  RAM. Beam's configured runtime rate is correspondingly updated to include GPU, CPU, and RAM pricing
  (`$0.0002672/s`). This is an operational admission/telemetry change only: no pipeline version was
  bumped, existing artifacts were not invalidated, and no backfill is required.

- **LLM RPM limits now pace submissions continuously.** Route-level RPM values and provider-level
  RPM values are translated into persisted `requests_available_at` schedules instead of burstable
  wall-clock-minute counters. Mistral is configured at a shared provider limit of 60 RPM (one
  submission per second) across all models and accounts. This changes only ephemeral coordination
  state (`state/llm_budget.json` and the dispatch Worker budget); no durable catalog artifact is
  invalidated or backfilled.

- **Dallas City Council feed now includes special-called full-council sessions** (GH#1121). A
  source may now declare `body_any` for explicit alternative provider labels; the shared selector
  is applied consistently by feed rendering, audits, reports, build validation, and search. The
  Dallas feed keeps `City Council Agenda Meetings` as its primary label and adds
  `Special Called City Council Meeting`, while continuing to exclude `Council Briefing` and
  committee bodies. This changes feed membership only: existing audio/transcript artifacts and
  stable episode UIDs are reused, with no pipeline-version bump or forced artifact backfill.

- **Addison City Council feed now includes Swagit's recurring `Work Session` and `Work Session and
  Regular Meeting` labels.** These rows were previously excluded by the `City Council` selector,
  causing the feed to appear stale despite recent council recordings. This changes feed membership
  only: newly matching rows enter normal discovery/materialization, with no pipeline-version bump or
  global artifact invalidation.

- **One-off body naming drift now has an exact exception path and audit coverage.** A feed may use
  `source.body_includes` for provider-GUID-specific rows without permanently broadening its body
  selector. Feed-health audits suppress historical excluded labels, flag recurrence of a known
  one-off label with its prior inclusion GUID, and flag newly observed excluded labels so city
  configurations can stay current. Fort Worth's single `Work Session` recording is covered this
  way (GH#1005); no pipeline-version bump or artifact backfill is required.

### Added

- **Durable resumable-LLM dispatch queue recovery.** Topic tagger/pre-labeler, production chapter
  agenda extraction/location, and the persisted R5 benchmark/tournament evaluator now enter the
  Cloudflare Worker queue without consuming the runner-side provider-quota ledger or inheriting a
  producer-run submit deadline. The deferred sweep upgrades their legacy pre-dispatch handles to
  the same queue-only path rather than retrying them directly through LiteLLM. City onboarding
  discovery remains explicitly direct because its caller must act on the response in that pass.
  The Worker now maintains a pending-only R2 index so retained terminal request history cannot hide
  ready work behind its bounded scan; authenticated `POST /v1/queue/reindex` repairs pre-index
  queue entries after the rollout. No public tag output or calibration policy changes; existing B2
  deferred handles and R2 queue records are retained and re-enqueued/reindexed in place.
  The Worker request cap is now 8 MiB (up from 512 KiB). Chapter tagging uses deterministic
  token-and-byte-bounded batches under a 7 MiB producer guard, excludes episode-wide unmapped
  backup packets, and records per-batch context telemetry; a single oversized chapter remains a
  distinct deferred condition rather than being silently truncated. `TAG_PROMPT_VERSION` is bumped
  to `3`, so existing LLM tag candidates are gradually re-run through the new chapter-only recipe;
  deterministic candidates, prior ledger evidence, and visible output remain available until each
  replacement completes.
  Each physical provider route now compiles separate input and output context limits from
  `config/provider_limits.yml`; queue and direct selection compare the two request estimates before
  quota admission, so a batch fitting only a larger allowed route cannot be sent to a smaller route.
  Terminal Worker failures now clear their unusable deferred handle and retain one bounded per-recipe
  failure audit record, tolerating up to three terminal failures in total before pausing that unchanged
  recipe for investigation. A locally detected malformed structured reply instead gets one immediate
  schema-correction clone through the Worker; a second malformed reply exhausts that recipe. Transient
  dispatch/transport failures remain pending for Worker-owned retry; no existing public tags are invalidated.

- **R5 unified tag calibration and evaluator overlay.** ([`review/42`](review/42-unified-tag-calibration-and-evaluator-overlay.md))
  Deterministic rule matches and chapter-only LLM candidates now share the existing persisted candidate
  ledger. The tagger keeps its 12-review/90% admission gate; an independent Gemma 4 31B pre-labeler runs
  in shadow mode and can qualify at 50 reviewed examples with 95% precision for likely-correct and
  likely-incorrect decisions, suppressing display without deleting evidence. Weekly review defaults to 80
  stratified candidates and reports distance to both gates. Stored candidates are re-projected after policy
  changes; the TagsStage version bump backfills the ledger/projection, retains superseded rows as hidden
  historical evidence, and lazily migrates usable chapters without a blanket catalog recall.
  Added the manual shadow benchmark workflow (`r5-benchmark.yml`) and separate
  `r5_tag_benchmark.json` artifact for 200–300 frozen chapters, human ground-truth labels, model
  disagreement, per-source pre-labeler metrics, evidence fidelity, and call/quota telemetry; an
  explicit maintainer approval is required before a route recommendation is eligible, and it cannot
  modify public tags or calibration state.

- **Multi-Provider Cloudflare Worker Dispatch Proxy & Per-Route Ledger.** ([`review/41`](review/41-multi-provider-llm-dispatch.md))
  Extended `workers/llm-dispatch-proxy/` and the Python compute layer to route Gemini/Mistral/DeepSeek/
  OpenRouter through one Worker with real multi-account API key rotation, replacing R10's original
  single-Mistral design. An initial pass of this work shipped with several bugs (a credential-disclosure
  risk, a double-reservation bug, and a silent default that routed Gemini through the Worker instead of
  calling it directly, breaking city discovery's synchronous design) — all fixed in this same change; see
  review/41 §2 for the full account and §3 for the corrected design:
  - `config/provider_limits.yml` (replacing `config/mistral_model_limits.yml`) gives every provider its
    own `api_base`/`chat_path`/accounts, compiled by `scripts/compile_llm_limits.py` into
    `workers/llm-dispatch-proxy/src/dispatch_limits.json`. The default compile is pure YAML→JSON, no
    network call; a provider's live model/pricing discovery endpoint (OpenRouter today) is fetched only
    via an explicit, maintainer-run `--discover` flag, never from the deploy workflow.
  - `workers/llm-dispatch-proxy/src/index.js` gained a per-route/per-account R2 ledger
    (`state/dispatch_budget.json`) that actually enforces each route's compiled `rpm`/`rpd`/`tpm` and
    rotates onto a sibling account once one is exhausted, a `GET /v1/queue/estimate` endpoint, an
    owner-tokened cron lease, and an upstream fetch timeout sized under the lease duration.
  - `LLMRequestPolicy` gained `allow_paid`, `allow_batch` (plumbed through, currently inert — no provider
    batch endpoint exists yet), `submit_next`, `deadline_at`, `require_direct`, and
    `allow_dispatch_overflow` (a dual-transport route like Gemini only dispatches over the Worker on this
    explicit opt-in; it otherwise always goes direct).
  - Refactored `mistral/mistral-large-latest` alias to canonical `mistral/mistral-large-2512`. **Backfill:**
    no durable artifact is invalidated — only ephemeral coordination-state entries
    (`state/llm_budget.json` inflight rows, `state/llm_deferred/*.json`) keyed on the old model string
    become unreachable post-deploy, which is already documented as loss-tolerant (review/33 §10.4/§10.6).

- **Multi-provider dispatch follow-up corrections** (same PR, review pass):
  - Worker `routeAvailable` now supports paid routes declaring only `concurrency` and enforces
    their `inflight` slots; concurrency reservations are released with CAS after each task.
  - `delete_dispatched_ref` now normalises path-style refs (`/v1/requests/chatcmpl-…`) and full URLs,
    not just bare `chatcmpl-…` IDs — handles store the `location` header, which is always a path.
  - Worker CAS-retry loop re-checks `routeAvailable` against the freshly loaded ledger before
    reserving, preventing oversubscription after a concurrent write.
  - Idempotency collision check now compares policy fields (`allow_paid`, `deadline_at`, …) alongside
    the chat payload, catching policy-only mismatches that were previously silent.
  - `reconcile` releases the inflight reservation when a handle's model has been removed from `ROUTES`,
    instead of silently leaking quota until the ledger entry ages out.
  - `select_and_reserve` guards against returning a `None` transport when reusing an in-flight
    reservation whose dispatch transport has been removed from the backend config.
  - Deploy workflow gains a `dispatch_limits.json` drift check to catch uncommitted recompilations.

- **Direct provider catalog and bounded dispatch execution.** The provider-limits compiler now emits
  the Python LiteLLM route catalog as well as the Worker catalog: all 52 physical account routes are
  deduplicated into 38 logical models, each with direct and dispatch transports, direct LiteLLM
  selector/base/key metadata, and a physical `route_id`. Python and the Worker use the same
  versioned `routes[route_id]` ledger shape, including optional cost and `inflight` fields, so a
  future shared R2/B2 CAS ledger does not require a format migration. The Worker now renews its cron
  lease with CAS, computes an effective run deadline with a 20-second finalization reserve, prioritizes fast
  requests while reserving a first-batch long-lane slot, bounds long-context requests to the long lane, reaps expired reservations, retains all
  sibling task outcomes with `Promise.allSettled`, and sanitizes upstream error details. **Backfill:**
  no catalog/artifact invalidation; existing ephemeral ledgers with logical keys remain readable,
  while new reservations use physical route IDs.
  Operators configure the lane/run bounds with `FAST_UPSTREAM_TIMEOUT_SECONDS`,
  `UPSTREAM_TIMEOUT_SECONDS`, `FINALIZATION_RESERVE_SECONDS`, `MAX_EXECUTION_SECONDS`,
  `BATCH_CONCURRENCY`, and `MAX_TOTAL_REQUESTS`; the Worker emits `deadline_guard` when queued
  work cannot safely fit. See the Worker README's [Scheduling lanes](workers/llm-dispatch-proxy/README.md#scheduling-lanes).

### Fixed

- **H15/R5 ingest workflows could double-comment or leave a persisted decision unconfirmed on retry.**
  `asr-quality-ingest.yml` and `llm-tag-review-ingest.yml` each persist a review decision, then separately `gh issue comment` and `gh issue close` the source issue. A GitHub API failure between those steps left a durable decision recorded with no confirmation posted, and a retry re-ran the comment/close pair unconditionally — double-posting the comment if it had actually succeeded before the close call failed. The persist step was already safe to re-run (`record_review()` / `ingest_review_decision()` overwrite by candidate/sample identity, not append), so the fix is confined to the comment/close step: check existing comments for a stable `<!-- h15-ingest:N -->` / `<!-- llm-ingest:N -->` marker before commenting, and check the issue's current state before closing, mirroring the find-or-update comment pattern already used in `dep-bump-smoke.yml`.

- **Ingest workflows failed on scheduled runs and unreviewed issues.**
  The `asr-quality-ingest.yml` and `llm-tag-review-ingest.yml` workflows unconditionally ran `gh issue comment` and `gh issue close` inside a subshell with error trapping. When processing unreviewed open issues on scheduled fallback sweeps, `parse_issue_decision` and `parse_review` raised `ValueError`, causing subshells to fail with exit code 1, which marked `failed=1` and failed the entire scheduled workflow run in GitHub Actions.
  Fixed by:
  - Returning `{"stored": false, "reason": "no_decision_checked"}` with exit code 0 from `citypods transcript-quality ingest-review` and `citypods llm-evaluation ingest` when no decision checkbox is selected.
  - Adding `"stored": true` to `ingest_review_decision` results in `transcript_quality.py`.
  - Guarding `gh issue comment` and `gh issue close` behind `if jq -e '.stored == true' ingest.json` in both ingest workflows so unreviewed open issues are cleanly skipped without failing CI.

  The `llm-tag-review-ingest.yml` workflow was configured to ingest all open calibration issues on its scheduled run, but if triggered manually (`workflow_dispatch`) without an explicit issue number, it skipped the ingest block entirely instead of falling back to the same open-issue sweep. It now performs the full open-issue sweep on manual runs when no issue number is provided.

- **Calibration ingest job stuck per-issue due to full state snapshot sync.**
  `llm_tag_review.py` `ingest()` (and `package()`) called `push_state()` with no scope, causing a full upload of the entire state snapshot after recording each single review decision. Additionally, `ingest()` called `pull_state()` with no scope, downloading all episode records for all cities despite only needing `llm_evaluation.json`. `tournament.py` had the same bug — it called `push_state()` unscoped inside the per-episode loop (one full catalog upload per episode processed) and again at the end.

  Fixed by:
  - Adding `only_paths` support to `pull_state()` (mirroring `push_state()`'s existing API), so callers can fetch a single file instead of the full snapshot.
  - Scoping `ingest()`'s `pull_state()` call to `only_paths=[config.state_path]` (i.e. just `llm_evaluation.json`).
  - Scoping `ingest()`, `package()`, and both `tournament.py` `push_state()` calls to `only_paths=[<state file>]`.
  - Redirecting `pull_state` and `push_state` logging to `stderr` in `ingest()` (and `package()`) so each stage (pull, parse, push) is visible in Actions logs without polluting `stdout` (which is redirected to `ingest.json` and parsed as JSON by `jq`).

- **Tag calibration ingest failed when marking checkboxes on the digest issue.**
  The `llm-tag-review-ingest.yml` workflow was missing a title check for the `issues` (edited) trigger. When a maintainer checked a progress-tracking checkbox on the parent digest issue (`R5 LLM tag calibration digest`), the workflow attempted to parse it as a review decision, failing with `ValueError` and exiting without commenting or closing anything. Added a `grep -q '^R5 LLM tag sample '` check to the `issues` event branch so the workflow only processes edits to the child issues where the actual review decisions live.

- **Provider chapter starts inside removed silence now snap to the next kept served boundary.**
  This preserves markers for the next agenda item after a removed recess/silence span while still
  dropping markers with no later kept audio. Chapter/tag source-index alignment and remap regression
  coverage were updated; canonical provider chapter records remain unchanged.

- **ASR runs failing intermittently from two unrelated causes, mixed together in CI's "failure" verdict.**
  Auditing recent `asr.yml` runs (workflow history + job logs, not just code review) showed the
  reconcile-step `NotImplementedError: backend 'b2' is not cas_capable` crash was already fixed
  (see the `work-leases-index/` routing entry below) but two other causes were still live and
  distinct from it and from each other:
  - **Hosted-audio download connection drops.** `ChunkedEncodingError`/`IncompleteRead` while
    streaming the multi-hundred-MB audio file from B2/R2 killed the claim with zero retries —
    `_download_audio_file()` (`citypods/stages.py`) did a single `requests` GET with no
    retry around the `iter_content()` read loop. Now retries up to 4 attempts with exponential
    backoff (2s/4s/8s) on `ChunkedEncodingError`/`ConnectionError`, re-downloading the whole file
    from scratch each attempt. The stream is also capped at 1 GiB per attempt
    (`HostedAudioTooLargeError`, not retried) — hosted audio is our own ≤96 kbps mono AAC encode,
    so a legitimate file is well under that, and the cap bounds disk use if a response is
    malformed or hangs open across the retry attempts.
  - **Media-decode quarantine silently skipped on the GitHub Actions/local-subprocess ASR path.**
    `_is_deterministic_media_decode_error()` (`citypods/compute/external_worker.py`) is supposed to
    quarantine a recording whose audio can't be decoded (`IndexError: tuple index out of range`,
    etc.) instead of leaving it to fail and re-fail every run. The killable local-subprocess ASR
    backend (`ProcessLocalBackend.run_inference`, `citypods/compute/local_process.py`) re-raises
    worker-side exceptions as a plain `RuntimeError` whose message embeds the original type name
    (`"local inference worker IndexError: tuple index out of range"`), which the classifier's
    `isinstance(exc, IndexError)`/`type(exc).__name__` checks never matched — so on-runner decode
    failures kept hitting the generic failure path (and CI's exit code 1) forever instead of being
    quarantined. Added `LocalInferenceWorkerError`, which preserves the worker's original exception
    name/message as attributes, and taught the classifier to unwrap it.

  Both were confirmed against real failed runs (workflow IDs 226/221/214 for the download drops,
  227/213 for the decode errors) rather than reproduced synthetically. Neither one actually failed
  the whole batch — GitHub Actions marks a job `failure` on exit code 1 even when e.g. 7 of 8
  claimed episodes in that worker's batch succeeded — but both are worth fixing so a transient
  network blip or an already-known-bad recording stop consuming a "failed" run and, in the decode
  case, stop re-attempting a recording that can never succeed until its audio changes.

- **`LLM Tag Calibration Ingest` / `ASR Quality Ingest` failed on every run, silently.** Both
  workflows' `resolve`/`finalize` jobs passed `token: ""` to `actions/checkout@v6` intending an
  anonymous, no-token sparse checkout; the pinned checkout version's bundled code calls
  `core.getInput('token', { required: true })` unconditionally, and `@actions/core`'s `getInput`
  treats an explicitly empty string the same as "not supplied" regardless of the action.yml
  schema's default — so the checkout step threw `Input required and not supplied: token` on every
  invocation, before the `ingest` job's own `set +e` failure-tolerance even ran. Every reviewer
  checkbox on an `R5 LLM tag sample …` / `H15 sample …` issue this week was therefore never
  ingested, regardless of how it was filled in. Fixed by dropping `token: ""` and letting checkout
  default to `github.token`, already scoped down to `issues: read`/`issues: write` by each job's
  own `permissions:` block, with `persist-credentials: false` unchanged.

- **`zoning-reform` fired on individual-property rezoning cases instead of code-wide zoning
  reform** (confirmed on real open calibration issues: GH #1057/#1062/#1072/#1076 — "PUBLIC
  HEARING FOR ZONING CASES," individual PD/SUP/replat/variance items). Split into `zoning-reform`
  (citywide/district-wide text amendments, code rewrites) and a new `rezoning` tag (individual
  parcel rezonings, planned-development cases, specific/special use permits, replats, variances),
  each with a description that explicitly cross-references the other to disambiguate them for the
  LLM path. `config/taxonomy.yml` bumped to `version: 2`.

- **`neighborhood-engagement` fired on standard, every-meeting hearing sign-up boilerplate**
  (confirmed on real GH #1068 — a phone-number sign-up instruction, not an engagement
  opportunity). Tightened the tag's description (the LLM path's only signal — it never sees the
  keyword lists) to explicitly exclude recurring procedural notices, removed the overly generic
  `public meeting` keyword from the rule path, and added a small defense-in-depth exclude list of
  common hearing-procedure phrases. The load-bearing fix is structural, not keyword-based — see
  the agenda-text-preamble-stripping entry below.

### Added

- **Source-grounded agenda chapter research contracts.** Agenda extraction now preserves immutable
  source evidence and identifier references, with pure timed-transcript locator request contracts
  and offline validation tests. These contracts are not wired into episode materialization yet.
- **Reusable chapter-locator research toolkit.** The repository now contains read-only cohort
  builders, retrieval/scorer evaluators, packet runners, and localhost adjudication tools under
  `scripts/research/agenda_chapters/`, isolated behind the offline `chapter-research` dependency
  profile. The tools never pass provider labels to models and never mutate episode records.

- **Audio existence checks now use persisted trust with a bounded audit backstop** ([GH#1024](https://github.com/BashfulBits/city-meeting-podcasts/issues/1024), child of [GH#1012](https://github.com/BashfulBits/city-meeting-podcasts/issues/1012)). Successful audio reuse, credit, and upload paths persist the immutable key/spec verification marker, which is also invalidated by a storage-backend generation/epoch change (e.g. bucket replacement or restore), not just a key/spec mismatch. Matching trusted pointers skip routine storage probes; small dirty sets use direct existence checks and larger batches escalate to the existing single-prefix cache. A daily rotating audit sweeps every trusted pointer in one of 32 stable hash-based partitions (concurrent HEAD checks, no per-run item cap), so the whole catalog gets a full sweep monthly regardless of size; a wall-clock budget bounds run time instead, skipping (not failing) remaining sources once spent, and clears missing audio pointers and the Audio completion marker so the normal lane rebuilds them. Legacy, changed, and repaired pointers remain fail-closed. No audio pipeline-version bump or encoded-byte backfill is required.
- **Body-aware three-tier retention and gradual archive backfill** ([review/39](review/39-body-aware-tiered-retention.md)). All feeds now inherit 500 RSS-visible episodes per body, retain hosted audio and every artifact through 2,000 per body, and retain metadata plus non-audio artifacts through 10,000 per body. The shared source record store contains the union of body windows, preventing active boards from evicting quieter ones; audio is removed only from the metadata-only tier and reclaimed through normal orphan GC. Feed-visible work is prioritized before bounded 501–2,000 backfill under the existing wall-clock budget. No pipeline-version bump or forced re-encode is introduced; pre-existing artifacts remain valid and the deeper cohort fills gradually.
- **Agenda backup/attachment document text is now used for tagging, and its discovery no longer
  depends on English keyword matching.** Backup documents were already fetched and text-extracted
  (`AgendaTextStage`) but silently unused by both the rule and LLM taggers; `episode_tag_inputs()`
  now folds this text in. Getting there required generalizing the backup-document pipeline itself,
  validated against real, currently-live agendas from two independent platforms (Legistar,
  Granicus) fetched during investigation, not synthetic fixtures:
  - Discovery no longer requires an English keyword (`agenda`/`packet`/`backup`/`attachment`/
    `supporting`) in a link's label or URL — a real gap, since a different city's agenda platform
    may label these links entirely differently, or not with words at all (confirmed on Legistar's
    bare "File #" links).
  - New content-based chapter/item attribution (`attribute_links_by_content`,
    `citypods/agenda_text.py`) matches a backup document to its agenda item via an embedded case
    identifier or the item's title — confirmed live on both a real Granicus agenda (backup
    filenames embed the case number, e.g. `PD20-25`) and a real Legistar attachment page (the
    per-item detail page repeats the file number and title verbatim). Replaces
    `attribute_links_to_chapters`'s page-position-proportional guess as the primary mechanism
    (kept as a documented fallback) — that function and `extract_backup_item()` were designed in
    [review/29](review/29-agenda-text-extraction.md) §6a but had zero call sites until now.
  - A bounded second hop (one extra fetch per originally-discovered link) follows a linked page
    when its own fetched content confirms — by the same content-match, not a page-shape guess tied
    to any one provider — that it's an item's own detail/attachment-enumeration page (the real
    shape of Legistar's `MeetingDetail.aspx` → `LegislationDetail.aspx` → Attachments chain).
  - Meeting-notice/hearing-procedure boilerplate that precedes an agenda's first resolved chapter
    title is now excluded from tagging input entirely, at both the rule and LLM path
    (`resolve_chapter_spans`/`_strip_preamble`, `citypods/tags.py`) — validated directly against
    the real document behind the GH #1068 false positive above, not a synthetic approximation.
  - The material sent to the tagging LLM is no longer truncated to a small fixed character count;
    a pre-flight check (`llm_tag_suggestions()`) instead compares the real estimated token count
    against half of every allowed `tpm`-capped route's budget (accounting for the structured-call
    worst-case double-attempt reservation, `citypods/compute/llm.py`) and only distinctly flags/
    defers the rare payload that could never fit any window at all — an ordinary "fits, but not
    this minute" case is already handled correctly by the existing token-aware reservation ledger
    (`citypods/compute/llm_budget.py`), which was previously undermined by an unrelated fixed
    truncation. If this new signal fires only occasionally in production, the intended next step
    is routing those calls to a route with no `tpm` cap (Mistral, DeepSeek); if it fires
    frequently, truncation is the more appropriate fix — neither is implemented yet, this just
    makes the decision measurable.
  - `TAGGER_VERSION` bumped `"1"` → `"2"` so already-tagged episodes reprocess under the new logic.

- **ASR transcript-record commits are now batched per source, not pushed once per episode**
  ([GH#1019](https://github.com/BashfulBits/city-meeting-podcasts/issues/1019), child of
  [GH#1012](https://github.com/BashfulBits/city-meeting-podcasts/issues/1012)). Every successful
  transcript previously called `push_records_merged()` — a whole-source fetch+merge+put of
  `sources/<src>/episodes.json` — immediately; on the largest inspected source (~5,480 records) 59
  of one run's 93 successes each paid that full-file round-trip for a single uid's delta.
  `ExternalTranscribeWorker` (shared by external Modal/Beam and internal ASR workers) now queues a
  successful commit into an in-memory per-run batch and flushes one `owned_uids`-scoped
  `push_records_merged()` call per 5 queued records, 1800 seconds, or end of run — whichever comes
  first — cutting the number of whole-source round-trips from one per episode to roughly one per
  batch. (The age bound shipped at 120s first; raised to 1800s after finding every backend's own
  `min_runtime_seconds` floor — 180–240s, `config/site_config.yml` — already exceeded 120s, which
  had capped real-world batches at ~2 instead of 5 regardless of the item-count bound. A regression
  test now locks in a realistic per-item gap across a full batch.) Lease liveness needed no new
  keepalive thread: `lease_ttl_seconds` (6–20h) already dwarfs the batch window given the existing
  per-item renewal thread's minutes-fresh refresh at queue time. A failed flush remains queued for
  another in-process attempt (the owned-block merge is idempotent); if the process exits before a
  later attempt succeeds, the in-memory batch does not survive it — the durable artifact is instead
  re-adopted and its record re-queued by a later run. Media-decode-quarantine/timeout-backoff paths
  are unchanged (still immediate, single-item pushes). Each flush now logs its `sources`/`records`/`payload_bytes`/`elapsed_s` for real
  production measurement. This is the "same-source commit batching" option from the two the issue
  proposed; the sidecar/per-uid-object alternative was investigated directly against R6/R7's actual
  record-shape additions and Backblaze B2's real pricing (transactions are entirely free; egress is
  free up to 3× average monthly storage) and found not currently justified — worked numbers and
  concrete re-open triggers in the design doc, not "once R6/R7 ship." Design:
  [review/18 §4.8–§4.9](review/18-work-distribution-sharding.md#48-batched-transcript-record-commits-gh1019--implemented).

- **`compute reconcile`'s Stage-2 work-lease sweep now costs `O(active leases)`, not `O(backlog)`**
  ([GH#1018](https://github.com/BashfulBits/city-meeting-podcasts/issues/1018), child of
  [GH#1012](https://github.com/BashfulBits/city-meeting-podcasts/issues/1012)). The prior
  candidate-probe `reap()` GETted every pending `transcript-asr` item's lease key regardless of
  how many were claimed — live runs measured ~9–11 minutes probing 6,034 keys for zero active
  leases. `ops/work_leases.py` adds a fixed/sharded CAS-managed active-lease index (64 buckets,
  `work-leases-index/bucket-<n>.json`) that `claim`/`renew`/`release`/`abandon` optionally
  maintain (`update_index=True`, on for both external and internal ASR workers); `reap_indexed()`
  sweeps only the bounded bucket set, re-validating every entry against the real lease object
  (still claim authority) before applying the same settle/requeue/leave decision as before. A
  rotating one-partition-per-run integrity sweep recovers a lease whose index write raced a crash.
  `reconcile_compute(..., use_lease_index=False)` (`work_lease_index_enabled: false` under
  `defaults:`) reverts to the original candidate-probe sweep with no code change. Design:
  [review/18 §4.7](review/18-work-distribution-sharding.md#47-active-lease-index-gh1018--implemented).

### Fixed

- **`ASR Quality Eval` silently produced zero H15 samples for 3 consecutive weekly runs**
  (2026-07-13, 07-20, 07-27) while reporting green. `asr-quality-eval.yml` never had an `ffmpeg`
  install step; `citypods transcript-quality evaluate` clips each sampled candidate's audio with
  `ffmpeg` before scoring, so every one of the 8 samples found each week failed with
  `FileNotFoundError: 'ffmpeg'`. That error is caught per-sample by design (one bad sample
  shouldn't sink the whole batch), so `evaluate` finished with 0 rows written to the rollups
  ledger and the job's own exit code stayed 0 throughout — `ASR Quality Review` then correctly
  found nothing to package, leaving the weekly parent issue empty with no signal that anything
  was wrong. Added the same checksum-pinned static-ffmpeg install (`FFMPEG_URL`/`FFMPEG_SHA256` →
  `scripts/install_static_ffmpeg.py`, prepended to `PATH`) already used by `asr.yml`/`audio.yml`.

- **`compute reconcile` failed every run since the GH#1018 active-lease index shipped**, crashing
  with `NotImplementedError: backend 'b2' is not cas_capable; get_bytes unavailable`. The index's
  `work-leases-index/bucket-<n>.json` CAS objects were never added to `RoutingStorage`'s
  `COORDINATION_PREFIXES` (`citypods/storage/routing.py`), so every bucket read/write fell through
  to the B2 primary backend, which doesn't implement compare-and-swap, instead of routing to R2.
  `work-leases-index/` is now registered alongside `work-leases/` and `provider-leases/`, with the
  matching ephemeral-prefix declaration (a lost bucket is re-derived from the lease objects, which
  remain claim authority, via the integrity sweep).

- **An authenticated Cloudflare Worker fallback covers Swagit list-page `403`s from GitHub-hosted
  runners.** Paired local/GitHub-Actions probes ([PR #1011](https://github.com/BashfulBits/city-meeting-podcasts/pull/1011)),
  plus production Audio #257-#259 and the LLM tag-lane enrich, showed every known Swagit tenant's
  list/view page (`SwagitProvider.fetch_episodes`) returning `403` (`server: awselb/2.0`, an AWS
  load balancer) from GitHub Actions egress while the same requests succeeded cleanly from a
  residential network under heavier load — the same shared-egress-reputation signature already
  diagnosed and fixed for Granicus media (GH#300/#353), on a different host class
  (`<tenant>.new.swagit.com` list pages, not the `archive-video.granicus.com` media CDN).
  `workers/swagit-list-proxy` is a sibling Cloudflare Worker to `granicus-media-proxy`, narrowly
  scoped to `/views/...` list pages: bearer authentication, tenant-hostname allowlist, a single
  bounded `page` query parameter, no upstream redirects. `citypods/swagit_proxy.py` wraps the
  list-page GET with the same direct-first, single-Worker-attempt-on-403 shape production already
  uses for Granicus; unset `SWAGIT_PROXY_BASE_URL`/`SWAGIT_PROXY_TOKEN` is a no-op. Both the direct
  and Worker-proxied requests refuse redirects outright and re-validate their target through the
  SSRF gate immediately before the request, rather than relying on validation from an earlier point
  in the call chain. Deployed and confirmed working end-to-end (a direct authenticated request from
  a residential network returned `200`). Not yet covered: per-tenant transport telemetry (would
  need a broader `SwagitProvider` interface change to thread `ctx` through) and
  `fetch_chapters`/video-page fetches (not observed failing; the Worker doesn't accept that path
  shape yet).

### Fixed

- **Every Dallas Swagit feed (35 sources, one shared list page) failed every Audio run for days
  with `GET https://dallastx.new.swagit.com/views/default/city-council returned 502`.** Not a new
  GitHub-egress block: Swagit now resolves that legacy `views/default/...` alias with a same-tenant
  `302` to its canonical numeric view (`views/113/city-council`), confirmed live. GitHub Actions
  still gets denied direct access to the Dallas tenant (the `403` shape above), so the request
  correctly fell through to `workers/swagit-list-proxy` -- which then blanket-refused *any*
  non-`/download` `3xx` from upstream as a synthetic `502` (a guard against redirect-based SSRF),
  turning Swagit's own benign alias resolution into a permanent failure with no further fallback.
  Fixed both ends: `config/feeds/dallas-tx-*.yml` now point directly at the canonical
  `views/113/city-council` URL, sidestepping the alias; and `workers/swagit-list-proxy` now follows
  exactly one redirect hop when the target is still an allowed host and accepted path shape (mirrors
  the granicus-media-proxy `304` fix, [CR-WK-04](review/19-coderabbit-findings-audit.md)) instead of
  refusing every `3xx` outright, so the next alias/renumbering Swagit does doesn't reproduce this.

### Changed

- **Production Pages deploys now render without provider refresh (GH#1023).** `deploy.yml` invokes
  the existing records-only `build --phase render --no-refresh` path, so a provider outage cannot
  block publication of the last-known catalog. The build log reports canonical-state age, oldest
  source-refresh age, due sources, and refresh errors; the later discovery-centralization design in
  [`review/38`](review/38-discovery-centralization.md) remains separate. No pipeline version or
  artifact backfill changed.
- **Deferred LLM reconciliation now uses a route-partitioned B2 pointer index (GH#1022).** Pending
  `tag` and `classify-civic-platforms` records are indexed under the existing `ROUTES` model keys
  (one small pointer object per record, no shared aggregate file and no time-bucket layer -- no
  code path persists a genuine future retry time for these records, so bucketing by day would
  only add LIST calls with nothing to skip). The sweep lists only the route partitions for models
  with current ledger capacity, then re-verifies canonical records before acting; stale or missing
  pointers are safe. Migration is dual-read until `scripts/llm_deferred_sweep.py --repair-index`
  rebuilds the index. This is metadata-only: no model-output pipeline bump, retry-semantic change,
  or automatic backfill is required; rollback is to omit the repair marker and use the canonical
  full listing.
- **Audio now skips empty matrix shards.** GH#1021 adds a canonical preflight that restores the
  durable state once, emits a fingerprinted source-atomic plan and a dynamic matrix containing only
  positive-load shards, then packages that snapshot for workers. A fully idle Audio cycle produces a
  visible successful no-op; no artifact or pipeline version is invalidated.

- **Granicus sustained-probe parsing is offline-safe.** Custom `--clip` arguments now perform
  syntax/allowlist validation during argparse without DNS; the probe still performs the full
  resolving SSRF check immediately before ffmpeg runs. This prevents unit tests and local offline
  validation from depending on DNS availability.

- **B2 durable state sync is now manifest- and dirty-path-driven (GH#1015).** A versioned
  `state/catalog/manifest.json` lets warm workers GET only new or changed JSON/JSONL objects;
  central state writers register exact dirty paths, and explicit tombstones are required for
  removals. Manifest publication uses conditional CAS when the backend supports it and otherwise
  retains the existing safe full-sync/list fallback. No pipeline-version bump or artifact backfill
  is required.

- **Conditional source refresh and dirty episode planning now form the S1 efficiency foundation (GH#1014).** `SourcePipeline` invokes each adapter's `detect_change()` probe, persists validator/content-digest state in `state/source_refresh.json`, and compares a canonical normalized input fingerprint per stable episode UID. Unchanged validator-backed sources skip full list parsing; validator-less adapters use the safe fetch-and-digest path (with configurable TTL/full-refresh bounds), and only new/materially edited UIDs enter heavy-stage planning. Append-only archives, stable provider-migration UIDs, SSRF validation, and all content-addressed artifact hashes remain unchanged; no pipeline-version bump or automatic artifact backfill is required.

- **Swagit's Worker fallback now covers all tenant-page requests used by enrichment.** A recurring
  LLM topic-tag failure showed that the initial fallback covered only archive lists and its secrets
  were not wired into the tag workflow. The tag and Audio lanes now receive the proxy configuration;
  `/videos/{id}` chapter/legacy-segment pages and `/videos/{id}/download` resolution use the same
  direct-first fallback as lists. The Worker remains narrowly allowlisted and never follows a
  download redirect; the Python provider validates its returned target before media use. Because
  redirects are disabled on these fetches, `fetch_chapters`/`_page_segment_objects` now require a
  2xx response rather than merely rejecting `>=400`, so a bare 3xx is rejected instead of being
  silently parsed as an empty page. This is a transport-only correction with no pipeline-version
  bump or artifact backfill.

- **Unchanged episodes now use durable dirty-stage completion markers (GH#1013).** Each episode
  records a versioned input fingerprint and terminal state for enrichment stages, including
  complete-empty and identity results. Legacy records are classified lazily from their existing
  artifacts, and subsequent runs omit clean episodes from stage invocation; relevant URL/hash,
  repair, or pipeline-version changes invalidate only the affected stage. This is metadata-only
  scheduling state: no output-affecting pipeline version was bumped and no artifact backfill is
  required.
- **ASR claim admission now respects the scheduled handoff while draining admitted work (GH#1017).**
  Internal workers use the existing runtime estimator against the earlier of the 5-hour handoff
  (with a 10-minute upload/commit reserve) and the hard backstop, so work that cannot finish in the
  current window is not downloaded or started. A queued successor now stops admission only; it no
  longer terminates healthy native inference already in progress. Lease renewal and hard/explicit
  termination behavior remain intact. No ASR pipeline version or artifact backfill changed.

- **The daily deferred-LLM sweep now reuses one registry snapshot per run (GH#1020).** Selection,
  expiry pruning, and the final pending count share one ordered B2 listing and one decode per
  record instead of independently traversing the registry three times. Completed records and
  pruned entries are applied to the in-memory view, while the existing public one-off helpers keep
  their behavior. No schema, model-output, pipeline-version, or artifact backfill change is
  required.

- **The LLM scheduler now spreads load across equally-eligible free routes instead of always
  favoring whichever sorts first alphabetically, and the deferred sweep's ledger accounting and
  ordering got a further round of fixes.** `select_route` picks among tied free/equal-cost/
  simultaneously-eligible candidates by *current utilization* (remaining RPM/RPD headroom on
  whichever axis is tightest), not just model name -- previously `gemini-3.1-flash-lite` won
  every tie against `gemini-3.5-flash-lite` regardless of how close it was to its own ceiling, so
  the second route's independent free-tier pool sat almost entirely unused. A rejected (429)
  attempt no longer counts against the proactive request ledger -- only the specific attempt that
  was turned away is excluded from settlement, not the whole call, so a structured retry's real
  first attempt (which reached the model and merely failed validation) still stays billed. The
  deferred sweep's capacity-exhaustion cache is now keyed on the resolved candidate route pool
  (the model set + paid gate `select_route` actually evaluates) instead of
  `(task, structured_output, purpose)`, so two different features drawing on the same underlying
  quota pools benefit from a single exhaustion determination instead of each independently
  re-discovering it. The registry stream is now ordered oldest-`last_modified`-first (free from
  the listing, no extra reads) instead of arbitrary key order, and the sweep logs a per-pool
  breakdown of how many records were skipped once a pool proved exhausted.

### Fixed

- **`tag.yml` runs still got hard-cancelled by GitHub's job timeout with nothing persisted, even
  after several rounds of narrowing specific in-pass cost sinks (state restore parallelization,
  duration-heal gating, input-fingerprint short-circuiting, the wall-clock check ordering inside
  `TagsStage` -- see the entries below).** None of those touch `_run_enrich_global_queue`'s
  source-prepare pass (step 1: a `ThreadPoolExecutor` running `fetch_merge` over every unique
  source in scope) -- it runs to completion unconditionally, with no `ctx.stop()` check anywhere
  in that loop, *before* any of the stage processing the tag lane's graceful-yield deadline governs
  even starts. A slow-fetching backlog (cities with many committee/board sources, plus the added
  latency of the new Swagit Worker fallback relay above) can alone exceed a tight job timeout
  regardless of how well-tuned `tag_run_time_budget_minutes` is. Rather than chase another specific
  cost sink, `tag.yml`'s job `timeout-minutes` is now 240 (was 25), mirroring
  `llm-deferred-sweep.yml`'s existing headroom -- not a completion guarantee (source-prepare still
  has no bound of its own, see below), but real additional room for it to finish in practice;
  `tag_run_time_budget_minutes` (`config/site_config.yml`) is now 240 to match (window = 204m via
  the existing `budget_safety`, leaving the same ~36m tail `run_time_budget_minutes` already uses)
  so the stage-processing budget that was already correctly implemented gets a real amount of time
  to do LLM work once prepare completes, instead of inheriting whatever scraps were left under the
  old 25-minute cap. The source-prepare pass itself still has no time bound of its own -- if it
  ever needs one, it needs its own `ctx.stop()` check inside that loop, which this change does not
  add.

- **Swagit and Granicus requests now share one denial-recovery transport.** Provider adapters use a
  single SSRF-gated request API that retries denied-access responses (especially HTTP 403) and
  exhausted transport errors once through each provider's narrowly allow-listed, authenticated
  Cloudflare Worker. Audio, render, tag, audit, contracts, and availability workflows receive both
  Worker configurations, and the Granicus Worker now supports bounded metadata/player endpoints in
  addition to native archive media. Direct success still costs one request and clean dirty-stage
  skips remain untouched: no pipeline version changed and no artifact backfill or extra B2/R2 read
  is introduced.

- **Scoped lanes now upload only the run-event file created by the current run (GH#1016).** The
  append-only `run_events/` push uses an exact relative path returned by the run-history writer,
  so retained historical events are not rescanned and re-uploaded on every Audio/ASR shard. The
  general prefix-scoped state sync API remains unchanged for source ownership; no pipeline-version
  bump or artifact backfill is required.

- **Archive rows that the source retention cap will immediately prune no longer consume document,
  timeline, audio, or ASR work ([GH#1025](https://github.com/BashfulBits/city-meeting-podcasts/issues/1025)).**
  Planning and final persistence now share one deterministic prospective-retention helper
  (`merge_records` → `prune_archive`): the full provider observation set still reaches the
  authoritative append/prune write, but only surviving stable UIDs enter enrichment. This breaks
  the Granicus archive-expansion loop in which Fort Worth repeatedly downloaded/decoded old MP4s,
  rediscovered existing content-addressed M4As as hundreds of audio “credits,” then discarded those
  pointers under the 5,000-record cap and repeated the work next run. Bounded per-source logs report
  fetched, retained, and suppressed counts without new B2 telemetry. Retained rows missing a real
  pointer still use the existing credit path; repair flags do not bypass retention. There is no
  pipeline-version bump and no artifact backfill.

- **`select_route`'s pacing retry-time prediction could busy-retry a genuinely daily-exhausted
  route for hours instead of correctly waiting for the real reset, discovered live in the first
  production run of the deferred-sweep changes above.** `_next_quota_reset` offered "next minute"
  as a candidate reset time whenever the ledger's per-minute window had merely been *checked*
  during the current minute -- true on nearly every call, since checking availability itself
  stamps that key -- regardless of whether RPM/TPM were anywhere near their cap. When the real
  (and only) blocker was the daily quota, `min()` still picked that bogus near-immediate time over
  the correct tomorrow reset, so `LiteLLMBackend._run_policy_job_paced` (which never gives up on a
  non-`None` `retry_at`) would sleep ~0s, recheck, see the same "exhausted" result, and repeat --
  burning the caller's entire deadline on one route (observed live as an unbroken stream of
  `llm rate limit: ... pacing 0s` log lines) instead of reaching whatever else was queued behind
  it. `_next_quota_reset` now only offers a reset-time candidate for the axis (RPM/TPM/RPD/a
  reactive block) actually responsible for the current `available()` failure.

- **The `_next_quota_reset` fix above was incomplete: a stale `blocked_until` timestamp reproduced
  the identical busy-retry-forever symptom it was meant to fix, confirmed live on the very next
  sweep run after that fix merged.** `LLMBudget.block()` only ever extends `blocked_until` forward
  and never clears it, so a route blocked earlier by a real 429 keeps that (now past) timestamp in
  its ledger entry long after the block itself expired. `_next_quota_reset` added it to the reset
  candidates unconditionally, so once the daily quota was *also* exhausted, the stale past
  timestamp always won `min()` over the correctly-computed future "tomorrow" reset -- `retry_at`
  came back in the past, `_pacing_wait_seconds` computed `wait <= 0` and returned `0.0` rather than
  giving up, and the pacing loop spun forever. `_next_quota_reset` now mirrors the same in-effect
  check `LLMBudget.available()` already uses (`now < blocked_until`) before offering it as a
  candidate at all. Traced the full pacing chain to confirm no other axis can reintroduce the same
  failure: `_pacing_wait_seconds` itself has no independent defense against a past `retry_at` --
  it gives up only on `retry_at is None` or `retry_at >= deadline_at`, so correctness rests
  entirely on `select_route`/`_next_quota_reset` upstream never handing it a stale one. Pinned
  that contract with direct unit tests on `_pacing_wait_seconds` (give-up, wait-and-cap, and a
  test documenting the no-independent-defense behavior explicitly) so a future change to either
  layer can't quietly reintroduce this. Of the two remaining unmodeled axes in `available()`,
  `daily_cost_cap` (live today on `deepseek/deepseek-v4-flash`'s $0.10/day cap) now gets the same
  real reset-time treatment as RPD -- it resets on the identical daily boundary
  (`daily_reset_key`), so `_next_quota_reset` predicts tomorrow's reset for it too instead of
  falling into the "next minute" fallback (extracted the shared "next local midnight" computation
  into `_next_local_midnight` so RPD and `daily_cost_cap` can't drift out of sync with each
  other). `concurrency` and the monthly `cost_cap` are left on the fallback: `concurrency` frees on
  an arbitrary future settle/release rather than a clock boundary, so there is no reset time to
  compute -- periodic polling *is* the correct strategy there, not an approximation of one; the
  monthly `cost_cap` has no route configuring it today, so there's nothing live to get right yet.

- **`llm-deferred-sweep.yml` now gives the deferred LLM tag backlog a long graceful drain window
  instead of a short hard cancel.** The GitHub Actions job timeout is 240 minutes, and the backing
  script gets an explicit 235-minute internal wall-clock budget; deferred-direct retries use that
  sweep deadline (not the stale short deadline from the original tag lane) so they can pace through
  provider minute windows and stop only after the remaining pending items cannot fit before the
  deadline. The sweep records each completed result as `backend.reconcile()` returns, treats
  SIGTERM/SIGINT as a signal-safe stop flag checked between records (rather than interrupting a
  storage write), streams pending records rather than materializing the whole registry, and once a
  cohort of same-capacity records proves it can't fit, skips further *reconcile attempts* for the
  rest of that cohort (each record is still read from storage to check which cohort it belongs to
  -- see above for the follow-up that keys that skip on the resolved route pool rather than the
  originating feature -- reconciliation, not the read, is what's avoided), and still prunes expired
  registry records at the end. The LLM quota ledger also settles structured calls back from their
  worst-case two-request reservation to the actual request count on success, so proactive daily
  accounting no longer reports route exhaustion at roughly half of the provider dashboard's request
  allowance when most calls succeed on the first attempt.

- **Gemini structured-output calls now use native JSON-schema mode via a direct LiteLLM call,
  bypassing Instructor entirely for `gemini/*` routes.** The `litellm` bump below did not fix the
  `Mode Mode.JSON_SCHEMA is not registered for provider Provider.OPENAI` error: two live `tag.yml`
  runs on two different `litellm` versions (`1.83.0` and, after the bump, `1.95.0.dev1`) produced the
  *identical* error, including the identical "available modes" list — `Provider.GEMINI` and
  `Provider.VERTEXAI` have no `Mode.JSON_SCHEMA` entry at all in `instructor==1.15.4` (confirmed its
  own latest release), only `MD_JSON`/`TOOLS`. This is Instructor's own (provider, mode)
  compatibility gate, not a LiteLLM provider-auto-detection bug — no LiteLLM version changes it.
  Gemini's REST API genuinely supports native schema-constrained JSON (`responseJsonSchema`,
  confirmed against the live API by `citypods/llm_compat_probe.py`'s `_native()` check, which calls
  it directly with no LiteLLM/Instructor involved), so rather than switch to a different Instructor
  mode or add runtime fallback/re-probing logic, `LiteLLMBackend._run_gemini_structured_direct()`
  (`citypods/compute/llm.py`) calls `litellm.completion()` directly with the same OpenAI-shaped
  `response_format` LiteLLM already translates into Gemini's native mechanism, and replicates
  Instructor's own "parse, validate, one corrective retry on failure" contract by hand. Every other
  route (DeepSeek, Mistral) is unaffected — still routed through Instructor exactly as before.

- **`push_state()` (the tag lane's finalization-tail write) now uploads across a bounded worker pool
  instead of one file at a time.** A real `tag.yml` run — with the finalization-tail logging below
  already in place — was caught pushing only 1,503 of 3,554 state files (42%) serially in the ~9
  minutes of tail budget it had left before GitHub's `timeout-minutes: 25` hard-cancelled it
  mid-upload; the pass itself had finished cleanly (`run end: wall-clock window spent`) well inside
  its own deadline. Same latency-bound-not-bandwidth-bound cost `pull_state()` was already fixed on
  the download side (`_PULL_STATE_MAX_WORKERS`, ~11 min serial → well under a minute parallelized, for
  the same ~3.5k-object scale) — `push_state()` just never got the symmetric fix. Renamed the shared
  constant to `_STATE_SYNC_MAX_WORKERS` and applied the same `ThreadPoolExecutor(max_workers=16)`
  pattern to the upload side (`citypods/statesync.py`); each upload writes its own distinct remote
  key, so there's no shared mutable state to guard, same as the restore side.

- **`TagsStage`'s live LLM dispatch call now registers with `PROGRESS`, the process-wide
  stall-diagnostic registry `citypods.run`'s heartbeat already reads every tick.** Every other
  lane's heavy per-item work (`TimelineStage`, ASR, audio-encode) already tracks itself this way;
  the tag lane's dispatch call (which can pace/sleep waiting out a per-minute quota window) never
  did, so the heartbeat printed `active work: no tracked work active` for the tag lane's entire run
  regardless of whether a real dispatch was in flight — making a genuinely slow-but-healthy pass
  indistinguishable from a stuck one in the GitHub Actions log. `llm_tag_suggestions()`'s call site in
  `TagsStage.process()` (`citypods/stages.py`) now wraps that one call in
  `PROGRESS.track(source=city.slug, uid=episode_uid, phase="tag-llm-dispatch")`.

- **Static ffmpeg switched from BtbN/FFmpeg-Builds to johnvansickle.com; `7.1.4` → `7.1.5`
  (output-affecting, smoke-gated — see `review/22`).** `audio-runner-image.yml` (and every other
  workflow sharing the same `FFMPEG_URL`/`FFMPEG_SHA256` pin: `audio.yml`, `asr.yml`, `ci.yml`,
  `dep-bump-smoke.yml`, `duration-normalize.yml`, `granicus-probe.yml`) started failing with
  `HTTP Error 404: Not Found` downloading the pinned BtbN release asset
  (`autobuild-2026-06-18-14-21`) — BtbN only retains a rolling ~1-month window of dated autobuild
  tags and had pruned it. Re-pinning the *same* BtbN build wasn't possible (the exact asset is
  gone), and BtbN's rolling retention means any tag pinned there will eventually 404 again by
  design — not a one-off fluke. Switched to johnvansickle.com's per-version archives, which keep
  every past release available indefinitely (the standard long-lived static-ffmpeg source used
  broadly across the ecosystem), landing on `7.1.5` (the current release at that source; `7.1.4`
  is no longer published there either). The new SHA256 is a trust-on-first-use pin, not an
  independently verified one: it was read back from the pipeline's own checksum-mismatch error
  after a real download (johnvansickle does not publish its own checksums/signatures to verify
  against). `scripts/install_static_ffmpeg.py` refuses to proceed if a *later* download doesn't
  match this exact digest, which catches drift after the fact but doesn't authenticate the initial
  pin. Also updates the Renovate custom regex manager (`.github/renovate.json5`) to match the new
  URL shape and track real upstream `FFmpeg/FFmpeg` tags as the "is there a newer release" source
  of truth (the old regex was BtbN-URL-shaped and would have silently stopped matching anything).
  Licensing note: johnvansickle's build is GPLv3 (vs. BtbN's LGPL variant previously used).
  citypods only invokes `ffmpeg`/`ffprobe` as a subprocess, never linking against them, so
  citypods' own source isn't brought under GPL/LGPL copyleft — but that's a separate question from
  GPLv3's *distribution* obligations for the binary itself: `audio-runner-image.yml` publishes a
  GHCR image containing this GPL binary, which is a conveyance under GPLv3 and needs its own
  accompanying source offer/notice, not yet added here. Resolved below by building ffmpeg from
  the official upstream source instead of vendoring *any* third-party redistribution — LGPL-only,
  no GPL notice question to answer. Per the `review/22` contract, this is *not* a no-op re-pin (the
  version genuinely moved, forced by source availability) — deferred to `dep-bump-smoke`'s
  automated per-source before/after comparison (triggered via the `output-affecting` label) rather
  than speculatively bumping `AUDIO_PIPELINE_VERSION` without evidence of actual output drift.
  `scripts/install_static_ffmpeg.py` now tries the other path (`releases/` ↔ `old-releases/`)
  automatically when the pinned one 404s, so the exact day johnvansickle moves a version doesn't
  need a same-day pin update to keep builds working — only a real download failure (not a
  checksum mismatch, which still fails hard and never silently retries a different URL) triggers
  the fallback. **Superseded within days** (see the next entry): johnvansickle turned out to be
  just as unreliable as BtbN under this repo's real usage pattern — a verified download, then
  repeated mismatched-bytes and 404 failures on the identical URL within minutes — so re-pinning
  to it was never a durable fix, only what unblocked things immediately.

- **Static ffmpeg now built from official upstream source (`github.com/FFmpeg/FFmpeg`) and
  self-hosted, instead of pinning any third-party redistribution.** Both prior pins in this file
  (BtbN/FFmpeg-Builds' dated release tags, then johnvansickle.com) were third-party redistributors
  of ffmpeg builds and both proved unreliable as *ongoing* dependencies — re-pinning to yet another
  mirror would only relocate the same problem. `scripts/build_ffmpeg_static.sh` clones the
  requested FFmpeg git tag and configures LGPL-only (no `--enable-gpl`, ever — sidesteps the
  GPLv3 distribution-notice question raised above entirely, rather than answering it). FFmpeg's
  native decoders already cover h264/hevc/vp8/vp9/av1/aac/mp3/opus/vorbis/ac3/flac without any
  external library, which matters because citypods decodes whatever providers serve (Granicus MP4,
  Swagit HLS/mp4, CivicPlus tokenized HLS) and doesn't control their encoding. Four permissively
  licensed external libraries widen that further without needing GPL: `libopus`/`libvpx`/`libdav1d`
  (BSD) and `libmp3lame` (LGPL); network protocol support (`--enable-gnutls`, LGPLv2.1-compatible —
  `get_or_fetch` in `media.py` feeds ffmpeg remote URLs directly over http/https, so this is
  load-bearing, not optional) stays on LGPLv2.1 rather than pulling in `--enable-version3` the way
  OpenSSL ≥3.0 would require. citypods' own encode usage is exactly `-c:a aac` and `-c:a flac`
  (both native, no external library at all). The enabled-libs list lives in the build script
  itself, so adding an encode codec is a normal reviewable diff.
  `.github/workflows/build-ffmpeg.yml` is the dispatch-only "dependency change prep" workflow —
  build, then `scripts/vendor_pinned_binary.py --local-file` uploads the result to
  `deps/ffmpeg/<version>/...` in B2, served through the existing Cloudflare-fronted
  `B2_PUBLIC_BASE_URL` (never the metered B2 API). `vendor_pinned_binary.py` (new, generalized
  for any future pinned external binary, not ffmpeg-specific) refuses to overwrite an existing
  `deps/` key — vendored objects are immutable — and gates any `--source-url` fetch through
  `validate_source_url` (SSRF/private-network guard); `--local-file` skips that gate since it's
  this job's own build output, not a caller-supplied URL. No workflow fetches from an upstream or
  mirror host on every run anymore, and no third-party redistributor is a runtime dependency.

- **Self-built ffmpeg `7.1.5` vendored to B2 and wired into all seven consuming workflows;
  external codec/TLS libraries link dynamically instead of statically.** `build-ffmpeg.yml`'s
  first real dispatch failed configure twice before producing a working archive: first with
  `PKG_CONFIG_PATH` not covering apt's `.pc` location (`actions/setup-python` overrides it),
  then — after that fix — with the same `gnutls not found using pkg-config` error even though a
  bare `pkg-config --exists gnutls` succeeded, because `--pkg-config-flags="--static"` makes
  configure query gnutls with `--static`, which additionally requires gnutls's entire transitive
  dependency chain (nettle/hogweed/gmp/p11-kit/tasn1/idn2/unistring) to resolve statically — at
  least one link in that chain doesn't, via Ubuntu's apt packages. Fix: drop
  `--pkg-config-flags="--static"` entirely. FFmpeg's own libraries (libavcodec etc.) still link
  statically (`--enable-static --disable-shared`); the external codec/TLS libraries (gnutls,
  opus, vpx, dav1d, mp3lame) now link dynamically, so wherever the binary runs needs the matching
  runtime (non-`-dev`) packages installed alongside it — a real, permanent change to the
  deployment story, not a workaround to later undo. The third dispatch succeeded
  (`sha256=30d8f18138393081d7fdf95f7006fa132e7b063fd87c0e955652c64a4bc0d52d`, uploaded to
  `deps/ffmpeg/7.1.5/ffmpeg-7.1.5-linux64-static.tar.xz` in B2), and that pin now replaces the
  prior johnvansickle URL/SHA256 in `ci.yml`, `asr.yml`, `audio.yml`, `audio-runner-image.yml`,
  `dep-bump-smoke.yml`, `duration-normalize.yml`, and `granicus-probe.yml`, each building
  `FFMPEG_URL` from the `B2_PUBLIC_BASE_URL` secret at the step that needs it (CR-GH-07/23/25 —
  secrets scoped to the consuming step, not the whole job) rather than hardcoding the CDN domain
  the secret happens to hold. This dynamic-linking switch is a **permanent deployment contract**,
  not a one-off fixup: every place this ffmpeg binary runs must have the matching runtime
  (non-`-dev`) packages installed, forever, not just at the moment of this pin's introduction.
  `.github/audio-runner/Dockerfile`'s base image was switched from the official
  `python:3.12-slim-bookworm` to `ubuntu:24.04` for exactly this reason: CodeRabbit caught (PR
  #1003 review) that the bookworm image's `libvpx7`/`libdav1d6` packages ship different SONAMEs
  (`libvpx.so.7`/`libdav1d.so.6`) than what the binary is actually linked against
  (`libvpx.so.9`/`libdav1d.so.7`, from Ubuntu noble — the `ubuntu-latest` distro
  `build-ffmpeg.yml` builds on) — the binary would fail to load in the container, not just warn.
  Matching the base image to the build host, rather than publishing a second Debian-targeted
  archive, keeps this to one ffmpeg build/pin shared by every consumer (GH Actions host-fallback
  paths already run on noble). A real `audio-runner-image.yml` dispatch against this change
  confirmed the noble packages install cleanly (Docker Hub pulls aren't reachable from the
  sandbox this was authored in, so this couldn't be checked ahead of time) but surfaced a second,
  unrelated bug on the same dispatch: `install_static_ffmpeg.py` imports `scripts._pinned_fetch`
  as a sibling module, and the Dockerfile only ever `COPY`'d the single file, not the `scripts/`
  directory it depends on — `ModuleNotFoundError: No module named 'scripts'`, since this is the
  first dispatch to ever get past the checksum/404 failures that blocked every earlier one before
  reaching this step. Fixed by copying the whole `scripts/` directory in. A clean re-dispatch
  (run `30064762550`) then built and smoke-tested successfully — `ffmpeg -version`/
  `ffprobe -version` printed real output (proving the SONAME fix: a mismatch would have failed to
  load the binary at all, not just warned) and `python -c "import boto3, citypods"` succeeded.
  The base image is now pinned to the exact digest that dispatch resolved
  (`ubuntu:24.04@sha256:4fbb8e6a8395de5a7550b33509421a2bafbc0aab6c06ba2cef9ebffbc7092d90`, read
  from that build's provenance metadata — `review/22`'s base-image-immutability convention).
  Not an `AUDIO_PIPELINE_VERSION` bump: same ffmpeg version, same build flags/codecs as already
  shipped: only *how* its external libraries are linked (and therefore which container they run
  in) changed, not what bytes a correctly-running binary produces — CI's `dep-bump-smoke` table
  is expected to show no diffs, which is itself part of what closes this out.
  `.github/renovate.json5`'s ffmpeg-specific custom regex manager was removed (a URL-pattern
  version bump doesn't apply to a self-built, vendored pin — bumping now means dispatching
  `build-ffmpeg.yml` and manually updating the seven workflows' `FFMPEG_SHA256` and `FFMPEG_URL`
  version segment, still smoke-gated per `review/22`).

- **`litellm` bumped to `1.95.0.dev1` (pre-release), and `instructor`'s `[litellm]` extra dropped, to
  unblock `gemini-3.5-flash-lite`.** A real manually-triggered `tag.yml` run showed the second Gemini
  route added below got **zero** live requests despite `gemini-3.1-flash-lite` repeatedly hitting its
  15 rpm cap — the smoking gun was a captured error: `Mode Mode.JSON_SCHEMA is not registered for
  provider Provider.OPENAI`. Root cause: `litellm==1.83.0` (the prior pin) doesn't recognize
  `gemini-3.5-flash-lite` in its model registry, so its provider auto-detection fell through to a
  generic default (`Provider.OPENAI`) and Instructor rejected the `(mode, provider)` pair before any
  request reached Gemini — every dispatch attempt onto the second route failed client-side and landed
  in the error count instead of contributing throughput. Confirmed via litellm's own upstream history
  (`BerriAI/litellm@59ebe043c2`, "day-0 pricing for gemini-3.6-flash and gemini-3.5-flash-lite",
  2026-07-21): no *stable* litellm release contains this fix yet (`v1.93.0`, 2026-07-19, predates it
  by two days), so `pyproject.toml`'s floor is pinned to the first pre-release that has it
  (`litellm>=1.94.0rc3` — a prerelease lower bound opts pip-compile into prerelease space for just
  this package per PEP 440, without a blanket `--pre`). That floor conflicted with
  `instructor==1.15.4` (its own latest release)'s `[litellm]` extra, which caps `litellm<=1.83.7`;
  since `litellm` is already declared as our own top-level dependency, the extra was redundant and got
  dropped instead of blocking the bump. Revisit and relax the floor once litellm cuts a stable release
  containing the fix.

- **The tag lane's finalization tail (per-stage tally through `push_state`/`reconcile_state`) now
  flushes its output and logs LLM rate-limit pacing/429s.** The same manually-triggered run above
  showed the job's `stop()` budget tripping correctly and the dispatch loop winding down cleanly
  (`tags: 142 ran, 708 reused, 15034 queued, 5 errors`), then **7.5 minutes of complete silence**
  before GitHub's `timeout-minutes: 25` hard-cancelled the job with no trailing output — no
  `run end:`, no `state: pushed N file(s)`, nothing. Root cause: unlike almost every other `print()`
  in `run.py`, this block never passed `flush=True`; stdout is block-buffered (not line-buffered)
  when redirected in CI, so the unflushed output sat in memory and was silently discarded when the
  job was SIGKILLed, making genuine (possibly slow) finalization work indistinguishable from an
  actual hang. Fixed: `flush=True` throughout the tally/finalization block plus a print at each major
  step (run history, manifest rebuild, budget flush, state push, state reconcile);
  `push_state()`/`reconcile_state()` (`citypods/statesync.py`) take an optional `log` callback
  (matching `push_records_merged`'s existing pattern) and report a start count plus one line per
  file/reclaim instead of running silent end-to-end. Separately, the paced LLM dispatch loop
  (`LiteLLMBackend._run_policy_job_paced`) now logs when it's rate-limited (which route(s) were
  rejected and why, and whether it's waiting or giving up) and when a live `429` blocks a route —
  both were previously invisible, matching the "3 flash lite: 0 requests" / "3.1 flash lite: 15/min
  cap reached" confusion this same run surfaced on the Gemini side.

- **The `tag` lane's `tag.yml` job logs are no longer silent.** Diagnosing why a real scheduled run
  made almost no live LLM calls required inferring everything from external evidence (the provider's
  own request log, wall-clock timing) and could not be read from the GitHub Actions log at all —
  `run_stages()` was called with `quiet=True` for every lane in the global queue's per-episode
  invocation, since audio/ASR's much larger passes would otherwise emit thousands of "ran=0 reused=1"
  lines. The `tag` lane now passes `quiet=False` (`quiet=ctx.lane != "tag"` in `_run_for`), so it logs
  a `[enrich] stage start/done ... ran=X reused=Y queued=Z errors=W` line per episode; other lanes are
  unaffected. This does add real volume to the tag lane's own log (its backlog is large too), but
  visibility into what's actually happening matters more than log size while this lane's throughput is
  still being tuned.

- **`TagsStage` no longer re-parses the taxonomy YAML and calibration-state JSON from local disk on
  every episode — it loads each at most once per run.** A real scheduled `tag.yml` run with every
  prior fix in place (parallel state restore, budget-gated fetch, in-memory triage, quota pacing)
  still made only 2 live LLM calls across a ~13k-episode backlog before exhausting its wall-clock
  budget — essentially no tagging. Root cause: the global queue invokes `TagsStage.process()` once
  **per episode**, and it re-read + re-parsed `config/taxonomy.yml` (`yaml.safe_load`) and
  `llm_evaluation.json` (calibration state) at the top of every single call, even for episodes that
  hit the new no-fetch triage fast paths. Measured against the real taxonomy file: ~28ms/call —
  ~6.3 minutes of pure YAML parsing alone across the backlog, before `taxonomy_from_dict`
  construction, the evaluation-state JSON parse, or the admission-policy hash, and before dispatch
  logic ever ran for most episodes. Both are read-only, unchanged loads for the whole run, so they're
  now cached on `StageContext.tag_taxonomy_cache` (the same `ctx` object is shared across every
  per-episode call in one build) — a load failure is cached too, so a broken taxonomy/state file
  still reports on every call without re-attempting the same failing read thousands of times.
  Measured effect: ~28ms/call → ~0.6ms/call (cache hit) for the fixed per-call cost, ~48x; projected
  total for a 13k-episode backlog drops from minutes to under 8 seconds. Two follow-up fixes from
  code review, both real: (1) `yaml.YAMLError` is not a `ValueError` subclass, and PyYAML is
  documented to leak raw `ValueError`/`KeyError`/`IndexError` for some malformed explicit-tag
  scalars instead of wrapping them — the original `except (OSError, ValueError)` around
  `load_taxonomy()` missed all of these, so a genuinely corrupt `taxonomy.yml` would propagate
  uncaught instead of degrading gracefully via the new cached-error path; broadened to catch all of
  them. (2) The global queue calls `TagsStage.process()` from a worker thread pool sharing one
  `ctx`, and the cache bundle is written as three separate, non-atomic dict assignments — a second
  thread could observe `evaluation_state` already cached but `admission_policy` not yet written and
  KeyError, or (the case a barrier-synchronized regression test actually reproduces) every
  concurrently-arriving thread could see an empty cache at once and each perform its own duplicate
  load. Both check-then-populate paths are now serialized under a new `tag_taxonomy_cache_lock`.

- **LLM topic tagging now drives its real free-tier throughput: a second Gemini route, the true
  per-model quotas, and within-run rate pacing.** Three connected changes on top of the tag lane's
  in-memory triage. (1) **Two routes.** `gemini/gemini-3.5-flash-lite` joins `gemini-3.1-flash-lite`
  in the route table (each an independent free-tier pool: 500 req/day, 15 rpm, 250k tpm), and the
  tag policy now allows both (`tagging.llm_models`, primary first) so a run spills onto the second
  model once the first's window fills — ~1000 tags/day at ~30 rpm combined. The primary stays the
  single stable route string for the recipe hash and calibration key; each candidate still records
  the model that actually answered, so calibration keys on real usage without fragmenting the cache.
  (2) **Real quotas.** `gemini-3.1-flash-lite` is raised from its initial `rpd=20`/`rpm=10` safety
  ceiling to the real `rpd=500`/`rpm=15`/`tpm=250k`. (3) **Within-run pacing.** The scheduler
  (`select_route`) now reports `retry_at` — the soonest an allowed route frees up (per-minute
  rollover, daily reset, or the end of a real-429 block) — and `LiteLLMBackend._run_policy_job_paced`
  waits that out and retries, bounded by the request's `deadline_at`, so a run **drains its full
  daily quota across successive minute windows** instead of bursting one window's ~15 and stopping.
  It respects a token-per-minute (or request-per-minute) limit and a real `429` identically: both
  surface as a near-future `retry_at`, so the loop backs off and retries at the next reset; it only
  gives up (deferring to the sweep / a later run) when the sole remaining reset is a daily one past
  the run's wall-clock budget. The tag lane passes that budget through as `ctx.tag_llm_deadline`
  (`StageContext`), a UTC twin of its graceful-yield deadline. Pacing is gated on `deadline_at`, so
  any caller without one (discovery) keeps the exact single-attempt-then-defer behavior; the LLM
  tournament, which already sets a 20-minute deadline, now paces within it too. Reservations still
  settle/release per attempt and no intermediate deferred record is written between paced retries.

- **Dormant-resumption review is now actionable and issue commands verify real repository
  permission.** A `dormant-resumed` child offers `/stale activate`, which creates a review PR that
  removes the dormant lifecycle block and restores normal freshness monitoring; an unhandled child
  may still age out after the recent-publication window. Stale lifecycle children now show whether
  the provider fetch responded on the current conclusive/inconclusive audit. `/stale` and `/r12`
  command workflows share one fail-closed permission policy backed by GitHub's repository-permission
  endpoint and require write, maintain, or admin access instead of trusting comment association.
  Expected `/r12` authorization denials post generic issue feedback as a successful no-op, while
  malformed permission data and unexpected errors still fail the workflow. No pipeline version or
  artifact backfill is involved.

- **Stale-cohort parents now document the complete operator workflow.** The generated parent and each
  child show the exact `/stale pause`, `/stale dormant`, and `/stale retire` syntax, collaborator-only
  authorization, review-PR approval semantics, manual feed-YAML source-repair/provider-migration path,
  and automatic recovery closure. Because this guidance lives inside the automation-owned generated
  section, future audits keep it current without overwriting maintainer notes. No pipeline version or
  artifact backfill is involved.

- **Stale feeds now have a complete lifecycle instead of one permanent warning table (H4,
  GH#970–#975).** Optional stable `source_id` and reviewed UID overrides preserve the append-only
  archive and provider-independent episode identity across both historical-copy and forward-only
  provider cutovers. Feed YAML supports `active`, finite `paused`, polling `dormant`, and non-polling
  archive-preserving `retired` states. Stale and dormant-resumed findings reconcile as capped native
  per-feed sub-issues; collaborator-authorized `/stale pause|dormant|retire` comments create validated,
  deterministic lifecycle PRs, while manual source repair/migration starts from the child issue's YAML
  link. The dry-run-first, resumable rollout converted all 11 rows of legacy GH#774 to native children
  #979–#989, preserving exact `first_seen` timestamps and changing the parent marker only after every
  child was attached. No pipeline version changed, so existing audio, transcript, feed, and artifact
  state is not invalidated or backfilled.

- **Initial three-model tag tournament is enabled.** The weekly runner compares the same bounded
  real-meeting tag input through Gemini Flash-Lite, DeepSeek V4 Flash, and Mistral Large Latest;
  every provider pair is judged by the third provider in both display orders. Results are durable
  in `state/llm_tournament.json`; incomplete/deferred contests resume idempotently. It records
  quality information only and never changes the R5 production route automatically.

- **R5 topic-tag production is now scheduled conservatively.** A dedicated daily `tag.yml` lane
  runs `enrich --lane tag`, creating the persisted LLM candidates that the calibration workflow
  scores. The initial default is `gemini/gemini-3.1-flash-lite`, rather than Gemini 3 Flash
  Preview. Its route, Mistral Large Latest, and DeepSeek V4 Flash are limited to 20 actual
  provider attempts per reset day; DeepSeek has an additional $0.10/day CAS-backed spend cap.
  The Mistral dispatch Worker now permits only one upstream attempt per queued request, so its
  retry loop cannot turn a single ledger reservation into multiple API calls. Existing stored
  artifacts are not invalidated: only tag candidates without the new model-specific recipe run.

- **Human-scoring batches now use native GitHub sub-issues.** R5 LLM-tag calibration and H15
  transcript-quality review publishers attach each candidate/sample issue to its digest parent through
  GitHub's parent/sub-issue relationship, rather than relying on a `Parent issue: #…` body convention.
  This makes hierarchy and progress visible in Issues and Projects; both completion finalizers now query
  the native relationship directly, and R5 starts a fresh digest after a completed batch to remain below
  GitHub's 100-sub-issue limit. Existing review artifacts and scoring state are unchanged.

- **LLM calibration review CLI is now packaged.** The scheduled tag-review workflow previously failed
  before processing candidates because the `citypods llm-evaluation` console command imported its R5
  adapter from the un-packaged top-level `scripts/` directory. The adapter now lives in `citypods/`,
  so the installed command works from a clean GitHub Actions environment; a regression test executes
  the CLI from outside the checkout under isolated Python imports.

- **Local-source concat now honors `audio_ffmpeg_threads`.** `_concat_local_sources` (the
  `filter_complex` decode/concat of already-downloaded multi-source segments, driven by
  `SourceCache.get_or_fetch_concat`) built its ffmpeg command with no thread-pinning flags, unlike
  every other ffmpeg invocation in `media.py`, which goes through `CommandFfmpeg`'s
  `-threads`/`-filter_threads`/`-filter_complex_threads` helpers. It now takes the same thread pin
  (wired from `run.py`'s existing `ffmpeg_threads`), keeping it inside the documented
  one-core-per-lane discipline instead of falling back to ffmpeg's auto-detected thread count on a
  shared runner. No artifact-identity or output change.

- **Audio concat stall fixes (root-caused via the phase diagnostics below).** Found and fixed the
  cause of a recurring `audio` shard hang: a real 2009 Austin archive segment with a malformed AAC
  stream sailed through the stream-copy segment fetch undetected and then stalled ffmpeg's decoder
  inside the multi-segment concat filtergraph for hours, silently consuming an entire shard's job
  budget every run until GitHub's hard 6h ceiling force-killed it (an undiagnosable `cancelled`
  job, not a clean encode error). Each concat segment is now decode-validated (`ffmpeg -xerror`)
  immediately after download; a corrupted segment now fails fast into the normal #120 backoff as
  `CorruptSourceSegmentError` (code `corrupt-segment`) instead of ever reaching the filtergraph.
  The validation call itself runs through the same guarded ffmpeg path (memory-floor termination +
  `stop()` preemption) as every other ffmpeg invocation, rather than a bare unguarded subprocess
  call. The local concat step also gets its own much shorter timeout
  (`audio_concat_timeout_minutes`, default 20min) independent of the network-fetch budget
  (`audio_encode_timeout_minutes`, up to 6h) — real concats measured seconds-to-minutes even for
  multi-hour meetings, so inheriting the network budget gave a pathological concat far more silent
  runway than it needed. Separately, the ffmpeg process-monitor loop (`_run_ffmpeg_popen_monitored`)
  now also honors the run's wall-clock `stop()` signal, terminating an in-flight child (network or
  local) the same way not-yet-started work already yields gracefully — previously a thread already
  inside a monitored ffmpeg call was blind to the run running out of time and kept polling toward
  its own much longer per-operation timeout instead. `.github/workflows/audio.yml`'s `audio` job
  now sets `timeout-minutes: 360` explicitly (GitHub's existing hosted-runner default, made
  visible rather than implicit) so the relationship to the internal timeouts above is documented
  in-repo. `SourceCache.concat_timeout_seconds` now distinguishes "caller didn't pass this
  parameter" (inherits the parent network-fetch budget, unchanged) from an explicit `None`
  (genuinely uncapped, matching `audio_concat_timeout_minutes: 0`'s documented "0 = no cap") — a
  configured zero/negative value previously fell back to the parent budget instead of disabling
  the cap. No artifact output or pipeline-version change.

- **Audio encode phase diagnostics.** Audio materialization now logs bounded phase markers and
  elapsed time for media resolution, source-cache fetch, rendering, duration probing, and storage
  upload, without logging signed media URLs. This makes long-running or cancelled audio items
  diagnosable without changing artifact output or retry behavior.

- **Beam deploy CLI maintenance.** Updated the reproducibly pinned GitHub Actions `beam-client`
  install from `0.2.198` to Beam's required minimum `0.2.202`; no worker runtime or pipeline
  output changes are introduced.

- **LLM quota and cost scheduling (R13).** Added provider-neutral route policy, Gemini RPM/RPD/TPM
  accounting with Pacific-midnight resets, DeepSeek off-peak preference, exact allowlists, and a
  CAS-backed `state/llm_budget.json` ledger. Reservations are released only before a provider call
  and settled for every post-call outcome. A real provider 429 now reactively blocks that route until
  its `Retry-After` hint. A caller that can't complete synchronously (nothing eligible yet, a real
  rate limit, or a genuine in-flight Mistral dispatch) gets the same portable `JobHandle` back either
  way, completed later via `reconcile()`; a new B2-backed deferred-request registry
  (`state/llm_deferred/`) and a once-daily `llm-deferred-sweep` workflow (timed to DeepSeek's off-peak
  window) let a caller with no retry cadence of its own eventually get a result without rebuilding the
  request. City discovery (the only current caller) requires a free, immediate result — no deadline.
  See [`review/33`](review/33-llm-quota-cost-scheduler.md) §13 for the full revision history. This
  adds no LLM artifact backfill or pipeline-version bump. Found while migrating R5 onto these
  adapters: the sweep never registered any feature's structured-output contract in its own process,
  so a pending "tag" or "classify-civic-platforms" record could never actually reconcile (it failed
  silently, per-record, forever, until its 38-day TTL expired) — fixed by registering both known
  contracts before the sweep reconciles anything.

- **Topic taxonomy and calibrated chapter-scoped tagging (R5).** Added a 37-tag Strong Towns/livability
  taxonomy, deterministic evidence-backed episode/chapter annotations, taxonomy-ordered episode
  rollups with a no-chapter fallback, chapter-aware meeting/search payloads, and an Instructor/Pydantic
  structured LLM path running through dispatch. Validated model suggestions are retained as shadow
  candidates with quoted, source-checked evidence; a reusable sparse calibration matrix and weekly
  human-review digest control automatic admission. The initial feature/provider fallback is 100%
  confidence, so unquantified candidates remain hidden. Policy changes reproject stored candidates
  without re-running vendor jobs. No manual override field or automatic taxonomy web crawl is
  introduced; annual taxonomy review and future moderated community proposals are documented in
  `review/14`. A pre-merge review pass then closed a set of correctness/integrity gaps: episode
  records now correctly restore persisted tag state on every normal run (previously every episode was
  silently re-tagged and re-dispatched to the LLM each run), chapter identity survives a dropped
  chapter, exactly-100%-confidence suggestions can no longer bypass calibration, evidence timestamps
  no longer span the whole episode on a common word, and the weekly review workflow now authenticates
  its comment-triggered ingestion and serializes its matrix jobs against a shared state race. See
  `review/14` for the full list. The LLM dispatch path now runs through R13's
  `LLMRequestPolicy`/scheduler/budget adapters instead of a static single-model call — see `review/14`
  and `review/33` for the migration.

- **Audio source-cache failure cleanup.** Failed source downloads and multi-part concatenations now
  remove partial `.mka` outputs immediately, and failed concat attempts release already-downloaded
  episode parts before falling back to remote rendering. This prevents temporary audio artifacts
  from accumulating across a shard's rolling queue; successful audio identity and content-addressed
  outputs are unchanged.

- **ASR worker failure handling.** Transient remote-record read failures now requeue the owned
  transcript claim instead of marking it terminally failed. Deterministic audio decoder failures
  are durably quarantined against the current hosted-audio identity and retried only after that
  identity changes. ASR claim logs now include duration, runtime estimate, outcome, and actual
  elapsed time for per-item diagnosis. Existing records and successful artifacts are unchanged;
  the new quarantine fields are additive and do not invalidate stored transcripts.

- **Withheld recordings no longer enter the ASR queue.** The shared transcription-work planner now
  excludes `media_availability`-withheld episodes, matching `AudioStage`'s gate. This prevents
  legacy hosted artifacts for confirmed empty, missing, or invalid recordings from being sent to
  Whisper; it changes queue admission only and does not invalidate existing transcripts.

- **Provider transport retry hardening.** The shared HTTP retry engine now explicitly retries
  connect and response-read failures in addition to its existing 403/429/5xx policy. An exhausted
  requests transport timeout is recorded as deferred work so a temporary endpoint outage does not
  redden the audio lane.
- **Bounded audio source retention.** The global audio queue now admits work through a rolling
  submission window and releases each episode's downloaded source files as soon as its audio stages
  finish. Multi-source segment files are removed only after concatenation has captured durations and
  timeline metadata, preventing the run from retaining the full eligible backlog on runner disk.

- **Static meeting search (R4).** Render builds now publish deterministic per-source search shards and
  a global `/search/` page using a vendored MiniSearch bundle. Results search durable metadata, chapter
  titles, available transcript segments, agenda/backup/minutes text, vote and roster names, and future
  tags; unavailable recordings remain discoverable without playback controls. The first build after
  deployment backfills every retained episode from the append-only record store; unchanged sources
  then skip sidecar reads and stale shards are pruned. Available transcript text is always indexed,
  while the search page discloses exact transcript coverage for the selected city/body scope;
  missing sidecars remain partial text coverage and do not block the rest of the index.

- **Runner reliability fixes.** Content-addressed S3 uploads now retry transient transfer-manager
  failures after boto's per-part retry budget is exhausted. Internal ASR workers first receive a
  catchable interrupt before terminate/kill escalation so native semaphore resources can unregister
  cleanly; failed claims now log their exception type and redacted message for diagnosis. Beam and
  Modal deploy workflows retain their protected GitHub environments so environment-scoped provider
  credentials are available during deployment.

- **Agenda/minutes document enrichment (R3).** Added bounded agenda/packet text and backup-link
  extraction, agenda-derived minutes candidates for the immediately preceding same-body meeting,
  and a separate minutes text stage with conservative per-member vote and roster sidecars. A
  provider-supplied minutes URL always overrides an agenda-derived candidate; document artifacts are
  content-addressed and do not affect audio specifications.

- **LiteLLM LLM backend (R2).** Added `citypods.compute.llm.LiteLLMBackend` with direct provider
  completion and asynchronous R10 Worker enqueue/poll transports. The adapter validates the
  provider-qualified model route, keeps per-task prompt/version registries, maps both transports to
  the shared `JobResult`/`JobHandle` contract, and never logs or persists provider secrets. Install
  the optional `llm` extra; no LLM backfill or pipeline-version bump is performed by this change.

- **Waco now has a PrimeGov/OneMeeting agenda companion.** The new API-first auxiliary adapter
  walks the configured year range at `wacotexas.primegov.com`, preserving official agenda, packet,
  and minutes document links for meetings while Swagit remains the recording provider. It is
  auxiliary-only: PrimeGov rows remain calendar records; Swagit supplies any podcast episodes.
- **Gainesville CivicEngage Archive Center enrichment (R11).** Gainesville's CivicMedia recording
  feed now composes with its official CivicEngage City Council agenda and minutes archives. The
  auxiliary adapter joins dated archive rows without creating document-only podcast episodes; links
  remain additive and existing CivicMedia media/audio identities are unchanged.
  
- **Swagit archive pagination and Austin aggregate coverage (R11).** Swagit view fetches now follow
  every advertised archive page instead of only the first 20 rows. Austin retains its dedicated body
  feeds and adds a city-wide all-boards-and-commissions projection; overlapping recordings reconcile
  by stable Swagit video GUID, enrich the canonical dedicated record, and reuse its UID/audio artifact
  rather than creating a duplicate public episode. This is metadata/discovery behavior only: no audio
  pipeline version changed and existing hosted audio is not invalidated.

- **Granicus episode discovery is now archive-first (R11 phase 1).** The provider derives each native
  `ViewPublisher.php` archive from the configured `ViewPublisherRSS.php` view ID, removing the
  100-item RSS cap without changing an existing Granicus source key or clip-based episode identity.
  Archive rows add official Agenda and Minutes links when published. This does **not** bump an audio or
  stage pipeline version: existing audio specifications remain valid, while newly discovered historical
  recordings enter the normal restartable backlog and are materialized gradually under the existing
  budgets. RSS is no longer fetched as a discovery source or fallback; verified calendar companions are
  a subsequent R11 phase for archive-missing recordings and agenda-only meetings.

- **Verified calendar companions compose history without replacing video discovery (R11 phase 2).** A
  city can inherit an explicit `aux_provider` / `aux_source` from its entity configuration. Legistar
  supplies a full calendar index: video-linked rows merge with the native Granicus archive by normalized
  Granicus clip ID/GUID, while every no-video row is retained append-only in `calendar.json` and shown as a
  Calendar-only meeting in the city archive—not as an RSS item or an audio/transcript job. Pflugerville
  now uses its official calendar alongside the archive, covering 2,402 calendar rows and a 788-clip
  native-plus-calendar union (560 calendar-only recordings). This adds historical work gradually under
  existing budgets; it does not change a stage or audio pipeline version, invalidate prior audio, or
  re-encode existing clips. A companion failure leaves the primary archive and last-known calendar
  metadata available.

- **Swagit retains first-party agenda and minutes links (R11 phase 3).** Its archive-list parser now
  preserves a recording row's official `/videos/{id}/agenda` and `/videos/{id}/minutes` links when
  present, including when duplicate videos appear in overlapping views. Swagit remains the video
  discovery and media-resolution provider; this adds feed metadata only, with no pipeline-version
  bump or audio backfill.

### Fixed

- **Compute-budget stale-cycle reset now actually fires (previous fix didn't take).** The prior
  "ASR worker report no longer prints stale compute-budget totals" fix (below) added a reset check
  but the check itself had two holes that let both Modal and Beam's fossils survive it untouched:
  (1) `Budget._ledger` skipped the reset entirely whenever a backend's persisted `cycle_key` was
  blank — true for any ledger untouched since before day-keyed cycles existed, whose `used_units`
  can still carry a schema-v2 `used_gpu_seconds` total silently reinterpreted as dollars by the v3
  migration; (2) the legacy `"YYYY-MM"` compat added by that same v3 migration matched *any* read
  in the same calendar month as the persisted bare-month key, not just the one-time migration read
  it was meant for, so a backend untouched since before the migration kept re-validating as
  "current" all month long. Together these left Modal frozen at `$17810.2` and Beam at `$75.9`
  used — both far past their `$24` cap — since the migration, silently blocking all real dispatch
  to both (`available()` never saw room). Both holes are fixed: a blank `cycle_key` now resets like
  any other mismatch (harmless for a genuinely fresh ledger, since `used_units` is already `0`
  there), and the legacy month-key compat is gone now that the migration it bridged is long past.
  `external_worker.py`'s `_effective_max_claims` had the same blank-`cycle_key` hole duplicated
  inline; it now delegates to `Budget.current_ledger` instead of re-implementing the check.

- **The daily `tag.yml` workflow no longer reliably burns its full 25-minute job timeout and gets
  hard-cancelled with nothing persisted.** A scheduled run was observed spending ~14 of its 25
  minutes in a per-episode audio-duration ffprobe/heal pass
  (`_normalize_episode_durations_for_dispatch`) across the *entire* backlog before it could even
  build its candidate queue, even though the `tag` lane never runs `TranscriptStage` and has no
  audio dependency at all — `_run_enrich_global_queue()` gated that pass on `transcript_stages`
  being non-empty, and the `tag` lane's own `TagsStage` counts as one. The remaining time went
  into `TagsStage` re-fetching and re-hashing each episode's full agenda/transcript text just to
  discover most of the backlog hadn't changed since the last run. Both are fixed: the
  duration-normalization pass is now gated on an actual `TranscriptStage` being present, and
  `TagsStage` first computes a cheap, storage-I/O-free `tag_input_fingerprint()` (built from the
  content-addressed agenda/transcript artifact keys and chapter boundaries already on the
  episode, rather than their decoded text) — an unchanged episode short-circuits before any
  storage fetch or SHA-hash bookkeeping at all. The `tag` lane also gets its own
  `tag_run_time_budget_minutes` (default 18m, well inside the job's 25-minute `timeout-minutes`)
  wired into `ctx.stop()`, so a run that is still slow for some other reason yields and persists
  whatever it finished instead of being SIGTERM'd by GitHub with nothing written — the generic
  `run_time_budget_minutes` default (240m, sized for the 4h audio/ASR cron) never tripped inside
  this lane's much shorter job. New episode field `tags_input_fingerprint` is additive and
  lane-owned by `tag` (`_LANE_OWNED_BLOCKS`); nothing about existing `tags`/`tags_spec_hash`
  semantics changes. **A hard kill can still happen for other reasons, though** (infra outage,
  an unexpectedly large new backlog), and previously that meant losing the *entire* run's tag
  work — the only persist call for this lane's pass sat at the very end, and even that was only a
  local write; the durable bucket push happens once, later still, at the very end of the whole
  build. `_run_bounded()` now takes an optional `on_progress` hook, and the `tag` lane wires one
  up (`mid_run_checkpoint` in `_run_enrich_global_queue()`) that locally persists and pushes
  completed records to durable storage (the same foreign-block-preserving `push_records_merged`/
  `push_state` the end-of-run push already uses) every 3 minutes of wall clock during the
  transcript/tags-only passes. A checkpoint push failure is logged and swallowed rather than
  aborting the run — the end-of-run push still gets a chance. Other lanes are unaffected
  (`mid_run_checkpoint` defaults to `None`).

- **The `tag.yml` workflow kept hard-timing out even after the fix above landed, because the
  cheap `tag_input_fingerprint()` pre-check could never fire for the pre-existing backlog.**
  `TagsStage` had a second, older reuse check below the new pre-check —
  `ep.tags_spec_hash == projection_hash` — that already required the storage fetch and full hash
  recompute the pre-check exists to skip, and it `continue`d without ever writing
  `tags_input_fingerprint`. Every episode resolved before that field existed (i.e. the entire
  backlog, the first time the pre-check shipped) has `tags_spec_hash` set but
  `tags_input_fingerprint` permanently `None`, so it can never satisfy the pre-check and instead
  falls through to this older branch — paying the full storage-fetch-and-hash cost again on
  *every* run, forever, not just once. A live run confirmed it: the transcript pass never even
  finished walking the ~13k with-audio candidates before `stop: wall-clock window spent` fired.
  Fixed by backfilling `ep.tags_input_fingerprint` in this branch too before continuing — exactly
  the same terminal-state condition the bottom-of-loop `fingerprint_after` assignment already
  covers, just reached without a `tags`/`tags_spec_hash` diff to persist alongside it. Only the
  run that first sees a given legacy episode still pays the storage cost; every run after that
  hits the cheap pre-check like the rest of the backlog.

- **The `tag.yml` timeout is finally root-caused: durable-state restore, not per-episode tagging,
  was eating the budget.** Both fixes above optimized the per-episode tagging loop — work that only
  begins *after* build start-up. A run with all of them still burned the full 25 minutes and was
  hard-cancelled. The job logs showed why: **~11 minutes of silence at the very start**, before the
  first line of output, restoring the durable state snapshot from the bucket. `pull_state()`
  downloaded every one of ~3,500 small state objects (`state/sources/<src>/episodes.json` and
  sidecars) **serially** — one latency-bound round trip each — and this runs *before* the
  wall-clock `stop()` window even opens. At ~44% of the `tag` lane's 25-minute job spent before any
  tagging, and with the graceful-yield deadline anchored *after* the restore, a slow restore
  (11 min vs the prior run's 9) slid that deadline past GitHub's hard job timeout and the run was
  cancelled outright with no candidates produced. The 4h audio/ASR lanes pay the same restore cost
  but hide it inside a 240-minute budget (and warm an `actions/cache` state blob the `tag` lane
  never had). Fixed at the source: `pull_state()` now fans the per-object downloads across a bounded
  thread pool (`_PULL_STATE_MAX_WORKERS`), overlapping their latency and collapsing the ~11-minute
  restore to well under a minute — every lane benefits, the short-budget `tag` lane most. The
  listing, CAS-managed-key skip, and per-key transient-error fail-soft (`is_transient_storage_error`
  keeps its existing local copy and continues; a real error still propagates) are all preserved. As
  belt-and-suspenders, the `tag` lane's graceful-yield deadline is now anchored to a wall-clock mark
  captured *before* the restore (`enrich_phase_start`) and clamped at `>= 0`, so start-up time
  counts against the window and a slow start can never again outlast the hard cap — it yields and
  persists (via the existing mid-run checkpoints) instead of being SIGKILLed.

- **`TagsStage` now honours the wall-clock budget *before* its per-episode storage fetch, so a spent
  budget actually ends the pass instead of grinding the whole backlog to a hard-cancel.** With the
  restore fixed and the graceful stop finally firing (both above), a run *still* burned to GitHub's
  25-minute hard cancel — ~8 minutes of it **after** `stop: wall-clock window spent` had already
  printed. The cause: `stop()` was only checked at the LLM-dispatch point, which sits *past* the two
  per-episode storage round trips (`episode_tag_inputs` + `chapter_tag_inputs`). Those fetches — not
  the LLM calls — are the real cost of walking this lane's backlog, and most of that backlog is
  episodes that need a tag but are parked behind the daily provider quota, so they never reach a
  terminal state, never cache a `tags_input_fingerprint`, and are re-fetched on *every* run. A spent
  budget stopped new LLM calls but let the fetch-walk grind on through thousands of remaining
  episodes until the job was killed mid-pass, with essentially no tagging accomplished. `TagsStage`
  now checks `ctx.stop()` at the top of each episode, right after the cheap fingerprint pre-check and
  **before** the storage fetch: once the window is spent, every remaining episode is deferred
  untouched (retried next run) and the pass drains in seconds to its end-of-run persist. Non-time-
  bounded runs (`ctx.stop is None`, e.g. local `all` builds) are unaffected. Note the separate,
  non-timeout throughput limit this exposes: with the provider capped at ~20 tag calls/day, working
  through a multi-thousand-episode untagged backlog is inherently many runs — the fix makes each run
  fast, bounded, and green, spending its budget newest-first, not a single run tag everything.

- **The `tag` lane no longer re-fetches the entire backlog's agenda/transcript text every run — it
  triages in memory first and fetches only episodes it will actually tag.** The timeout fixes above
  made the run *bounded*, but it was still doing the wrong work: a live run spent its whole budget
  re-reading agenda + transcript text for ~13k episodes and made no visible tag progress. The reason
  was structural — `tags_input_fingerprint`, the storage-free "have these inputs changed?" proxy, was
  cached only after a **fully resolved** LLM tag. With the provider quota far below the backlog size,
  virtually every episode was permanently non-terminal, so the cheap pre-check never matched and each
  episode fell through to the two-round-trip storage fetch, every run, purely to re-derive "still
  waiting on the LLM." `TagsStage` now decides what to do for each episode **entirely from the record
  already in memory** before any fetch: (1) inputs unchanged **and** an LLM tag already resolved (or
  LLM disabled) → *done*, skip with no fetch; (2) inputs unchanged, rules tags cached, only the
  quota-limited LLM tag outstanding, and the backend already out of dispatch capacity this run →
  *defer with no fetch*, retried when quota frees; (3) new/changed inputs, or capacity still available
  → fetch and tag. The enabling change: the input fingerprint is now cached as soon as a run captures
  an episode's inputs — **including while its LLM dispatch is still pending** (`tags_llm_recipe_hash`
  stays the sole "LLM resolved" signal, so a pending episode is never mistaken for done). A new
  run-scoped `StageContext.tag_llm_dispatch_exhausted` event, set the first time a *fresh* dispatch
  attempt comes back deferred, is what lets case (2) skip the fetch — gated on a pre-dispatch peek at
  the deferred registry so a stale, still-pending entry left over from a prior run (the daily deferred
  sweep just hasn't reconciled it yet) is never mistaken for live quota exhaustion and doesn't
  prematurely skip the rest of the backlog. Net effect: once warm, a run does an in-memory scan of the
  catalog and fetches only the handful of episodes it will actually tag (new meetings + up to the
  remaining quota, newest-first), instead of thousands of storage round trips. This in-memory triage
  is also what makes the quota/routing work above actually reachable within the job — the wasted
  fetches, not tag throughput, were the bottleneck.

- **The daily `tag.yml` workflow (`enrich --lane tag`) no longer fails immediately with
  "unknown lane 'tag'".** The `"tag"` lane was already fully wired everywhere it needed to be —
  the CLI's `--lane` choices, `LANE_STAGES` (which stages a lane runs), and
  `_LANE_OWNED_BLOCKS`/`protected_blocks_for_lane` (cross-lane write isolation) — except
  `citypods/run.py`'s `_build_impl()`, which still validated `lane` against only
  `("audio", "transcribe", "align")` and rejected everything else, including `"tag"`, before
  `TagsStage` ever ran. Also stopped the tag lane from needlessly pre-loading the multi-GB
  Whisper ASR model on every run — it never runs `TranscriptStage` (per `LANE_STAGES`), so it
  never needed one; the pre-load condition previously only excluded the `audio` lane.

- **Direct Gemini structured-output calls no longer 400 on the R5 tag contract, and the LLM
  tournament no longer crashes when one does.** `citypods/llm_compat_probe.py`'s new diagnostic
  runs (the `llm-safe-diagnostic` event, then an additive bisection, then a subtractive
  bisection that strips one JSON Schema construct at a time from the real contract's own
  schema) isolated the actual cause: Gemini's native schema-constrained mode rejects only the
  `minLength`/`maxLength`/`minimum`/`maximum`/`minItems`/`maxItems` keywords Pydantic emits for
  `Field(min_length=..., max_length=..., ge=..., le=...)` constraints — `$defs`/`$ref` (even
  through the contract's real two-level `Suggestion`/`Evidence` reference chain), `anyOf` for
  `Optional` fields, default values, `additionalProperties: false`, and `enum` are all fine on
  their own and in combination. Direct Gemini calls keep Instructor's native `JSON_SCHEMA` mode;
  the request schema Instructor derives is now built from a same-named subclass of the response
  contract whose `model_json_schema()` strips just that keyword family before the request is
  sent, so Gemini keeps enforcing everything else server-side. Local Pydantic validation of the
  actual reply is unaffected — the real contract's fields and constraints are unchanged, so a
  reply that violates one of those bounds still fails validation and still gets Instructor's one
  corrective retry, exactly as before; only Gemini's copy of the *request* schema lost
  server-side enforcement of this one keyword family it was already silently rejecting outright.
  Separately, `citypods/tournament.py`'s `run()` previously let any `LLMBackendError` (a
  malformed reply, a scheduler guard, ...) from either a contestant or a judge call propagate
  uncaught, crashing the whole scheduled run instead of skipping just the affected episode for a
  later attempt — the same `LLMBackendError`-catching pattern `scripts/city_discovery.py`
  already uses. The probe's own `_native()` check also now catches a request-level failure (e.g.
  a read timeout) instead of letting it abort the rest of the diagnostic matrix, and its
  `_safe_error()` now captures the provider's `error.details` field-violation payload when one
  is present.

- **ASR worker report no longer prints stale compute-budget totals.** `asr-worker-report` loaded
  the Modal/Beam dollar ledger straight off storage and printed it as-is, but the per-backend
  stale-cycle reset only ever fired as a side effect of a real dispatch attempt
  (`Budget.available`/`reserve`/`settle`/`release`) — something the report never calls. A backend
  dispatched only rarely (Modal's even-day, 4h+-only schedule in particular) could go a long time
  between touches, so the report kept reprinting whatever total was left over from its last touch,
  mislabeled as the current cycle — observed as Modal showing `$17810.2` used against a `$24`
  budget. `Budget` gains a public `current_ledger()` read path that applies the same reset check
  without granting a reservation; the report now calls it (and `roll_month()`) for Modal/Beam
  before serializing, so it always reflects the current cycle. Also fixed Beam's
  `rollover_day_of_month` (was `1`, matching Modal; Beam's free credits actually reset on the
  18th), which was compounding the same staleness for Beam specifically.

- **City discovery now uses Instructor/Pydantic for structured output.** LLM tasks name one typed
  response contract rather than hand-maintaining JSON Schema dictionaries. Direct Gemini/Mistral and
  DeepSeek calls use Instructor's provider modes, Pydantic validation, and one corrective retry;
  DeepSeek remains in JSON-object mode because its public chat route does not enforce a schema. The
  asynchronous Worker now carries the Pydantic-derived response format and validates a completed result
  during reconciliation, while a validation re-ask remains safely deferred pending a durable queue
  transition. An idempotent re-submit now consumes a completed Worker result; malformed structured output
  defers without exposing completion text in Actions logs, while other per-city failures complete the
  remaining queue and then fail the workflow visibly. This changes no stored meeting artifacts or audio/ASR
  pipeline version.

- **S3-compatible state and coordination reads now survive transient boto failures.** Shared storage
  reads retry transient transport errors, throttling/5xx responses, and the botocore
  `StreamingChecksumBody.strip` parser failure seen in GH#887. Missing objects, credentials,
  permission errors, malformed requests, and other non-transient failures still surface normally;
  no audio or ASR pipeline version changed and stored artifacts are not invalidated.

- **`ASR Quality Ingest` no longer fails on unrelated issue comments, and its parent-close pass
  now works without a checkout.** The workflow still listens to `issue_comment`, but the
  `finalize` job now skips runs where `resolve` found no H15 child issue to ingest, so routine PR
  automation comments stop generating red Xs. When `finalize` does run, it now passes
  `GH_REPO=${{ github.repository }}` to `gh`, avoiding the regression where the job intentionally
  skipped checkout and then failed with "not a git repository" while listing/closing parent
  issues.
- **Feed-health stale-body triage is quieter and easier to audit.** Feed YAMLs can now carry an
  operator-stamped `audit.lifecycle.status` of `inactive` or `superseded`; the feed-health audit
  suppresses `empty`/`stale` findings for those verified retired feeds while leaving structural
  checks like `view-cap`, dead enclosures, and meetings-URL verification intact. The GitHub
  feed-health reconciler also stops opening/refreshing standalone `empty` issues, closing existing
  ones on the next run instead of keeping low-volume or temporary bodies like GH#843 in the issue
  list forever. Remaining `stale` issues now include direct audit links back to the feed config,
  city config, official meetings page, and provider source, so the manual verification loop for
  "did this body die, rename, or keep meeting?" is a quick YAML-backed check instead of a search
  exercise.
- **Chapters now auto-heal after a later timeline correction instead of fossilizing served-time
  offsets (GH#775).** Provider chapter markers are now persisted separately as durable
  `source_chapters`, while `chapters` remains the current served-time/feed-facing projection.
  `ChaptersStage` backfills old single-source records into that shape automatically: source-basis
  records copy their existing chapter list into `source_chapters` with no network call, and older
  served-only records re-fetch provider chapters once to repopulate the source-time copy. With
  that durable raw copy available, `RemapStage` now reprojects chapters whenever a stored
  `served:<timeline-version>` no longer matches the episode's current timeline version, instead of
  reusing stale served offsets forever. Synthetic served-only chapter sets (currently Swagit
  concat's one-chapter-per-segment construction) remain intentionally write-once: they clear
  `source_chapters` and are skipped by the new backfill/remap logic because there is no safe
  single-source source-time representation to reproject. Cross-source planning reconciliation
  now treats `source_chapters` as part of the canonical planning state too, so split record stores
  cannot heal `chapters`/`timeline` while leaving the raw chapter copy stale.
- **Internal ASR worker teardown no longer flips graceful yielded runs into failures.** The
  killable spawned inference backend now calls `multiprocessing.Process.close()` after the child
  has been joined/terminated, releasing tracked process resources so Python's
  `resource_tracker` warning about a leaked semaphore does not turn a successfully-yielded
  `citypods compute run-internal-worker` job into exit code 1 at interpreter shutdown. Added a
  focused regression assertion in `tests/test_compute_local_process.py` proving teardown closes
  the process object as well as terminating it.

### Added

- **Rate-limited LLM dispatch Worker (R10).** Added a private, R2-backed Cloudflare Worker at
  `workers/llm-dispatch-proxy/` with bearer-authenticated OpenAI-shaped asynchronous queue/poll
  endpoints, a per-minute Cron dispatcher, R2 conditional request claims and rate-slot CAS,
  provider-qualified model routing, configurable HTTPS upstream/model settings, bounded exponential
  retry, idempotency keys, and redacted failure handling. It is an async transport for the future
  ROADMAP R2 LiteLLM backend's `JobHandle` path—not a replacement for LiteLLM or a synchronous
  LiteLLM endpoint;
  the configured upstream may be a provider's OpenAI-compatible API or a LiteLLM Proxy. The Worker
  stores queued prompts and generated results in its dedicated R2 bucket so scheduled
  pipeline work can pick up results later without keeping a GitHub Actions runner idle between
  tightly rate-limited provider calls. Deployment is path-scoped and uses the existing Cloudflare
  credentials. This is new infrastructure; it does not backfill existing records or change any
  pipeline version.

- **H15 `/admin/status` transcript-quality panel** ([#885](https://github.com/BashfulBits/city-meeting-podcasts/issues/885)).
  `/admin/status` now surfaces H15's existing trust-routing state as a first-class dashboard
  section instead of requiring operators to inspect raw JSON ledgers. The static status snapshot
  reads local H15 routes, raw-sample timestamps, rollup evidence, and calibration trend history to
  render: a per-source/body route table (route mode, calibrated yes/no, agreement rate, automatic
  margin, reviewed count, L2 coverage, last-sampled age), aggregate trust/calibration
  distribution, a capped needs-attention list for rows with review/calibration gaps, and a global
  L3 gold/calibration summary with the latest trend snapshot. This is reporting-only — no H15
  schema or routing changes — and fulfills the fast-follow admin surface called out in
  [review/12 §H15](review/12-hardening-and-efficiency.md#h15--transcript-quality-metric-periodic-caption-trust-scoring).
- **H15 Layer 3 — human-gold calibration anchor** ([#884](https://github.com/BashfulBits/city-meeting-podcasts/issues/884)).
  Harvests gold-reference text opportunistically from the existing weekly blind A/B review loop
  instead of a separate collection exercise. Redesigned the outcome model (no production review
  data existed yet, so it was a clean redesign, not a migration): `A is better` / `B is better` /
  **`Both fully correct`** (new, replaces the old ambiguous `Tie` option) / `Neither usable`. A
  `both_correct` verdict makes either candidate's already-stored text gold, no typing required,
  gated by a dedicated `gold_agreement_floor` (default 0.92) on the two candidates'
  `text_agreement`; a `neither` verdict's optional correction box is now pre-filled with the
  higher-`auto_score` candidate's text as an editable draft rather than left blank, and only an
  actual edit (diffed against the original draft carried in the hidden metadata) counts as gold.
  `package_reviews` also deliberately pulls `gold_coverage_good_limit`/`gold_coverage_bad_limit`
  (default 1 each) already-evaluated, not-yet-reviewed samples the automatic scorer was already
  confident about into each weekly batch — preferring L2-scored candidates and
  under-represented sources, but never letting source-balance override genuine score extremity —
  so gold coverage isn't limited to the ambiguous band `needs_review` already selects for. New
  `citypods transcript-quality calibrate` subcommand (folded into the existing weekly
  `asr-quality-review.yml`, installing a new lean `wer` extra rather than the full `asr` stack)
  computes real WER/CER (`citypods/text_metrics.py`, extracted from `asr-bench`'s own jiwer usage)
  against each gold-bearing sample, correlates it with `auto_score`/`l2_mean_score`, and writes a
  plain-language calibration report — an `auto_score` coverage histogram, a Pearson correlation,
  agreement-floor accept/reject counts, and a persistent trend log
  (`state/transcript_quality_calibration_trend.json`) — opened/updated as a standing GitHub issue
  each week. New `citypods transcript-quality check-gold-corrections` subcommand (run from
  `asr-quality-eval.yml`, which already has the torch/torchaudio stack) sanity-checks
  reviewer-typed corrections against their audio with the same independent CTC aligner L2 uses,
  flagging low-fit corrections in the report rather than auto-excluding them. Deliberately a
  reporting mechanism, not an auto-tuning one: with ~20-50 gold points expected,
  `agreement_threshold`/`trust_margin_threshold` stay a human-reviewed follow-up. Multi-language
  support stays out of scope, same boundary as L2. See
  [review/12 §H15](review/12-hardening-and-efficiency.md#h15--transcript-quality-metric-periodic-caption-trust-scoring)
  for the full design.

- **H15 Layer 2 — independent CTC forced-alignment judge** ([#883](https://github.com/BashfulBits/city-meeting-podcasts/issues/883)).
  Added `citypods/ctc_align.py::ctc_fit()`, wrapping `torchaudio.pipelines.MMS_FA` (a wav2vec2
  model trained purely for forced alignment — not Whisper, so it cannot share either candidate
  generator's bias) to score the provider-align and ASR-challenger candidates' clipped text
  against the same clipped audio, independently of both. `evaluate_samples` blends `ctc_fit()`'s
  score into `auto_score` (80% weight, with Layer 1's coverage/word-logprob as a 20% smoothing
  term) whenever it succeeds, bounded to `QualityConfig.l2_sample_limit` (default 2) samples per
  `evaluate()` run — combined with the sampler's existing already-sampled exclusion, this gives a
  rotating, oldest-checked-first subset without new cross-run state. Any failure (the
  `asr-align2` extra not installed, a non-English source, model-download failure) falls back to
  the pre-existing Layer-1-only `auto_score` for that candidate, the same per-sample resilience
  pattern used elsewhere in H15 — `TranscriptQualityRoute`'s calibration-gate mechanism
  (bootstrap → agreement check → continuous margin) is otherwise unchanged, per the acceptance
  criterion that L2 only replace what feeds `auto_margin_avg`, not the routing state machine
  itself. English only in v1 (`UnsupportedLanguageError` on other languages — MMS_FA's public
  bundle needs a G2P/uroman preprocessing step this PR doesn't implement). New optional
  `asr-align2` extra (`torch`/`torchaudio`/`torchcodec`) folded into the existing
  `constraints/asr.txt` lock rather than a separate file, since `torch`/`torchaudio` are already
  a transitive pin there via `stable-ts[fw]`'s own dependency on `openai-whisper` — `torchcodec`
  (the `torchaudio.load()` decoder backend as of torchaudio 2.9+) is the only genuinely new
  package. `asr-quality-eval.yml` installs the extra and caches the ~1.2 GB MMS_FA checkpoint via
  `actions/cache`. See [review/12 §H15](review/12-hardening-and-efficiency.md#h15--transcript-quality-metric-periodic-caption-trust-scoring)
  for the full design (L3 human-gold calibration, above, shipped as a follow-on).

- **H15 transcript-quality workflow (L1 wired, calibration-gated routing).** Added
  `citypods transcript-quality` with four sub-commands: `sample`, `evaluate`, `package-review`, and
  `ingest-review`. H15 now persists a capped raw evaluation log
  (`state/transcript_quality_log.json`) separately from a stable, unpruned body/source evidence
  ledger (`state/transcript_quality_rollups.json`), merge-pushing both through new durable-state
  helpers so concurrent runners do not clobber each other; the rollup ledger mutates through an R2
  CAS ledger when available, with a merge-push fallback so a run without R2 CAS still lands
  remotely. The review loop renders blind randomized A/B issue bodies plus a linked static
  synced-transcript review page, and ingests exactly-one-primary task-box decisions back into the
  durable rollups. Added the GitHub Actions split for H15: `asr-quality-eval.yml`,
  `asr-quality-review.yml`, and `asr-quality-ingest.yml` (the latter's missed-event cron safety net
  scans every open child issue via a resolve → matrix-ingest → finalize job split, instead of the
  no-op it originally shipped as).
  - **Layer 1 is fully wired**: `citypods/asr.py`'s `align()`/`transcribe()` now return
    `coverage`/`word_logprob_mean`/`word_logprob_p10` (the words-JSON sidecar bumps to schema v2,
    additive-only), and every production ASR completion in `TranscriptStage` records a near-zero-cost
    L1 sample to the same capped log, independent of whether that source/body has any H15 review
    data yet.
  - **Routing is unblocked in both directions**: a trusted `route_mode` now overrides the
    site-wide `asr_alignment_enabled=false` default to schedule the align lane per source/body
    (not just force fresh transcription), via a calibration-gated mechanism — a bootstrap floor of
    2 net human-reviewed wins, then (once the automatic scorer's agreement with human decisions
    clears a threshold) a continuously-updated automatic score margin drives the ongoing decision.
    See [review/12 §H15](review/12-hardening-and-efficiency.md#h15--transcript-quality-metric-periodic-caption-trust-scoring)
    for the full mechanism and its explicitly-interim status pending L2/L3.
  - **Automatic scoring now measures acoustic fit**, not timing/density shape with a hardcoded
    confidence bias; cross-candidate text/timing comparison moved from a naive positional zip to a
    proper edit-distance alignment.
  - **Accepted-recipe policy** (`accepted_active_recipes`/`minimum_quality_rank`) now keys on the
    catalog-wide `transcript_pipeline_version` instead of the per-episode `transcript_spec_hash`,
    which could never match more than one episode by construction.
  - `evaluate_samples` now contains per-sample failures (e.g. `AlignmentQualityError` on a
    genuinely bad-caption episode) as a recorded `evaluation_error` event instead of aborting the
    whole batch and losing every other sample's work.
  - **Human review decisions are now permanent once ingested.** An independent review caught
    `_normalize_rollups` doing a plain per-`sample_id` dict replace when merging evidence rows —
    because `sample_id` is deterministic, a later periodic re-evaluation of the same episode
    (the common case, since weekly sampling has no reason not to resample recent episodes) would
    silently overwrite a recorded `manual_decision` with a fresh, unreviewed entry. The merge now
    refuses to let an unreviewed entry clobber one that already has a decision, and
    `build_sample_manifest` excludes `sample_id`s that already have any rollup evidence so the
    sampler reaches new episodes over time instead of re-grinding the same recent ones forever.
  - **Calibration bootstrap now requires net human wins, not raw wins.** The gate that decides
    whether a `(source_key, body_key)` row is eligible for calibration checked
    `provider_wins >= 2 or challenger_wins >= 2` — a 2-2 split panel (no net human preference)
    satisfied it. Now reuses `_bootstrap_route_mode`'s own net-margin check, so a split panel
    can't be calibrated into letting the same-generator-biased automatic margin decide routing.
  - Fixed a substring-match bug in `asr-quality-ingest.yml`'s parent-issue-closing check
    (`contains("Parent issue: #5")` also matched "#50"/"#500") with an anchored regex, and raised
    its issue-listing limit so a >200-open-issue backlog can't undercount a parent's true open
    children.
  - The later H15 follow-ups all shipped: L2 in
    [#883](https://github.com/BashfulBits/city-meeting-podcasts/issues/883), L3 in
    [#884](https://github.com/BashfulBits/city-meeting-podcasts/issues/884), and the
    `/admin/status` trust panel in [#885](https://github.com/BashfulBits/city-meeting-podcasts/issues/885)
    via [PR #891](https://github.com/BashfulBits/city-meeting-podcasts/pull/891).

- **H19 internal ASR pull workers now use the same lease ledger as external workers.**
  `asr.yml` no longer consumes a static transcribe shard plan; its reconcile job rebuilds the work
  manifest from canonical records and reaps expired leases, then the matrix runs identical
  `citypods compute run-internal-worker` jobs against the shared Stage-2 pull/claim contract.
  The internal worker now layers local-only supervision on top of the shared claim loop: it uses a
  persistent killable inference subprocess, carries forward timeout/backstop behavior as reusable
  worker-side supervision instead of stage-local threading, prefers shorter known-duration items,
  enforces the hard 4-hour local-duration cap, and admits a claim only when its estimated runtime
  still fits before the 350-minute job backstop. The same runtime-estimate substrate external
  workers use (`state/compute_budget.json` runtime coefficients) now learns a separate
  `github-actions` ASR coefficient from completed local claims, so the start-admission limit can
  shrink automatically as wall-clock time runs down. A locally timed-out claim terminates the child
  process, records ASR timeout backoff on that episode, and abandons the lease back to the queue
  rather than failing it terminally; a superseded claim (a newer run queued behind it) terminates
  and abandons the same way but records no backoff, since the item itself wasn't at fault. That
  backoff is now enforced, not just recorded: every worker's claim admission (Modal/Beam included)
  refuses a still-backing-off item, closing the gap where `abandon()`'s instant no-TTL requeue let
  any worker immediately re-claim and re-time-out the same poisoned recording every run. The daily
  `asr-worker-report` also now opens/updates a tracking issue when a recording has timed out 3+
  times in a row (`asr_timeout_notify_threshold`), and closes it once the backlog clears.

- **H21 duration canonicalization and repair surfaces.** Persisted episode records now treat
  `source_duration_seconds` and `served_duration_seconds` as the canonical scalar duration fields.
  Hot consumers (workqueue ordering, external-worker telemetry, feeds, reports, and dispatch
  planning) now read duration through shared helpers instead of raw legacy fields. The enrich path
  gained a bounded pre-dispatch normalization pass that probes hosted audio via object-key range
  reads when `served_duration_seconds` is missing and emits explicit warning telemetry for probe,
  failure, and still-missing cases. If no canonical probe is available, served duration now stays
  missing rather than being inferred from timeline or source metadata. Added a manual
  `Normalize durations` workflow plus
  `scripts/normalize_durations.py` for one-off catalog repair with dry-run by default, bounded
  `max_items`, JSONL/summary artifacts, and scoped safe writes that only persist canonically probed
  served duration values. New records stop re-emitting legacy `duration` and
  `audio.duration_served` fields, while compatibility reads remain for historical state.

- **Swagit provider gains `list_urls` (multi-view merge), and Austin's three City Council feeds
  are combined into one.** Swagit's `list_url` was always a single view page; Austin splits City
  Council business across three dedicated views (regular meetings, work sessions,
  special-called/budget work sessions), so there was no way to publish one feed covering all
  three. `citypods/providers/swagit.py` now accepts `source.list_urls` (a list), fetched and
  deduped by video id — mirrors the existing Granicus `feed_url`/`feed_urls` pattern. `body` is
  no longer a hard-required key (only `list_url`/`list_urls` is), so a combined feed can omit it
  and take every row across its merged views. `config/feeds/austin-tx-city-council.yml` now lists
  all three views with no `body` filter and carries `aliases` for the two retired feed slugs
  (`austin-tx-city-council-work-session`, `austin-tx-special-called-meetings-budget-work-sessions`)
  so old subscribers get a redirect stub instead of a dead feed.
- **H14d provider-cycle dollar ledger + learned runtime estimator for external ASR workers.**
  `citypods/compute/budget.py` now stores per-backend cycle keys and a persisted runtime-estimate
  model keyed by backend/task/GPU/model/compute profile, while `citypods/compute/policy.py` parses
  provider-cycle dollar caps (`monthly_dollars`, `reserve_dollars`, `rollover_day_of_month`) plus
  backend hardware (`hardware.gpu_type`) and task-level runtime-estimate knobs from
  `config/site_config.yml`. `external_worker.py` now reserves budget in provider dollars, settles
  completed claims with per-run provider spend allocated back to claim owners, and feeds actual
  runtime back into the learned coefficient after each completion so estimates drift with real
  workload behavior instead of fossilizing. Beam wrappers now pass through the provider task id for
  runtime-based settlement and default their GPU target from YAML, while Modal wrappers capture the
  function call/input ids so the worker can attempt billing-report settlement before falling back to
  runtime-rate pricing and also default GPU choice from YAML. Worker telemetry/reporting now surface
  dollar estimates rather than only generic units.
- **H14d policy substrate for external-worker pacing and characterization.** `citypods/compute/policy.py`
  now parses a richer per-backend YAML policy shape from `config/site_config.yml`: generic budget
  units + soft reserve, per-backend preferred run days (`all` / `even` / `odd`), long-meeting
  preference, freshness windows, and fixed-per-run / fixed-per-claim planning knobs.
  `citypods/compute/budget.py` remains backward-compatible with the old `used_gpu_seconds` ledger field
  but now stores generic `used_units`, so future Beam/Modal/diarize cost models are not forced to
  pretend billing is pure elapsed GPU-seconds. `external_worker.py` consumes the parsed policy to pace
  **sequential** claims per invocation against remaining monthly budget and remaining preferred run
  slots. Off-days now stay deliberately conservative while backlog still exists: they admit only fresh
  work and cap that freshness maintenance to one claim, then reopen full pacing once the long-meeting
  backlog is actually cleared. `config/site_config.yml` now carries the first empirical production
  defaults from the H14d benchmark loop: Modal tuned for `L4`, Beam tuned for `RTX4090`, both using
  `effective-runtime-second` budget units with monthly caps conservatively scaled down from raw GPU
  credits to absorb CPU/RAM billing, plus much higher sequential `max_claims_per_run` ceilings so the
  preferred-day planner can actually spend the monthly budget. The current production cap remains one
  active transcription at a time per container; the backlog lever here is sequential multi-claim
  throughput, not in-container GPU concurrency. Also adds `scripts/compute/beam_canary.py` and
  `scripts/compute/modal_canary.py`, one-off characterization wrappers used to collect live Beam and
  Modal telemetry without touching the production schedule path.
- **Incomplete-source (short-media) quarantine lifecycle: publish with a disclaimer instead of
  churning findings or excluding real content
  ([GH#851](https://github.com/BashfulBits/city-meeting-podcasts/issues/851)).** Some cities publish
  a recording genuinely shorter than the meeting; this extends the GH#795 withheld/dead lifecycle
  with a sibling `suspected_partial`/`confirmed_partial` verdict for media that is real and playable
  but short, rather than empty/dead. The trigger is deliberately pre-trim and probe-only — the
  decoded audio-stream end vs. `min(container_duration, ep.duration)` in `SilencePlanner.plan()` —
  never the EDL or `audio_duration_served` (both were the buggy/fossilizing side of GH#702/#849) and
  never the post-silence-trim served duration (which would conflate legitimate trimming, like
  Arlington's real 13550s→6681s cut, with a truncated fetch). Confirmation reuses the existing
  two-independent-fetch discriminator (`CONFIRM_THRESHOLD`) via its own `partial_confirmations`
  counter; an unconfirmed observation withholds a "done" timeline exactly like a degenerate/
  near-silent decode does today, so the retry loop itself proves reproducibility with no new bypass
  logic anywhere. Once confirmed, planning proceeds normally (the EDL is already decoded-length-
  bounded) and a stale `chapters_basis` is reset so `RemapStage` drops any provider-agenda chapters
  beyond the real content on its next pass. `check_timeline_integrity` treats `confirmed_partial` as
  terminal for repair (`media-partial`, mirroring `media-withheld`) and `TimelineStage` gates it on
  the same flat 30-day recheck as confirmed-dead media. The feed keeps publishing the episode
  (deliberately never added to `WITHHELD_STATES`) with a factual disclaimer prepended to its show
  notes, linked to the source watch page when known.
- **Unified storage-reclaim policy with a data-loss recovery backstop
  ([GH#496](https://github.com/BashfulBits/city-meeting-podcasts/issues/496)).** The weekly `audio-gc`
  workflow is now **"Storage reclaim"** and runs three backstops on its existing cron. (1) **Bucket
  lifecycle as-code** (`scripts/apply_bucket_lifecycle.py`): idempotently expires the control-plane
  validator's R2 scratch prefixes (`work-leases/__validate__/`, `provider-leases/validate-`) after 1
  day — the infrastructure fix for CR-SC-15, since a killed runner can't run the validator's
  best-effort cleanup — through Cloudflare's dedicated R2 lifecycle API credential rather than the
  normal object-access key — and configures B2's noncurrent-version retention window
  (`defaults.b2_retention_days`, default 30d) so a mistaken delete stays restorable without expiring
  live current objects. A hard guardrail refuses any R2 rule broader than
  a scratch prefix (an over-broad `work-leases/` rule would expire live leases). (2) **Double-confirmed
  auto-apply orphan GC** (`gc_audio.py --auto-confirm`): a scheduled run now deletes the provably-safe
  subset without a human — orphans seen unreferenced across ≥2 runs past `defaults.orphan_quarantine_days`
  (default 21d), tracked in `state/orphan-ledger.json`; a key that reappears in the live set drops from
  the ledger, so a GH#421 flip-flop never matures. Manual `apply=true` (main only) still deletes
  everything reported. (3) **Resurrection watchdog** (`check_reclaim_resurrection.py`): every delete is
  logged to the append-only `state/reclaim-log.jsonl` with a `recover_by` deadline; if a live record
  comes to reference a reaped key while it is still restorable, a HIGH-priority (`priority:high`) issue
  is opened in time to restore the B2 version before it purges. Also promotes **"R2 holds only
  ephemeral/derivable objects"** to a test-enforced invariant (`routing.py` `_EPHEMERAL_R2_PREFIXES`):
  adding a coordination prefix without declaring it ephemeral now fails at import and in tests, so a
  canonical (backup-less) record can't be routed to R2 by accident. **The rolling GC issue's
  open/update/close lifecycle moved from workflow-YAML `if:` conditions into Python**
  (`reconcile_gc_issue` in `gc_audio.py`, gated behind `--reconcile-issue`), mirroring
  `scripts/audit_feeds.py`'s established `reconcile()`/`_gh()` pattern instead of a second, less
  testable variant — this also fixed a real gap where a scheduled auto-confirm run that fully
  cleared the backlog matched neither the old open nor close step, leaving the ticket open forever.

- **`.coderabbit.yaml` settings-as-code for CodeRabbit reviews.** A measurement of the last 100 PRs
  showed the repo already runs near the review floor (~1.65 review-runs/PR; ~97% of runs are the
  unavoidable first review plus fix-response re-reviews), so this config's real value is review
  **quality**, not a cut in review volume — and backoff is a per-hour **burst** problem best handled
  by agent behavior (batch all fixes for a review round into one push; space out PR openings; check
  `@coderabbitai reviews remaining?` when near the limit), which the docs now spell out. On quality:
  sets `profile: assertive` (more precise findings, no extra review-event cost) and preloads
  `AGENTS.md`/`ARCHITECTURE.md`/`CONTRIBUTING.md` as knowledge-base context plus per-path
  instructions covering this repo's documented invariants (append-only records, split hashes, stage
  ordering, the wall-clock budget, untrusted LLM output, the SSRF gate) so they aren't flagged as
  bugs. Excludes only genuinely non-reviewable paths (per-city/feed data under `config/`, compiled
  `constraints/*.txt`, generated `docs/**`, lockfiles) — docs (`**/*.md`) stay in scope for a single
  sanity pass since ARCHITECTURE/review/AGENTS/CHANGELOG are load-bearing here; skips
  Renovate-authored PRs; suggests `type:*`/`area:*` labels without auto-applying them
  (`auto_apply_labels: false`), keeping CONTRIBUTING.md's "Project fields" table as the single
  taxonomy source (ingested via `code_guidelines`) rather than a duplicate list in the YAML; and adds
  an advisory (`warning`-mode, non-blocking) custom pre-merge check that flags source-changing PRs
  missing a `CHANGELOG.md`/`ARCHITECTURE.md`/`review/*.md` update per the doc-update contract. Draft
  PRs (`drafts: false`, CR's default) are documented as a **conditional** tool — worth it only for
  genuinely iterative/long-churn PRs, since measurement put their saving at ~1–2%. AGENTS.md gained a
  "Working with CodeRabbit on a PR" section (burst-avoidance habits; doc-only PRs get one review then
  stop): agents must triage findings with a strong-reasoning model (Opus/GPT-5.5, not the fast
  default), push back/fix/fix-and-expand per comment, resolve CI, and report a summary — now also a
  `PULL_REQUEST_TEMPLATE.md` checklist item.
- **Austin, TX coverage via Swagit.** Added Austin entity config plus City Council, work session,
  special/budget, Austin Housing Finance Corporation, and active board/commission feeds whose official
  Austin boards list has a matching non-empty Swagit historical subcategory.
- **`citypods compute reclaim-transcript --source-key SK --episode-uid UID [--write]`.** Recovery
  tool for the class of loss #833 fixed: an ASR artifact (VTT + words JSON) already uploaded to
  storage, but the record's `transcript` block never got updated to reference it — the lease
  reaper infers `done` from artifact presence, so nothing else would ever retry it. Recomputes the
  same recipe hash the original transcribing worker used (`_asr_recipe_hash`, deterministic from
  the current city config + episode fields) and re-attaches the existing keys if present — it
  never re-transcribes. Dry-run by default (reports what it found); `--write` pushes the fix
  through the same owned-block-scoped `push_records_merged` path a real worker uses.

### Fixed

- **Audit-backlog paydown: review/24's Critical/High/Medium findings, plus the bulk of review/23
  (100 rows) and review/21 (17 rows), fixed across a themed 11-batch sweep on
  `fix/repository-code-review-2`.** review/24's own S1 observation was that the audit trail had
  accumulated a "growing tail of small, individually-minor, already-catalogued correctness/security
  gaps" with no forcing function to pay it down; this sweep is that paydown, following review/24's
  Critical → High → Medium → Low remediation order and then closing out the remaining review/23/
  review/21 rows by theme. Full per-row disposition lives in each doc (review/24's new "Disposition
  (2026-07-10)" section; review/23's Status column; review/21's inline notes) — summary by theme:
  - **CI/CD script-injection + secrets scoping (C1/C2/H6, MR-GH-01/02, CR2-GH-*):**
    `clear-materialization.yml`/`reset-backoff.yml` no longer splice `workflow_dispatch` inputs
    directly into shell text (script injection) or pipe a uid filter through an env var literally
    named `UID` (silently shadowed by bash's own readonly builtin, so `--uid` filtering never
    worked); both gained a default-branch guard before any destructive `apply`/`delete_objects`.
    Job-level secrets moved to step-level `env:` in `deploy.yml`, `availability-digest.yml`, and
    `asr.yml`'s reconcile job. `contracts.yml`'s wait loop now polls both `audio.yml` and
    `granicus-probe.yml` and retries a `gh` failure instead of masking it as `"[]"`; `ci.yml`/
    `spike-r2-cas.yml` gained `concurrency` groups. The mixed-SHA-pinning half of H6 is deferred to
    review/22, the separate dependency-pinning effort.
  - **SSRF gate completion (C3, H1, H3, MR-CP-01/02):** `concat.py`'s legacy multi-segment Swagit
    duration probe, CivicPlus's HLS-manifest resolver, and Swagit's redirect/scraped media URLs all
    now call `validate_source_url` explicitly instead of relying on an incidental size-cap side
    effect; `security._is_blocked_ip` now blocks the RFC 6598 shared address space
    (`100.64.0.0/10`, used by CGNAT and some cloud-internal routing) that every other private/
    reserved-range check missed.
  - **Presigned-URL redaction sweep (H5, MR-CP-03/04, CR2-CP-07/28):** every "detail becomes a
    public GitHub issue" call site (`audit.py`'s self-heal note, `contracts.py`'s media check,
    `availability_digest.py`'s issue table) now routes through the existing
    `redact_subprocess_text`/`_media_fetch_detail`-style redaction instead of raw truncation or
    partial pipe-only escaping.
  - **CAS-capability + timeout backstop (H2/M2, CR2-CP-53/09):** `RoutingStorage.put_cas`/
    `get_bytes` now gate on the backend's own `cas_capable` flag instead of `hasattr`, so a
    B2-without-R2 backend raises instead of silently degrading a coordination write to
    non-atomic; `GuardedHTTPAdapter` now applies `DEFAULT_TIMEOUT` when a caller omits `timeout=`.
  - **Batch-loop resilience (M6, MR-SC-01/02/05, CR2-CP-41):** `gc_audio.py`, `probe_granicus_worker.py`,
    `stages.py`'s per-source planning, `refresh_fixtures.py`, `check_endpoints.py`, and
    `clear_run_materializations.py`/`probe_granicus_sustained.py` now record-and-continue (or
    persist output in a `finally`) instead of letting one item's failure abort the whole run.
  - **Availability & rendering correctness (M3/M4/M5, MR-TM-01, CR2-CP-02/03/11/12/25):**
    `with_operator_override(None, None, …)` is now a true no-op instead of fabricating an
    `AVAILABLE` verdict; an operator override no longer survives a source-fingerprint reset;
    `render_city_page` now includes video-only episodes; provider-supplied RSS `<link>` values are
    scheme-validated before reaching an `href`; `admin.html` gained the same `esc()` escaping
    `status.html` already had.
  - **Pipeline/report/run correctness (M1/M7/M8/M9 + misc, CR2-CP-18/19/20/22/23/26/29/35/37/39/
    40/43/45):** `asr_workers: 0` now rejected at config load instead of a runtime
    `ZeroDivisionError`; VTT timestamps round to whole milliseconds before splitting h/m/s (no
    more invalid `SS=60.000`); the alignment quality gate no longer skips on a zero-word result;
    `h16_report` sorts shards numerically past 9; `report.py` routes through the shared UTC-aware
    ISO parser; `run.py` closes its owned `ffmpeg` process in a `finally`; `http.py`'s per-host
    concurrency slot now holds across the buffered body read, not just the initial round trip;
    `materialize_audio` no longer risks a self-deadlock when two episodes in the same call share a
    cache key; `providers.register` rejects a duplicate name instead of silently overwriting; a
    stale ASR transcript key can no longer mask a newly-arrived provider transcript as "done" in
    the workqueue planner.
  - **scripts/ cleanup (CR2-SC-01/05/06/07/08/09/10/12/15/17, MR-SC-06/07):**
    `validate_control_plane.py` now fails (instead of silently skipping) the routing check when a
    backend lacks the introspection method, and mkdir's its `--output` parent;
    `generate_board_cities.py` normalizes stored body names the same way discovery does and flags
    same-run slug collisions instead of misreporting them as pre-existing files;
    `availability_digest.py`'s provider resolve call is now bounded by `--timeout`;
    `compare_timeline_diagnostics.py`'s fixed/worsened counters are now mutually exclusive;
    `prepare_whisper.py` skips already-downloaded files on retry; `spike_r2_cas.py` scopes its CAS
    mechanism-detection to an actual 412, not any client error.
  - **tests/ + workers/ hygiene (CR2-TS-*, CR2-WK-*):** helper threads across `test_resources.py`/
    `test_http.py` are `daemon=True`; several tests now assert what their name/docstring claimed
    (`check_rehost_backlog` actually gets called, `_tick()` is directly callable instead of racing
    a background thread's timing); a shared `write_local_backend_site_config()` helper replaces 4
    copies of the same fixture text; the Cloudflare Worker's test suite gained a
    `WWW-Authenticate` assertion on the missing-bearer case and a plain-GET-without-Range
    happy-path test.
  - Deferred, with rationale recorded in the review docs: the mixed-SHA-pinning half of H6/MR-GH-03
    (→ review/22), `templates/base.html.j2`'s inlined stylesheet (CR2-TM-06, needs a build-pipeline
    change + snapshot regen), the ffmpeg `file`-protocol whitelist (CR2-CP-06, needs per-call-site
    local-vs-remote differentiation), `concat.py`'s stop-budget gating on the legacy-segment fetch
    (CR2-CP-38, reframed by the review itself as a non-urgent efficiency gap), two
    `audio-runner-image.yml`/Dockerfile rows out of this pass's scope (CR2-GH-10/12), the S3
    linearizability test harness (standalone testing-infra project), and the `fork()`
    `DeprecationWarning` in `test_compute_local_process.py` (S5, minor test-only hygiene).

- **The Stage-2 work-lease reaper never actually ran in production, despite `config/site_config.yml`
  saying `work_lease_reaper_enabled: true` since H14b/H14c went live
  ([GH#706](https://github.com/BashfulBits/city-meeting-podcasts/issues/706) §6(b)).** `citypods
  compute reconcile`'s CLI wiring read `site_config.get("work_lease_reaper_enabled", False)` at the
  document root, but the key lives nested under `defaults:` (sibling to `compute_backend`/
  `compute_backends`) — the lookup silently fell back to `False` every run, so the `if cas and
  sweep_work_leases:` gate in `reconcile_compute()` never engaged and `reap_work_leases()` was never
  called. Found while closing out §6(b): a manual raw-ledger audit
  ([#858](https://github.com/BashfulBits/city-meeting-podcasts/pull/858)) turned up 108 leased
  work-lease objects, 90 already past their ~20h TTL, that every scheduled reconcile run since
  2026-07-06 had reported `0 requeued/settled/in-flight` against — `asr-worker-report`'s live lease
  counts looked correct throughout because that path reads the ledger directly for display, with no
  `sweep_work_leases` gate. Fixed to read the flag from `site_config["defaults"]`; added a CLI-level
  regression test (`test_cli_reconcile_reads_work_lease_reaper_enabled_from_defaults_block`) that
  exercises `cli.main(["compute", "reconcile", ...])` against a real YAML file with the flag nested
  under `defaults:`, since every prior test called `reconcile_compute()` directly with
  `sweep_work_leases` passed as a Python argument and so never exercised the config-parsing path
  where the bug actually lived.
- **The weekly `Validate R2/CAS control plane` health check no longer requires a public R2 URL for
  coordination-only validation.** `scripts/validate_control_plane.py` now constructs the R2 backend
  with `require_public_base_url=False`, matching the validator's real role: it exercises only private
  CAS coordination objects on R2, not publicly served artifact URLs. This fixes the scheduled
  workflow regression where a blank `R2_PUBLIC_BASE_URL` made the validator exit before running any
  checks, even though the repo's weekly control-plane health check intentionally uses the
  coordination-only path.
- **Cheap timeline-duration fallback no longer files sub-second padding noise, and large fallback
  mismatches can now self-heal through targeted repair.** GH#798/GH#799 were paired
  `timeline-duration-mismatch` / `timeline-short-coverage` issues from the record-only fallback
  path used when no live hosted-audio stream probe is available. That fallback was still using the
  structural 0.1s EDL tolerance, so normal AAC/sample-rounding deltas below the live
  `rendered-duration-mismatch` issue threshold kept opening operational issues. The fallback now
  uses the same `timeline_finding_min_delta` floor (1.0s by default) for feed-health findings, while
  genuinely large stored EDL-vs-served-duration mismatches stamp `timeline-replan`,
  `audio-rematerialize`, and `transcript-regenerate` repair actions when the explicit repair gate is
  enabled. Repairable timeline feed-health issue bodies now link directly to the Feed health audit
  workflow and list the `timeline_repair=true` / `timeline_repair_cohort` inputs to run. Existing
  sub-second rows should clear on the next audit after this ships; truly stale rows remain visible
  and repairable.
- **External workers and `asr-worker-report` could trust a stale persisted `work.json`, hiding the
  real long-meeting backlog even when the canonical records had durations.** The worker/report path
  had been rotate-reading the persisted manifest directly, so whichever in-Actions lane last rebuilt
  `state/work.json` effectively froze the external queue view until the next rebuild. In live H14d
  validation that made the duration band read `2393 total, 0 over 4.0h, 2393 unknown duration`
  despite most records already carrying `audio.duration_served`. The fix does **not** treat
  `work.json` as canonical for derivable fields anymore: external workers and `report_workers.py`
  now rebuild a fresh manifest from `episodes.json` records, then overlay only persisted
  operational sidecar state (running/backoff/dead state, leases, retry/error/estimate fields). That
  preserves the durable coordination hints without letting stale manifest content suppress the true
  duration-aware queue order. The first post-fix worker report immediately recovered the intended
  view: `2108 total, 91 over 4.0h, 77 unknown duration, max known 10.92h`, with both Beam and
  Modal reporting `backlog long 91`.
- **`audio_duration_served` could fossilize at a pre-repair value and re-file a resolved
  `timeline-duration-mismatch` forever ([GH#847](https://github.com/BashfulBits/city-meeting-podcasts/issues/847),
  [GH#849](https://github.com/BashfulBits/city-meeting-podcasts/issues/849)).** The field is only
  written by the encode path (on a fresh upload) and by ASR; when a post-repair episode's audio
  object was reused rather than re-encoded and ASR hadn't (re)run, neither writer fired, so the
  stored value never advanced past whatever it was before the repair — and the daily no-probe
  audit trusted it indefinitely. Separately, both writers probed the MP4 container's advisory
  `format.duration` rather than the exact audio-stream sample clock; that field legitimately
  disagrees with the played audio by up to ~1s (AAC/`mvhd` rounding), which is what let a benign
  sub-1s band form in the first place. Both writers (`AudioStage` finalize, ASR's served-duration
  refresh) now probe the stream-sample clock (`duration_ts * time_base`, falling back to the
  container only when stream timing is absent) via a new `_probe_served_duration_secs`, so the
  stored field can't drift from the timeline-audio audit's own measurement. `check_timeline_integrity`
  also now self-heals a stale `audio_duration_served` in place whenever a run actually probes the
  hosted object and `--persist-timeline-integrity` is set — the same bounded, audit-owned write
  path used for repair blocks — so an already-repaired episode's fossil clears on the next
  diagnostics-enabled audit instead of waiting on an unrelated re-encode/ASR pass.
- **The GH#849 self-heal never actually persisted** — `check_timeline_integrity` corrected
  `ep.audio_duration_served` on the transient `Episode` object, but `audit_city` only ever copied
  the `integrity` block back into the saved record, not the served-duration field, so the
  correction was silently discarded the moment the audit returned. Extracted the write-back into
  `sync_timeline_integrity_mutations`, which now copies back both fields, and added direct tests
  for it so this can't regress unnoticed again.
- **Same uid, different `audio_key`/`audio_spec_hash`/`audio_duration_served`/integrity across two
  feed shards ([GH#850](https://github.com/BashfulBits/city-meeting-podcasts/issues/850)).** A
  combined feed and its per-board siblings are meant to share one `sources/<source_key>/episodes.json`
  store (`source_key()` deliberately ignores `body`), but `config/feeds/fort-worth-tx.yml`'s
  `feed_urls` list had been missing one `view_id` since the file was created — silently hashing
  the combined feed to a different `source_key` than its 17 per-board siblings (fixed; all 18
  Fort Worth feeds now agree). Once split, `AudioArtifactCache.canonical_source` (GH#421) only
  synchronizes a shared uid's audio fields across the two stores at the moment both need a fresh
  encode/credit in the very same run — a later run touching only one of them leaves the other
  stale indefinitely with nothing to reconcile it. Added `reconcile_cross_source_audio`: after
  every city is audited, it groups sources by `city_entity`, finds any uid present in more than
  one store whose audio-owned fields disagree, and (when `--persist-timeline-integrity` is set)
  corrects the stale copies to match a canonical one — preferring whichever copy this run's live
  probe classified `ok`, falling back to the newest `audio_encode_time`, and leaving genuinely
  ambiguous cases unresolved (with a `cross-source-audio-divergence` finding) rather than
  guessing. A whole-catalog scan found Fort Worth was the only city with this specific
  majority-consensus-with-one-outlier config pattern; no other city needed the config fix.
- **Cross-source-shard reconciliation didn't cover the field that actually caused #850's audio
  divergence ([GH#854](https://github.com/BashfulBits/city-meeting-podcasts/issues/854)).**
  `reconcile_cross_source_audio`'s equality check only hashed audio+integrity fields, so a uid
  whose audio had already converged but whose `chapters`/`chapters_basis`/`timeline` still
  disagreed across two feed shards was silently treated as "already converged" — masking the root
  cause: `TimelineStage`/`ChaptersStage` plan once per source-key store and never recompute
  ("chapters don't change once set"), so two stores sharing one physical uid can independently
  derive different chapters/timeline for it, which is what produces a different `audio_spec_hash`
  (and thus a different independently-encoded `audio_key`) on each side in the first place. Added
  `chapters`/`chapters_basis`/`timeline` to both the divergence-detection signature and the
  canonical-copy write-back, using the same canonical-selection rule already established for the
  audio fields (live-probe `ok` wins, else newest `audio_encode_time`). Runs inside the same
  existing audit pass, so it heals already-divergent uids on the next `--persist-timeline-integrity`
  run with no separate backfill, and durably prevents future re-divergence (e.g. after a planner
  version bump lands on only one shard) instead of the #850 fix having to keep correcting the same
  symptom forever.
- **Work-manifest persistence dropped `duration_hours`, making 100% of the feed-visible
  transcript-asr backlog read as unknown-duration.** `_workitem_to_dict` / `_workitem_from_dict`
  serialized every `WorkItem` field *except* `duration_hours` — a computed ordering input (from
  `audio.duration_served`), not one of the inert reserved fields. `build_manifest` set it correctly
  in memory, but `save_manifest`→`load_manifest` silently reset it to `0.0` on every round trip, so
  every consumer that reads the persisted `state/work.json` (the `long_first` comparator, the
  `asr-worker-report` duration band) saw *unknown duration* for the entire backlog even though the
  records carried a real served duration (confirmed by the pending-unknown diagnostic: 2292/2391
  sampled records had `audio.duration_served` populated while the manifest reported them all as 0h).
  This is what made `long_first` float nothing and kept the duration band pinned at 2393/2393
  unknown across every rebuild. `duration_hours` now round-trips; the manifest self-heals on the
  next `build_manifest`+`save_manifest` (no backfill needed — it is rederived from records each run).
- **Owned-block merge: a better remote plan no longer silently drops an owning lane's just-written
  artifact.** `_preserve_remote_planning_if_better` (part of `merge_preserving_foreign`) overwrote
  *all* artifact blocks — including `transcript` — from remote whenever remote's timeline/source
  planning rank was strictly better than the pushing worker's snapshot, bypassing the
  `protected`/`owned_uids` scoping the rest of the merge respects. When remote had no value for that
  block, it was popped. Surfaced on GH#831's first long ( ~6.6h ) Modal canary: the worker reported
  `completed: 1` and its VTT/words artifact was uploaded, but the record showed `transcript: null` —
  a permanent, invisible loss, because the lease reaper infers *done* from artifact presence while
  nothing reconciles the empty record block. The preservation path now only *replaces* an owned
  block when remote has a truthy (fresher) value for it — the legitimate stale-container-audio →
  remote-decoded-audio case — and never *drops* one the run just produced; non-owned (`protected`)
  blocks and planning fields keep the original replace-or-drop behavior.
- **External worker never persisted its transcript record: an orphaned `return` made the
  `push_records_merged` call dead code.** `_run_transcribe_item` (Modal/Beam pull worker) wrote the
  transcript block into the worker's *local* `state_dir` (`save_records`) and then hit `return
  adopted` — placed directly *above* the `push_records_merged` that durably commits the owned block
  to canonical storage, so the push never executed. The VTT/words artifact still uploaded via
  `put_file`, but the record's `transcript` block only ever lived on the ephemeral worker
  filesystem, discarded when the function exited — so *every* external transcription since the
  regression (PR #824, 2026-07-05) landed its artifact but silently lost its record block, the same
  invisible loss the owned-block-merge fix above guards against but one layer earlier (the guarded
  push was simply never reached). This is why fresh completions kept reading back as un-transcribed
  and needed `reclaim-transcript`. The `return adopted` now runs *after* the push; a regression test
  asserts `push_records_merged` is invoked (with `owned_uids` scoping) on both the fresh-transcription
  and adopted branches. Affected episodes are recoverable via `reclaim-transcript --write` — the
  artifacts were never lost.
- **Modal and Beam model-bake image-build steps hit HF Hub unauthenticated, risking a rate-limited
  build on rebuild ([GH#811](https://github.com/BashfulBits/city-meeting-podcasts/issues/811)).**
  Neither `modal.Image.run_commands()` nor Beam's `Image.add_commands()` inherits the provider
  runtime-secret bundle — that binding only exists at function-runtime (Modal's
  `@app.function(secrets=...)`, Beam's `@schedule(secrets=...)`), not during image build — so the
  `snapshot_download()` call that bakes the pinned Whisper model logged HF Hub's anonymous-request
  warning on both providers' first live deploy. Both builds still succeeded (the model repo is
  public), but a busy-period anonymous rate limit could fail a future rebuild triggered by a
  dependency bump or model-revision change. Fixed by threading `HF_TOKEN` into the build step
  itself: Modal's model-bake `run_commands()` now passes
  `secrets=[modal.Secret.from_name(SECRET_NAME)]` (the same `citypods-modal-worker` bundle used at
  runtime), and Beam's image chain adds `.with_secrets(["HF_TOKEN"])` before `add_commands()`.
  `huggingface_hub` picks up `HF_TOKEN` from the environment automatically, so no other code
  changed. Non-blocking, additive fix — no behavior or pipeline changes otherwise.

### Added

- **`asr-worker-report`'s `--recent N` / `recent` workflow input.** The aggregated worker-telemetry
  counts (success/failed, peak RSS/VRAM) never retained *which* episode a completion was — surfaced
  during live H14b/H14c canary validation ([#706](https://github.com/BashfulBits/city-meeting-podcasts/issues/706))
  when a completed run's log gave no way to identify the claimed episode for a post-canary spot-check.
  `report_workers.py --recent N` (or the workflow's `recent` `workflow_dispatch` input) now also lists
  the last N raw telemetry samples — `backend`, `source_key`/`episode_uid`, `outcome`,
  `duration_hours`, `elapsed_seconds`, `finished_at` — reusing fields `_append_telemetry_sample`
  already wrote per-sample; no new storage writes. Defaults to `0` (unchanged report).

- **`asr-worker-report` manifest-freshness and `long_first` backlog-composition diagnostics.**
  Two canary sessions were spent trying to reason about whether `long_first` had "taken effect" by
  cross-referencing GitHub Actions run *start* times against the config merge — which gave a wrong
  answer once (a job starting after the merge can still finish, and rebuild `state/work.json`, well
  after a canary run in between already read the stale pre-merge manifest). The report now reads
  `state/work.json`'s own last-modified time directly from storage (`manifest_last_modified`, an
  exact-key list, not a broad scan) instead of inferring freshness indirectly. It also reports
  `transcript_asr_duration_band` — of the current feed-visible/queued transcript-asr backlog
  (exactly `external_worker.py`'s own candidate filter), how many exceed
  `asr_local_max_duration_hours` (what `long_first` actually floats), how many have unknown
  duration (can never be floated regardless of true length), and the max known duration — so "why
  didn't a canary land on a long meeting" is answered by one report call instead of a live-canary
  guessing game.

### Changed

- **`long_first: 4` enabled in `backlog_priority` — external-required (>4h) transcript work now
  drains first.** Recordings over `asr_local_max_duration_hours` (4h) can only be transcribed by the
  capped external GPU tier (the in-process backend refuses them), so with recency-only ordering a
  steady stream of short episodes could starve them indefinitely. `long_first` floats the >4h
  transcript band ahead of `recency`, catalog-wide. It never reorders `audio`
  (`workqueue.DURATION_AWARE_WORK_CLASSES` excludes it), and the local ASR lane simply defers the
  floated >4h items at preflight (a cheap, pre-download duration check) — so local throughput on
  short meetings is unchanged; only the ordering the external workers see changes. Also lets a
  `max_claims`-elevated canary walk into the long band to validate long-audio + lease renewal.

### Added

- **External-worker adopt/renewal log lines.** The pull worker now prints `[external-worker] adopted
  <source>/<uid>` when it reconciles an already-present artifact instead of transcribing, and
  `[external-worker] lease renewed <source>/<uid> expiry=…` (or `… renew skipped … (no longer held)`)
  each time the renewal thread fires during a long inference. Renewal success was previously silent,
  making it unobservable in a live canary; the interval is now a `_renew_interval()` method so tests
  drive the renewal thread deterministically without a real long transcription. New tests cover the
  renewal-thread wiring and the budget-decline → abandon-to-`queued` path.

### Fixed

- **Pull-worker `max_claims` counts only new transcriptions, not adopted items.** The claim loop
  (`compute/external_worker.py`) incremented `claimed` and checked `max_claims` at lease acquisition —
  before the transcribe path discovers whether the item's ASR artifacts already exist. An
  already-transcribed item that got re-claimed (stale `work.json`, or a prior owner that uploaded then
  crashed before recording) was *adopted* (state reconciled, no GPU work) yet still consumed a
  `max_claims` slot and ended the run, so a manual `max_claims=1` canary would adopt the head-of-queue
  item and stop instead of transcribing a fresh one (surfaced smoke-testing the Modal worker:
  `completed: 1` but `peak_gpu_vram_used_bytes: 0`). `max_claims` now caps new transcriptions:
  adopted items increment a distinct `adopted` summary counter, don't consume a slot, and the loop
  scans past them. A new `max_scan` bound (default `max_claims + 50`; overridable via
  `CITYPODS_WORKER_MAX_SCAN` or the per-backend `site_config` `max_scan`) keeps a fully-stale manifest
  from making one run walk the entire queue. Failed attempts still consume a slot (real work / budget).

- **`S3CompatibleStorage` normalizes a bare-host `endpoint_url`.** First live Beam
  (H14c) scheduled run crashed in `b2_from_env()` — `boto3.client(endpoint_url=...)` raises
  `ValueError: Invalid endpoint` when the URL has no scheme. The Beam secret for `B2_ENDPOINT` had
  been set to the bare host (`s3.us-west-002.backblazeb2.com`), unlike the GitHub Actions secret of
  the same name which includes `https://` — the two secret stores are populated independently, so
  they silently drifted. `_region_from_b2_endpoint()` already tolerated a missing scheme (its
  `split("://")` is a no-op without one), which masked the gap until boto3's stricter endpoint
  validation hit. `S3CompatibleStorage.__init__` now prepends `https://` when `endpoint_url` has no
  `://`, so a bare-host secret in any backend's env store no longer takes down the worker.

- **H11d deploy resilience: `deploy.yml` retries `actions/deploy-pages` on transient GitHub Pages
  backend failures.** Two scheduled/push `Build & Deploy` runs on 2026-07-05 failed at the deploy
  step with GitHub's generic `Deployment failed, try again later.` after an otherwise-clean render
  (this repo's `pages` concurrency group already prevents self-inflicted races, and neither failure
  overlapped another Pages deploy) — a known intermittent backend hiccup in `actions/deploy-pages`
  itself. The deploy step now retries up to 3 attempts total with backoff (15s, then 30s) before
  failing the job, so a single transient GitHub-side error no longer reds out an otherwise-good
  build. See [review/11 H11d](review/11-technical-design-roadmap.md).

### Added

- **Beam worker: resolve pinned deps/model locally instead of referencing build-time repo files (GH#816/#818).**
  Beam's remote image build has no access to local repo files (confirmed against the installed SDK:
  `Image.add_python_packages()` given a file path reads it locally, before anything reaches Beam's
  backend — there is no Modal-style `add_local_dir()` build-time equivalent). `beam_app.py` now reads
  `constraints/asr.txt` and `citypods/asr.py` on the machine invoking `beam deploy` and bakes the
  resolved `package==version` list and HF model repo/revision into the image spec as literal values;
  `add_local_path("citypods/")` is kept for what it actually does — staging the package for the
  deployed function's own runtime import. `scripts/check_dependency_policy.py`'s external-worker
  guard was sharpened to flag only an actual hardcoded version (`pkg==x`/`pkg>=x`), not a bare
  package-name selector key with no adjacent version (the new pattern this fix relies on).

- **Beam external transcription worker pins dependencies + model to the runner (GH#277, part of #804).**
  Same parity as the Modal worker, applied to `scripts/compute/beam_app.py`: the hand-maintained
  package list is replaced with `pip install '.[storage,asr-transcribe]' -c constraints/asr.txt` (same
  pinned versions as the runner, no torch), a digest-pinned CUDA 12 + cuDNN 9 `base_image`, and the
  pinned Whisper model baked into the image via `ASR_MODEL_PATH`. Stacked on GH#276; validated on live
  bounded single-recording Beam test runs.

- **External worker resource telemetry (GH#276/GH#277).** Shared worker code
  (`citypods/compute/worker_telemetry.py`, `external_worker.py`) records per-claim RSS / GPU-VRAM
  peaks with backend, model, compute type, device, GPU type, and outcome, persisted to a single R2-CAS
  object (`state/asr_worker_telemetry.json`) and surfaced in `asr-worker-report`. Applies to both the
  Modal and Beam workers (it lives in the shared `run_worker` path). Telemetry failures never fail
  transcript work.

- **Modal external transcription worker pins dependencies + model to the runner (GH#276, part of #804).**
  `scripts/compute/modal_app.py` replaced its hand-maintained `>=` dependency list with
  `pip install '.[storage,asr-transcribe]' -c constraints/asr.txt` — the exact same versions
  (`faster-whisper`/`ctranslate2`/`av`) the in-Actions transcribe lane uses, resolved from the
  pyproject extras (no duplicate list; enforced by `scripts/check_dependency_policy.py`), and without
  torch (the `asr-transcribe` extra excludes `stable-ts`). The image base moved to a digest-pinned
  **CUDA 12 + cuDNN 9 runtime** (provides ctranslate2's cuBLAS/cuDNN on GPU and is forward-compatible
  with a future torch-based diarize step), and the **pinned Whisper model revision is baked into the
  image** (fast cold start, same bytes as the runner via `ASR_MODEL_PATH`). The canonical model
  repo+revision constants moved to `citypods.asr` so the runner (`prepare_whisper.py`) and the worker
  share one Renovate-tracked source. First deployment — to be validated on bounded single-recording
  test runs. Beam (GH#277) follows the same pattern.

- **Hugging Face Whisper models are pinned to explicit commit revisions (GH#498).**
  `scripts/prepare_whisper.py` downloaded via mutable `main` on both the direct-CDN and
  `snapshot_download` paths, so model bytes could drift silently while `asr_spec_hash()` still
  treated the recipe as unchanged. Both paths now pin `HF_PREFERRED_REVISION` /
  `HF_FALLBACK_REVISION` commit SHAs, logs show `repo@revision`, and the B2 mirror prefix is
  revision-scoped so a future bump lands under a fresh prefix instead of overwriting the old bytes.
  Pinning the current revision is a reproducibility no-op — **no `ASR_PIPELINE_VERSION` bump and no
  transcript reprocessing**; a later intentional revision bump decides invalidation separately
  (review/22). Renovate surfaces upstream revision changes for Dashboard approval.
- **Repository dependency pinning & update policy (GH#498, GH#734).** New normative contract in
  [`review/22`](review/22-dependency-and-reproducibility-policy.md): pins are the default for
  reproducible builds, and Renovate opens PRs so they do not stall past security/beneficial updates.
  Foundation landed: compiled hash-pinned Python `constraints/*.txt` (source of truth for CI, the
  runner image, and the external workers) with a `lock.yml` compile workflow and a `ci.yml` drift
  gate; all third-party GitHub Actions pinned to full commit SHAs and unified to the current tips
  (GH#734); `.github/renovate.json5` with a light-touch two-lane flow (hygiene auto-PRs; a
  Dependency-Dashboard approval gate + per-source `dep-bump-smoke` for output-affecting bumps);
  `scripts/check_dependency_policy.py` CI guard; and the "adding a dependency" contract in
  CONTRIBUTING/AGENTS. Pure pinning is a reproducibility no-op — no pipeline-version bump, no artifact
  reprocessing. HF model-revision pinning (GH#498) and external-worker (Modal/Beam) parity follow as
  their own PRs.
- **Production media fetches now have a size ceiling before ffmpeg reads a remote source
  (issue #497).** `citypods/media.py:_download_audio()` and the direct-remote render paths
  (identity render, multi-source concat fallback) previously handed ffmpeg a remote URL with no
  byte cap at all — `MAX_RESPONSE_BYTES` only ever covered feed/JSON/HTML fetches through
  `requests`, not media bytes ffmpeg reads directly via libavformat. A new
  `citypods.http.preflight_media_size()` issues a `HEAD` (falling back to a ranged `GET` for CDNs
  that reject/ignore it) before any ffmpeg process starts; a source that honestly discloses a size
  over the new `source_media_max_bytes` config ceiling raises `MediaSourceTooLargeError` and is
  never retried by falling back to an unguarded direct stream — it lands in the normal
  materialize-failure/backoff path instead. Unverifiable ("unknown") sizes are logged and allowed
  through, since ffmpeg's own fetch can't be bounded after the fact. `audio_encode_timeout_minutes`
  (the existing per-encode wall-clock cap) is recalibrated from 45 to 360 minutes and
  `source_media_max_bytes` defaults to 54 GB, both derived from a conservative 12-hour
  longest-meeting ceiling (see the comments in `config/site_config.yml` for the full derivation).
  `scripts/probe_granicus_worker.py`'s production-encode check now inherits this same guard instead
  of its own probe-only `--full-download-max-mib` cap. A new `citypods.audit.check_media_too_large`
  feed-health check files one always-visible finding per rejection (never folded into aggregate
  backoff noise), since this should be rare and each occurrence needs a human to verify the meeting
  and decide whether the cap needs raising.
- **Timeline/audio diagnostics probe MP4 headers instead of downloading whole episodes.** When
  `timeline_diagnostics=true`, `check_timeline_integrity` now defaults to a header-only probe that
  range-reads just an episode's `ftyp`/`moov` boxes (`StorageBackend.get_range`, implemented for
  S3-compatible and local storage) instead of downloading the full hosted `.m4a`. Every hosted
  episode is written `-movflags +faststart`, so `format.duration` and the stream's
  `duration_ts`/`time_base` live entirely in `moov` — ffprobe never reads `mdat` for these fields
  either way, so this yields identical values at a fraction of the bytes (verified against a full
  download for both short and multi-hour synthetic fixtures in `tests/test_media.py`). As a
  standing guard against that assumption ever breaking for a real file, any episode the header
  probe flags as non-"ok" is automatically re-measured with a full download
  (`probe_audio_full`); the full read supersedes the header read for the actual finding/repair
  decision, and a new `timeline-audio-probe-divergence` finding fires if the two disagree beyond
  float noise.
- **Feed-health audit returns to the cheap default path while audio queued work gains UID-level
  evidence.** The audit workflow no longer downloads and ffprobes every hosted audio object on every
  scheduled/default run just to emit the timeline canary artifact; full `timeline-audio-integrity`
  diagnostics are now opt-in via `timeline_diagnostics=true` and still forced when
  `timeline_repair=true` needs persisted repair rows. Audio materialization deferrals now log
  `[enrich] audio materialize deferred ... uid=... reason=...` and carry reason counts/samples into
  the run summary, so a lingering `queued` count can be tied back to specific UIDs.
- **Correction: timeline/audio canary repair is now decoded-only and fails closed (GH#702).**
  The canary stamp still forces targeted `timeline-replan` / `audio-rematerialize`, but a healthy
  `status="ok"` now clears or ignores stale repair actions so resolved episodes stop re-keying.
  `SilencePlanner` resolves media through `SourceCache`, runs `detect_silences` on the local cached
  file only, and no longer falls back to container, provider, or stream-sample duration when the
  decoded duration is missing. Cache/decode/degenerate failures defer as typed timeline reasons
  (`deferred_cache_unavailable`, `deferred_decode_unavailable`, `deferred_degenerate_timeline`) with
  timeline-specific materialization backoff; `AudioStage` skips same-run timeline deferrals so stale
  timelines cannot be credited or encoded. This supersedes the earlier fallback-tier language below:
  non-decoded clocks are diagnostic for this planner, not planning authority.
- **Correction: the rendered-duration survivors were still selecting source spans on raw PTS
  (GH#702).** The run 5 → run 6 audit artifacts showed the prior fix only partially converged:
  9/63 original repair-cohort UIDs fixed, 54/63 still `rendered-duration-mismatch`, with nearly every
  survivor showing a changed `timeline_digest` and `audio_key`. That proved the repair lanes were
  firing and the planner had moved from `duration_basis="container"` to `"decoded"`, but the renderer
  was still not using the same clock for selection. Root cause: `_build_streaming_single_source_filter`
  applied `asetpts=N/SR/TB` **after** `aselect`, so the final served output was left-packed but the
  selector still compared compacted EDL boundaries against raw source PTS. A source with a 2s PTS gap
  therefore rendered a 10s EDL as ~8.056s. The streaming filter now rewrites PTS to the contiguous
  decoded-sample clock before boundary framing / `aselect`, and keeps the post-select reset that packs
  retained samples onto served time. A synthetic PTS-gap regression now renders a 10s EDL as 10.0s.
- **Correction: the "decoded audio-stream end" fix below did not converge in production — fixed by
  resetting the decode pass to a sample-index clock before measuring it (GH#702).** A before/after
  production audit of the repair cohort showed 0/56 survivors improved despite genuine re-encodes and
  re-planned EDLs for many of them. Root cause: ffmpeg's `time=` progress field is a
  **presentation-timestamp clock, not a decoded-sample-count clock** — it carries forward any PTS
  discontinuity in the source (a stream splice, an ad-insertion boundary, a dropped HLS segment) as if
  the gap were real elapsed audio, so it overstates by exactly the gap size and lands on the same value
  as the (also PTS-based) container `Duration` header — confirmed bit-identical for the three largest
  survivors, one of which (`media_kind="direct"`) isn't even HLS, ruling out segment loss as the
  mechanism. `detect_silences` now prepends `asetpts=N/SR/TB` ahead of `silencedetect`, so its `time=`
  reading and silence boundaries are measured on a contiguous sample-index clock. A pure per-frame
  timestamp rewrite at the native rate — no resampling, no second decode pass, a no-op on a source with
  no discontinuity. Reproduced directly: a constructed 10s file with a deliberate 2s forward PTS jump
  read `time=12.0x` unfixed (matching its container header) and `time=10.06s` fixed. The follow-up above
  makes the renderer's selector use that same pre-select clock.
- **`SilencePlanner` now anchors the single-file EDL on the *decoded* audio-stream end when no
  stream-sample clock is exposed, closing the GH#702 `rendered-duration-mismatch` survivor gap.** PR
  #704 made the planner prefer ffprobe's stream-sample duration over the container header, but for the
  exact sources that overstate their audio — HLS manifests and fragmented MP4 — ffprobe exposes no
  stream-level `duration_ts`/`time_base`/`duration`, so `_probe_stream_sample_duration` returns `None`
  and the planner fell straight back to the container header. Those episodes therefore re-planned (even
  under a forced `timeline-replan` flag or a version bump) onto the *same* over-claiming EDL — identical
  `timeline_digest`, identical short rendered file — so the repair cohort never converged. The
  `silencedetect` pass already performs a full `-vn` decode, so its final `time=` progress timestamp is
  the real audio-stream end; `detect_silences` now returns that decoded duration alongside the container
  header, and the planner uses it as a `duration_basis="decoded"` tier between `stream-sample` and
  `container`. Re-planned survivors now produce a corrected (shorter) EDL the renderer matches, and the
  audit artifact shows `source_duration_bases=["decoded"]` instead of `["container"]`. No second media
  pass; the container header remains the honest fallback when even the decode end is unparseable.

### Fixed

- **Withheld/dead episodes no longer file noisy `rendered-duration-mismatch` tickets, and get a
  flat ~30-day recheck lifecycle (GH#795).** Once an episode's media is withheld — silent/quarantined
  (`confirmed_empty`) or unreachable (`missing`/`invalid`) — the stale hosted object no longer
  represents anything served, so the timeline-audit now classifies it as terminal `media-withheld`:
  no finding, no repair, no integrity stamp, and it skips both the full-download reconciliation and
  the cheap stored-field duration checks (`check_enclosures` also skips withheld). Separately, a
  **confirmed-dead** episode now polls on a **flat 30-day cadence** (`confirmed_dead_recheck_due`,
  anchored on the availability verdict's `last_check`) instead of the exponential #120 backoff — a
  recheck that stays dead just sleeps another full interval with no new cooldown escalation or ticket;
  `suspected_empty` keeps the exponential ramp so it can reach its second silent confirmation quickly.
  A **repair flag bypasses the exponential #120 backoff** in `TimelineStage` so a flagged
  transient/broken-EDL episode re-plans immediately (those flags clear via the post-repair audit,
  which owns the integrity block). For **confirmed-dead** media the flat gate deliberately takes
  precedence over the flag: the integrity/repair block is audit-owned and the audio lane preserves
  it from remote on push, so a lane-side clear cannot persist — letting the flag bypass the flat gate
  would re-download a quarantined episode every run. Anchoring the flat cadence on the
  audio-lane-owned `media_availability.last_check` keeps it self-managing (recheck ≤ every 30 days)
  without needing to clear the flag.
- **Scoped state pushes no longer regress repaired timeline plans back to stale container-basis
  records.** `push_records_merged` already re-read remote state to preserve sibling artifact blocks,
  but timeline/source planning metadata lived in the unprotected whole-record body. A long-running
  audio or ASR shard that started before a repair could therefore finish later and overwrite a remote
  `duration_basis="decoded"` plan with its older local `container` or missing-source plan, while still
  preserving enough artifact data to make the feed look partially repaired. The merge now ranks
  planning metadata by timeline version and source duration basis, preserves the fresher remote
  planning fields when they are strictly better, and keeps the matching remote artifact blocks so stale
  local artifacts computed against the old EDL cannot be attached to the newer plan.
- **A B2 connectivity blip on one `state/` key no longer fails the whole Build & Deploy run.**
  `pull_state()` restores every object under `state/` from the durable bucket at build start;
  `S3CompatibleStorage.get_file()` already retried transient connection errors in-process, but once
  those retries were exhausted it re-raised, so a single key that kept timing out (Build & Deploy
  runs #452-455 each failed on `boto3.exceptions.RetriesExceededError` inside `download_file`)
  crashed the render-only deploy outright — even though render "must always finish so the deploy
  isn't gated" and the bucket is meant to be a self-healing cache. `pull_state()` now catches the same
  connectivity-level exceptions (`storage.s3.transient_download_errors()`, hoisted out of
  `get_file()` so both call sites share one definition), logs a warning, and keeps whatever local copy
  already exists for that key instead of aborting — the bucket resyncs it on a later run that can
  reach it. A real (non-transient) error, e.g. a 403 from rotated/invalid credentials, still
  propagates and fails the build loudly.

### Changed

- **Stage-2 work-lease reaper enabled now that H14b/H14c are live (GH#706 §4).**
  `config/site_config.yml`'s `work_lease_reaper_enabled` flips from `false` to `true`: the per-item R2
  lease ledger the Modal/Beam pull workers claim against was dormant (and its sweep skipped as
  pointless backlog-scaled GETs) until those workers existed. With H14b (#807) and H14c (#808) merged,
  `compute reconcile` now sweeps it — a crashed worker's claim is reclaimed/requeued instead of the
  ledger going unswept.

- **`audit_feeds.py` consolidates feed-health GitHub issues from one-per-feed to one-per-check.**
  Filing a separate issue for every `(feed, check)` pair meant a single systemic regression (e.g. a
  code bug affecting every feed's timeline check) could open dozens of near-duplicate issues in one
  run. Every check now files a single issue covering all affected feeds — the title shows a live
  `N feed(s) [across M cities]` count, and the body lists every affected feed with how long it's
  been failing (tracked in a hidden JSON state block in the issue body, not an external file) plus a
  representative example. `meetings-url-dead`/`meetings-url-changed` stay one-issue-per-city, since
  each is a genuinely distinct problem needing a specific human to fix a specific city's YAML.
  Issue matching now uses a hidden `<!-- citypods:feed-health:key=... -->` marker in the body
  instead of the title, so the title can change every run without breaking run-to-run
  create/update/close reconciliation. A visible comment posts only when the affected-feed *set*
  changes (a feed newly failing or clearing), not on every cosmetic body refresh (e.g. a "since Nd
  ago" day count ticking over) — the second-order goal being that fixing the "many issues" chattiness
  doesn't just relocate it into "many comments on one issue." Every check's body also gained
  substantially more verbose causes/resolution guidance specific to that check.
- **`work_leases.py` gains a public `scan_offset`/`ordered_candidates` ordering primitive, extracted
  from `run_claim_loop` (review/18 §4).** The H14b Modal pull-worker prototype (unmerged) needed
  budget-gating/lease-renewal/retry `run_claim_loop` doesn't have, so it composed its own loop directly
  on the `claim`/`release`/`renew` primitives — but reimplemented the scan-offset rotation by reaching
  into `work_leases._scan_offset` rather than sharing it. `scan_offset`/`ordered_candidates` are now
  public and generic over candidate shape, so any worker that builds its own loop (instead of calling
  `run_claim_loop`) shares the rotation logic instead of re-deriving it. Docstrings on `run_claim_loop`,
  the module header, and review/18 §4.3/§6 now spell out precisely what `run_claim_loop` is missing
  (external-budget gating, lease renewal, retry) and flag the in-Actions push→pull migration
  (review/18 §6 step 4) as the moment to fold those in as shared hooks instead of writing a third loop.
- **New `long_first: N` backlog-priority comparator prioritizes the catalog's long-meeting transcript
  backlog ahead of everything else (review/12 §H5).** Once an episode exceeds
  `asr_local_max_duration_hours`, the in-process ASR backend refuses it outright
  (`stages._asr_local_duration_eligible`) — the capped external GPU free tier (H13/H14) is its only
  path to ever being transcribed. With no duration awareness, a steady stream of short episodes (which
  have a working local fallback) could keep consuming that scarce dispatch/claim budget in arrival
  order while long ones starved behind them indefinitely. `long_first` is a binary bucket (same shape
  as `recent_first`) scoped to transcript-producing work classes only — `audio` items are never
  reordered. Bare `long_first` (no params) resolves to the site's configured
  `asr_local_max_duration_hours` so the two boundaries can't silently drift apart; an explicit value
  overrides it. Fixed a latent gap found while adding this: `_materialize_set` unconditionally labelled
  every stage's ordering `WorkItem` `work_class="audio"`, which would have silently neutralized any
  work-class-scoped comparator for local ordering and the live H14a push-dispatch order (only the
  separately built `work.json` manifest the H14b/H14c pull workers also read would have seen it);
  `TranscriptStage` / `ProviderTranscriptDiarizeStage` now pass their real work class. Accepted
  tradeoff: a hard catalog-wide drain — one pathological multi-hour backlog can deprioritize all
  short-meeting transcript throughput until it clears; no reserved-capacity split is implemented.
- **`SilencePlanner.version` bumped 2→3 to re-plan every single-file silence EDL on the stream-sample
  clock (GH#702, PR6).** This re-trims the whole single-file silence catalog onto the corrected source
  clock, eliminating the last container-basis EDLs. Because `Timeline.version` is part of
  `timeline_digest`/`audio_spec_hash`, this re-encodes **every** single-file silence episode (and
  regenerates transcripts), not only the gap-affected cohort — a deliberate but large cost. All four
  GH#702 PR6 merge thresholds were confirmed before enabling: PR2–PR5 have run in production; a
  post-GH#795 full-catalog audit shows zero `rendered-duration-mismatch` survivors (stronger evidence
  than the cohort-scoped comparison the gate originally called for); the re-encode cost is accepted;
  and both stragglers (Dallas, Pflugerville) resolved on their own — Dallas now only shows ordinary
  `container-duration-drift`, and Pflugerville's `missing-audio-key` row resolved into the GH#795
  withheld-media lifecycle (`media-withheld`, `confirmed_empty`). See review/20.
- **The rendered-vs-EDL duration audit uses a 0.5s classification floor, separate from the 0.1s
  structural tolerance (GH#702, PR5).** A clean re-encode legitimately differs from the EDL sum by AAC
  priming/padding plus per-cut sample rounding (~0.1–0.4s) with no cue-integrity problem; classifying
  that band as `rendered-duration-mismatch` produced a long tail of sub-finding artifact noise. A new
  `_RENDERED_DURATION_TOLERANCE` (0.5s) cleanly separates padding noise from genuine drift (cohort
  divergences are ≥1s) while leaving the 1.0s finding/repair thresholds and structural checks untouched.
  review/20 gains an operator remediation runbook for the already-broken cohort and the Dallas /
  missing-audio-key stragglers.
- **Single-source many-cut timelines always render via the bounded-memory streaming filter, with an
  OOM guard on the generic fan-out (GH#702, PR4).** `_build_streaming_single_source_filter` is now
  attempted regardless of `audio_processing_profile` (loudnorm is appended to its output on the legacy
  path), so the OOM-prone single-source `atrim`-fan-out in `build_filter_complex` can no longer be
  reached through the empty-profile branch. If such a shape ever does reach the generic graph the render
  raises `StreamingFilterBypassedError` rather than risking the RSS-growth OOM that motivated the
  streaming graph. `build_filter_complex` is retained for its legitimate uses — multi-source concat
  assembly/fallback and intro/outro inserts.
- **`audio_duration_served` is now the probed hosted-stream duration, never the EDL sum (GH#702,
  PR3).** The post-encode/ASR/reuse paths no longer overwrite the measured duration of the actual
  hosted object with the EDL total (`_backfill_served_duration` / `_refresh_served_duration_from_audio`
  are fill-when-missing / probe-first), so a render that disagrees with its EDL stays visible to the
  audit instead of being masked. The RSS `<itunes:duration>` for audio feeds now advertises this
  served duration (a trimmed episode's real played length) instead of the longer source duration. The
  cheap stored-field `timeline-duration-mismatch` / `timeline-short-coverage` checks defer to the
  precise live `rendered-duration-mismatch` probe when one is supplied, so a broken slug is filed once.
- **`SilencePlanner` now plans single-file silence EDLs against the source's audio stream-sample
  clock, not the container `Duration` header (GH#702, PR2).** When a source's container overstates its
  audio (HLS manifests, or a direct MP4 whose video stream outlasts its audio), anchoring the
  trailing-silence test and the final keep-span on the header made the renderer hit EOF early, so the
  rendered file came out shorter than the planned EDL — the single-file `rendered-duration-mismatch`
  class. The planner now ffprobes `duration_ts * time_base` (mirroring `SwagitConcatPlanner`, which
  already used this basis) and records `duration_basis="stream-sample"`, falling back to the container
  header then the provider duration. Re-planned episodes get a corrected EDL that the renderer matches.
- **The EDL (cue) clock now derives from a single `timeline.edl_duration` primitive (GH#702, PR1).**
  `media._served_duration`, `stages._edited_timeline_served_duration`, and `audit._timeline_duration`
  previously each re-summed served segment spans with subtly different fallbacks; they now delegate to
  one canonical accessor so the three duration facts review/20 must keep distinct — source,
  served/hosted, and EDL/cue — cannot drift apart through divergent local math. No behavior change;
  foundation for making the probed hosted-stream duration authoritative for `audio_duration_served` and
  for the single-file silence stream-sample planner fix.
- **Timeline/audio integrity diagnostics and targeted repair plumbing are implemented, with PR6 still
  gated.** The feed-health workflow now uploads an `audit-timeline-integrity.jsonl` artifact that
  distinguishes container-only duration drift from real stream-sample/EDL mismatches. Episode records
  can carry an audit-owned `integrity.timeline_audio` repair block, `/admin/status` reports the repair
  queue, `SourceMedia` records duration basis, and source-aware identity detection now reaches both
  hashing and render-path selection so tail-only trims cannot collapse to identity/copy handling
  (GH#495). `TimelineStage`, `AudioStage`, ASR, and provider-align consume targeted repair actions (`timeline-replan`, `audio-rematerialize`,
  `transcript-regenerate`) without bumping global pipeline versions. Automatic persistence/repair is
  still off in the scheduled audit; `--persist-timeline-integrity` is a manual gate. The feed-health
  audit now queues `timeline-replan` for confirmed stream-vs-EDL mismatches up front, so the next
  repair pass rebuilds the EDL from planner inputs before rematerializing audio/transcripts instead
  of faithfully reproducing the same bad timeline.
  The feed-health
  workflow can now be manually dispatched from `main` to stamp a named repair cohort above a chosen
  stream-delta threshold, while keeping scheduled runs read-only and suppressing sub-threshold
  feed-health noise. A new `scripts/compare_timeline_diagnostics.py` helper compares the cohort's
  before/after artifacts so the operator can verify fixed, still-mismatched, missing-after, worsened,
  audio-key-changed, and timeline-digest-changed counts before widening repair scope. The audit
  artifact now also records planner-facing source telemetry for each row: whether the episode was
  single-file or multi-part, the per-source measured durations and duration bases, the timeline
  version/digest, and the total source-span lengths the EDL mapped. Silence-planned single-file
  episodes now persist that source duration/basis too, so a stubborn mismatch can be traced back to
  whether the planner used a measured container duration or a provider fallback. Backfill story:
  no global invalidation, no `ASR_PIPELINE_VERSION` or `AUDIO_PIPELINE_VERSION` bump, and only records
  explicitly flagged for repair get new audio/transcript recipes. The diagnostics artifact now also
  records `probe_error` reasons such as missing audio keys, storage/download failures, ffprobe
  failures, or absent duration metadata so PR6 can gate on actionable evidence instead of a single
  opaque `duration-probe-inconclusive` bucket. Scheduled feed-health no longer files per-slug issues
  for inconclusive diagnostics, and the workflow now installs `ffmpeg`/`ffprobe` before probing.
- **Timeline/audio integrity repair is now an L3 Phase-H series with a cheap sample-clock duration
  probe.** `review/20` breaks the work into read-only diagnostics, persisted repair flags, planner
  duration-basis fixes, and targeted re-plan/re-materialize/re-transcribe consumers. PR1 adds
  `AudioDurationProbe`, which reads both `format.duration` and the first audio stream's
  `duration_ts * time_base` without decoding the whole file. This does not change audit behavior,
  records, pipeline versions, or artifact invalidation yet.
- **ASR audio-duration refresh preserves edited timeline durations.** The transcript stage no longer
  overwrites `audio_duration_served` on non-identity timelines with ffprobe's container duration, which
  kept resolved `timeline-duration-mismatch` / `timeline-short-coverage` feed-health issues open after
  the audit started reading durable state. Identity/no-timeline audio still uses hosted-file probes for
  ASR budgeting. This is a metadata correction only: no pipeline-version bump, no automatic artifact
  invalidation, and affected records update gradually as audio/ASR touches them again.
- **Multi-source (`SwagitConcatPlanner`) concat episodes now use local-concat source caching
  ([`review/11`](review/11-technical-design-roadmap.md) "Per-segment source caching for
  multi-source concat episodes").** `SourceCache.get_or_fetch_concat` downloads each segment
  individually (own bounded timeout, releases the rate-limit slot between segments) and
  concatenates them once into a cached local file, rendered as a single source instead of
  streaming N remote URLs into one `filter_complex` invocation on every encode attempt.
  `ep.timeline`/`ep.sources` on the persisted record are unchanged — clips/soundbites still
  resolve through the real per-segment EDL; only the render-time encoder input changes.
- **Admin status now exposes provider-transcript rollout health
  ([GH#453](https://github.com/BashfulBits/city-meeting-podcasts/issues/453), PT-PR7).**
  `/admin/status` now includes a provider-transcript rollout block with source-document fetch/storage
  counts, `known_good`/candidate/history and rejected-rollback counts, provider-align and
  provider-diarize work-state slices, coarse confidence distributions, diarize error reasons, and
  operator recovery guidance. This is a reporting/UI-only change: it does **not** change transcript,
  provider-align, provider-diarize, or ASR pipeline versions and triggers no artifact backfill.
- **Selected provider-aligned transcripts now produce independent speaker-turn artifacts
  ([GH#453](https://github.com/BashfulBits/city-meeting-podcasts/issues/453), PT-PR6).**
  The work manifest emits `provider-transcript-diarize` after a provider-aligned transcript is active.
  `ProviderTranscriptDiarizeStage` conservatively extracts `SPEAKER: text` cues from the served-time
  provider-align VTT into a content-addressed `speakers.json` block and records diarization status on
  the provider registry. If speaker extraction fails or finds no labels, the successful transcript text
  remains active and the episode records only a speakers error/status for retry/operator inspection.
  The new `speakers` block is owned by the `diarize` lane and protected from audio/transcript pushes.
  This adds `PROVIDER_DIARIZE_PIPELINE_VERSION` but does **not** bump `ASR_PIPELINE_VERSION`; no ASR
  artifacts are invalidated or regenerated.
- **Timed provider transcript documents now have a provider-align queue and confidence gate
  ([GH#453](https://github.com/BashfulBits/city-meeting-podcasts/issues/453), PT-PR5).**
  Hosted episodes with a synced provider VTT/SRT registry entry now surface as
  `provider-transcript-align` work. `TranscriptStage` parses the provider document in source time,
  remaps cues through the canonical timeline, publishes a served-time `provider-align` VTT when no
  active transcript already owns the episode, and records a `float | null` confidence on the provider
  registry. Changed candidates promote to `known_good` only when their confidence is at least the
  prior known-good artifact; worse candidates move to bounded history and the known-good remains active.
  The provider registry is now a transcript-lane-owned record block so audio-lane pushes preserve
  concurrent confidence/promotion updates. This adds `PROVIDER_ALIGN_PIPELINE_VERSION` but does **not**
  bump `ASR_PIPELINE_VERSION`; no ASR artifacts are invalidated or regenerated.
- **ASR transcript keys now use a timeline/recipe transcript media hash instead of the audio-byte
  recipe hash ([GH#453](https://github.com/BashfulBits/city-meeting-podcasts/issues/453), PT-PR4).**
  ASR VTT and word JSON keys are now based on source media identity plus the served timeline and ASR
  recipe, so codec, loudness, chapter, or audio-processing recipe changes no longer mark completed ASR
  transcripts stale. Current-version ASR records with old audio-spec-derived keys are migrated by copying
  the existing VTT and word sidecar to the new key shape when the old objects are present; missing/corrupt
  artifacts are reported and only those episodes fall through to regeneration. The run summary/history now
  reports ASR migration counts (`copied`, `already_present`, `missing`, `regenerated`). This does **not**
  bump `ASR_PIPELINE_VERSION`; the expected ASR regeneration count is zero except for genuinely missing
  old artifacts.
- **Provider transcript source documents now surface separately from the active podcast transcript
  ([GH#453](https://github.com/BashfulBits/city-meeting-podcasts/issues/453), PT-PR3).**
  A synced `provider_transcript.known_good` document can fill `<podcast:transcript>` only while no
  ASR/provider-aligned active transcript exists; once `transcript_hosted_url` is synced, that served-time
  artifact owns the Podcasting 2.0 tag. The known-good provider document remains exposed in feed notes and
  city pages as **Original city-provided transcript**. This is a render-only exposure change: transcript
  pipeline versions, artifact keys, and stored bytes are unchanged, so **no ASR backfill or regeneration**
  is triggered.
- **Provider transcript source documents are now fetched into the H15 provider registry
  ([GH#453](https://github.com/BashfulBits/city-meeting-podcasts/issues/453), PT-PR2).**
  `TranscriptStage` keeps the current provider transcript URL in `links["transcript"]` and stores
  each non-empty provider document under a content-addressed `provider-` transcript key in
  `provider_transcript.candidate`. Re-fetching identical bytes refreshes `checked_at`; changed bytes
  become the new candidate and the superseded candidate is retained in bounded history for
  later rollback. Candidates are **not** promoted to `known_good` and do not replace the active
  podcast transcript until the follow-up provider-alignment/scoring path proves them at least as good.
  No ASR or transcript pipeline version changes, so **no ASR backfill or regeneration** is triggered.
- **The distributed provider concurrency-slot pool moved to per-slot R2 compare-and-swap (H17 PR6,
  the final H17 PR; [GH#390](https://github.com/BashfulBits/city-meeting-podcasts/issues/390)).**
  `DistributedProviderLeasePool` (the cross-process Granicus/ffmpeg throttle that caps aggregate
  overlap across the four audio shards) no longer emulates an N-slot FIFO semaphore by writing a
  per-waiter candidate object and **listing + sorting** the prefix every poll. It now models a
  domain's N slots as N fixed CAS objects `provider-leases/<domain>/slot-<i>.json` (`i` in `0..N-1`),
  each with an independent ETag: a worker reads a slot (cheap Class-B) and claims a free one with
  `put_cas(if_none_match="*")` or an expired one (dead owner) with `put_cas(if_match=<etag>)`, walking
  the slots from a per-owner offset. Because the old per-poll *list* was itself an R2 Class-A op, a
  blocked waiter used to burn Class-A continuously; the CAS model **never lists** and spends Class-A
  only on a claim, renewal, or release (waiting is read-only). `provider-leases/` is added to
  `COORDINATION_PREFIXES` so the slots route to R2 and are excluded from the bulk B2 state sync.
  Behavioral changes: waiters no longer acquire in strict FIFO arrival order (the contract is the
  concurrency *cap*, not fairness), and the soft cap can briefly admit N+1 holders on a
  reap-vs-release race — both acceptable for a rate limiter. The pool now requires a **CAS-capable**
  backend; on a non-CAS backend (b2-only / local dev) the distributed layer disables and only the
  in-process `HostRateLimiter` applies (production runs on `audio_storage_backend: routing` → R2). The
  live validation harness (PR5) gains a provider-slot check (acquire two of two slots, third caller
  blocked, release frees) under a `provider-leases/__validate__-…` scratch namespace. Slot payloads,
  TTL/renew cadence, telemetry, and `stop`-budget abort are unchanged; no audio bytes, pipeline
  versions, or artifacts change — **no backfill**.
- **`compute reconcile`'s Stage-2 work-lease sweep is now gated behind `work_lease_reaper_enabled`
  (default `false`).** Flipping `audio_storage_backend: routing` activated the reaper on the CAS
  path, but the per-item lease ledger external pull workers claim against is **dormant** until those
  workers (H14b/H14c) exist — so the sweep would GET one R2 lease key per pending `transcript-asr`
  item only to find every one absent (cheap Class-B, but pointless and backlog-scaled). The sweep is
  lossless to skip while dormant (nothing to settle/requeue), so it stays off until a deployment sets
  the flag once external workers are live. `reconcile_compute(..., sweep_work_leases=False)` and the
  matching `compute reconcile --dry-run` preview are both gated.
- **The Granicus rate-limit circuit breaker (plus its queue parking and half-open canary recovery)
  was removed ([GH#353](https://github.com/BashfulBits/city-meeting-podcasts/issues/353)).** It was
  built for a hypothesis H16 disproved — the Actions-runner 403s were shared GitHub-egress IP
  reputation (handled by the authenticated Cloudflare Worker), not request-shape or concurrency
  throttling — and it never tripped across Audio runs #51–#56 (zero trips/deferrals/recovery probes).
  `citypods/provider_circuits.py` is replaced by a lean `citypods/provider_transport.py`
  (`ProviderTransportTelemetry`) that keeps only the per-tenant direct/Worker-fallback/truncation
  counters feeding the H16 `transport` criterion; the storage-backed open/trip/defer state, the
  `_run_enrich_global_queue` parking/canary loop (and its latent double-retry race), the
  `CircuitOpenMediaFetchError` admission gate, and the `circuit_skipped`/`circuit_keys` stage plumbing
  are gone. Telemetry domains are configured via `provider_transport_telemetry_domains` (replacing
  `provider_rate_limit_circuit_breakers`); the H16 report schema bumps to v2 (per-tenant rows drop the
  circuit columns). Aggregate provider load stays bound by the distributed provider-lease ceiling and
  the per-episode materialize backoff, and rollback to direct-only fetch remains config-only (unset
  `GRANICUS_PROXY_BASE_URL` / `GRANICUS_PROXY_TOKEN`). Audio bytes, pipeline versions, and stored
  artifacts are unchanged, so **no backfill**.
- **Duplicate combined/per-board audio views now share one encode and one CAS object
  ([GH#421](https://github.com/BashfulBits/city-meeting-podcasts/issues/421)).** Some per-board
  feeds use a wider `feed_urls` set than their city's combined feed, so stripping only the `body`
  filter produces distinct `source_key`s for the same stable meeting. Audio shard planning now
  keeps all source keys for one configured city entity on the same shard, and a thread-safe
  run-local `(provider, stable uid, audio recipe)` cache lets the first successful alias supply its
  artifact pointer to every follower. New duplicate work chooses one deterministic source prefix;
  existing valid artifacts can be adopted as the shared winner, and superseded duplicate objects
  become ordinary orphan-GC candidates once no record references them. Source keys, episode UIDs,
  audio recipes, and pipeline versions are unchanged, so this causes **no catalog backfill or
  re-encode storm**.
- **The per-episode ASR timeout now carries a configurable safety margin
  (`asr_timeout_safety_margin`, default `1.2`).** ASR run #32 timed out and discarded a 3.4h
  recording that was actively transcribing, not hung — a sibling episode from the same run
  finished at ratio=0.503 against a budget computed assuming ratio 0.5, leaving only ~3% of
  margin. The base+per-audio-hour budget is now multiplied by this margin (values <1.0 are
  ignored) before being clamped to the existing hard backstop deadline, so routine variance no
  longer kills genuinely-progressing inference. The hard backstop and timeout-backoff behavior are
  unchanged.
- **Audio shard assignment is duration-weighted and availability-aware.** Source-atomic Audio
  planning now sums the expected served duration of pending encodes whose media is available,
  recovered, or not yet classified, using the current Timeline first, then the last served duration,
  then provider duration; unknown durations use their source's known-duration average. Media already
  classified as withheld contributes only a small recovery-recheck cost because TimelineStage still
  probes it but AudioStage will not encode it. This replaces flat pending-episode counts, preventing
  short/empty-media backlogs from monopolizing a shard while sibling shards carry hours of playable
  audio. This changes scheduling only: audio recipes, pipeline versions, stored artifacts, and
  backfill behavior are unchanged.

### Added

- **Provider transcript retention schema added for the H15 rollout.** Episode records can now carry a
  separate `provider_transcript` registry (`known_good`, `candidate`, and `history`) for city-supplied
  transcript documents while the existing `transcript` block remains the active podcast transcript. The
  schema stores URL/B2-key/content-hash/format/basis/confidence metadata for later provider-transcript
  fetch, alignment, diarization, and rollback work; referenced provider transcript objects are included
  in the GC live set. This is schema-only and does not change transcript recipes or invalidate existing
  ASR artifacts, so **no ASR backfill or regeneration** is triggered.
- **A scheduled Audio orphan-GC workflow reports reclaimable storage and only deletes on demand
  ([GH#421](https://github.com/BashfulBits/city-meeting-podcasts/issues/421) follow-up).** Until now
  `scripts/gc_audio.py` was operator-run only, so superseded content-addressed objects (regenerated
  artifacts, retired recipes, and the now-coalesced duplicate source views) accumulated until
  someone swept by hand. A new `audio-gc.yml` workflow runs weekly as a **dry-run**: it restores the
  bucket state, finds objects no record references, and — when any exist — opens/updates one rolling
  *operations* issue with a per-city summary table (file count + total size per city, plus a grand
  total) and attaches the full object list (`orphans.tsv`) as a run artifact. It never deletes on a
  schedule; reclaiming is a manual **Run workflow** with `apply = true`. `gc_audio.py` gains
  `--pull-state` (restore the durable state so the live set is current before sweeping), `--out`
  (write the tsv/json/markdown report), and per-city attribution via `source_key → city` entity;
  storage backends gain `iter_objects` (a size-bearing listing, free from S3/B2/R2 pagination and the
  local stat). The GC live set, `--min-age-days` floor, and `state/` exclusion are unchanged.
- **Weekly empty-recording review digest emits bounded audio evidence
  ([GH#353](https://github.com/BashfulBits/city-meeting-podcasts/issues/353), H16 PR3b).** A new
  `availability-digest.yml` workflow (`scripts/availability_digest.py`) scans the persisted
  media-availability verdicts for meetings classified suspected/confirmed empty, deterministically
  samples a small set of *new or changed* candidates (keyed by uid + source fingerprint + detector
  version, so a re-classification re-surfaces), and for each renders an evidence record (durations,
  sizes, hashes, silence intervals, profile/detector version, canonical watch-page URL, and a
  **redacted** source identity) plus two low-bitrate mono proxies — the untrimmed source audio and
  the silence-trimmed candidate. It zips the bundle as a workflow artifact and opens/updates a
  single rolling digest issue **only when** new/changed candidates exist; an already-reviewed
  candidate is recorded in a `state/availability_digest.json` ledger so it is not re-digested. The
  issue body and evidence never carry a signed/credential-bearing URL.
- **Durable media-availability classification withholds empty/missing recordings from feeds
  ([GH#353](https://github.com/BashfulBits/city-meeting-podcasts/issues/353), H16 PR3a).** A
  meeting whose source media is missing or (near-)totally silent now carries an explicit, versioned
  `media_availability` verdict on its record (`available` / `suspected_empty` / `confirmed_empty` /
  `missing` / `invalid` / `recovered`, plus operator overrides) instead of being re-attempted every
  run with no durable outcome. The verdict rides the audio lane's existing silence-detection decode
  (no extra ffmpeg pass): a successful decode that is near-totally silent is *suspected* and, after
  a second independent successful silent fetch, *confirmed*; a transport failure (403/429/timeout/
  truncation) can never confirm silence or flip a known-good episode. Withheld verdicts are kept out
  of both audio and video feeds and out of `AudioStage`, so a bad/empty enclosure is never published
  and a confirmed-unavailable meeting keeps its prior known-good artifact — while metadata stages
  (chapters/links) keep running so agenda/minutes still reach the meeting page. Classification is
  re-evaluable via a dedicated detector version, a query-stripped source fingerprint, the detection
  profile, and operator overrides, and recovers automatically when the city later supplies playable
  media — none of which bumps the audio pipeline version or backfills the catalog. Per-run
  availability counts flow through each shard run event into the H16 acceptance report as
  informational observability (not a transport pass/fail criterion).
- **H16 Audio acceptance now proves Granicus record and artifact identity and generically redacts
  subprocess diagnostics ([GH#353](https://github.com/BashfulBits/city-meeting-podcasts/issues/353)).**
  The audio lane snapshots each Granicus meeting after provider/persisted-record merge and verifies
  after media processing that stable UID, provider GUID, official/source URLs, canonical video URL,
  and source duration did not drift. Reused current-spec artifacts must retain key, public URL, and
  served duration; newly materialized or refreshed artifacts must match the deterministic
  content-addressed spec/key/public URL and report a positive served duration. Aggregate checked,
  artifact-checked, mismatch, and bounded category counts flow through each shard run event into
  the existing H16 report. ffmpeg/ffprobe stderr, timeout/error payloads, and exception command
  arguments now strip all media URL queries and redact bearer or credential-shaped values while
  preserving host/path/status diagnostics. This is transport/observability only: no audio bytes,
  pipeline versions, artifact recipes, or backfill behavior change.
- **Audio runs now publish a machine-readable GH#353/H16 acceptance report after all four shards
  finish.** Each shard uploads only its run event plus redacted secret-scan metadata—never the raw
  log—to a post-matrix `validate-h16` job. The merged JSON artifact and GitHub step-summary table
  classify transport recovery per Granicus tenant, including direct successes/403s, Worker
  successes/failures, circuit activity, truncations, lease behavior, and the unchanged 1-local /
  2-distributed ceiling. Identity stability consumes the record/artifact invariant checks described
  above; a run without applicable identity activity is `insufficient_activity`, not a false pass.
  Credential-shaped query strings, bearer values, and Worker endpoint paths are detected locally
  on each shard and represented only by redacted category/file/line metadata. This adds telemetry
  and workflow evidence only: no audio bytes, pipeline versions, artifact identities, or backfill
  behavior change ([GH#353](https://github.com/BashfulBits/city-meeting-podcasts/issues/353)).
- **A live B2+R2 validation harness to verify the R2/CAS control plane before production cutover
  (H17 PR5, [GH#390](https://github.com/BashfulBits/city-meeting-podcasts/issues/390)).**
  `scripts/validate_control_plane.py` + the `Validate R2/CAS control plane` workflow
  (`workflow_dispatch` + weekly schedule) exercise the *real* plumbing end-to-end against live
  services — `RoutingStorage` routing/`cas_capable`, native R2 compare-and-swap
  (`put_cas`/`get_bytes`: create-if-absent, conditional update, stale-ETag rejection), and the
  Stage-2 work-lease ledger (`claim`/contended-skip/`renew`/`release`/`reap`) — and emit a per-check
  JSON report (+ R2 Class-A/B op telemetry). **It never touches production data:** every object is
  written under a unique scratch namespace (`work-leases/__validate__/<run-id>/…`) and deleted on
  exit; the real budget/lease keys are never read or written, and the discovery index never
  references `__validate__`. The workflow header documents the recommended pre-cutover sequence (set
  secrets → run → confirm all checks pass → only then flip `audio_storage_backend: routing`). The
  validation logic is unit-tested offline against an in-memory CAS fake.
- **Stage-2 pull-based work-lease ledger — the frozen contract distributed ASR workers claim against
  (H17 PR4, [GH#390](https://github.com/BashfulBits/city-meeting-podcasts/issues/390); review/18 §4).**
  New `citypods/ops/work_leases.py` adds per-item compare-and-swap lease objects on R2
  (`work-leases/<source_key>/<uid>.json`) so heterogeneous workers (in-Actions shards today; external
  Modal/Beam/Mac-mini workers next) can **competitively claim** transcribe work from a shared ledger
  instead of being handed a static `--shard K/N` slice. Per-item objects have independent ETags, so
  concurrent claims of different uids never contend (the CAS-retry-storm mitigation, review/17 §6).
  The module implements the full claim protocol — `claim`/`renew`/`release`/`reap` plus the
  `run_claim_loop` orchestrator (read discovery index → CAS-claim → injected `transcribe` → durable
  artifact/record commit → settle), with the neural inference left as the injected seam H14b/H14c
  fill. `compute reconcile` now also reaps the ledger (expired claim → requeue; artifact present →
  done), derived from the discovery index. **Cost discipline (review/18 §4.6)** keeps it at ≈1 R2
  Class-A op per *claimed* item: never list the lease prefix (derive keys from the B2 index),
  read-before-claim + per-worker scan offset (no failed-claim writes; workers target different items
  first), infer completion from the artifact (no `done` write), and a generous TTL (renew is the
  exception). `work-leases/` routes to R2 via `COORDINATION_PREFIXES`. **In-Actions matrix shards keep
  using the Stage-1 static plan** (review/18 §6) — this PR freezes the contract and lands the
  substrate so external workers build against it from day one; it changes no scheduled production
  behavior. No pipeline-version bump, no backfill.
- **The free-tier GPU budget ledger moved to R2 with an atomic compare-and-swap decrement (H17 PR3,
  [GH#390](https://github.com/BashfulBits/city-meeting-podcasts/issues/390); review/17 §3/§5).**
  `state/compute_budget.json` is the first coordination artifact to migrate off the bulk B2 state
  sync onto the R2 CAS path: `RoutingStorage` now routes it to R2 (`COORDINATION_PREFIXES`), and
  every `reserve`/`settle`/`release` is a compare-and-swap read-modify-write (`budget.mutate_budget`:
  GET ETag → apply → `put_cas(if_match=…)` → re-read and retry with bounded backoff + jitter on a
  412). Concurrent shards can no longer lose each other's reservations or overspend the monthly
  free-tier cap: the reservation is an **atomic check-and-reserve** (`reserve_if_available`) that
  re-evaluates availability against the freshest ledger on every CAS retry, taken **before** the
  irreversible remote submit (released if the submit fails) — so two shards selecting the same
  backend from a stale snapshot can't both commit. `statesync` excludes CAS-managed keys from
  `pull_state`/`push_state` (so a
  plain `put_file` can't clobber the CAS object), gated on a new `cas_capable` flag set **only for
  R2** (B2 silently ignores conditional headers); a plain-B2 / local / dry-run backend keeps the
  prior local-file ledger behavior byte-for-byte. External dispatch is still dormant (no adapter
  registered), so this is a no-op in production today — it proves the router + CAS helper on the
  lowest-stakes coordination key before the throttle-path migration. No pipeline-version bump, no
  backfill (a pre-existing B2 `compute_budget.json`, if any, is simply superseded by the R2 ledger).
- **Per-episode transcribe sharding so one skewed source spreads across all shards (H17 Stage 1,
  [GH#390](https://github.com/BashfulBits/city-meeting-podcasts/issues/390); review/18 §3).** The
  transcribe lane now plans per `(source, uid)` episode instead of per source: a Granicus source with
  thousands of pending episodes no longer pins to a single shard while its siblings idle. `ShardPlan`
  gains a `unit` field (`source` for the source-atomic audio/align lanes, `episode` for transcribe)
  and `SHARD_PLAN_VERSION` bumps to `2` (reconcile emits a fresh plan every run, so there is no
  durable v1 artifact to migrate — **no backfill**). New `records.pending_transcribe_items` emits the
  per-episode backlog from the same classifier as the aggregate `estimate_transcribe_shard_work`, so
  per-uid weights sum to the source's shard weight. `sources_for_shard` becomes `episodes_for_shard`,
  returning both the owned sources and a per-source owned-uid set. **The load-bearing safety change:**
  `records.merge_preserving_foreign` gains an `owned_uids` axis — a per-episode-sharded shard writes a
  `transcript` block only for the uids it owns and preserves the freshest remote for siblings' uids,
  closing the cross-*uid* lost update two shards splitting one source would otherwise hit (the
  reviewer's race). `owned_uids=None` reproduces the prior source-atomic behavior byte-for-byte, so
  audio/align and the unsharded full enrich are unchanged. Audio stays source-atomic (its bottleneck
  is the per-source provider rate limit, not the runner — review/18 §2.3). No pipeline-version bump.
- **Storage substrate for the R2/CAS control plane (H17, [GH#390](https://github.com/BashfulBits/city-meeting-podcasts/issues/390)).**
  `S3CompatibleStorage` gains compare-and-swap primitives — `put_cas()` (native boto3
  `IfNoneMatch`/`IfMatch`, raising `CASConflict` on a 412) and its `get_bytes()` read companion — the
  storage-level gain R2 has over B2 (review/17 §1.3/§5; confirmed by the §7 spike on boto3 1.43). A new
  `RoutingStorage` backend (`audio_storage_backend: routing`) implements the `StorageBackend` Protocol
  and dispatches by key prefix to a B2 *primary* and an R2 *coordination* backend, degrading to
  B2-only when R2 creds are absent, and tallies R2 Class-A/Class-B op counts for free-tier telemetry
  (review/17 §4). **Routing is a deliberate no-op in this change** (`COORDINATION_PREFIXES` is empty),
  so no artifact moves to R2 yet and production behavior is unchanged; later H17 work appends prefixes
  as each coordination artifact migrates. No pipeline-version bump or backfill: nothing about audio or
  transcript identity changes.
- **A mid-run kill of the enrich phase (SIGTERM, GitHub cancel, lost-comms) now shuts down
  gracefully instead of silently losing every record update for the run.** Previously the global
  enrich queue persisted each source only once, after *both* the audio and transcript passes
  finished, and nothing intercepted SIGTERM — a kill mid-queue dropped all in-memory record updates
  since the last (end-of-run) persist and left no trailing `run_history.jsonl` entry. The CLI entry
  now installs a SIGTERM handler (`install_signal_handlers`) that latches a process-wide interrupt
  the existing `StopSignal` predicate ORs in, so in-flight workers start deferring immediately and
  the run flows through its normal persist + run-history + state-push path on the way out. The
  global queue also persists every source as soon as the **audio pass** drains — before the
  decoupled transcript pass even starts — and again at the end, shrinking the unpersisted window for
  *every* run, not just killed ones; the repeat persist is idempotent (append-only `merge_records`,
  and `persist_source` no longer mutates the caller's notes list so the "{n} archived" note can't
  double-append). An interrupted run is tagged `interrupted: true` / `outcome: "interrupted"` in
  `run_history.jsonl` + `run_summary.json` and the `enrich`/`build` CLI exits `143` (128+SIGTERM) so
  `continue-on-error` and log readers don't mistake a cut-short run for a clean success — a normal
  wall-clock/supersession yield is **not** an interrupt and still exits `0` (GH#377,
  [#386](https://github.com/BashfulBits/city-meeting-podcasts/pull/386)).
- **Native Granicus audio can fall back once to authenticated Cloudflare egress after a direct
  GitHub-runner HTTP 403.** The retry applies only to strict canonical
  `archive-video.granicus.com/<tenant>/<tenant>_*.mp4` inputs and remains inside the existing local
  limiter, distributed lease, and circuit admission. Worker success prevents the direct 403 from
  tripping the circuit; Worker throttling is counted once before lease release. Audio workflow
  secrets are passed to both container and host-fallback runtimes, while bearer headers are redacted
  from logs and exception commands and the Worker endpoint ffmpeg echoes on error is scrubbed from
  logs. A half-configured `GRANICUS_PROXY_*` pair disables the fallback (warned once) instead of
  aborting the shard, and each attempt/outcome is counted per Granicus tenant on the circuit
  (`worker_fallback_attempts`/`successes`/`failures`) — surfaced in the build log and run summary —
  so the three post-activation runs required by GH#337 can be judged from telemetry rather than log
  archaeology. The isolated probe now classifies authenticated HTTP 200 responses that ignore Range
  as `range_unsupported` access successes and can run one full Arlington/Pflugerville source through
  the production source-cache and `podcast-speech-v2` recipe. This changes transport only: no
  official metadata, audio recipe, pipeline version, artifact key, existing object, or backfill
  behavior changes.
- **An authenticated Cloudflare Worker probe can test alternate egress for Granicus archive media.**
  The GitHub-hosted transport artifact returned 403 for all 12 direct curl/ffmpeg/header cases while
  the same exact objects all succeeded from a Mac, including one full download and local media
  validation. `workers/granicus-media-proxy` therefore provides a deliberately narrow streaming
  experiment: fixed Granicus archive origin, bearer authentication, committed tenant allowlist,
  tenant-prefixed MP4 validation, no queries/redirects/cache, selected Range validators only, and no
  response buffering. The manual Audio-isolated Granicus workflow adds a `worker` mode that compares
  direct versus Worker-routed curl and ffmpeg on one GitHub runner, then performs at most one
  size-capped full-download/local-processing proof. Setup and teardown are documented in the Worker
  README. A path-filtered deployment workflow tests and redeploys the
  Worker automatically when its source or Wrangler configuration changes on `main`, using a scoped
  Cloudflare deployment token while leaving the runtime bearer secret Cloudflare-managed. No audio
  recipe, pipeline version, artifact identity, or stored artifact changes.
- **The isolated Granicus probe can now distinguish HTTP transport behavior from runner/CDN
  throttling.** Manual `granicus-probe.yml` defaults to a low-volume transport mode that pairs curl
  and the production-pinned ffmpeg against the same exact Audio #40 Arlington, Pflugerville, and Fort
  Worth archive objects plus an Audio #33 control, alternating which client goes first. It records
  selected redacted response status/range/timing metadata, tests browser-context curl requests, and
  performs at most one size-capped full curl download by default before validating it with local
  ffprobe and a local 30-second ffmpeg stream-copy. Curl is restricted to the already-resolved archive
  object without automatic redirects; the existing request-shape matrix retains the separately
  guarded `DownloadFile.php` test. The sustained request-count/volume/cooldown matrix also remains
  selectable. Both modes retain Audio-queue isolation and bounded transfers; this is diagnostic only
  and does not change production media fetching, audio identity, pipeline versions, or stored
  artifacts.
- **Local ASR now has a configurable duration admission guard for runner memory safety.**
  `asr_local_max_duration_hours` defaults to 4 hours in production and applies only to synchronous
  faster-whisper/stable-ts execution. `compute_backend: auto` still attempts external dispatch first;
  when dispatch declines—or under `compute_backend: local`—a known oversized recording is deferred with
  `reason=external-required` before semaphore acquisition/download, or after the hosted-audio probe if
  duration was initially unknown. It is not marked failed and remains eligible for later external
  dispatch. Non-positive values disable the guard. The existing rolling 100-sample runtime estimator,
  timeout formula, 285-minute start cutoff, and 350-minute backstop are unchanged. No ASR pipeline
  version or artifact identity changed, so stored transcripts are not invalidated and no backfill is
  triggered.
- **Audio runners now use a prebuilt, version-pinned GHCR runtime with a verified static fallback.**
  `.github/workflows/audio-runner-image.yml` builds and smoke-tests the linux/amd64 runtime weekly and
  whenever its definition changes. The image pins the official Python base by digest and installs an
  immutable FFmpeg 7.1.4 archive only after SHA-256 verification. `audio.yml` pulls that image with a
  five-minute bound and runs the current checkout inside it; if GHCR is unavailable, the shard restores
  or downloads the same checksum-pinned ffmpeg/ffprobe bundle and runs on the host. This removes
  `apt-get update/install` and its unbounded Ubuntu-mirror failure mode from all Audio shards. ASR
  shards reuse the same verified static ffmpeg cache directly on the host, while Whisper model weights
  remain in their existing Actions-cache/Hugging Face/B2 cascade rather than inflating the runtime
  image. No pipeline version or artifact identity changes, so there is no audio or transcript backfill.
- **H14a — external-dispatch substrate + free-tier budget ledger, wired into the live ASR flow
  ([#275](https://github.com/BashfulBits/city-meeting-podcasts/issues/275)).** The dispatch half of the
  H13 compute seam now routes the transcribe/align path. New `citypods/compute/budget.py`
  (statesync-backed `state/compute_budget.json`) enforces each backend's `monthly_gpu_seconds` /
  `max_inflight` as a **hard cap — the $0 guarantee** (decrement-on-dispatch, settle actuals on done,
  reap on expiry, monthly reset). New `citypods/compute/dispatch.py` adds the router (fill free tiers,
  then **overflow to `local`**), a thread-safe `DispatchCoordinator` that records a live `work.json`
  lease (`lease_owner="modal:<job_id>"` — the first competitive use of the H5 lease API) and decrements
  budget, and `reconcile_compute` (reap a dead worker's expired lease → re-queue; settle completed
  jobs). `compute_backend: auto` (now the default) routes inference through the coordinator; with no
  external adapter registered yet (Modal/Beam land in H14b/H14c) every job **overflows to `local`** —
  behavior-identical to before. A new `citypods compute reconcile` CLI runs at `asr.yml` start (a
  dedicated job the sharded `asr` job `needs`), and a `FakeDispatchBackend` exercises the whole path in
  `tests/test_compute_dispatch.py`.

### Added
- **A stall-diagnostics progress registry surfaces which episode/source/phase a stuck enrich
  thread is on, with a thread-stack dump as a backstop.** `audio.yml` runs had intermittently shown
  a shard stuck for the whole run with no further log output, and the existing heartbeat only
  printed CPU/memory snapshots — useless for telling a stuck shard apart from a slow-but-healthy
  one. New `citypods/progress.py` (`PROGRESS`, a thread-safe per-thread-ident registry) is updated
  by `AudioStage`'s encode worker and `TimelineStage`'s planner loop on entry/exit; the heartbeat
  now prints the longest-running active operations every tick (`[enrich] active work: ...`) and, if
  the oldest tracked operation has made no progress for `CITYPODS_STALL_DUMP_SECONDS` (default
  600s, 0 disables), dumps every thread's stack via `faulthandler.dump_traceback` (cooled down to
  once per 30 minutes so a genuine stall doesn't flood the log).
- **The host rate limiter, distributed provider lease pool, and per-run source cache now stop
  waiting once the run's wall-clock budget expires, instead of blocking out a full queue/lease
  cycle.** These three coordination waits were previously unbounded by `stop()` — a worker idle
  past the run's deadline still queued behind whichever thread held the slot/lease/lock, sometimes
  for minutes, before the caller could even check the budget. `HostRateLimiter`/`_Slots`
  (`citypods/http.py`) and `DistributedProviderLeasePool`/`_acquire` (`citypods/provider_leases.py`)
  now accept an optional `stop` predicate and raise a new `StopRequested` if it fires before the
  wait acquires; `SourceCache.get_or_fetch` (`citypods/media.py`) does the same for its per-uid
  lock. `CommandFfmpeg` and `SourceCache` bind `stop` once at construction (`citypods/run.py`)
  rather than threading it through the `FfmpegRunner` Protocol, so existing test doubles are
  unaffected. `StopRequested` is handled as a graceful defer (no backoff recorded) in
  `_encode_one`, `SwagitConcatPlanner.plan()`, and `SilencePlanner.plan()` — the same treatment
  already given to `CircuitOpenMediaFetchError` — since running out of time isn't a source/provider
  failure and shouldn't count against an episode's retry backoff. The actual ffmpeg subprocess call
  remains intentionally out of scope: `stop()` still can't preempt a thread parked in
  `subprocess.run`, only `audio_encode_timeout_minutes` bounds that.
- **The heartbeat now surfaces live `NativeWorkGate` occupancy and provider-lease queue depth each
  tick, not just cumulative end-of-run totals.** `total_wait_seconds` and `telemetry()` could only
  show *how much* waiting had happened over the whole run, not *whether* the current tick was
  blocked — useless for telling "the gate is fully booked right now" apart from "nothing has
  contended in a while." `NativeWorkGate.current_counts()` (`citypods/resources.py`) and
  `DistributedProviderLeasePool.current_waiting_counts()` (`citypods/provider_leases.py`, a new
  live per-domain gauge incremented/decremented around `_acquire`'s wait loop) expose the live
  state; `_ResourceHeartbeat` (`citypods/run.py`) prints a `[enrich] gate: ...` line and one
  `[enrich] leases: <domain> ...` line per tick, suppressed entirely when idle to avoid log noise
  on quiet ticks (GH#376). Observability-only — no change to gate/lease admission logic.

### Fixed
- **The audio orphan GC now allow-lists managed artifacts, so an `--apply` run can no longer delete
  the ASR model mirror or other bucket infrastructure
  ([#448](https://github.com/BashfulBits/city-meeting-podcasts/issues/448) investigation).** The
  unscoped sweep (`--prefix ""`) only protected the `state/` prefix, so the dry-run report flagged
  `models/faster-whisper-large-v3-turbo/*` — including the 1.6 GB `model.bin` written by
  `scripts/prepare_whisper.py` and depended on by the ASR workers — as reclaimable orphans; an apply
  run would have broken transcription. `scripts/gc_audio.py` now treats a key as a deletion candidate
  only when it is a managed artifact (`is_managed_artifact`: content-addressed audio `*.m4a`, or a
  `transcripts/…` object); everything else (`state/`, `models/`, `clips/`, or any future infra prefix)
  is allow-listed out and counted as "protected" in the run summary. This is an allow-list of artifact
  shapes rather than a deny-list of known infra, so a newly introduced infrastructure prefix can never
  be reaped by an older copy of the script. Report/scan logic only — no audio bytes or stored artifacts
  change.
- **The H16 identity check no longer false-fails on coalesced duplicate source views, and a coalesced
  follower keeps a valid served duration ([GH#421](https://github.com/BashfulBits/city-meeting-podcasts/issues/421)
  follow-up).** Audio run #58 — the first with the GH#421 duplicate-coalescing active — failed the
  `identity` criterion on 20 Fort Worth episodes. Coalescing makes a combined feed's record adopt the
  per-board feed's *canonical* shared object (same `uid` + spec, different source prefix). The
  `_artifact_matches_recipe` exemption already tolerated that for `audio_key`/`audio_url`, but
  `current_artifact_changed` still fired on the accompanying served-duration delta. That category now
  fires only when the artifact no longer resolves to a valid content-addressed object for the recipe —
  a re-probe or coalesced-sibling adoption is metadata-only, not a changed artifact. Separately, a
  *credited* canonical winner can carry no probed duration; `_apply_artifact` (`citypods/media.py`) no
  longer downgrades a follower to `0s` by adopting that — it keeps the shared duration when present and
  otherwise backfills from the episode's own timeline/source (which fixes the 5 episodes that also
  tripped `served_duration`). Diagnostics + an in-place metadata fix only — no audio bytes, pipeline
  versions, or stored artifacts change, so **no backfill**.
- **The H16 `concurrency_ceiling` criterion's expected ceiling is now configurable.** It was hard-coded
  to the GH#300 `1`-local / `2`-distributed envelope, so deliberately tuning Granicus concurrency (e.g.
  bumping `provider_distributed_leases.granicus.com.slots`) made the acceptance report `fail` for an
  unrelated reason. A new `provider_audio_concurrency_ceiling` config key (default `1`/`2`) declares the
  intended ceiling; the criterion asserts the operative `provider_rate_limits` + distributed `slots`
  match it, still catching an accidental drift between the two operative knobs. Update the declared
  ceiling in lockstep when tuning.
- **A failed audio upload no longer leaves the record partially mutated, and the H16 identity check
  no longer reports a false mismatch for any artifact retained across a transient failure
  ([GH#353](https://github.com/BashfulBits/city-meeting-podcasts/issues/353)).** Audio runs #54 and
  #56 failed the `identity` criterion with a single `1/~939` mismatch (`audio_key` + `audio_spec_hash`
  + `audio_url`, with neither `served_duration` nor `current_artifact_changed` firing). Root cause
  (proven from run #56's per-shard log): an episode's recipe changed during the run and its re-encode
  probed a new served duration, then the **upload failed transiently** (B2 `ServiceUnavailable`).
  `materialize_audio` (`citypods/media.py`) had already written `audio_duration_served` *before* the
  `put_file`, so the failed upload left the record carrying the new artifact's duration while still
  pointing at the prior, valid artifact (old spec). `H16IdentityTracker.verify`
  (`citypods/h16_identity.py`) saw the duration change, entered the artifact-comparison branch, and
  flagged the legitimately-retained old key/spec/url against the freshly-recomputed `_expected()`
  spec. Two fixes: (1) the encode now commits `audio_duration_served` **atomically with the artifact
  pointer, only after a successful upload**, so a failed upload leaves the episode untouched and
  simply retries next run; (2) `verify` no longer compares key/spec/url when the artifact identity is
  **unchanged from capture** (no successful re-materialization this run) — a divergence from the
  recompute is then a pending re-encode, not corruption — which also covers budget-deferred re-encodes
  and reused migrated `legacy` artifacts (generalizing the earlier `legacy_ok` exemption). A freshly
  *written* artifact is still validated, so genuine content-addressing drift is still caught. The
  earlier same-issue entry attributing this to legacy reuse was incorrect — the mismatching artifact
  carried a real content-addressed spec, not `"legacy"`, and the lease `stale_leases_reaped`
  correlation was common-cause (infra-troubled runs), not causal. No audio bytes, pipeline versions,
  or stored artifacts change, so **no backfill**.
- **Swagit concat probes no longer deadlock the global Granicus media pool.** The concat duration
  probe now acquires the process-local host limiter before the cross-shard distributed lease,
  matching every other ffmpeg/ffprobe media path and the #342 lock-order invariant. The reversed
  order could let concat probes hold both distributed slots while waiting for a local slot held by
  source-cache work that was itself waiting for a distributed slot; Audio #51 exposed the cycle by
  renewing both leases for the full run without launching ffprobe. This changes coordination only:
  audio bytes, artifact identity, and pipeline versions are unchanged, so **no backfill** is
  triggered.
- **Duplicate source views of one stable meeting no longer run the same ASR recipe twice.** The
  per-episode planner now groups matching `(stable uid, ASR recipe)` work, co-locates every
  source-local alias on one shard, and charges the inference weight once. `TranscriptStage` uses a
  thread-safe run-local result cache with per-key in-flight reservations, so concurrent aliases fan
  one completed VTT/word-JSON result out to their existing source-scoped object keys instead of both
  entering native inference — even if multiple ASR worker permits are configured. Fresh-ASR recipes
  now include the stable `author + body + title` prompt plus language, compute type, and beam size;
  different inference inputs remain independent. Existing current-version transcripts are still
  accepted before recipe recomputation, and `ASR_PIPELINE_VERSION` is unchanged, so already-stored
  artifacts are **left as-is** and no catalog backfill is queued. Pending items use the complete recipe.
  The ASR workflow also suppresses the known upstream Node `Buffer()` deprecation only for the pinned
  `actions/download-artifact` step; application warnings remain visible.
- **A busy ASR shard transcribing a multi-hour recording no longer looks idle/stalled, errored
  audio no longer wastes scarce ASR slots, and one unprobed source no longer skews shard weighting
  into the thousands of hours.** Investigating a run where three shards appeared to have "no work"
  while one ground on surfaced four issues — all observability/efficiency, no recipe or
  pipeline-version change:
  - **ASR inference is now registered in the progress registry.** `TranscriptStage` runs native
    inference in a killable child process while the parent thread only polls; it never wrapped that
    wait in `PROGRESS.track`, so the heartbeat printed `active work: no tracked work active` for the
    entire (sometimes 90-minute) transcription — a healthy run indistinguishable from a hung one,
    the exact failure the registry exists to prevent. The poll loop now registers an `asr-<mode>`
    entry (`citypods/stages.py`), so the heartbeat shows the in-flight episode/elapsed and the
    stall-dump backstop can actually fire.
  - **The heartbeat now surfaces `ResourceAdmission` waiters.** Worker threads parked on the
    `load>N`/`mem_avail<N` guard block *before* reaching the `NativeWorkGate`, so its `asr_waiting`
    stayed `0` even with real work queued behind a running ASR job — reading as "no demand."
    `ResourceAdmission.current_waiting_counts()` (`citypods/resources.py`) exposes the live per-kind
    queue and `_ResourceHeartbeat` (`citypods/run.py`) prints a `resource guard: waiting ...` line.
  - **Episodes with a materialization error are no longer queued for ASR.** Audio that failed to
    materialize (e.g. bytes uploaded but no probeable duration) still passed the transcribe
    audio-readiness gate, wasting a serial ASR slot on broken audio and inflating the backlog/shard
    weight (~600 such episodes in the investigated run). `TranscriptStage` skips them
    (`reason=audio-error`), `estimate_transcribe_shard_work` excludes them, and `materialize_audio`
    no longer reuses/credits an errored record so the audio lane re-encodes it (clearing the error +
    recording a duration on success, with the existing exponential backoff guarding genuinely-broken
    sources).
  - **Unknown-duration items are weighted by their source's own average, not a flat 2h ceiling.**
    `estimate_transcribe_shard_work` previously added a 2-hour fallback per unprobed episode, so one
    source with thousands of them estimated at ~3,550h and pinned its whole (source-atomic,
    unsplittable) backlog to a single shard. Unknown items now take the average known local duration
    in the same source (`citypods/records.py`); the constant fallback applies only when the source
    has no known duration to average against.
- **Fresh transcription no longer installs the alignment and benchmark dependency stacks.**
  Optional dependencies are split into `asr-transcribe` (faster-whisper), `asr-align` (stable-ts
  with its faster-whisper adapter), and `asr-bench` (both plus jiwer), while the existing `asr`
  aggregate remains backward-compatible. Scheduled `asr.yml` installs only
  `asr-transcribe,storage`; the manual benchmark installs `asr-bench`; the future align-only lane
  is explicitly assigned `asr-align,storage`. This reduces install time/disk and prevents
  transcribe runners from importing the unused torch/stable-ts alignment stack.
- **Logical-run telemetry no longer presents a partial shard matrix as the latest completed run,
  and ASR deferrals now expose structured reasons.** Scoped events receive a stable logical-run id
  from GitHub run + phase + lane; KPI selection retains the previous complete run until every
  expected shard reports, while a first-ever partial run remains explicitly marked incomplete.
  `StageStats.defer_reasons` now records stable ASR reason tokens through run history, cross-shard
  aggregation, status JSON, and the `/admin/status` stage table instead of collapsing every queued
  item into one opaque Deferred count.
- **Local ASR timeouts now terminate native inference and back off only the offending episode.**
  Production local execution moved into a persistent spawned subprocess that keeps model caches warm
  across episodes but can be terminated/restarted when faster-whisper or stable-ts exceeds the
  per-item deadline. The prior daemon-thread fallback could not stop CTranslate2 work, abandoned the
  runner slot, and skipped all remaining ASR for the run. Timeout attempt count and timestamp now
  persist in the transcript record with exponential 1–30 day backoff; successful reuse or inference
  resets it, shard weighting treats active timeout backoff as blocked work, and other episodes
  continue after a killed worker. No transcript recipe or pipeline-version change.
- **ASR shard ownership now comes from one canonical pre-matrix snapshot, eliminating divergent
  assignments and four redundant full B2 restores per workflow.** The reconcile job restores durable
  state once, reconciles leases/budget, writes a versioned `ShardPlan`, and uploads both state and plan
  as one run-scoped artifact. All four ASR matrix jobs validate and consume that exact assignment,
  fail closed on lane/shard/source drift, and skip `pull_state`; they no longer independently restore
  the full `state/` prefix or calculate weights while sibling state changes. The local CLI path keeps
  deterministic in-process planning when no artifact is supplied.
- **The scheduled transcribe lane no longer defers episodes merely because they have untimed
  provider text while forced alignment is disabled.** `TranscriptStage` previously applied the
  `alignment-disabled` guard before `--lane transcribe` discarded the alignment hint, so exactly
  the caption/minutes-bearing episodes that the fresh-ASR lane was intended to cover stayed queued.
  Lane routing now happens first: `transcribe` always selects fresh faster-whisper, while the
  unscheduled `align` lane and combined auto mode retain the alignment-disabled defer behavior.
- **`SilencePlanner`'s `ffmpeg silencedetect` pass no longer oversubscribes the CPU alongside
  `AudioStage`'s encodes.** `detect_silences()` shelled out to ffmpeg with no `-threads` cap and
  wasn't gated by `NativeWorkGate`, so `TimelineStage`'s per-episode planner threads (parallelized up
  to `ctx.max_encodes_per_source`) could each spawn an unbounded, all-cores ffmpeg `silencedetect`
  process — running concurrently with, or ahead of, the gated audio encodes the gate exists to budget.
  `detect_silences()` now accepts a `threads: int | None` param applied as `-threads N` the same way
  `CommandFfmpeg` pins its encode passes, and `SilencePlanner.plan()` acquires/releases
  `ctx.native_work_gate` (`kind="audio"`) around the call using the same `ffmpeg_threads` value
  `CommandFfmpeg` is configured with — so a silencedetect pass competes for the same admission slots
  as `AudioStage`'s encodes instead of running outside the budget. A denied/stopped admission defers
  the planner pass (`ep.timeline` stays unstamped) rather than running ungated or raising.
- **The silence-trim and Swagit-concat planners no longer produce or silently swallow degenerate
  results.** `SilencePlanner` could stamp a near-empty served timeline (observed: 0.005s/0.010s
  outputs) when `detect_silences` misread a throttled/truncated source as almost entirely silent;
  `build_silence_timeline`'s result is now checked against `is_degenerate_served_duration` (new
  `silence_min_served_seconds`/`silence_min_served_fraction` `StageContext`/site-config knobs,
  defaults 5.0s / 2%) — a degenerate result preserves the prior valid timeline if one exists,
  otherwise falls back to the untrimmed identity timeline instead of hosting near-silence.
  `SilencePlanner.version` bumped 1→2 to re-examine episodes that may already carry a degenerate
  stamped timeline from before this guard existed (a one-time, wall-clock-bounded re-trim).
  Separately, `SwagitConcatPlanner` collapsed page-fetch and per-segment duration-probe failures
  into one bare `return None` with no record of which sub-operation failed; both paths now call
  `record_materialize_failure` with a distinct code (`concat-fetch`, `concat-probe:s<i>`) so
  retries/backoff and diagnostics target the actual failure instead of a generic deferral.
- **ASR shard assignment is now weighted by routing-aware transcription cost, not the audio lane's
  pending-encode backlog.** `run.py` fed `asr.yml`'s `--shard K/4` partition the same
  `pending_audio_work` signal as `audio.yml`; in steady state (Audio runs more often than ASR) that
  backlog sits near zero for nearly every source, which silently collapsed ASR shard assignment to
  alphabetical round-robin — blind to how much transcription work a source actually had outstanding,
  so one shard could own a multi-hour local backlog while a sibling finished and sat idle. New
  `estimate_transcribe_shard_work` / `TranscribeShardWork` (`citypods/records.py`) mirror the
  `lane="transcribe"` reuse check and separate duration-weighted local inference from cheap external
  dispatch, blocked/deferred inspection, and already-in-flight work. Current production has no real
  external adapter, so locally eligible recordings are weighted by duration, known recordings above
  the 4-hour local ceiling contribute only a minimal blocked cost, and unknown durations receive a
  conservative local estimate. H14's canonical planner can inject one route classification computed
  from restored state and a single GPU budget/capacity snapshot; matrix shards will consume that
  immutable decision instead of independently guessing dynamic external availability. `run.py`
  selects the estimate only for `lane == "transcribe"`, leaving Audio weighting unchanged. No
  audio/transcript recipe, pipeline version, stored artifact, or backfill behavior changes.
- **Granicus throttle circuits are now shared across Audio shards and isolated by tenant
  ([#337](https://github.com/BashfulBits/city-meeting-podcasts/issues/337)).** The previous breaker
  counted failures independently in each shard and opened one registrable-domain circuit, so the same
  three provider failures could be repeated by every shard and a Fort Worth throttle could defer
  healthy Denton or Granicus-owned Swagit work. Circuit failure/open/probe state now uses deterministic
  ordinary storage objects protected by a separate one-slot FIFO lease. Native archive paths and
  tenant subdomains receive stable tenant scopes; three throttles open that tenant only, while two
  distinct tenant trips inside the cooldown window trigger a domain emergency. Exactly one shard owns
  a half-open canary, siblings observe its recovery, and abandoned probes are reclaimable after a
  bounded TTL. The global queue parks and releases work by tenant/domain scope. Existing Granicus caps
  remain 1 process-local / 2 distributed, and no audio recipe, pipeline version, stored artifact, or
  backfill changes.
- **Granicus throttle failures no longer cause an immediate duplicate request or force every
  remaining meeting out of the run ([#337](https://github.com/BashfulBits/city-meeting-podcasts/issues/337)).**
  A 403/429 raised while `TimelineStage`/`SilencePlanner` fills the per-run source cache now records
  exactly one persisted materialization attempt and halts that episode before `AudioStage` can fetch
  the same URL again. Circuit-open meetings remain deferred without backoff, but the global queue now
  parks them while other-provider work drains, waits only while the normal stop budget permits, and
  runs one half-open canary after cooldown. A canary throttle immediately reopens the circuit; a
  completed materialization records recovery and releases the parked work through the unchanged
  Granicus caps. Run telemetry adds recovery-probe/recovery counts. A new manual, isolated
  `granicus-probe.yml` measures repeated request count, progressive bounded transfer volume, cooldown,
  exact Audio #37 Fort Worth failures, and concurrency-last behavior with redacted JSON artifacts.
  No audio recipe/pipeline version changed and no stored artifact is invalidated.
- **Stale-lease and release/renewal logs now name the GitHub run, job, matrix shard, and lease
  state ([#345](https://github.com/BashfulBits/city-meeting-podcasts/issues/345)).** GH#336 already
  stored `github_run_id`/`github_run_attempt`/`github_job` in renewable lease payloads, but
  stale-reap logs only ever surfaced the internal `hostname:pid:uuid` owner token — Audio #33 reaped
  two stale Granicus candidates and an operator could not tell which prior run or shard had held
  either one. Lease payloads now also carry the writer's `K/N` matrix shard label (threaded through
  `DistributedProviderLeasePool.configure(shard=...)` from `build()`'s existing shard tuple), and
  stale-reap/release/renewal-failure log lines append a concise `owner=… run_id=… job=… shard=…
  state=…` suffix built only from the fields present in the payload. Legacy or unreadable payloads
  still reap safely via the object-modification-time fallback, with no metadata suffix. No secrets
  are stored; payload-read caching is unchanged.
- **The global audio queue now drains promptly after a graceful `stop()`
  ([#344](https://github.com/BashfulBits/city-meeting-podcasts/issues/344)).** The H5 PR3 global queue
  dispatches `AudioStage` once per *episode* (for true newest-everywhere-first ordering across
  sources), so `materialize_audio()`'s `_hosted_keys()` re-listed the same source's storage prefix once
  per episode instead of once per source — Audio #32 shard 0 spent ~25 minutes draining queued-but-cheap
  items after its last in-flight encode finished. A new `HostedKeysCache` (wired through
  `StageContext.hosted_keys_cache`) shares one `list_objects` listing per source across every
  `AudioStage` call for that source during a build, so listings scale with the number of sources, not
  episodes. Cheap reuse/credit bookkeeping is unchanged and still runs regardless of `stop()`; only the
  redundant listing is eliminated.
- **The ffmpeg/ffprobe rate-limit circuit now opens before the failed attempt's provider lease is
  released ([#343](https://github.com/BashfulBits/city-meeting-podcasts/issues/343)).** GH#336 added
  post-lease circuit admission, but the circuit was only recorded/opened by the higher-level
  materialization caller, after the subprocess boundary had already released its distributed and
  process-local provider slots on the way out. A queued waiter could acquire that just-released lease,
  pass the still-closed circuit check, and start one extra ffmpeg process per threshold crossing —
  Audio #33 shard 3 showed four direct Granicus 403s against a configured threshold of three, twice.
  `_raise_if_rate_limited` now records the failure (and atomically opens the circuit, when the
  threshold is crossed) from inside the same `with` block that holds both provider slots, for both the
  `subprocess.run` and monitored/`Popen` ffmpeg paths; `RateLimitedMediaFetchError` carries
  `circuit_recorded`/`opened_domain` so the materialization caller skips re-recording (no
  double-counting) while still logging the open transition and applying episode backoff. Circuit-open
  logging remains once per transition; rate-limit/circuit-deferred telemetry is unchanged.
- **Process-local workers can no longer hoard distributed provider slots
  ([#342](https://github.com/BashfulBits/city-meeting-podcasts/issues/342)).** The ffmpeg/ffprobe
  guard now acquires the process-local `HostRateLimiter` slot *before* joining the distributed
  provider-lease election (previously the other way around), for both monitored and unmonitored
  ffmpeg paths and the ffprobe probe. A process with a local cap of one can therefore hold at most
  one distributed slot for a domain at a time, instead of letting several of its own threads win
  every distributed candidate while they wait behind the local cap — which had let one early-starting
  shard occupy both Granicus slots in Audio runs #32/#33 while other shards were still starting up.
  Existing aggregate slot limits and post-slot circuit admission are unaffected.
- **Distributed provider leases now renew safely, reap dead owners promptly, and stop queued media
  work after a provider circuit opens ([#336](https://github.com/BashfulBits/city-meeting-podcasts/issues/336)).**
  Waiting and acquired lease candidates refresh their explicit expiry while alive; winner election
  uses immutable FIFO candidate-key order, so renewal cannot demote an active holder behind waiters.
  Lease payloads include GitHub run/job metadata, storage reads use payload expiry when available
  (object modification time remains the compatibility fallback), and acquisition failures clean up
  their candidate. The ffmpeg/ffprobe boundary now rechecks the run-local circuit only after both
  distributed and process-local provider slots are held, preventing already-queued workers from
  starting after another worker trips the circuit. Circuit opening is atomic per cooldown instead of
  being logged once per concurrent failure. Run telemetry now records lease acquisitions, total/max
  wait, renewals, stale reaps, direct throttles, trips, and circuit deferrals per media domain. No
  pipeline version changed and no stored artifact is invalidated; deferred work retries naturally.
- **Peak-constrained recordings no longer fail bounded linear loudness normalization.** Some
  low-average/high-transient Granicus recordings required enough constant gain to predict +7–8 dBTP,
  which correctly prevented FFmpeg's linear `loudnorm` from silently reverting to dynamic mode but
  dropped the episode. The normal measured-linear path is unchanged. When peak headroom is
  mathematically insufficient, pass 2 now applies the same constant integrated-loudness gain followed
  by FFmpeg's short-lookahead `alimiter` at 192 kHz, then returns to 48 kHz for AAC. The limiter uses a
  -2.5 dB ceiling to leave AAC reconstruction headroom; memory remains duration-independent because
  only resampler and millisecond lookahead buffers are retained. Extremely high-crest-factor material
  may land slightly below -16 LUFS rather than clip or disappear. Existing records carrying the old
  `loudness` error code bypass their stale exponential backoff once and retry immediately; new genuine
  measurement failures use `loudness_measurement`. This fallback changes only items that previously
  failed, but it ships inside the `podcast-speech-v2` recipe described below.
- **Long-meeting podcast mastering and edited timelines are now bounded-memory.** Production audio
  uses the versioned `podcast-speech-v2` profile:
  `80 Hz high-pass → dynaudnorm → gentle compressor → final measured linear EBU R128 loudnorm
  (-16 LUFS, -1.5 dBTP)`. Monotonic single-source silence timelines now use one streaming selector
  instead of parallel `atrim` branches; it switches to one-sample frames only near each boundary,
  selects by integer 48 kHz sample PTS, and coalesces normal frames afterward. This keeps RSS bounded
  without the cumulative frame-boundary duration drift of plain `aselect`. Pass 1 applies the timeline
  and speech leveling while streaming once from the provider into a temporary mono FLAC and measuring
  that exact signal with `ebur128`; pass 2 reads the local FLAC, applies measured **linear** loudnorm
  or the bounded peak-limiter fallback, and encodes 96 kb/s AAC. Native admission is phase-specific:
  source caching happens before a CPU slot is held, and finalization has a small dedicated executor
  while sharing the same total FFmpeg cap. The path reserves a fixed 768 MiB independent of duration,
  retains the 1.5 GiB mid-flight safety floor, and rejects sub-second edited timelines as unusable
  audio. `audio_processing_profile` participates in `audio_spec_hash`; changing v1 → v2 gradually
  invalidates and remasters already-hosted v1 artifacts through the normal wall-clock queue. No
  separate pipeline-version constant changed.
- **ASR shard provider-fetch outages now fall back to the persisted archive.** The `transcribe` and
  `align` lanes are best-effort transcript backfill over already-hosted audio in `episodes.json`, so a
  transient source refresh failure now loads the last-known record archive and continues ASR instead of
  returning a run error. If no archive exists, the source is skipped/deferred for that ASR run. Audio and
  full enrich lanes still surface provider fetch failures as errors. No pipeline version changed and no
  artifact backfill is triggered.
- **ASR shards now run every 5h with a 285m start cutoff, 350m backstop, and rolling runtime
  estimates.** The `transcribe`/`align` lanes stop starting new local ASR after
  `asr_start_cutoff_minutes` (285m), but an already-started transcript may continue until
  `asr_backstop_minutes` (350m), even if the next scheduled ASR run is queued. `TranscriptStage` keeps
  a fixed-size `state/asr_runtime_log.json` buffer of the previous 100 successful ASR runtime /
  recording-duration samples, falling back to the conservative timeout formula until real samples exist,
  and starts a recording only when `recording_duration × average_ratio` fits before the start cutoff. The
  ASR semaphore remains as the single-transcript gate; waiters poll `stop()` / abort so a timed-out
  native ASR call cannot pin sibling workers, and the shared runtime log is merge-pushed so ASR shards do
  not overwrite each other's samples. No pipeline version changed and no artifact backfill is triggered.
- **`/admin/status` "Last Run" block now reports the Build & Deploy action, not the latest enrich run.**
  Run history (`run_summary.json`) is recorded only by the time-bounded enrich (audio/ASR) workflows, so
  the at-a-glance "Last Run" card was surfacing the newest audio/ASR lane — duplicating the adjacent
  Audio/Transcribe/Diarize run cards and never reflecting the deploy that actually rendered the page.
  `build_status` now reads the GitHub env it runs under (it executes *inside* `deploy.yml`) into a new
  `kpis.last_deploy` block (`status`, `workflow`, `github_run_id`, `github_run_url`, `ts`); the status
  page renders that block, so the card shows the Build & Deploy workflow, the render timestamp, and a
  link to that Actions run. Off-CI it degrades to a link-less `local` status. No pipeline version
  changed and no artifact backfill is triggered.
- **Cross-lane record clobber in the sharded enrich workers (the `hosted_audio −16` regression).** The
  `audio` and `asr` workflows shard over the same `source_key` partition but run on different schedules,
  so both write the *same* `state/sources/<key>/episodes.json` at overlapping read→write windows. Each
  run pulled state once at start, held it for its whole multi-hour run, then pushed back the **whole**
  record file — so an ASR run that started before an audio run hosted new audio re-uploaded its
  start-of-run `audio` block on finish, silently erasing the freshly hosted URLs (and, symmetrically, a
  late audio run could erase transcripts). The scoped push prevented cross-*shard* clobber but not this
  cross-*lane* lost update. Now a scoped run owns only its lane's artifact block
  (`records.protected_blocks_for_lane`: `audio` vs `transcript`) and, on push, re-reads the freshest
  remote per owned source and preserves the block it doesn't own (`records.merge_preserving_foreign`,
  `statesync.push_records_merged`/`fetch_remote_records`); a present-but-unreadable remote skips that
  source's push rather than clobber. `stages.LANE_STAGES` (one source of truth, enforced in `run_stages`
  and the global queue) keeps each lane to its own work-class stages so it never re-derives a foreign
  block. Because the status KPIs read straight from the record store, this also stops the periodic
  `/admin/status` `hosted_audio`/`transcripts_synced` numbers from bouncing backwards after an ASR-only
  update. The block/lane registries are designed to extend to the near-term `diarize` lane (review/12
  §H5/§H6).
- **Admin status now reports latest telemetry per pipeline stage.** `/admin/status` keeps the existing
  newest-lane `stage_totals` for compatibility, but now also exposes `backlog.stage_runs` keyed by
  stage (`audio`, `transcript`, and future stages such as `diarize`). Each entry points at the latest
  completed logical run that actually reported that stage, with sibling shard totals aggregated, so a
  later ASR-only run no longer makes the audio row look like "not run." Scoped `run_events/` remain
  upload-only and are not deleted by later lane pushes. At-a-glance now includes Audio / Transcribe /
  Diarize run-status cards next to Last Run. The Hosted Audio card also shows feed-visible audio
  coverage, not-hosted count, and stale re-encode count, while the Transcripts card surfaces the
  text-only/provider bucket that sits between synced transcripts and missing ones. Linked Video counts
  are now config-aware: direct-provider records are counted as linked only when `extract_audio` is
  false; with the production default `extract_audio: true`, unhosted direct records are audio backlog.
  The run-status cards now render warning-level alerts in yellow separately from run-level errors in
  red. Workflow cache steps were updated to Node 24-compatible `actions/cache@v5` / restore `@v5`,
  and the temporary `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24` runner override was removed now that GitHub
  runners default to Node 24. No pipeline version changed and no artifact backfill is triggered.
- **Admin status actuals now use source records and the work manifest instead of overlapping feed
  rows.** `/admin/status` headline totals for archived meetings, hosted audio, linked video, storage,
  stale items, and issue counts are now aggregated once per canonical `source_key`, so combined feeds
  and per-board feeds no longer hide or double-count source records. The backlog block derives a fresh
  work manifest from records and overlays live `work.json` sidecar state such as shard/dispatch leases,
  while shard `run_events/` continue to drive last-run telemetry.
- **Audio #11 source-cache no longer forces non-AAC audio into an M4A container.** The per-run source
  cache now remuxes provider audio into a local Matroska audio copy (`.mka`) instead of writing
  `*.m4a` with the iPod muxer during download. Final materialization still writes podcast M4A, but the
  identity path now probes both codec and bitrate: only under-cap AAC is stream-copied, while MP2/MP3/
  PCM/other source codecs are transcoded to AAC. No pipeline version was bumped: already-hosted audio
  is left as-is, and deferred/failed source-cache items retry naturally through the normal audio lane.
- **Audio #10 encode-failure follow-up: Granicus storms are now cross-shard capped and classified.**
  `provider_rate_limits.granicus.com: 2` is still the per-process cap, but audio has four shard jobs;
  the new `provider_distributed_leases` layer uses B2-compatible soft lease candidate objects so
  shards share one aggregate Granicus limit for ffprobe/ffmpeg media reads. Live probes on 2026-06-15
  showed 1–8 concurrent short Granicus ffprobe/ffmpeg reads succeed from this client, while Audio run
  #10 failed under sustained 8-way Actions overlap, so production starts at 6 aggregate Granicus slots
  rather than dropping to a conservative 3–4. ffmpeg/ffprobe 403/429 stderr is now classified as
  `rate_limited`, source-cache throttles no longer immediately fall through into a second direct render
  attempt, and a run-local circuit breaker pauses new Granicus media work after repeated throttles.
- **B2-compatible provider leases.** The first cross-shard lease implementation used S3 conditional
  `PutObject` (`IfNoneMatch="*"`), which Backblaze B2 rejects with `NotImplemented` and broke
  post-merge `Build & Deploy` while fetching Granicus feeds. Leases now use only ordinary
  upload/list/delete operations and are scoped to ffprobe/ffmpeg media reads, not the shared
  `requests` adapter.
- **Swagit legacy concat probes now use the same browser UA and provider slots as other media reads.**
  The Addison 55844 failure was reproduced: the page parser finds three legacy segments and the
  concat planner is registered before silence planning, but the first segment's MP4 is unreadable
  (`moov atom not found`) and its HLS playlist returns 404, so publishing partial audio would be
  unsafe. The planner still defers that episode, but healthy legacy segments are no longer falsely
  deferred by bare ffprobe calls without the Granicus-compatible UA or rate-limit guards.
- **Feed-health fixes for Dallas meeting links and edited-timeline audio duration metadata.** Dallas'
  `meetings_url` now points at the live Swagit archive URL instead of the old `dallascityhall.com`
  page whose TLS certificate fails Python Requests verification. Edited/non-identity timelines now
  record `audio_duration_served` from the EDL's served-length total even when ffprobe reports a
  slightly rounded container duration or an existing record carries that stale rounded value. No
  pipeline version was bumped: already-hosted audio is not re-encoded, and affected records self-heal
  as the audio lane revisits them through reuse, credit, or encode paths.
- **`-user_agent` is now passed only for remote ffmpeg/ffprobe inputs (regression from the granicus
  UA fix).** The browser-compatible `-user_agent` was added to *every* ffmpeg/ffprobe invocation, but
  the encode pass reads the **local cached copy** from the source-cache (`/tmp/citypods_src_*`), and
  `-user_agent` is an HTTP-only option — ffmpeg errors `Option user_agent not found` on a `file:`
  input. The first post-fix Audio run (#6) hit this on ~1,300 cache-hit encodes (`returncode=8`,
  zero hosted). New `_ua_args(url)` emits `-user_agent` only when the input is `http(s)://`; local
  files (and insert assets) omit it. Verified end-to-end with real ffmpeg (remote → UA sent + works;
  local → no UA + encodes). `_download_audio`, `_render_identity`, `_render_filter`,
  `_probe_audio_bitrate` all route through it.
- **PR preview no longer depends on live providers (was failing/ballooning on provider outages).**
  The preview ran `citypods build --phase render` with **no record store**, so it fetched all ~84
  feeds live just to have something to render — slow, and the *only* thing that could fail it (a
  granicus connection-timeout storm, amplified by a concurrent Audio run, produced 33 errors → exit
  1). New `citypods build --no-refresh` renders **purely from the record store with zero provider
  connections** (`SourcePipeline.render_from_records`; an empty store renders an empty feed, not an
  error). `preview.yml` now restores the `build-state-*` Actions cache (read-only, no B2 creds — a PR
  can read its base branch's caches) and runs `--phase render --no-refresh`: ~seconds instead of
  minutes, deterministic, and immune to provider availability. Production deploys are unchanged (they
  still refresh + already fall back to `archive_from_records` on a fetch error). URL/contract
  validation continues to live in `contracts.yml`.
- **Swagit deep-link contract check no longer false-fails on the SPA player route.** Swagit's
  `/play/{id}/{t}` is a client-side route the server `404`s on a direct request — even the real
  chapter-anchor timestamps the watch page itself links — so the contract check's `HEAD` (which
  assumed a server-resolvable 2xx, true only for Granicus' `?starttime=`) flagged a false breakage.
  The check now, on a 4xx for an SPA-style path-timestamp deeplink, confirms the scheme is still
  current by finding the deeplink's path on the live watch page (`citypods/contracts.py`,
  `_is_spa_seek_url`). The deeplink *generation* was always correct (it matches the page's anchors).
- **Granicus CDN UA block round 2: drop bot-disclosure form from `USER_AGENT`.** After the initial
  Granicus UA fix landed (`Mozilla/5.0 (compatible; citypods/0.1; …)`), Granicus CDN updated its
  bot-detection to also block the `(compatible; citypods/…)` disclosure form — the Monday contracts
  check failed the next day (`arlington-tx` `media-fetch`, issue #300). `USER_AGENT` is now a plain
  Chrome-on-Linux string (`Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 … Chrome/124.0.0.0
  Safari/537.36`) with no citypods identifier; this is the only form that reliably passes CDN
  bot-detection. Also fixed: `_download_audio_file` in `stages.py` was using its own bare
  `citypods/0.1` session instead of the shared `USER_AGENT` constant — same 403 risk, now unified.
- **Granicus audio now downloads — the CDN `403` was a User-Agent block, not signing/rate-limiting.**
  `archive-video.granicus.com` `403`s non-browser User-Agents; our bare `citypods/0.1` UA (and
  ffmpeg's default `Lavf/…`) were blocked, so Granicus audio had **never** materialized (every run
  encoded only swagit). `USER_AGENT` (`http.py`) is now browser-compatible, and `media.py` passes it
  to ffmpeg/ffprobe via `-user_agent` on every remote fetch (`_download_audio`, `_render_identity`,
  `_render_filter`, `_probe_audio_bitrate`). PRs #245/#250/#251 had misdiagnosed this as a
  signing/rate-limit issue and only tested against a **mocked signed redirect**, so it passed CI while
  failing live. To prevent a recurrence, `citypods/contracts.py` gains a **media-fetch** check that
  truncated-downloads each provider's newest clip through the production fetch path (UA + protocol
  whitelist + timeout); it runs in the `-m live` suite and `scripts/check_endpoints.py` (ffmpeg added
  to `contracts.yml`), so a silent "audio never downloads" regression now fails loudly.

### Changed
- **Granicus distributed-lease TTL dropped from 3600s to 900s so a dead holder's slot is reclaimable
  in ~15 minutes instead of up to an hour.** A holder that dies without releasing (crash, SIGKILL,
  lost comms) previously pinned one of the two Granicus lease slots for the full hour-long TTL before
  `stale_leases_reaped` logic could reclaim it. This is free in renewal traffic:
  `DistributedProviderLeasePool._renew_interval` clamps the renewal cadence to 60s for any
  `ttl_seconds >= 180`, so 900/3=300 still resolves to the same 60s interval as 3600/3=1200 did — a
  legitimate fetch+encode (bounded by the 45-minute `audio_encode_timeout_minutes`) renews well
  within the shorter window. Config-only diff in `config/site_config.yml`; a new
  `test_renew_interval_is_capped_so_lowering_ttl_costs_no_renewal_traffic` locks in the
  no-extra-renewals reasoning (GH#378).
- **Audio memory admission recalibrated from Audio run #10 telemetry.** Long loudnorm/filter encodes
  in the run peaked around 9–13 GiB, beyond the old 6.5 GiB clamp. `estimate_encode_rss_bytes` now
  uses a 64 MiB/min served-duration coefficient with a 12,000 MiB max/unknown reservation, so very
  long or unknown-length filter jobs run alone against the 12 GiB budget instead of being admitted
  beside another large encode and then hitting the 1.5 GiB memory floor.
- **Audio encodes are admitted by *predicted* memory, not instantaneous free memory (stops mid-flight
  ffmpeg terminations).** The first clean Audio runs (#8/#9) hosted real audio but terminated ~46% of
  the large filter-render (loudnorm) encodes of multi-hour meetings with `ffmpeg filter-render stopped:
  mem_avail … below floor`. Root cause: admission was an *instantaneous* `mem_available` check, which is
  a **trailing** signal — a long loudnorm encode grows for minutes (memory-floor kills fired **220–1080 s**
  into encodes that peaked at up to **5.9 GiB**), so free memory still looks healthy when a *second* big
  encode starts, and the two then collide. New `MemoryReservation` (`citypods/resources.py`) admits each
  encode against a **budget** (`audio_memory_budget_mb`, ~12 GiB of the 15.6 GiB runner): each encode
  reserves its **estimated peak RSS** — `media.estimate_encode_rss_bytes`, keyed on the known-ahead
  served length (the EDL the `TimelineStage` already built, or the feed duration; a conservative default
  when neither is known) — and a new encode begins only when `reserved + estimate ≤ budget`. That gates
  on the job's *future* footprint, so ≈2 big encodes (or many small ones) overlap with headroom and a
  third big job waits instead of colliding. `native_audio_max_active` drops `4 → 3` (the hard
  concurrency ceiling); the 1.5 GiB `audio_ffmpeg_memory_floor_mb` stays as the backstop for estimate
  misses. The reservation supersedes the old `resource_guard_min_available_mb` gate for audio (that gate
  now governs only ASR). The cost-model coefficients are a first heuristic, calibratable from the
  per-encode `peak_rss` already logged.
- **Source shards are now weighted by configured feed/body count instead of raw source count.**
  `records.shard_assignment` still assigns each `source_key` to exactly one shard (so scoped state
  pushes remain safe), but it now greedily packs heavier sources onto the lightest shard. `run.py`
  passes a stable config-derived weight — the number of configured feeds sharing the source — so
  every matrix job computes the same partition while large multi-body sources like Dallas/Fort Worth
  are no longer bundled with extra small sources merely because source counts balanced.
  **Superseded:** this config-derived weight was later replaced by each source's actual pending
  audio-encode backlog (`pending_audio_work`), and — for the ASR lane only — by pending
  routing-aware transcription cost (`estimate_transcribe_shard_work`, above).
- **Per-provider (per-host) rate limiting + sharding-regression fixes (#39)** —
  ([#274](https://github.com/BashfulBits/city-meeting-podcasts/issues/274)). The first sharded Audio
  run after H6b regressed: comparing it to the last pre-sharding Enrich run, source fetches collapsed
  from a real 5–135 s spread to **all ~5 s** and produced **zero** encodes, with Granicus `403`s and
  no logged error. Root cause: 4 parallel shard jobs each concentrate their workers on a few sources
  sharing one provider CDN, and that burst throttles the tenant (Granicus answers `403`; Swagit
  returns short responses ffmpeg copies and exits 0 on — a truncated "5-second" episode that passed
  the old `size > 0` check). Fixes:
  - **`HostRateLimiter`** (`citypods/http.py`) — a process-global per-**registrable-domain**
    concurrency cap, configured by `provider_rate_limits` in `config/site_config.yml`
    (`granicus.com: 2`, `swagit.com: 2`, `civicclerk.com: 4`). Acquired by **both**
    `GuardedHTTPAdapter.send` *and* the ffmpeg fetch paths (`citypods/media.py`), so one cap bounds
    requests *and* the media downloads that actually caused the storm. Keyed by registrable domain so
    the Granicus-owned Swagit CDN (`*.granicus.com`) is matched by the host the tenant sees.
  - **403-as-rate-limit lifted into the shared layer** — `403` joins the `_ClampedRetry`
    `status_forcelist` (provider throttle, never auth, since media bytes never go through `requests`);
    the bespoke backoff loop in `GranicusProvider.resolve_media_url` is removed. The Retry-After clamp
    is preserved.
  - **Truncation safety net** — an encode that probes shorter than 50 % of the feed-declared duration
    (Granicus/CivicClerk) **or** is empty/near-empty (under a small absolute byte floor — catches
    duration-less Swagit, whose throttled `/download` produced 258-byte stubs) is failed into the
    existing #120 backoff instead of being hosted, so a throttled fetch never ships a 5-second meeting.
  - **Cleanup tool for the already-hosted bad audio** — `scripts/clear_run_materializations.py`
    (+ a `workflow_dispatch` **Clear materialization** workflow) takes an Actions run ID, parses its
    `audio encode done` lines, and resets those records (optionally deleting the B2 objects) so the
    next `audio.yml` re-encodes them. Dry-run by default. Undoes the first sharded run's truncated
    output wholesale.
  - **Balanced shard assignment** — `records.shard_index` (hash-mod, which left `audio (0)` empty with
    few sources) is replaced by `records.shard_assignment`: initially round-robin over sorted
    source_keys, later upgraded to weighted greedy packing by configured feed/body count. It remains
    deterministic, source-atomic, disjoint, and exhaustive.
  - **Accurate ffmpeg timing** — the guard's poll cadence drops from 5 s to 0.5 s so the logged
    `seconds=` reflects a child's real runtime (the 5 s cadence had made every sub-5 s fetch read as
    `seconds=5.0`, masking the truncation).
- **Sharded `audio.yml` + `asr.yml` workflows, lane-pinned (H6b)** —
  ([#273](https://github.com/BashfulBits/city-meeting-podcasts/issues/273)). The combined
  `enrich.yml` (H11b) is replaced by two dedicated workflows, each on its own concurrency group
  (`audio` / `asr`, both distinct from `pages`) and a `strategy.matrix.shard` of 4 source-shards, so
  a deploy is never canceled by heavy work and concurrent shards clear the backlog without clobbering
  records. New `citypods enrich` flags: `--shard K/N` (keep only sources assigned to shard `K`;
  source-atomic, disjoint + exhaustive across `K`), `--source KEY`, and
  `--lane {audio,transcribe,align}`. `run.py` filters cities to the shard and threads the lane into
  the two-pass queue (`audio` → audio pass only; `transcribe`/`align` → transcript pass only), and a
  sharded/scoped run uses the H11b hooks — `push_state(only_prefixes=…owned sources…)` +
  `reconcile_state(full_run=False)` — so it pushes back only the records it owns and never sweeps a
  sibling's. `audio.yml` runs `--lane audio` (no `[asr]` extra, no Whisper); `asr.yml` runs
  `--lane transcribe` (fresh faster-whisper only). The `align` lane (stable-ts forced alignment) is
  implemented but **not scheduled** — forced alignment is deferred to a later issue, so caption-bearing
  feeds get fresh transcription for now. A direct `citypods enrich` (no lane/shard) is unchanged.
- **Render-only deploy; the enrich workflow is the sole record writer (H11b)** —
  ([#272](https://github.com/BashfulBits/city-meeting-podcasts/issues/272)). `deploy.yml` is stripped
  to render-only (checkout → install → restore state → render → validate → upload → deploy): no
  ffmpeg, no Whisper model, no encodes, and the `actions: read` graceful-yield token is dropped (only
  the time-bounded heavy phase polls the Actions API). The heavy phase moves to a new
  **`.github/workflows/enrich.yml`** with its own `enrich` concurrency group, so audio/ASR work can
  never block or redden the Pages deploy. Critically, **the render phase now writes only `docs/`**:
  `build()` gates `save_records` / `push_state` / `reconcile_state` off `--phase render`, so a stale
  render push can no longer silently erase a transcript/hosted-audio that the enrich workflow wrote
  (the lost-update "record-write race" — review/12 §H6/H11b). No pipeline-version bump and no record
  migration: existing artifacts are untouched; this only changes *which workflow* persists them.
  `statesync.push_state(..., only_prefixes=)` and `reconcile_state(..., full_run=)` add the
  scope hooks H6b's source-sharded jobs will use (no behavior change at the single-writer default).

### Added
- **`reset-backoff` recovery tool + workflow — drain the #120 backoff after a fixed encode bug.** When
  a now-fixed bug made encodes fail (Granicus UA 403s #293/#297, Swagit truncation #274), each failure
  incremented the record's `materialize_attempts`, so those episodes are skipped for up to 30 days even
  though the bug is gone. Granicus never hosted at all, so `clear-materialization.yml` (which keys on
  `audio encode done`) can't reach its records. New `scripts/reset_materialize_backoff.py` scans the
  durable record store and clears the backoff fields (`attempts`/`last_attempt`/`error`) of every
  record that is **un-hosted *and* in backoff** — never touching a hosted record, the transcript block,
  or the durable `state/` snapshot. Optional `--provider`/`--source` filters (verify one provider
  end-to-end first); dry-run unless `--apply`; pushes back only the affected `sources/<key>/` prefixes.
  Dispatch via the new **Reset materialization backoff** workflow (shares the `audio` concurrency group
  so it never races an Audio run's state push).
- **GPU/ASR execution-backend interface + `local` adapter (H13)** — the pre-1.0 "compute is
  pluggable" lock ([#271](https://github.com/BashfulBits/city-meeting-podcasts/issues/271)). New
  `citypods/compute/` module, peer of `storage/`: `base.py` defines `InferenceJob(task, inputs,
  recipe_hash)` — `task` typed for the **full §5.5 verb set** (ASR `transcribe`/`align`/`diarize`
  + the reserved R3/R4 LLM verbs `summarize`/`tag`/`soundbite-select`) — plus `JobResult`/
  `JobHandle` and a `runtime_checkable` `Backend` protocol `run_inference(job)`. `local.py` wraps
  the in-process faster-whisper/stable-ts path (**byte-identical** VTT + words.json output);
  `TranscriptStage` now routes inference through `backend.run_inference(...)`, and
  `make_compute` selects the backend from `compute_backend` (`site_config.yml` default `local`;
  `COMPUTE_BACKEND` env override). Behavior-preserving refactor — `ASR_PIPELINE_VERSION`
  unchanged. The seam H6b's lane split and H14's Modal/Beam **dispatch** adapters (which return a
  `JobHandle`) both build on.
- **Stage backlog manifest + configurable prioritization policy (H5)** — shipped across three PRs
  ([#263](https://github.com/BashfulBits/city-meeting-podcasts/pull/263) ·
  [#264](https://github.com/BashfulBits/city-meeting-podcasts/pull/264) ·
  [#265](https://github.com/BashfulBits/city-meeting-podcasts/pull/265)):
  - *Ordering engine (PR1)*: new `citypods/ops/workqueue.py` — a declarative `backlog_priority` policy
    (`site_config.yml`) of composable comparator keys: `recency` (with an optional `within_days`
    horizon that collapses beyond the window so the next key governs), `recent_first`, `city_order`
    (explicit slug list, partial lists fall through), `body_order`, `feed_visible_first`. `order()` is
    the identity with no policy, so wiring it into `_materialize_set` is byte-identical by default.
    Production runs `recency: {order: desc, within_days: 30}`.
  - *Work manifest + lean sidecar + status (PR2)*: `build_manifest` derives a `WorkItem` per
    (episode, output `work_class`) from records — `audio` / `transcript-asr` / `transcript-align`,
    tagged done/queued/alignment-disabled, bucketed feed_visible vs deep_archive — persisted to
    `state/work.json` (statesync-synced) with a `lease`/`release`/`is_leased` API (the H6b substrate).
    `/admin/status` gains a backlog-by-work-class block. `order_cities_by_policy` adds coarse
    cross-source ordering to the per-city pool.
  - *Global two-pass enrich queue (PR3)*: the time-bounded `enrich` phase becomes a global,
    policy-ordered queue — prepare all sources in parallel, then process the backlog
    **newest-everywhere-first across all sources** as an on-runner **audio pass**
    (`chapters→timeline→remap→audio`, gated by the H8/H11a `native_work_gate`) followed by a
    **decoupled transcript pass**. `all`/`render` keep the per-city pool. The transcript pass is
    **dispatch-not-await-ready**: transcription/diarization will run on external workers
    ("over the wall", H9/H6b), reconciled from durable state on a later deploy — per-episode
    `audio→transcribe→diarize` order is enforced by sequential dependency-gated passes in-run and by
    manifest `state` across runs; fused vs separate execution is the backend adapter's call via
    groupable leases. Design: [`review/12` §H5](review/12-hardening-and-efficiency.md).
- **Feed-health backlog triage + provider drift (H4)**: three sub-deliverables:
  - *Rehost-backlog triage*: `check_rehost_backlog` applies a three-tier model — catching-up
    (any hosted > 0, or pipeline not yet active enough) is **suppressed** (existing issues
    auto-close via reconcile); stalled (≥ 3 of last 5 runs encoded but feed still 0 hosted) is
    **`WARN`**; real provider failures stay `ERROR`. `_load_run_history` + `run_history` threaded
    through `audit_city` / `audit_all`. 6 new tests.
  - *Provider error-rate tracking*: `_record_run_history` in `run.py` now writes a
    `provider_errors: {name: count}` dict of city-level source-fetch failures per run to
    `run_history.jsonl`. `check_provider_error_rates` in `audit.py` raises a `WARN
    provider-errors:<name>` finding for any provider with failures in ≥ 2 of the last 5 runs,
    surfacing provider drift before it turns deploys red. 8 new tests.
  - *Auto-comment on state transitions*: `audit_feeds.py` now adds a timestamped comment to an
    existing issue whenever its computed body changes (state transition), in addition to updating
    the body — making transitions visible in the GitHub issue timeline. The close-on-resolve
    comment was already in place. 5 new tests in `tests/test_audit_feeds.py`.
- **Feed-validation publish gate (H3, #53)**: `citypods validate-build docs/` scans every
  generated `*.xml`, skips redirect feeds, demotes empty feeds for known-backfill-in-progress
  cities to warnings, and exits non-zero on structural errors or unexpectedly empty feeds.
  Wired as a gate step in `deploy.yml` after render, before the Pages artifact upload, so a
  malformed feed can't slip through to production. 11 new tests.
- **H2 projection wall-clock fix + per-run telemetry**: `per_run_cap` now defaults to `None`
  (wall-clock-bounded) when `materialize_budget_per_run` is absent; `measured_inputs` calibrates
  `sec_per_ep` from `materialize_encoded` (real encodes only, not cheap re-credits); `to_markdown`
  updated to say "delete the cap" rather than recommending the removed config key; `_feed_row` adds
  a bytes-based `hours_hosted` estimate for providers (Swagit/CivicPlus) that never supply duration
  metadata; `_ResourceHeartbeat` now samples `peak_load_per_cpu` + `min_mem_avail_bytes` via
  `current_snapshot()`, and `NativeWorkGate` accumulates `total_wait_seconds`; `_record_run_history`
  persists `peak_load_per_cpu`, `min_mem_avail_mb`, `window_used_pct`, and `gate_wait_seconds` to
  `run_history.jsonl`; `build_status` returns `audio_backlog` + `transcript_backlog` sub-dicts with
  ETAs so the status dashboard shows both queues without JS math.
- **H1 issue reconciliation**: closed GH#154 (`<podcast:transcript>` — 28 tags confirmed live in the Arlington TX feed); narrowed GH#110 (ASR transcripts) to backfill + ops follow-up only; marked GH#141 (timeline epic) umbrella-only for remaining Phase R features (#153/#155/#156/#157).
- **ASR benchmark workflow (H6a)**: added a manual `asr-bench.yml` workflow that runs
  `citypods asr-bench` over maintainer-selected `city:uid` cases, compares max/med/min
  model + beam-size + CPU-thread profiles under a capped runner budget, and publishes a text report
  artifact. The CLI now accepts `--beam-size` for targeted WER/speed checks.
- **Documentation architecture & handoff**: `VISION.md`, forward-looking `ROADMAP.md`, this
  `CHANGELOG.md`, `ARCHITECTURE.md`, `SECURITY.md`, `AGENTS.md` + `CLAUDE.md`; the living canonical
  design index `review/11-technical-design-roadmap.md` + breakouts `review/12–14`; the feature
  lifecycle / doc-update contract in `CONTRIBUTING.md`.
- **Contributor scaffolding (partial #57)**: PR template, feature-request + bug-report issue templates,
  and an `area:*` / `needs-*` GitHub label taxonomy.
- **Word-level transcript timestamps**: `citypods/asr.py` now passes `word_timestamps=True` to
  faster-whisper. `ASR_PIPELINE_VERSION` bumped to `"2"`; transcripts produced after this carry
  word-level timing, which speaker diarization (#7) and phrase-level search / clip selection need.
  *(Superseded by the **H12 transcript artifact rework** below (PR #253): the served VTT reverts to clean
  segment cues, a word-level JSON sidecar is added, and version-aware gradual re-transcription is wired —
  `ASR_PIPELINE_VERSION` is now `"3"`.)*

### Fixed
- **Transcript artifact rework (H12, [PR #253](https://github.com/BashfulBits/city-meeting-podcasts/pull/253))**:
  ASR now emits a clean **segment-cue VTT** for `<podcast:transcript>` (fixing #249's one-word-per-cue
  regression and ~5× size bloat) **plus a word-level JSON sidecar** (`…-asr-<recipe>.words.json`) for
  phrase search / clip selection / diarization. `ASR_PIPELINE_VERSION` → `"3"`; already-stored **ASR**
  transcripts re-transcribe gradually across enrich runs, while provider-supplied transcripts are never
  invalidated. The word-JSON key is content-addressed and protected from orphan-GC.
- **ASR alignment fallback (H10, PR #232)**: forced alignment now uses a stable-ts faster-whisper model
  that supports `.align()`, and any alignment failure falls back to fresh transcription instead of
  skipping caption-bearing episodes.
- **Runner resource guard (H8, PR #235)**: AAC ffmpeg encodes now pin `-threads`, heavy audio/ASR
  work waits for memory/load headroom before admission, and abandoned ASR daemon inference keeps its
  worker slot until the native thread exits instead of stacking new CPU/RAM work on top.
- **ffmpeg audio memory guard**: silence detection ignores video streams, source-cache/identity/filter
  ffmpeg phases log distinct start/finish markers, and audio ffmpeg children stop when runner memory
  falls below the configured floor instead of risking an Actions lost-comms kill.
- **Deploy resilience — native work gate + one-slot audio lane (H11a, PRs #239/241/242/243/244)**:
  `NativeWorkGate` strictly serializes ASR and audio so they never run concurrently; `native_audio_max_active`
  caps the global ffmpeg encode slots; ffmpeg filter/complex threads pinned to 1; per-child peak RSS and
  minimum runner `MemAvailable` logged per encode; ASR teardown hardened to avoid post-state-push crashes.
  Together with H8, enrich now completes its full 204-min window without exit-143/lost-comms kills.
- **Audio concurrency tuning (H11a, PR #246)**: raised `native_audio_max_active` from 1 → 4 after 3
  consecutive green scheduled runs; at `-threads 1` per encode, 4 slots saturate all 4 cores while
  targeting ~8 GiB RAM.
- **`audio_ffmpeg_threads` auto-calc divisor ([PR #257](https://github.com/BashfulBits/city-meeting-podcasts/pull/257))**:
  the auto-calc for per-encode ffmpeg thread count divided `cpu_count` by `max_encodes_per_source`
  (per-source limit, default 1) instead of `native_audio_max_active` (global encode slots, currently 4).
  Latent bug — production pins `audio_ffmpeg_threads: 1` explicitly so it was never triggered, but
  clearing that pin would have assigned 16 threads to 4 cores. Config comment and regression test added.
- **HTTP Retry-After clamp (PRs #247/[#254](https://github.com/BashfulBits/city-meeting-podcasts/pull/254))**:
  the shared session honors `Retry-After` but **caps it at 120s** rather than obeying it verbatim — a
  Granicus 429 returning `Retry-After: 3600` previously caused urllib3 to sleep inside the retry loop for a
  full hour, blocking the entire build. Short, legitimate delays are still respected; a request that keeps
  failing surfaces as a `ProviderError` for the next scheduled run. (#247 first ignored the header; #254
  clamps instead, so a well-behaved provider's backoff is still honored.)
- **Granicus archive-video URL resolution (PRs #245/#250/#251)**: the adapter now bypasses the broken
  `DownloadFile.php` path by pre-following its redirect to the signed `archive-video.granicus.com` URL and
  handing ffmpeg the signed URL directly; concurrent-access `403`s (Granicus rate-limits with `403`, not
  `429`) are retried with backoff+jitter. Resolves the recurring Granicus `403` enclosure failures.

## Timeline & content-transform foundation

### Added
- **Served↔source timeline/EDL model** (`citypods/timeline.py`) unifying silence-trim, concat, intros,
  transcripts, and clips behind one served-vs-source time map (design: `review/08`; audit: `review/09`;
  INFRA-1..9, epic #141).
- **Silence trimming** (`citypods/silence.py`, `trim_silence`) — removes long lead/trail/mid-meeting
  dead air, remapping chapters and transcripts onto the served audio (#22/#111).
- **EBU R128 loudness normalization** (`audio_loudness_profile: ebuR128:-16LUFS`) (#21).
- **Multi-segment concat** (`citypods/concat.py`) for meetings split across source segments (#122).
- **Clip / soundbite extraction** (`citypods/clips.py`) — forward-maps a served range to source cuts.
- **ASR transcripts** (`citypods/asr.py`) — reuse provider transcripts first; otherwise forced alignment
  (stable-ts) or fresh transcription (faster-whisper); `asr-bench` CLI for WER/throughput (#1/#110).
- **`<podcast:transcript>`** emission for synced hosted transcripts (#11/#154).
- **Operational status dashboard** at `/admin/status/` rendered by `citypods report` (#124).

### Changed
- **Materialization budget replaced** with a wall-clock window + graceful yield: a run processes
  recordings until a shared `stop()` predicate (time window spent, or a newer Build & Deploy run queued).
  Removed `materialize_budget_per_run` / per-source count budgets (PR #128).

## Episode-record & enrichment-stage foundation

### Added
- **Append-only archive** (`records.merge_persisted`): meetings that drop off a provider feed (Granicus
  100-item cap, Swagit windowing) are retained and rendered from the full store (#52).
- **Stable episode UID** (author+body+date), **content-addressed audio keys**, and **split hashes**
  (`audio_spec_hash` vs `feed_content_hash`) for independent re-encode vs re-render invalidation.
- **Enrichment-stage pipeline** (`citypods/stages.py`): `EnrichmentStage` Protocol + `default_stages()`;
  adding a feature = adding a stage.
- **Stable feed URLs** — aliases + `<itunes:new-feed-url>` + redirect map for provider migrations.
- **No-cost feature stages**: resource/agenda links + `content:encoded` notes; chapters across capable
  providers; universal Podcasting 2.0 `<podcast:chapters>` sidecars.
- **Durable bucket-backed state** (`statesync.py`): object storage is the source of truth; Actions cache
  is latency only.
- **Resource cost/throughput projection** (`projection.py`) + `citypods report` + static what-if admin
  page; persisted `run_history.jsonl`.

## Scale, QA & discovery foundation (Phase 5 PR-A)

### Added
- **Feed-health audit** (`citypods/audit.py`, `scripts/audit_feeds.py`, `audit.yml`): staleness,
  view-cap, enclosure liveness, empty-feed, rehost-backlog checks → idempotent GitHub issues.
- **Endpoint contract tests** (`contracts.yml`, opt-in `@pytest.mark.live`) kept out of PR CI.

### Security
- **SSRF / source-URL gate** (`validate_source_url`), per-provider host allowlists, bounded
  redirects/response-size; **ffmpeg `-protocol_whitelist`**; **defusedxml** provider parsing; fetch
  retry/backoff; alias/slug collision validation.

## Earlier foundations (Phases 0–4)

### Added
- Python `citypods/` package; provider adapters for **Granicus, CivicPlus/CivicMedia, CivicClerk,
  Swagit**; one-feed-per-board generation.
- Audio materialization pipeline (ffmpeg → M4A) with **Backblaze B2 + Cloudflare CDN** (free egress).
- Static frontend (Jinja2 → `docs/`): index with instant search + group-by-city accordion, per-city
  feed pages with inline player + subscribe links, generated cover art, custom domain.
- Offline pytest suite with byte-for-byte feed snapshots; CI (`ci.yml`), per-PR preview (`preview.yml`),
  scheduled deploy (`deploy.yml`, 4h cron); incremental builds + content-hash change detection.
- **LLM context ceilings are now explicit per physical route.** The provider registry no longer
  falls back to provider-wide input/output limits; every route carries the verified model ceiling,
  including gateway-specific caps such as OpenRouter's free Gemma route. The compiler rejects a
  route missing either limit, and the generated Python/Worker catalogs are regenerated from those
  values.

- **DeepSeek V4 Flash now uses the current direct API model identifier.** The physical route sends
  `deepseek-v4-flash` while retaining the `-0731` logical alias for compatibility; the retired
  direct API identifier is no longer emitted in the compiled route catalog.

- **OpenCode model routing is corrected.** OpenCode's free DeepSeek aliases now send
  `deepseek-v4-flash-free`. The proposed LongCat route was removed because the official API
  requires authentication/billing and OpenCode Zen does not advertise a free LongCat model.
