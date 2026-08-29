"""Live contract tests for Cloudflare AI Gateway's Custom Provider routing (``pytest -m live``).

AI Gateway does **not** join a Custom Provider's Base URL the way its documentation describes.
Rather than `{base_url}/{provider-path}`, it rewrites the Base URL's last path segment to a
hardcoded ``v1`` before appending the caller path -- established 2026-08-29 by registering a
throwaway custom provider pointed at an echo service (see workers/llm-provider-shim/README.md).
Every custom provider's configuration is shaped around that undocumented behaviour.

Because the behaviour is undocumented, it can change without notice, and nothing in the offline
suite can detect that: the deviation lives in Cloudflare's edge, not in this repo. These tests
exercise the real gateway so a change surfaces as a named failure here rather than as production
404s that no `retryableStatus` list will retry.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

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


def _one_route_per_custom_provider():
    seen = {}
    for route in ROUTE_REGISTRY.values():
        if not (route.ai_gateway_slug or "").startswith("custom-"):
            continue
        seen.setdefault(route.provider, route)
    return sorted(seen.values(), key=lambda r: r.provider)


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


def _gateway_base() -> str:
    account = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    if not account or not os.environ.get("AI_GATEWAY_AUTH_TOKEN"):
        pytest.skip("CLOUDFLARE_ACCOUNT_ID and AI_GATEWAY_AUTH_TOKEN required")
    return f"https://gateway.ai.cloudflare.com/v1/{account}/{GATEWAY_ID}"


@pytest.mark.parametrize("route", _one_route_per_custom_provider(), ids=lambda r: r.provider)
def test_custom_provider_route_reaches_its_upstream(route):
    """Each custom provider's configured gateway URL must reach the real API, not a 404 page.

    This is the check that would have caught the outage: every NVIDIA route, and every SambaNova
    route, silently dispatched to the provider's origin root for want of a path prefix. 404 is not
    in either dispatch Worker's ``retryableStatus`` set, so those hard-failed with no failover.
    """
    base = _gateway_base()
    api_key = os.environ.get(route.api_key_env)
    if not api_key:
        pytest.skip(f"{route.api_key_env} not set")

    url = f"{base}/{route.ai_gateway_slug}{route.ai_gateway_chat_path}"
    status, body = _post(
        url,
        api_key,
        {
            "model": route.upstream_model,
            "messages": [{"role": "user", "content": "Say OK"}],
            "max_tokens": 8,
        },
    )

    _reject_edge_block(route.provider, status, body)
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


def test_gateway_still_drops_the_base_url_path_for_nvidia():
    """Canary: NVIDIA's bare ``/chat/completions`` must still fail.

    NVIDIA is registered with Base URL ``https://integrate.api.nvidia.com/v1``, and the caller path
    has to repeat that ``/v1`` because the gateway does not honour the registered path. If this
    request ever *succeeds*, Cloudflare has changed the join and the extra prefix in
    ``ai_gateway_chat_path`` is now double-prefixing for some providers -- which would be a silent
    outage in the other direction. Failing here is the signal to re-derive the rule and simplify
    the config, not a regression in this repo.
    """
    base = _gateway_base()
    api_key = os.environ.get("NVIDIA_API_KEY")
    if not api_key:
        pytest.skip("NVIDIA_API_KEY not set")

    status, body = _post(
        f"{base}/custom-nvidia/chat/completions",
        api_key,
        {
            "model": "nvidia/nemotron-3-super-120b-a12b",
            "messages": [{"role": "user", "content": "Say OK"}],
            "max_tokens": 8,
        },
    )

    _reject_edge_block("nvidia", status, body)
    assert status == 404, (
        "Cloudflare AI Gateway now honours the Custom Provider Base URL path (NVIDIA's bare "
        f"/chat/completions returned HTTP {status}, body {body[:120]!r}). Re-derive the URL join "
        "with an echo provider, then drop the compensating prefixes from "
        "config/provider_limits.yml and this test."
    )
