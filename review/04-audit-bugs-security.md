# Bug & Security Audit

Severity: 🔴 high (broken / exploitable) · 🟠 med (latent / scale) · 🟡 low (hygiene).
Each item has a concrete fix. The deploy path is healthy; most items are dev-tooling or
forward-looking (Phase 5 trust boundary).

## Bugs

### 🔴 B1 — `scripts/generate_board_cities.py` is broken (stale import)
`from citypods.media import _source_key` — `_source_key` was removed in the R1 refactor
(replaced by `records.source_key`). The script `ImportError`s immediately:
```
ImportError: cannot import name '_source_key' from 'citypods.media'
```
This is the tool that onboards per-board feeds, so per-board scaling is currently blocked.
**Fix:** `from citypods.records import source_key` and replace the two `_source_key(c)` calls with
`source_key(c)` (signature is identical: takes a `City`). One-line change; add a smoke test that
imports every `scripts/*.py` so this class of breakage is caught in CI (see doc 05).

### 🟠 B2 — No alias/slug collision validation (can clobber a real feed)
`load_city_configs` checks duplicate **slugs** but not **aliases**. `_write_aliases` runs *after*
`_process_city` in `build()` and writes `docs/<alias>/audio_feed.xml` + `index.html`. If a city's
`aliases:` contains another city's **slug** (or two cities share an alias), the alias write
**overwrites the real feed with a redirect stub** — silently. Also two cities sharing an alias =
last-writer-wins.
**Fix:** in `load_city_configs`, after collecting slugs, assert each alias is unique and disjoint
from the set of all slugs; raise `ValueError` otherwise. Add a test.

### 🟠 B3 — Stale per-source records leak in the bucket (storage + GC correctness)
`statesync.push_state` uploads all local `state/**.json` but **never deletes remote objects** that
no longer exist locally. If a city's `source` is edited, its `source_key` changes; the old
`sources/<old_key>/episodes.json` remains in the bucket forever and `pull_state` keeps restoring it.
Two consequences: (a) unbounded slow growth of stale record files; (b) `referenced_audio_keys`
includes the stale keys, so `gc_audio` **won't reclaim** the now-unreferenced audio those stale
records point at.
**Fix:** make `pull_state`/`push_state` reconcile — after pushing, list remote `state/` objects and
delete those with no local counterpart (age-guarded, like gc_audio). Or: key records by a stable id
that doesn't change with source edits. Lower urgency (source edits are rare) but it's a real leak.

