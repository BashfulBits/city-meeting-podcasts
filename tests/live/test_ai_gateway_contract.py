"""Live contract tests for Cloudflare AI Gateway's Custom Provider routing (``pytest -m live``).

AI Gateway's Custom Provider URL join is undocumented and changed between the 2026-08-29 and
2026-09-15 probes. It formerly rewrote the Base URL's last path segment to a hardcoded ``v1``;
the current behavior honors the registered path as documented. Every custom provider's
configuration and the provider shim must tolerate the current behavior, while the live test keeps
watch for another edge-side change.

Because the behaviour is undocumented, it can change without notice, and nothing in the offline
suite can detect that: the deviation lives in Cloudflare's edge, not in this repo. These tests
exercise the real gateway so a change surfaces as a named failure here rather than as production
404s that no `retryableStatus` list will retry.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import warnings

import pytest

from citypods.compute.llm_policy import ROUTE_REGISTRY

pytestmark = pytest.mark.live

GATEWAY_ID = os.environ.get("AI_GATEWAY_ID", "citypods-dispatch")

# Large enough that a real completion body is never truncated -- a JSON check on a clipped body
# would fail on a perfectly healthy response.
_MAX_BODY = 65536

# A routing failure and a healthy provider are told apart by *shape*, not status code: several of
# these providers legitimately answer 4xx (quota, balance, rate limit) and that means routing
# worked. What must never come back is one of these -- the fingerprints of a request that never
# reached the provider's API.
ROUTING_FAILURE_BODIES = (
    "404 page not found",  # Go/ELB default -- SambaNova, NVIDIA's app router
    "<!DOCTYPE html",  # a marketing site answering instead of an API -- Kilo, OpenCode
    "<html",
)

# z.ai's Alibaba edge sometimes rejects the GitHub runner's gateway egress before the API can
# produce JSON. The fixed shim has already accepted and rewritten the request at that point, so
# this particular branded response is evidence of the custom-provider path reaching z.ai -- not a
# URL-join failure. It remains a warning because it is an upstream-availability limitation, not a
# production dispatch success.
ZAI_WAF_BLOCK_MARKERS = ("errors.aliyun.com", "potential threats to the server's security")

# A second identical request distinguishes a fleeting connection/capacity failure from an endpoint
# contract change. Never retry semantic 4xx responses: a 404, an HTML body, or a WAF response is
# diagnostic evidence and must remain visible to this test.
TRANSIENT_STATUSES = frozenset({500, 502, 503, 504})
PROBE_ATTEMPTS = 2
RETRY_DELAY_SECONDS = 1

# Keep probes on named, deliberately chosen free routes rather than YAML ordering: NVIDIA's
# first listed Kimi K3 route has twice exceeded the contract's 60-second read timeout while the
# Nemotron Super URL canary stayed responsive.
PREFERRED_FREE_PROBE_ROUTE_IDS = {
    "nvidia": "nvidia_nemotron_3_super_120b_a12b_free",
}


def _one_route_per_custom_provider():
    """One route per custom provider, preferring a free one.

    This runs weekly against real providers, so which route it picks is a spending decision, not
    an implementation detail. Iteration order alone would leave it accidental -- reordering the
    catalog could silently move a provider onto a paid route. Free wins wherever one exists.

    SiliconFlow is the deliberate exception: it has no free route at all, so probing it through
    the gateway is necessarily a paid call. It stays in the sweep because a provider that is
    unreachable is exactly what these tests exist to catch, and the cost is a completion capped at
    `max_tokens=8`. (Today it does not even reach billing -- the account balance is zero, so it
    answers 402, which still proves routing works.)
    """
    seen: dict[str, object] = {}
    for provider, route_id in PREFERRED_FREE_PROBE_ROUTE_IDS.items():
        route = ROUTE_REGISTRY.get(route_id)
        if route is None or route.provider != provider or not route.free:
            raise AssertionError(
                f"{provider}: preferred contract probe route {route_id!r} is missing or not free"
            )
        seen[provider] = route
    for route in ROUTE_REGISTRY.values():
        if not (route.ai_gateway_slug or "").startswith("custom-"):
            continue
        if route.provider in PREFERRED_FREE_PROBE_ROUTE_IDS:
            continue
        current = seen.get(route.provider)
        if current is None or (route.free and not current.free):
            seen[route.provider] = route
    return sorted(seen.values(), key=lambda r: r.provider)


def test_the_sweep_picks_a_free_route_wherever_one_exists():
    """Guards the selection above: a paid probe must be a recorded choice, not an accident."""
    paid = {r.provider for r in _one_route_per_custom_provider() if not r.free}
    assert paid == {"siliconflow"}, (
        f"custom providers probed on a paid route: {sorted(paid)}. SiliconFlow is the only one "
        "with no free route; anything else here means a free route exists and should be used, or "
        "the exception list needs updating deliberately."
    )


def _post(url: str, api_key: str, payload: dict, timeout: int = 60):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "cf-aig-authorization": f"Bearer {os.environ['AI_GATEWAY_AUTH_TOKEN']}",
            # urllib's default User-Agent is refused by Cloudflare's edge with `error code: 1010`,
            # which is a 403 -- indistinguishable from a healthy provider rejection unless it is
            # kept out of the response set entirely.
            "User-Agent": "citypods-live-contract/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(_MAX_BODY).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(_MAX_BODY).decode("utf-8", "replace")


def _post_with_retry(
    url: str,
    api_key: str,
    payload: dict,
    *,
    timeout: int = 60,
    post=_post,
    sleep=time.sleep,
):
    """Retry exactly once for a connection failure or a transient upstream 5xx response."""
    for attempt in range(PROBE_ATTEMPTS):
        try:
            status, body = post(url, api_key, payload, timeout=timeout)
        except (TimeoutError, urllib.error.URLError):
            if attempt + 1 == PROBE_ATTEMPTS:
                raise
        else:
            if status not in TRANSIENT_STATUSES or attempt + 1 == PROBE_ATTEMPTS:
                return status, body
        sleep(RETRY_DELAY_SECONDS)

    raise AssertionError("unreachable: final retry attempt must return or raise")


def _get_with_retry(
    url: str,
    api_key: str,
    *,
    timeout: int = 60,
    get=urllib.request.urlopen,
    sleep=time.sleep,
):
    """GET a non-inference endpoint with the same bounded transient retry policy."""
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "cf-aig-authorization": f"Bearer {os.environ['AI_GATEWAY_AUTH_TOKEN']}",
            "Accept": "application/json",
            "User-Agent": "citypods-live-contract/1.0",
        },
        method="GET",
    )
    for attempt in range(PROBE_ATTEMPTS):
        try:
            with get(request, timeout=timeout) as response:
                return response.status, response.read(_MAX_BODY).decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(_MAX_BODY).decode("utf-8", "replace")
        except (TimeoutError, urllib.error.URLError):
            if attempt + 1 == PROBE_ATTEMPTS:
                raise
        sleep(RETRY_DELAY_SECONDS)

    raise AssertionError("unreachable: final retry attempt must return or raise")


def _is_json(body: str) -> bool:
    try:
        json.loads(body)
    except ValueError:
        return False
    return True


def _reject_edge_block(provider: str, status: int, body: str) -> None:
    """Fail loudly on a Cloudflare edge block instead of scoring it as a provider response.

    `error code: 1010` is a 403 emitted by Cloudflare's bot protection before the gateway is
    reached. It carries no information about provider routing, so treating it as a normal 4xx
    would make every assertion here vacuously true.
    """
    if status == 403 and "error code:" in body:
        pytest.fail(
            f"{provider}: blocked by Cloudflare's edge, not the gateway (HTTP {status}, "
            f"{body.strip()!r}). This test's User-Agent is being refused; the run proves nothing "
            f"about provider routing until that is fixed."
        )


def _is_zai_waf_block(provider: str, status: int, body: str) -> bool:
    """Return whether z.ai's branded upstream firewall, rather than its API, answered."""
    body_lower = body.lower()
    return (
        provider == "zai"
        and status == 405
        and all(marker in body_lower for marker in ZAI_WAF_BLOCK_MARKERS)
    )


