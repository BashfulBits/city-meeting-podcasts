# Security Policy

This project ingests media and metadata from third-party government-meeting platforms and publishes a
static site plus derived audio/transcripts. This document describes the security posture and how to
report a vulnerability.

## Reporting a vulnerability

Please report security issues **privately** via GitHub's
[private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
(Security tab → "Report a vulnerability") rather than opening a public issue. If that is unavailable,
contact the maintainer listed on the GitHub profile.

Please include reproduction steps and the affected component (provider adapter, build workflow, storage,
feed output, etc.). We aim to acknowledge within a few days. There is no bug-bounty program; this is a
single-maintainer civic project.

## Trust model

- **Source URLs are maintainer-authored today.** The build runs in GitHub Actions with repo write and
  object-storage secrets in the environment, so any URL it fetches is part of the trust boundary.
- **When onboarding opens to public submissions** (Phase 5 / city-request issues), submitted source
  URLs become untrusted input. The SSRF/abuse gate below is the defense-in-depth boundary for that, and
  the `/approve` flow stays **human-in-the-loop**.

## Current posture (implemented)

- **SSRF / source-URL gate** (`citypods/security.py`, `validate_source_url`): https-only; per-provider
  host allowlists (e.g. `*.granicus.com`, `*.swagit.com`, `*.api.civicclerk.com`, the city's own domain
  for CivicPlus); resolve-and-reject private / loopback / link-local IPs; bounded redirects.
- **Response-size caps + timeouts** on fetches; retry/backoff (`citypods/http.py`) to avoid both DoS of
  upstreams and false-positive health alerts.
- **ffmpeg protocol whitelist** — media is decoded with `-protocol_whitelist` restricted to the set
  needed for HTTPS HLS/MP4, so a hostile manifest cannot coax ffmpeg into reading local files.
- **The Granicus Worker fallback is a closed media relay, not an open proxy** —
  `workers/granicus-media-proxy` requires a bearer secret, constructs the upstream URL from a
  committed tenant allowlist plus a strict tenant-prefixed MP4 filename, hard-codes
  `archive-video.granicus.com`, refuses queries and redirects, forwards only selected range/cache
  headers, and never writes the secret or configured endpoint to probe artifacts. Production uses
  it only after a direct canonical archive request returns HTTP 403; the retry stays inside the
  Granicus coordination envelope, subprocess errors are sanitized so the bearer header cannot
  appear in logs, and the Worker endpoint that ffmpeg echoes in stderr on error is scrubbed before
  any log line. A half-set or invalid `GRANICUS_PROXY_*` configuration disables the fallback (logged
  once) rather than surfacing an endpoint value through an unexpected error.
- **Hardened XML parsing** — provider feeds are parsed with `defusedxml` (entity-expansion safe).
- **Secrets are environment-only** — credentials are provided via GitHub Actions secrets; none are
  committed. Enabling GitHub secret-scanning + push protection (free for public repos) is recommended as
  a backstop.
- **Least-privilege workflow tokens** — e.g. the feed-health audit runs with `issues: write` only.

## Rules for future work

- **LLM/AI output is untrusted.** Generated summaries, tags, articles, or audio scripts must **never**
  overwrite official links, titles, dates, vote records, or transcript text. Generated content is
  additive and clearly labeled, with provenance/confidence where applicable.
- **Any new fetch of user-influenced URLs** must pass through `validate_source_url` (same gate, same
  allowlist discipline) before the request is made.
- **No invasive analytics.** Download analytics, if added, follow the aggregate, privacy-respecting
  OP3-style posture (issue #125) — no per-user tracking, minimal PII.
- **Public meeting records stay free and unpaywalled** regardless of any future monetization.

## Scope

In scope: the `citypods` package, build/deploy workflows, generated feeds and pages, and storage
handling. Out of scope: vulnerabilities in third-party meeting platforms themselves (report those to the
platform), and the security of forks' own infrastructure/credentials.