### 🟡 B4 — `_materialize_set` / chapters fetched for non-hosted direct feeds
`ChaptersStage` fetches chapter pages for every provider with `fetch_chapters`, including the 32
direct-MP4 Granicus feeds we don't re-host. This is **not wasteful anymore** now that
`<podcast:chapters>` sidecars surface chapters without hosting (PR #22) — noting it only to confirm
it's intentional. No fix needed; documented here so it isn't "fixed" by mistake.

### 🟡 B5 — `min_meetings`/`min_samples` edge in staleness check
`check_staleness` returns `None` when `median <= 0` (all same-day) — correct — but a feed with
exactly `min_samples` very bursty meetings then a long real gap can still under-flag. Minor; the
`floor_days=30` floor mostly covers it. No change recommended; documented for awareness.

## Security

### 🔴 S1 — SSRF / abuse surface opens at Phase 5 (currently mitigated by trust)
**Today this is fine:** every `source` URL is maintainer-authored, so `fetch_episodes` only hits
URLs you wrote. **The moment Phase 5 onboards cities from GitHub-issue submissions**, the build
(which runs with repo write + B2 secrets in env) will fetch attacker-influenced URLs. Risks:
- SSRF to internal/cloud-metadata IPs (`169.254.169.254`), `file://`, localhost.
- Redirect to an internal host (Swagit's `resolve_media_url` already follows redirects; CivicPlus
  chases watch-page → embed → m3u8).
- Resource exhaustion (huge response / slow-loris) — timeouts exist (30s) but no size cap.
**Fix (prereq for Phase 5, doc 02 Change 9):** a `validate_source_url()` gate — https-only, per-provider
host allowlist (`*.granicus.com`, `*.swagit.com`, `*.api.civicclerk.com`, plus the city's own
domain for CivicPlus), resolve-and-reject private/loopback/link-local IPs, cap redirects and
response size. Enforce at config load and before each fetch.

### 🟠 S2 — `ffmpeg -i <url>` has no protocol whitelist
ffmpeg is invoked on a provider-resolved media URL with no `-protocol_whitelist`. ffmpeg supports
many protocols (file, concat, etc.); a malicious redirect/manifest could, in theory, coax it to
read local files via an HLS playlist referencing `file:` segments.
**Fix:** pass `-protocol_whitelist file,crypto,data,http,https,tcp,tls` (the set needed for HLS/MP4
over HTTPS) and `-allowed_extensions` as appropriate. Cheap defense-in-depth; do it alongside S1.

### 🟠 S3 — XML parsing not hardened (defusedxml)
`granicus.py` and `civicplus.py` use stdlib `xml.etree.ElementTree.fromstring` on provider feeds.
Modern CPython doesn't resolve external entities by default (no classic XXE), but stdlib ET is not
hardened against entity-expansion ("billion laughs") in all versions, and the feeds become
untrusted at Phase 5.
**Fix:** depend on `defusedxml` and use `defusedxml.ElementTree.fromstring`. Tiny, removes a class
of risk. Do with S1.

### 🟡 S4 — `gh` token scope in `audit.yml`
`audit_feeds.py` shells `gh` with `GITHUB_TOKEN`/`issues: write`. Fine and minimal. Just confirm the
workflow token stays `issues: write` only (it does). No change.

### 🟡 S5 — No secret scanning / no secrets committed (verified)
Grepped `citypods/`, `scripts/`, `cities/`, `*.yml` for AWS/B2/api-key patterns — **none found**.
Credentials are correctly env-only via Actions secrets. Recommend enabling GitHub secret-scanning
+ push protection on the repo (free for public) as a backstop.

## Robustness (not strictly bugs)

### 🟠 R1 — No fetch retry/backoff
A single transient 5xx/timeout marks a city `error` for the whole run and can file a false-positive
health issue. **Fix:** wrap `make_session` with `urllib3.Retry` (e.g. 3 retries, backoff, on
429/5xx). Cheap; reduces audit noise at scale. (Feature #38.)

### 🟠 R2 — No response-size cap on downloads
`session.get(...).content` reads the whole body into memory; a hostile/huge response could OOM the
runner. **Fix:** stream + cap (e.g. refuse > N MB for list/JSON endpoints). Pairs with S1.

### 🟡 R3 — Broad `except Exception` in two spots
`audit.check_enclosures` and `stages.*` catch `Exception` (intentional, `noqa`'d) so one bad
item/page doesn't fail a run — acceptable. Just ensure the swallowed error is always recorded in
`StageStats.errors`/findings (it is).

### 🟡 R4 — `request_delay_seconds=0.1` global politeness, but no per-host rate limit
At 80 feeds across ~4 platforms this is fine. At 1,000 feeds many share a Granicus/Swagit tenant
host; concurrent workers could hammer one host. **Fix (scale):** per-host token bucket. (Feature #39.)

## Summary table

| ID | Sev | One-line | Fix size |
|----|-----|----------|----------|
| B1 | 🔴 | `generate_board_cities` stale `_source_key` import | S (1 line) |
| B2 | 🟠 | alias can clobber a real feed; no uniqueness check | S |
| B3 | 🟠 | stale per-source records leak in bucket → GC can't reclaim audio | M |
| S1 | 🔴* | SSRF/abuse when sources become user-submitted (Phase 5) | M |
| S2 | 🟠 | ffmpeg no `-protocol_whitelist` | S |
| S3 | 🟠 | use defusedxml for provider XML | S |
| R1 | 🟠 | fetch retry/backoff | S |
| R2 | 🟠 | response-size cap | S |

\* S1 is 🔴 *conditional on Phase 5*; today it's mitigated by the maintainer-only trust boundary.

**What I'll fix in phase 3 (code branches):** B1 (+ scripts import smoke test), B2, S2, S3, R1.
B3 and S1 are larger/forward-looking — I'll write the fix but flag them for your review rather than
assume the design (S1 especially is coupled to the Phase 5 onboarding design).
