"""Provider I/O: list a catalog and send one bounded canary. No provider names appear here.

Everything provider-specific comes from the plugin (`ProviderRules`) and the provider's block in
config/provider_limits.yml (`api_base`, `chat_path`, `accounts`).
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import requests

from citypods.compute.structured_shaping import shape_structured_request
from citypods.provider_catalog.rules import ProviderRules, Response

# The Worker's own response ceiling (wrangler.jsonc MAX_RESPONSE_SECONDS): a canary is never
# stricter than production. Live first bytes took up to 224 s (NVIDIA, 2026-09-24).
CANARY_TIMEOUT_SECONDS = 720
CANARY_PROMPT = "Reply exactly: ok"
_MAX_FIRST_EVENT_BYTES = 65_536


@dataclass
class Catalog:
    models: dict[str, dict[str, Any]] = field(default_factory=dict)
    error: str | None = None


def account_key(provider_cfg: Mapping[str, Any], account_id: str | None = None) -> str | None:
    """The API key for `account_id` (default: the provider's first account) from the env."""
    for account in provider_cfg.get("accounts") or []:
        if account_id is None or account.get("id") == account_id:
            env_name = account.get("api_key_env")
            return os.environ.get(env_name) if env_name else None
    return None


def _auth_headers(rules: ProviderRules, api_key: str) -> dict[str, str]:
    if rules.catalog.auth == "google_api_key":
        return {"x-goog-api-key": api_key}
    return {"Authorization": f"Bearer {api_key}"}


def fetch_catalog(
    rules: ProviderRules, provider_cfg: Mapping[str, Any], session: requests.Session
) -> Catalog:
    api_key = account_key(provider_cfg)
    if not api_key:
        return Catalog(error="API key not configured")
    spec = rules.catalog
    url = spec.url or str(provider_cfg["api_base"]).rstrip("/") + spec.path
    headers = _auth_headers(rules, api_key)
    models: dict[str, dict[str, Any]] = {}
    params: dict[str, Any] = {"pageSize": 1000} if spec.style == "google" else {}
    try:
        while True:
            response = session.get(url, headers=headers, params=params, timeout=30)
            if not response.ok:
                return Catalog(error=f"catalog HTTP {response.status_code}")
            payload = response.json()
            # A malformed body is this provider's error, never an exception that aborts the run
            # (and with it every later provider and the issue update).
            if not isinstance(payload, dict) and not (
                spec.style != "google" and isinstance(payload, list)
            ):
                return Catalog(error="catalog malformed response")
            if spec.style == "google":
                for item in payload.get("models") or []:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("name") or "").removeprefix("models/")
                    if name:
                        models[name] = item
                token = payload.get("nextPageToken")
                if not token:
                    break
                params = {"pageSize": 1000, "pageToken": token}
                continue
            items = payload if isinstance(payload, list) else payload.get("data") or []
            if not isinstance(items, list):
                return Catalog(error="catalog malformed response")
            for item in items:
                if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]:
                    models[item["id"]] = item
            break
    except (requests.RequestException, ValueError) as exc:
        return Catalog(error=f"catalog {type(exc).__name__}")
    return Catalog(models=models)


def chat_url(rules: ProviderRules, provider_cfg: Mapping[str, Any]) -> str:
    path = rules.canary_path or str(provider_cfg.get("chat_path") or "/chat/completions")
    return str(provider_cfg["api_base"]).rstrip("/") + "/" + path.lstrip("/")


def _first_event_error(raw: str) -> bool:
    """Whether the first streamed event (or a plain JSON body) is an error, not a completion.

    Parsed, not substring-matched: an ordinary chunk can legitimately contain `"error": null`.
    SSE comment lines (`: keep-alive`) are skipped.
    """
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith(":"):
            continue
        payload = line.removeprefix("data:").strip()
        if payload == "[DONE]":
            return False
        try:
            event = json.loads(payload)
        except ValueError:
            continue
        if not isinstance(event, dict):
            return False
        return bool(event.get("error")) and not event.get("choices")
    return False


