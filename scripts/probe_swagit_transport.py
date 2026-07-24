#!/usr/bin/env python
"""Diagnose the Swagit list-page 403/503/504 storm seen in Audio #257/#258.

Swagit is Granicus-owned (``citypods/providers/swagit.py``), and the failure signature —
escalating 5xx into a blanket 403 across unrelated tenants in the same run — matches the
already-diagnosed Granicus media 403 (GH#300/#353): a paired test proved that cause was shared
GitHub-runner egress-IP reputation, not request shape or our own concurrency, and was fixed with
an authenticated Cloudflare Worker fallback (``workers/granicus-media-proxy``).

This probe targets the *list/archive page* fetch (``SwagitProvider.fetch_episodes``), which is
where #257/#258 actually failed — a different host class (``<tenant>.new.swagit.com``) than the
media CDN (``archive-video.granicus.com``) the existing Granicus probes exercise, and not yet
covered by the Worker fallback or by ``provider_distributed_leases``/telemetry.

Run the exact same script from two vantage points and compare the JSON outputs:

  * **locally** (or any non-Actions network) — a clean run here while GitHub Actions gets
    403/5xx is the same signal that isolated the Granicus cause to egress reputation.
  * **from a GitHub-hosted runner** (e.g. ``workflow_dispatch``, once wired up) — for the other
    half of the pair.

It also doubles as a way to test candidate fixes *before* changing production config:
``--pages`` reproduces Austin's real pagination burst, ``--inter-request-delay`` previews the
candidate fix in `review` step 3, ``--host-cap`` previews step 2's distributed-lease idea using
the real process-local ``HostRateLimiter``, and ``--concurrent-tenants`` tests whether hitting
multiple tenants at once (not just one tenant's burst) matters.

Uses ``citypods.http.make_session()`` — the exact session (UA, SSRF gate, retry policy)
production uses — so a result here reflects what production's own code would see from this
network, not a bespoke fetch that might behave differently.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import requests

from citypods.http import DEFAULT_TIMEOUT, HOST_LIMITER, make_session
from citypods.providers.swagit import _page_url
from citypods.security import validate_source_url

ALLOWED_HOSTS = ("*.swagit.com",)

# Real, currently-configured list_urls (config/feeds/*.yml) for the three tenants that failed in
# Audio #257/#258 — not synthetic URLs, so a probe result is directly comparable to those runs.
DEFAULT_TENANT_URLS = {
    "austin": "https://austintx.new.swagit.com/views/117/city-council-meetings",
    "dallas": "https://dallastx.new.swagit.com/views/default/city-council",
    "denton": "https://dentontx.new.swagit.com/views/5",
}

# Headers worth keeping in full: cheap WAF/edge fingerprints without echoing anything sensitive
# (these list pages carry no signed/query-string secrets, unlike Granicus media URLs).
_FINGERPRINT_HEADERS = (
    "server",
    "cf-ray",
    "cf-mitigated",
    "retry-after",
    "content-type",
    "content-length",
)
_WAF_MARKERS = ("attention required", "access denied", "sorry, you have been blocked", "cf-error")


@dataclass
class ProbeResult:
    sequence: int
    label: str
    tenant: str
    page: int
    url: str
    started_at: str
    elapsed_seconds: float
    status_code: int | None
    outcome: str
    ok: bool
    headers: dict[str, str]
    body_snippet: str
    waf_signature: bool
    error: str


def _outcome(status_code: int | None, exc: Exception | None) -> str:
    if exc is not None:
        if isinstance(exc, requests.Timeout):
            return "timeout"
        if isinstance(exc, requests.ConnectionError):
            return "connection_error"
        return "error"
    if status_code is None:
        return "error"
    if status_code == 403:
        return "http_403"
    if status_code == 429:
        return "http_429"
    if status_code in (500, 502, 503, 504):
        return f"http_{status_code}"
    if status_code < 400:
        return "success"
    return f"http_{status_code}"


def _waf_signature(headers: dict[str, str], body_snippet: str) -> bool:
    if headers.get("cf-mitigated"):
        return True
    lowered = body_snippet.lower()
    return any(marker in lowered for marker in _WAF_MARKERS)


def probe_one(
    *,
    sequence: int,
    label: str,
    tenant: str,
    page: int,
    url: str,
    session: requests.Session,
    timeout: float,
) -> ProbeResult:
    validate_source_url(url, allowed_hosts=ALLOWED_HOSTS)
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    started = time.monotonic()
    status_code: int | None = None
    headers: dict[str, str] = {}
    body_snippet = ""
    error = ""
    exc: Exception | None = None
    try:
        resp = session.get(url, timeout=timeout)
        status_code = resp.status_code
        headers = {
            k.lower(): v for k, v in resp.headers.items() if k.lower() in _FINGERPRINT_HEADERS
        }
        body_snippet = " ".join(resp.text[:500].split())[:300]
    except requests.RequestException as caught:
        exc = caught
        error = str(caught)[:300]
    elapsed = time.monotonic() - started
    outcome = _outcome(status_code, exc)
    return ProbeResult(
        sequence=sequence,
        label=label,
        tenant=tenant,
        page=page,
        url=url,
        started_at=started_at,
        elapsed_seconds=round(elapsed, 3),
        status_code=status_code,
        outcome=outcome,
        ok=outcome == "success",
        headers=headers,
        body_snippet=body_snippet,
        waf_signature=_waf_signature(headers, body_snippet),
        error=error,
    )


def _emit(result: ProbeResult) -> None:
    print(
        f"{'PASS' if result.ok else 'FAIL'} seq={result.sequence} tenant={result.tenant} "
        f"page={result.page} status={result.status_code or '-'} outcome={result.outcome} "
        f"elapsed={result.elapsed_seconds:.3f} waf={result.waf_signature} "
        f"headers={result.headers or '-'}",
        flush=True,
    )


def _run_tenant(
    *,
    label: str,
    tenant: str,
    base_url: str,
    pages: int,
    inter_request_delay: float,
    timeout: float,
    sequence_start: int,
) -> list[ProbeResult]:
    results: list[ProbeResult] = []
    # One session per tenant run: this is what production does too (a fresh session per
    # fetch_episodes call), and it keeps a burst honest -- no cross-tenant connection reuse
    # smoothing over what a real shard would see.
    with make_session() as session:
        for page in range(1, pages + 1):
            url = base_url if page == 1 else _page_url(base_url, page)
            result = probe_one(
                sequence=sequence_start + page - 1,
                label=label,
                tenant=tenant,
                page=page,
                url=url,
                session=session,
                timeout=timeout,
            )
            results.append(result)
            _emit(result)
            if inter_request_delay:
                time.sleep(inter_request_delay)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--label",
        required=True,
        help="Tags this run's vantage point, e.g. 'local-home', 'actions-run-260' -- "
        "compare two labeled JSON outputs to isolate egress-IP reputation.",
    )
    parser.add_argument(
        "--tenant",
        action="append",
        dest="tenants",
        choices=sorted(DEFAULT_TENANT_URLS),
        help="Repeatable; default is all three tenants that failed in Audio #257/#258.",
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=1,
        help="Sequential archive pages to fetch per tenant, back-to-back (reproduces Austin's "
        "real pagination burst; Austin feeds page as deep as 10).",
    )
    parser.add_argument(
        "--inter-request-delay",
        type=float,
        default=0.0,
        help="Seconds to sleep between requests within one tenant's page sequence -- previews "
        "the candidate step-3 fix (politeness delay) before changing production code.",
    )
    parser.add_argument(
        "--concurrent-tenants",
        action="store_true",
        help="Fetch all selected tenants' page sequences in parallel threads instead of one "
        "tenant at a time -- tests whether cross-tenant concurrency (not just one tenant's "
        "burst) matters.",
    )
    parser.add_argument(
        "--host-cap",
        type=int,
        default=None,
        help="If set, configures the real process-local HostRateLimiter for swagit.com to this "
        "value before running -- previews the candidate step-2 fix (distributed/process-local "
        "concurrency cap) using production's actual limiter.",
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--output", type=Path, default=Path("swagit-transport-results.json"))
    args = parser.parse_args()
    if args.pages < 1:
        parser.error("--pages must be >= 1")
    if args.inter_request_delay < 0:
        parser.error("--inter-request-delay must be non-negative")

    tenants = args.tenants or sorted(DEFAULT_TENANT_URLS)
    if args.host_cap is not None:
        if args.host_cap <= 0:
            parser.error("--host-cap must be positive")
        HOST_LIMITER.configure({"swagit.com": args.host_cap})

    results: list[ProbeResult] = []
    sequence = 1
    if args.concurrent_tenants:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(tenants)) as pool:
            futures = []
            for tenant in tenants:
                futures.append(
                    pool.submit(
                        _run_tenant,
                        label=args.label,
                        tenant=tenant,
                        base_url=DEFAULT_TENANT_URLS[tenant],
                        pages=args.pages,
                        inter_request_delay=args.inter_request_delay,
                        timeout=args.timeout,
                        sequence_start=sequence,
                    )
                )
                sequence += args.pages
            for future in futures:
                results.extend(future.result())
        results.sort(key=lambda r: r.sequence)
    else:
        for tenant in tenants:
            batch = _run_tenant(
                label=args.label,
                tenant=tenant,
                base_url=DEFAULT_TENANT_URLS[tenant],
                pages=args.pages,
                inter_request_delay=args.inter_request_delay,
                timeout=args.timeout,
                sequence_start=sequence,
            )
            results.extend(batch)
            sequence += args.pages

    per_tenant = {
        tenant: {
            "cases": sum(r.tenant == tenant for r in results),
            "successes": sum(r.tenant == tenant and r.ok for r in results),
            "outcomes": {
                outcome: sum(r.tenant == tenant and r.outcome == outcome for r in results)
                for outcome in sorted({r.outcome for r in results if r.tenant == tenant})
            },
            "waf_signatures": sum(r.tenant == tenant and r.waf_signature for r in results),
        }
        for tenant in tenants
    }
    payload = {
        "label": args.label,
        "settings": {
            "tenants": tenants,
            "pages": args.pages,
            "inter_request_delay": args.inter_request_delay,
            "concurrent_tenants": args.concurrent_tenants,
            "host_cap": args.host_cap,
            "timeout": args.timeout,
        },
        "results": [asdict(result) for result in results],
        "summary": {
            "cases": len(results),
            "successes": sum(result.ok for result in results),
            "per_tenant": per_tenant,
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"RESULTS path={args.output} cases={len(results)} label={args.label}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
