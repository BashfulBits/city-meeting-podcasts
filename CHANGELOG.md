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

### Fixed

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

- **One-off body naming drift now has an exact exception path and audit coverage.** A feed may use
  `source.body_includes` for provider-GUID-specific rows without permanently broadening its body
  selector. Feed-health audits suppress historical excluded labels, flag recurrence of a known
  one-off label with its prior inclusion GUID, and flag newly observed excluded labels so city
  configurations can stay current. Fort Worth's single `Work Session` recording is covered this
  way (GH#1005); no pipeline-version bump or artifact backfill is required.

### Added

- **Read-Only Chapter Locator Shadow Report & Rollout Controls.** ([`review/40`](review/40-generated-agenda-chapters.md))
  Added offline quality evaluation reporting and pure, disabled-by-default rollout cohort controls for generated agenda chapters (GH#1078):
  - `report_locator_shadow.py` joins completed locator shadow runs to hidden gold chapters and scoring crosswalks without mutating episode state, reporting timing-only recall/precision, greedy one-to-one strong-crosswalk item precision, boundary errors, abstentions, and operational metrics.
  - `evaluate_gate` evaluates operator-supplied quality gates with strict threshold validation (positive integer episode counts and bounded `[0, 1]` rates) and requires explicit `provider_labels_in_requests: false` attestation.
  - `citypods.chapter_rollout.ChapterRolloutPolicy` provides immutable, bounded rollout controls (`providers`, `bodies`, `max_duration_seconds`, `max_episodes_per_run`) that enforce per-run limits statelessly and downgrade overlay to shadow unless an independent shadow gate passes.

- **Reversible Served-Time Chapter Overlay & Publication Controls.** ([`review/40`](review/40-generated-agenda-chapters.md))
  Added publication-layer overlay for generated agenda chapters without altering underlying audio bytes or
  overwriting authoritative provider chapter markers:
  - `episode_public_chapters` returns canonical provider chapters when present, and overlays validated generated
    chapters only when `include_generated=True` (controlled by `generated_chapters_enabled` in `config/site_config.yml`).
  - Chapter start timestamps are strictly validated and normalized against booleans, non-numeric values, negative
    offsets, and non-finite floats.
  - Podcasting 2.0 JSON sidecars (`chapters_json`) and meeting permalinks (`render_meeting_page`) consistently round
    start times to whole-second integers per the Podcasting 2.0 specification.
  - `feed_content_hash` incorporates `include_generated_chapters` and public chapter state so toggling publication
    flags triggers sidecar and RSS re-rendering.
- **GH#1092 agenda-text quality gate and selective OCR.** `AgendaTextStage` now classifies native
  extraction, probes suspicious PDFs with bounded Poppler/Tesseract OCR, rejects placeholders and
  ambiguous documents, retains genuine short notices as chapter-ineligible diagnostics, and records
  versioned quality evidence. The feed-health audit consolidates repeated ambiguity into one
  maintainer issue and auto-closes it after recovery. The stage/version bump gradually re-evaluates
  existing agenda artifacts on normal enrich runs; OCR replaces native text only when clearly better,
  and no separate bulk backfill is performed. A rejected replacement document retains the prior
  accepted artifact while recording the new diagnostic for feed-health alerting. The audio-runner
  image is v2 with the OCR binaries; the host fallback and CI smoke checks verify the same tools.

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

- **Compact state manifest was incorrectly sent to B2 CAS stubs.** `state/catalog/manifest.json`
  now routes to the R2 coordination backend, where conditional publication is supported, while the
  indexed durable state remains on B2. This removes the repeated `backend 'b2' is not cas_capable`
  warnings and lets fresh workers rebuild/publish the manifest when the R2 copy is absent; no
  durable state backfill is needed. The ASR reconcile path now seeds a complete R2 manifest after
  its B2-list fallback restore, and scoped pushes refuse to publish an incomplete first manifest.

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