def _accept_zai_waf_block(provider: str, status: int, body: str) -> bool:
    """Record a known z.ai edge rejection without mistaking it for a gateway path failure."""
    if not _is_zai_waf_block(provider, status, body):
        return False
    warnings.warn(
        "zai: Alibaba WAF rejected the gateway request after the custom-provider path reached "
        "z.ai; endpoint routing is intact, but this probe could not verify API availability.",
        RuntimeWarning,
        stacklevel=2,
    )
    return True


def _gateway_base() -> str:
    account = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    if not account or not os.environ.get("AI_GATEWAY_AUTH_TOKEN"):
        pytest.skip("CLOUDFLARE_ACCOUNT_ID and AI_GATEWAY_AUTH_TOKEN required")
    return f"https://gateway.ai.cloudflare.com/v1/{account}/{GATEWAY_ID}"


@pytest.mark.parametrize("route", _one_route_per_custom_provider(), ids=lambda r: r.provider)
def test_custom_provider_route_reaches_its_upstream(route):
    """Each custom provider's configured gateway URL must reach the real API, not a 404 page.

    This catches a provider path mismatch before it becomes a production 404. 404 is not in either
    dispatch Worker's ``retryableStatus`` set, so a routing 404 hard-fails with no failover.
    """
    base = _gateway_base()
    api_key = os.environ.get(route.api_key_env)
    if not api_key:
        pytest.skip(f"{route.api_key_env} not set")

    if route.provider == "opencode":
        # This crosses the same custom-provider base URL and shim as chat completions, but does
        # not ask an overloaded, free model to perform inference. It is both a routing check and
        # a catalog check: the selected OpenCode model must still be advertised by the upstream.
        status, body = _get_with_retry(f"{base}/{route.ai_gateway_slug}/models", api_key)
        _reject_edge_block(route.provider, status, body)
        assert status == 200 and _is_json(body), (
            "opencode: gateway /models did not reach the provider API "
            f"(HTTP {status}, body {body[:120]!r}). Cloudflare's Custom Provider URL join may "
            "have changed; re-derive it with an echo provider before editing "
            "config/provider_limits.yml."
        )
        model_ids = {model.get("id") for model in json.loads(body).get("data", [])}
        assert route.upstream_model in model_ids, (
            f"opencode: selected catalog model {route.upstream_model!r} is absent from the "
            "provider's live /models response."
        )
        return

    url = f"{base}/{route.ai_gateway_slug}{route.ai_gateway_chat_path}"
    status, body = _post_with_retry(
        url,
        api_key,
        {
            "model": route.upstream_model,
            "messages": [{"role": "user", "content": "Say OK"}],
            "max_tokens": 8,
        },
    )

    _reject_edge_block(route.provider, status, body)
    if _accept_zai_waf_block(route.provider, status, body):
        return
    assert status != 404 or not any(marker in body for marker in ROUTING_FAILURE_BODIES), (
        f"{route.provider}: gateway call to {route.ai_gateway_chat_path} did not reach the "
        f"provider API (HTTP {status}, body {body[:120]!r}). Cloudflare's Custom Provider URL "
        f"join may have changed -- re-derive it with an echo provider before editing config; see "
        f"workers/llm-provider-shim/README.md."
    )
    assert not (status == 404 and body.strip() == ""), (
        f"{route.provider}: empty-body 404, the signature of a dispatch to the provider's origin "
        f"root with the Base URL path dropped."
    )
    # A provider that was actually reached answers in JSON -- either a completion or a semantic
    # error (quota, balance, rate limit). Anything else means the request died in transit, and
    # asserting only on status codes would let that pass as success.
    assert _is_json(body), (
        f"{route.provider}: non-JSON response (HTTP {status}, body {body[:120]!r}); the request "
        f"did not reach the provider API."
    )