def canary(
    rules: ProviderRules,
    provider_cfg: Mapping[str, Any],
    model: str,
    api_key: str,
    session: requests.Session,
    *,
    timeout: float = CANARY_TIMEOUT_SECONDS,
) -> Response:
    """One 4-token streaming completion. Success is decided on the first streamed event."""
    try:
        response = session.post(
            chat_url(rules, provider_cfg),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": CANARY_PROMPT}],
                "max_tokens": 4,
                "stream": True,
            },
            timeout=(20, timeout),
            stream=True,
        )
    except requests.Timeout:
        return Response(status=None, timed_out=True)
    except requests.RequestException as exc:
        return Response(status=None, transport_error=type(exc).__name__)
    try:
        headers = dict(response.headers)
        if not response.ok:
            return Response(status=response.status_code, headers=headers, body=response.text)
        buffered = b""
        for chunk in response.iter_content(chunk_size=None):
            buffered += chunk
            text = buffered.decode(errors="replace")
            if (
                any(
                    line.strip() and not line.strip().startswith(":")
                    for line in text.split("\n\n")[:-1]
                )
                or len(buffered) > _MAX_FIRST_EVENT_BYTES
            ):
                break
        text = buffered.decode(errors="replace")
        return Response(
            status=response.status_code,
            headers=headers,
            body=text,
            first_event_error=_first_event_error(text),
        )
    except requests.Timeout:
        return Response(status=None, timed_out=True)
    except requests.RequestException as exc:
        return Response(status=None, transport_error=type(exc).__name__)
    finally:
        response.close()


STRUCTURED_METHODS = ("json_schema", "json_schema_relaxed", "json_object", "prompt_only")
CANARY_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string", "minLength": 2, "maxLength": 2}},
    "required": ["answer"],
    "additionalProperties": False,
}
# Includes reasoning tokens: four tokens cannot establish that a reasoning model returns JSON.
STRUCTURED_CANARY_MAX_TOKENS = 4096


def structured_canary(
    rules: ProviderRules,
    provider_cfg: Mapping[str, Any],
    model: str,
    api_key: str,
    session: requests.Session,
    *,
    before_attempt: Callable[[], bool],
    methods: Mapping[str, Mapping[str, Any]],
    renew_pause: Callable[[], None] = lambda: None,
) -> dict[str, dict[str, Any]]:
    """Verify all four shapes; the caller spaces, renews and charges every request.

    Only visible content counts. Reasoning, malformed JSON, a truncated reply and a schema
    mismatch cannot establish support. Results contain no provider text or credentials.
    """
    results = {}
    for method in STRUCTURED_METHODS:
        if not before_attempt():
            break
        spec = methods[method]
        messages, response_format = shape_structured_request(
            [{"role": "user", "content": 'Return {"answer":"ok"}.'}],
            name="CatalogCanary",
            schema=CANARY_SCHEMA,
            response_format=spec["response_format"],
            include_schema_in_prompt=spec["include_schema_in_prompt"],
            strip_keys=spec.get("strip_schema_keys", ()),
        )
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": STRUCTURED_CANARY_MAX_TOKENS,
            "stream": True,
        }
        if response_format is not None:
            payload["response_format"] = response_format
        started = time.monotonic()
        result: dict[str, Any] = {"outcome": "inconclusive"}
        response = None
        try:
            response = session.post(
                chat_url(rules, provider_cfg),
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=(20, CANARY_TIMEOUT_SECONDS),
                stream=True,
            )
            result["status"] = response.status_code
            if not response.ok:
                # Capacity failures are not evidence against a method. Stop instead of spending
                # the same exhausted account's quota on the remaining methods.
                if response.status_code in (402, 403, 404, 410, 429):
                    results[method] = result
                    break
                if response.status_code == 400:
                    result["outcome"] = "rejected"
                results[method] = result
                continue
            content, size, finished, error = [], 0, False, False
            next_renewal = started + 60
            for line in response.iter_lines(decode_unicode=True):
                now = time.monotonic()
                if now - started > CANARY_TIMEOUT_SECONDS:
                    error = True
                    break
                if now >= next_renewal:
                    renew_pause()
                    next_renewal = now + 60
                if isinstance(line, bytes):
                    line = line.decode("utf-8", errors="replace")
                if not line or not line.startswith("data:"):
                    continue
                if "first_byte_seconds" not in result:
                    result["first_byte_seconds"] = round(time.monotonic() - started, 3)
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                size += len(data.encode("utf-8"))
                if size > _MAX_FIRST_EVENT_BYTES:
                    break
                event = json.loads(data)
                if not isinstance(event, dict) or event.get("error"):
                    error = True
                    break
                for choice in event.get("choices") or []:
                    if choice.get("index", 0) != 0:
                        continue
                    delta = choice.get("delta") or {}
                    if isinstance(delta.get("content"), str):
                        content.append(delta["content"])
                    if choice.get("finish_reason") == "stop":
                        finished = True
                    elif choice.get("finish_reason"):
                        error = True
                if finished or error:
                    break
            if finished and not error:
                text = "".join(content)
                if not text:
                    result["outcome"] = "empty"
                else:
                    try:
                        valid = json.loads(text) == {"answer": "ok"}
                    except ValueError:
                        valid = False
                    result["outcome"] = "valid" if valid else "invalid"
        except (requests.RequestException, ValueError, TypeError, AttributeError):
            # Neither timeout nor a malformed transport body establishes lack of support.
            pass
        finally:
            result["completion_seconds"] = round(time.monotonic() - started, 3)
            if response is not None:
                response.close()
        results[method] = result
    return results


