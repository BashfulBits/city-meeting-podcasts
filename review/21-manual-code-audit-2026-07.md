# Manual code audit (CodeRabbit CLI unavailable) — 2026-07

Follow-up to [`review/19-coderabbit-findings-audit.md`](19-coderabbit-findings-audit.md). That audit's
procedure calls for running the actual `coderabbit` CLI against a synthetic full-repo diff. This run
could not do that: the execution environment's egress policy blocked `cli.coderabbit.ai` (worked around
once allowlisted), but every API key available in this session was a regular CodeRabbit *user* key —
the CLI explicitly rejects those ("User API keys are not supported for the CLI. Please generate an
agentic API key from your CodeRabbit settings"), and no agentic/CLI key was obtainable in this session.

Rather than skip the audit, this run substitutes a **manual equivalent**: the same six directories
`review/19` covered, reviewed line-by-line by five parallel sub-reviews (three splitting `citypods/`,
one for `scripts/`, one covering `tests/`+`workers/`+`templates/`+`.github/`), each explicitly primed
with `review/19`'s existing findings (so it wouldn't re-report anything already `fixed`/`Confirmed no
action`/deferred) and with this repo's documented invariants (SSRF gate, append-only merge contract,
stop-budget semantics, split hashes, untrusted-LLM-output rule) so it wouldn't flag intentional design
as a bug. Every finding rated **major or above** below was then independently re-verified against the
current source by reading the actual code paths (not just trusting the sub-review's claim) before being
recorded here — in two cases (the Swagit/CivicPlus SSRF findings) that verification changed the
assessment from what was initially reported (see MR-CP-01/MR-CP-02 rationale).

**No raw CLI output exists for this run** (there was none to capture), so unlike `review/19` there is no
NDJSON appendix — the tables below are the full record.

## Run metadata

- Date: 2026-07-03
- Method: 5 parallel manual sub-reviews (general-purpose agents) + direct verification of every
  major/critical finding by the coordinating session
- Directories reviewed: `citypods/` (split into network/security, core pipeline, CLI/orchestration
  batches), `scripts/`, `tests/`, `workers/`, `templates/`, `.github/` — same scope as `review/19`
- Skipped: `config/`, `review/` — same rationale as `review/19`
- Base for comparison: `review/19-coderabbit-findings-audit.md` (all 129 prior IDs); items already
  `fixed`, `Confirmed no action`, or `Already fixed` there are treated as closed and are **not**
  re-reported below unless a regression was found (none were). Items `Confirmed valid; deferred to
  GH#NNN` are cross-referenced by ID where a sub-review rediscovered the same gap.

## Summary

| Directory | New findings | Recommend fix | Recommend defer (valid, low priority) |
|---|---|---|---|
| `citypods/` | 5 | 5 | 0 |
| `scripts/` | 7 | 6 | 1 |
| `.github/` | 5 | 5 | 0 |
| `templates/` (+ `citypods/site.py`) | 2 | 1 | 1 |
| `tests/` | 1 | 0 | 1 |
| `workers/` | 0 | — | — |
| **Total** | **20** | **17** | **3** |

Two findings worth flagging first: **`MR-CP-01`** (a legacy multi-segment Swagit meeting's per-segment
duration probe shells out to `ffprobe` on a page-scraped URL with **no SSRF gate at all** — the one
unambiguously unguarded network fetch found in this pass) and **`MR-CP-03`**/**`MR-CP-04`** (two more
call sites, beyond the one `review/19` already fixed as CR-CP-03, where a presigned/credentialed media
URL can be embedded verbatim into text that becomes a public GitHub issue).

## `citypods/` findings

| ID | Severity | File:Line(s) (current) | Issue | Recommendation | Rationale |
|---|---|---|---|---|---|
| MR-CP-01 | **critical** | citypods/concat.py:171-176 (`_probe_duration_url`, called from `SwagitConcatPlanner`) | Legacy multi-segment Swagit duration probe runs `subprocess.run(["ffprobe", ..., url])` directly on page-scraped `dfile` URLs with zero SSRF gate, no `validate_source_url` call, and no size-preflight | **Fix** | Confirmed by reading `concat.py` end-to-end: `seg_objs` comes straight from `provider.fetch_segment_objects()` (Swagit's regex-scraped `dfile` URLs from `/videos/{id}` page HTML) and is passed to `_probe_duration_url`, which calls `subprocess.run` on the raw string with no validation anywhere in the function or its caller. This is a different (and more clearly unguarded) code path than the AudioStage encode path — see MR-CP-02 for why that one is *not* fully unguarded. Fix by adding a `validate_source_url(url, resolve=True)` call (catching `SecurityError` to skip/defer the segment) before the `subprocess.run` in `_probe_duration_url`. |
| MR-CP-02 | major | citypods/providers/swagit.py:253-304 (`resolve_media_url`, `_page_segment_objects`), citypods/providers/civicplus.py:155-165 (`_find_hls_url`) | Neither provider calls `validate_source_url` on its resolved/redirect/scraped media URL before returning it, unlike `granicus.py:151-160`'s explicit fix for the identical shape (CR-CP-35) | **Fix** | Verified this is *not* a full unguarded exposure on the mainline single-source `AudioStage` encode path, contrary to the initial sub-review read: `SourceCache.get_or_fetch` → `_download_audio` (citypods/media.py:731-736) and the direct-render path's `_preflight_remote_source` (media.py:792-802) both call `preflight_media_size`, whose HEAD/GET runs through `make_session()`'s `GuardedHTTPAdapter`, which calls `validate_source_url(request.url, resolve=True)` on every request unconditionally (citypods/http.py:274) — so as long as `source_media_max_bytes` is non-zero (default 54 GB, `citypods/media.py:112`), a malicious redirect/scrape target *is* caught before ffmpeg touches it, incidentally, as a side effect of the #497 size-ceiling work. That said, this protection (a) is undocumented as an SSRF control, (b) vanishes entirely if an operator ever sets `source_media_max_bytes: 0`/`None` (the docstring says this "disables the preflight guard"), and (c) doesn't cover MR-CP-01's concat/duration-probe path at all. Recommend the same explicit, unconditional `validate_source_url` call Granicus already has, as defense-in-depth that doesn't depend on an unrelated size-cap setting. |
| MR-CP-03 | major | citypods/audit.py:562 (`check_enclosures` self-heal re-resolve) | `detail = f"re-resolved to {new_url!r}: HTTP {new_status}"` embeds the full, unredacted re-resolved URL — which can be a presigned Swagit S3 URL with `AWSAccessKeyId=`/`Signature=`/`Expires=` in the query string — into a `Finding.message` that `scripts/audit_feeds.py` files verbatim into a public GitHub issue body/comment | **Fix** | Same leak class CR-CP-03 fixed at `contracts.py:70`, but this call site wasn't covered by that fix. Confirmed `resolve` is wired to `provider.resolve_media_url(ep, source)` (audit.py:1412), which returns presigned URLs for Swagit. `availability_digest.py` already has the right pattern next to it (redacts via `redact_subprocess_text`) — reuse that helper (or `contracts.py`'s `_media_fetch_detail`-style redaction) before building `detail` here. |
| MR-CP-04 | major | citypods/contracts.py:105-107 (`check_city`, "media" resolution check) | `out.append(_r(provider_name, slug, "media", ok, resolved_url[:80]))` truncates but does not redact the resolved URL; the sibling "media-fetch" check a few lines later routes through `_media_fetch_detail()`, which explicitly strips the query string | **Fix** | Confirmed: truncating to 80 characters does not remove a query string — for a short host+path, the first 80 characters can still contain the start of `?AWSAccessKeyId=...`/`Signature=...`. This finding's `detail` becomes GitHub issue body text via `scripts/check_endpoints.py`. Fix by routing this check's detail through the same redaction helper `_media_fetch_detail` already uses. |
| MR-CP-05 | trivial | citypods/silence.py:522 | `except (ProviderError, Exception):  # noqa: BLE001` — `ProviderError` is already covered by `Exception`, same redundant-clause pattern CR-CP-26 fixed in `concat.py` | **Fix** (bundle with other cleanup) | Purely cosmetic; no behavior change from simplifying to `except Exception:`. Not independently worth a PR, but cheap to fold into whichever fix PR touches this file next. |

## `scripts/` findings

| ID | Severity | File:Line(s) (current) | Issue | Recommendation | Rationale |
|---|---|---|---|---|---|
| MR-SC-01 | major | scripts/probe_granicus_worker.py:95-105 (`_run_production_encode`) | The newer (#497) production-encode path only catches `MediaSourceTooLargeError`, not `RateLimitedMediaFetchError`; a throttled fetch propagates uncaught, aborting the whole probe run and discarding every already-collected result | Fix | `SourceCache.get_or_fetch`'s own docstring (citypods/media.py:927) documents that throttling "propagates as `RateLimitedMediaFetchError`," and neither this call site nor `main()` (line 356) catches it. `args.output.write_text()` only runs after the call returns, so this reintroduces the exact failure mode CR-SC-27 already fixed elsewhere in this same file — just in newer code that fix predates. |
| MR-SC-02 | major | scripts/gc_audio.py:250-254 | `storage.delete(key)` in the per-object GC sweep loop has no try/except; one transient delete failure aborts the whole sweep with no partial report | Fix | `S3CompatibleStorage.delete()` (citypods/storage/s3.py:211-212) has no internal error handling, so any boto3/network error propagates out of the `for key, ... in storage.iter_objects(...)` loop; `_write_report()`/the summary only run after the loop finishes. Same "one failure kills a multi-item batch" shape CR-SC-07/CR-SC-27 already fixed in sibling scripts. |
| MR-SC-03 | major | scripts/check_endpoints.py:102-131 | Issue-closing is scoped to the current run's target set (representative-city-only on the scheduled cron, per `citypods/contracts.py:216-222`'s `representative_cities()`), but treats "not present in this run" as proof the issue is resolved — so a manual broader `--all --issues` run's issue for a non-representative city gets auto-closed by the next scheduled representative-only run without that city ever being re-checked | Fix | Issue titles are keyed only by `f"{provider}: {endpoint}"` with no city/slug, so there's nothing distinguishing "checked and passing" from "not checked this run." Same scoping-mismatch shape as CR-SC-29 (`audit_feeds.py`), just expressed across two runs with different scopes instead of within one run. Recommend keying issues by `(provider, endpoint, slug)` or carrying forward an `audited_slugs` set the way `audit_feeds.py`'s CR-SC-29 fix now does. |
| MR-SC-04 | minor | scripts/audit_feeds.py:667-670 | The CR-SC-29 fix's `prior_slugs` derivation reads only a hidden JSON state block (`first_seen`) from the prior issue body; if that block is missing/corrupted (only possible for an issue predating the fix), `prior_slugs` silently collapses to `set()` and out-of-scope cities can drop out without re-evaluation | **Defer** (low priority) | Confirmed the code has no fallback to `_parse_prior_rows(prior_body)` for deriving which slugs exist if the JSON block is absent. Not reachable for any issue created under current code — only a pre-fix issue with a manually-stripped body could trigger it. Worth a defensive fallback eventually, not urgent enough to prioritize now. |
| MR-SC-05 | minor | scripts/refresh_fixtures.py:28-42 | Per-city fetch loop has no per-city try/except; one city's failure aborts the whole fixture-refresh run | Fix (cheap) | Same shape CR-SC-07 fixed in `fetch_seals.py`'s near-identical loop. Lower stakes than that fix (manual dev tool, not CI-invoked), but consistent and cheap to add. |
| MR-SC-06 | minor | scripts/validate_control_plane.py:254-256 | `open(args.output, "w")` has no parent-directory mkdir guard, unlike the mkdir-before-write pattern CR-SC-02/05/26 already applied to sibling scripts' output writes | Fix (cheap) | Low risk today since the one CI call site (`validate-control-plane.yml`) passes a flat filename, but a nested `--output` path would crash after the validation run completes, losing the report — the exact rationale CR-SC-05 used to justify the analogous fix. |
| MR-SC-07 | trivial | scripts/availability_digest.py:70 | `except (ProviderError, Exception):` — redundant clause, same pattern as CR-CP-26/MR-CP-05 | Fix (bundle with other cleanup) | Cosmetic only. |

## `.github/` findings

| ID | Severity | File:Line(s) (current) | Issue | Recommendation | Rationale |
|---|---|---|---|---|---|
| MR-GH-01 | major | .github/workflows/clear-materialization.yml:19-28,72-73, reset-backoff.yml:42-46,97 | No default-branch guard on the destructive `apply`/`delete_objects` `workflow_dispatch` inputs, unlike `audio-gc.yml`'s fixed CR-GH-04 pattern | Fix | Confirmed via direct grep: `audio-gc.yml:80-82` explicitly checks `github.ref != 'refs/heads/main'` before honoring `apply=true`; neither `clear-materialization.yml` nor `reset-backoff.yml` has any `github.ref` reference at all. Anyone who can dispatch a workflow from a feature branch can run these against production state/objects. Fix: add the same branch guard used in `audio-gc.yml`. |
| MR-GH-02 | major | .github/workflows/deploy.yml:49-64 (`build-deploy` job), clear-materialization.yml, availability-digest.yml, reset-backoff.yml, asr.yml `reconcile` job | Job-level `env:` exposes B2/R2/Cloudflare (and, for availability-digest.yml, Granicus) secrets to steps that never touch storage | Fix | Confirmed on `deploy.yml` directly: `checkout`, `setup-python`, the build-state `cache` restore, `configure-pages`, `upload-pages-artifact`, and `deploy-pages` steps all inherit the job-level B2/R2 env even though only the "Render feeds" step (which internally restores state from the bucket via `statesync.py`) needs it. CR-GH-07/23/25 already fixed the identical pattern in `audio-gc.yml`/`asr.yml`'s `asr` job/`audio.yml` — this is the same fix, applied to the remaining jobs it was never extended to. |
| MR-GH-03 | major | actions/cache@v5 in asr.yml, audio.yml, ci.yml, deploy.yml; actions/configure-pages@v6, actions/upload-pages-artifact@v5, actions/deploy-pages@v5 in deploy.yml; actions/upload-artifact@v7 in granicus-probe.yml; actions/setup-node@v6 + cloudflare/wrangler-action@v3 in granicus-worker-deploy.yml | Mixed SHA-pinning: several actions still on floating version tags while the identical action is SHA-pinned elsewhere in the same repo | Fix | E.g. `actions/cache@v5` is unpinned in the four files above but SHA-pinned (`actions/cache/restore@27d5ce7f...`) in `asr-bench.yml`/`preview.yml`; `actions/upload-artifact@v7` is unpinned in `granicus-probe.yml` while five other workflows pin the same action to `043fb46d1a93c77aae656e7c1c64a875d1fc6a0a`. Pin all listed instances to the SHAs already in use elsewhere in this repo for the same action/version. |
| MR-GH-04 | minor | tests/test_workflows.py:237-248 (`test_checkout_and_setup_python_are_sha_pinned_everywhere`) | Docstring claims this is a "blanket supply-chain policy" guarding the whole workflow directory, but the check only matches `actions/checkout@`/`actions/setup-python@` lines — which is exactly why the MR-GH-03 pinning gaps went undetected | Fix | Recommend broadening the regex to catch any third-party `uses:` line with a bare `@vN`/`@main` ref repo-wide (turning this into the actual blanket check its docstring already claims to be), rather than just narrowing the docstring to match current (narrower) behavior. |
| MR-GH-05 | minor | .github/workflows/ci.yml | No `concurrency:` group, despite running on every `pull_request` push — the highest-frequency trigger in the repo | Fix (cheap) | Every other workflow in the directory has a `concurrency:` block; rapid pushes to the same PR currently run overlapping CI jobs to completion instead of canceling superseded ones. Add `concurrency: group: ci-${{ github.ref }}` / `cancel-in-progress: true`, mirroring `preview.yml`. |

## `templates/` (+ `citypods/feeds.py`, `citypods/site.py`) findings

| ID | Severity | File:Line(s) (current) | Issue | Recommendation | Rationale |
|---|---|---|---|---|---|
| MR-TM-01 | major | templates/city.html.j2:41, templates/feed.xml.j2:33, via citypods/feeds.py `ordered_links()`/`episode_resource_links()` and citypods/providers/granicus.py:199 (`link = _text(item, "link")`) | Provider-supplied RSS `<link>` values are rendered into `href` attributes with only HTML-entity escaping (Jinja autoescape / `html.escape()`); no URL scheme is ever validated | Fix | Confirmed: `granicus.py` takes the upstream RSS `<link>` element verbatim with zero scheme check, and `feeds.py`'s `ordered_links()` does no filtering either — the codebase's only scheme gate, `security.py`'s `ALLOWED_SCHEMES = {"https"}`, is applied solely to URLs the app itself *fetches*, never to display-only links. A malformed/compromised upstream feed containing `<link>javascript:...</link>` would render as a clickable `javascript:` href on the city page and inside podcast show notes. Distinct from the already-closed CR-TM-02 (which was specifically about the internally-built feed-directory href, not per-episode provider links). Fix: reject or drop non-`http(s)`-scheme links in `ordered_links()`/`episode_resource_links()` before they reach any template. |
| MR-TM-02 | trivial | citypods/site.py:16-24 (`render_redirect_page`) | Builds raw HTML via f-string interpolation with no escaping, inconsistent with every other page (which goes through Jinja's autoescape) | **Defer** (heads-up only) | Not currently exploitable — `new_page_url` is built only from trusted, operator-configured `base_url` + the city's own internal slug, never provider/attacker-controlled data. Flagged so it isn't overlooked if that composition ever changes to include less-trusted input; no action needed now. |

## `tests/` findings

| ID | Severity | File:Line(s) (current) | Issue | Recommendation | Rationale |
|---|---|---|---|---|---|
| MR-TS-01 | trivial | tests/test_snapshot.py:40-46 (`_city()`) | Reloads and re-parses the entire `config/cities/*.yml` tree from disk on every parametrized snapshot case instead of loading once and looking up by slug | **Defer** (optional) | Redundant I/O only (~12 snapshot cases), not a correctness bug. Not worth a dedicated PR; fold in only if `test_snapshot.py` is touched for another reason. |

## `workers/` findings

None. All five `review/19` `workers/` items (CR-WK-01 through CR-WK-05) were re-verified and remain
fixed/valid-as-closed at current `HEAD`: constant-time-ish bearer-token comparison, `WWW-Authenticate`/
`Allow` headers present, 304 passthrough correct, `parseArchivePath` rejects `..`/percent-encoding, and
`head_sampling_rate` is a sane production value. No regressions, no new findings.