def test_gateway_honors_the_base_url_path_for_nvidia():
    """Canary: NVIDIA's bare ``/chat/completions`` must reach its registered ``/v1`` API.

    NVIDIA is registered with Base URL ``https://integrate.api.nvidia.com/v1``. The caller path is
    intentionally bare because the gateway now honors the registered path. This request is kept as
    a canary so a future reversal or another join change produces a named failure before it causes
    silent 404s in production.
    """
    base = _gateway_base()
    api_key = os.environ.get("NVIDIA_API_KEY")
    if not api_key:
        pytest.skip("NVIDIA_API_KEY not set")

    status, body = _post_with_retry(
        f"{base}/custom-nvidia/chat/completions",
        api_key,
        {
            "model": "nvidia/nemotron-3-super-120b-a12b",
            "messages": [{"role": "user", "content": "Say OK"}],
            "max_tokens": 8,
        },
    )

    _reject_edge_block("nvidia", status, body)
    assert status != 404 or not any(marker in body for marker in ROUTING_FAILURE_BODIES), (
        "Cloudflare AI Gateway did not honor NVIDIA's registered Base URL path (bare "
        f"/chat/completions returned HTTP {status}, body {body[:120]!r}). Re-derive the URL join "
        "with an echo provider before changing config/provider_limits.yml."
    )
    assert _is_json(body), (
        f"nvidia: non-JSON response (HTTP {status}, body {body[:120]!r}); the request did not "
        "reach NVIDIA's provider API."
    )