def build_context_request(
    route, provider_cfg, rules, *, dimension, target, ratio, attempt_ordinal, nonce
):
    """Shape a transient fixture using this identity's count mapping, never a universal ratio.

    Sentinels derive from an unpredictable per-run nonce; filler is deterministic and numbered.
    No fixture content is stored in observation/artifact records.
    """
    from fractions import Fraction

    from citypods.compute.llm_policy import estimate_tokens
    from citypods.provider_catalog.evidence import ContextRequest, context_identity, digest

    if dimension not in {"input", "output"} or type(target) is not int or target <= 0:
        raise ValueError("invalid context target")
    if target > (524288 if dimension == "input" else 32768):
        raise ValueError("context target exceeds reviewed per-call ceiling")
    ratio = Fraction(ratio)
    if ratio < 1 or not isinstance(nonce, str) or len(nonce) < 32:
        raise ValueError("context needs a positive mapping and unpredictable run nonce")
    opposite = 256 if dimension == "input" else 2048
    url = chat_url(rules, provider_cfg)
    identity = context_identity(
        route,
        provider_cfg,
        dimension=dimension,
        count_basis=dimension,
        parser_version="chat-usage-v1",
        opposite_reservation=opposite,
        gateway_path=url,
    )
    if dimension == "input":
        sentinels = {
            where: digest([nonce, route["route_id"], attempt_ordinal, where])[:32]
            for where in ("start", "middle", "tail")
        }
        instruction = (
            "Return only a JSON object with keys start, middle, tail and the exact "
            "corresponding sentinel values from this document.\n"
        )
        markers = {key: f"{key}_sentinel={value}\n" for key, value in sentinels.items()}
        raw_target = target * ratio.denominator // ratio.numerator

        def fixture(blocks):
            filler = [
                f"block {i:08d}: north east south west water stone cloud tree.\n"
                for i in range(blocks)
            ]
            middle = blocks // 2
            text = (
                instruction
                + markers["start"]
                + "".join(filler[:middle])
                + markers["middle"]
                + "".join(filler[middle:])
                + markers["tail"]
            )
            return [{"role": "user", "content": text}]

        # Bounded binary construction avoids a huge unmeasured prompt or an extra live count call.
        low, high = 0, max(0, raw_target // 10)
        if estimate_tokens(fixture(0)) > raw_target:
            raise ValueError("context target cannot fit minimum sentinel fixture")
        while low < high:
            mid = (low + high + 1) // 2
            if estimate_tokens(fixture(mid)) <= raw_target:
                low = mid
            else:
                high = mid - 1
        messages = fixture(low)
        requested_output = opposite
    else:
        messages = [
            {
                "role": "user",
                "content": "Write the positive integers in order starting with 1, one per line. "
                "No prose, code fences, or omissions. Continue until the output limit.",
            }
        ]
        requested_output = target
    estimated = estimate_tokens(messages)
    reserved = -(-(estimated * ratio.numerator) // ratio.denominator)
    if dimension == "output":
        if reserved > opposite:
            raise ValueError("output fixture exceeds fixed input allowance")
        reserved = opposite
    body = {
        "model": route["upstream_model"],
        "messages": messages,
        "max_tokens": requested_output,
        "stream": False,
    }
    return ContextRequest(
        route["route_id"],
        route["provider"],
        route["account_id"],
        route["upstream_model"],
        identity,
        dimension,
        f"context-{dimension}-v1",
        "chars4-v1",
        estimated,
        reserved,
        requested_output,
        attempt_ordinal,
        tuple(messages),
        {"url": url, "body": body},
    )