def test_post_with_retry_retries_a_transient_failure_once():
    """A one-off 5xx gets one confirmation attempt before the contract reports a failure."""
    calls = []
    pauses = []

    def post(*_args, **_kwargs):
        calls.append(None)
        return (503, "temporary") if len(calls) == 1 else (200, "{}")

    assert _post_with_retry("url", "key", {}, post=post, sleep=pauses.append) == (200, "{}")
    assert len(calls) == 2
    assert pauses == [RETRY_DELAY_SECONDS]


def test_post_with_retry_retries_a_timeout_once():
    """A one-off connection timeout gets the same bounded confirmation attempt as a 5xx."""
    calls = []

    def post(*_args, **_kwargs):
        calls.append(None)
        if len(calls) == 1:
            raise TimeoutError("temporary timeout")
        return 200, "{}"

    assert _post_with_retry("url", "key", {}, post=post, sleep=lambda _seconds: None) == (200, "{}")
    assert len(calls) == 2


def test_post_with_retry_does_not_retry_a_semantic_4xx():
    """A possible path/API failure remains a single, immediately visible contract result."""
    calls = []

    def post(*_args, **_kwargs):
        calls.append(None)
        return 404, "not found"

    assert _post_with_retry("url", "key", {}, post=post, sleep=lambda _seconds: None) == (
        404,
        "not found",
    )
    assert len(calls) == 1


def test_zai_waf_block_signature_is_provider_specific():
    """Only z.ai's exact branded WAF page is accepted as contact evidence."""
    body = "<html>errors.aliyun.com: potential threats to the server's security</html>"
    assert _is_zai_waf_block("zai", 405, body)
    assert not _is_zai_waf_block("opencode", 405, body)
    assert not _is_zai_waf_block("zai", 404, body)


def test_zai_waf_block_becomes_an_explicit_warning():
    """The known edge block remains visible without hiding a URL-join regression."""
    body = "<html>errors.aliyun.com: potential threats to the server's security</html>"
    with pytest.warns(RuntimeWarning, match="Alibaba WAF"):
        assert _accept_zai_waf_block("zai", 405, body)
