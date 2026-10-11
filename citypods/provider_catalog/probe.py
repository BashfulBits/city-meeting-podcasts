"""Provider I/O: list a catalog and send one bounded canary. No provider names appear here.

Everything provider-specific comes from the plugin (`ProviderRules`) and the provider's block in
config/provider_limits.yml (`api_base`, `chat_path`, `accounts`).
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

import requests

from citypods.compute.structured_shaping import shape_structured_request
from citypods.provider_catalog.rules import ProviderRules, Response, strict_context_json

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
        {
            "url": url,
            "body": body,
            "api_key_env": next(
                (
                    a.get("api_key_env")
                    for a in provider_cfg.get("accounts", [])
                    if a.get("id") == route["account_id"]
                ),
                "",
            ),
        },
    )


_CONTEXT_BYTES = 4 * 1024 * 1024


class ContextEnvelopeError(ValueError):
    """A bounded, payload-free reason why a provider stream could not be normalized."""

    def __init__(self, code):
        self.code = code
        super().__init__(code.replace("_", " "))


def _response_media_type(headers):
    value = next((v for k, v in headers.items() if str(k).lower() == "content-type"), None)
    if not isinstance(value, str) or len(value) > 256 or not value.isascii():
        return "unknown"
    media_type = value.split(";", 1)[0].strip().lower()
    if media_type in {"application/json", "text/event-stream"}:
        return media_type
    return "other"


def _provider_error_class(body):
    """Reduce structured error type/code to a small enum; never persist provider error text."""
    try:
        data = strict_context_json(body)
    except (TypeError, ValueError):
        return "unknown"
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return "unknown"
    values = [
        error[key].strip().lower()
        for key in ("code", "type")
        if isinstance(error.get(key), str) and len(error[key]) <= 64
    ]
    classes = {
        "rate_limit_exceeded": "rate_limited",
        "rate_limit_error": "rate_limited",
        "rate_limited": "rate_limited",
        "invalid_api_key": "authentication",
        "authentication_error": "authentication",
        "invalid_request_error": "invalid_request",
        "context_length_exceeded": "context_size_candidate",
        "maximum_context_length_exceeded": "context_size_candidate",
        "insufficient_quota": "quota",
        "quota_exceeded": "quota",
        "server_error": "server_error",
        "internal_server_error": "server_error",
    }
    recognized = [classes[value] for value in values if value in classes]
    if not recognized or any(value != recognized[0] for value in recognized):
        return "unknown"
    return recognized[0]


def _context_envelope(raw, content_type):
    """Normalize documented SSE final usage; malformed/truncated streams remain unsupported."""
    if "text/event-stream" not in content_type:
        return raw
    content, usage, finish, done = [], None, None, False
    for frame in raw.replace("\r\n", "\n").split("\n\n"):
        lines = [line[5:].strip() for line in frame.splitlines() if line.startswith("data:")]
        if not lines:
            continue
        value = "\n".join(lines)
        if value == "[DONE]":
            done = True
            continue
        if done:
            raise ContextEnvelopeError("invalid_sse_envelope")
        try:
            event = strict_context_json(value)
        except (TypeError, ValueError) as exc:
            raise ContextEnvelopeError("invalid_sse_json") from exc
        if not isinstance(event, dict) or event.get("error"):
            raise ContextEnvelopeError("invalid_sse_envelope")
        if event.get("usage") is not None:
            if usage is not None:
                raise ContextEnvelopeError("conflicting_sse_usage")
            usage = event["usage"]
        choices = event.get("choices")
        if not isinstance(choices, list) or len(choices) > 1:
            raise ContextEnvelopeError("invalid_sse_choice")
        if choices:
            choice = choices[0]
            if not isinstance(choice, dict) or choice.get("error") or choice.get("index", 0) != 0:
                raise ContextEnvelopeError("invalid_sse_choice")
            delta = choice.get("delta")
            if not isinstance(delta, dict) or delta.get("tool_calls") or delta.get("refusal"):
                raise ContextEnvelopeError("invalid_sse_delta")
            if delta.get("content") is not None:
                if not isinstance(delta["content"], str):
                    raise ContextEnvelopeError("invalid_sse_content")
                content.append(delta["content"])
            if choice.get("finish_reason") is not None:
                if finish is not None and finish != choice["finish_reason"]:
                    raise ContextEnvelopeError("conflicting_sse_finish_reason")
                finish = choice["finish_reason"]
    if not done or usage is None or finish is None:
        raise ContextEnvelopeError("incomplete_sse")
    return json.dumps(
        {
            "object": "chat.completion",
            "usage": usage,
            "choices": [{"finish_reason": finish, "message": {"content": "".join(content)}}],
        }
    )


def _collect_context(session, request, headers, timeout, clock):
    """Byte/deadline collector inside the cancellable child; never reads response.text."""
    from citypods.provider_catalog.registry import rules_for

    started = clock()
    started_at_ms = time.time_ns() // 1_000_000
    response = None
    raw_size = 0
    first_byte_ms = None

    def result(
        status,
        *,
        body="",
        timed_out=False,
        transport_error=None,
        code="unclassified_response",
    ):
        finished_at_ms = time.time_ns() // 1_000_000
        response_headers = response.headers if response is not None else {}
        diagnostics = {
            "diagnostic_code": code,
            "response_media_type": _response_media_type(response_headers),
            "response_bytes": min(raw_size, _CONTEXT_BYTES),
            "duration_ms": min(120_000, max(0, round((clock() - started) * 1000))),
            "first_byte_ms": first_byte_ms,
            "request_started_at_ms": started_at_ms,
            "request_finished_at_ms": finished_at_ms,
            **rules_for(request.provider).context_rate_diagnostics(response_headers),
        }
        if status is not None and status != 200:
            diagnostics["provider_error_class"] = (
                "rate_limited" if status == 429 else _provider_error_class(body)
            )
        return Response(
            status,
            body=body,
            timed_out=timed_out,
            transport_error=transport_error,
            context_diagnostics=diagnostics,
        )

    try:
        response = session.post(
            request.shaped_body["url"],
            headers=headers,
            json=request.shaped_body["body"],
            stream=True,
            allow_redirects=False,
            timeout=(min(20, timeout), timeout),
        )
        raw = bytearray()
        for chunk in response.iter_content(chunk_size=16384):
            if first_byte_ms is None:
                first_byte_ms = min(120_000, max(0, round((clock() - started) * 1000)))
            if clock() - started >= timeout:
                raw_size = len(raw)
                return result(response.status_code, timed_out=True, code="transport_timeout")
            if len(raw) + len(chunk) > _CONTEXT_BYTES:
                raw_size = len(raw) + len(chunk)
                return result(
                    response.status_code,
                    timed_out=True,
                    code="response_too_large",
                )
            raw.extend(chunk)
            raw_size = len(raw)
        if clock() - started >= timeout:
            return result(response.status_code, timed_out=True, code="transport_timeout")
        try:
            body = raw.decode("utf-8", errors="strict")
        except UnicodeError:
            return result(response.status_code, transport_error="UnicodeError", code="invalid_utf8")
        if response.status_code == 200:
            try:
                body = _context_envelope(body, response.headers.get("Content-Type", ""))
            except ContextEnvelopeError as exc:
                return result(response.status_code, code=exc.code)
        return result(response.status_code, body=body, code="response_received")
    except requests.Timeout:
        raw_size = len(raw) if "raw" in locals() else raw_size
        return result(
            response.status_code if response is not None else None,
            timed_out=True,
            code="transport_timeout",
        )
    except requests.ConnectionError as exc:
        return result(
            response.status_code if response is not None else None,
            transport_error=type(exc).__name__,
            code="transport_connection_error",
        )
    except requests.RequestException as exc:
        return result(
            response.status_code if response is not None else None,
            transport_error=type(exc).__name__,
            code="transport_request_error",
        )
    except UnicodeError:
        return result(
            response.status_code if response is not None else None,
            transport_error="ResponseDecodeError",
            code="invalid_utf8",
        )
    except ValueError as exc:
        return result(
            response.status_code if response is not None else None,
            transport_error=type(exc).__name__,
            code="transport_request_error",
        )
    finally:
        if response is not None:
            response.close()


def _context_http_worker(pipe, request, session, timeout):
    """Preflight before admission; parent permission is required before any provider submission."""
    from citypods.provider_catalog.registry import rules_for
    from citypods.security import validate_source_url

    try:
        validate_source_url(request.shaped_body["url"])
        config = {"accounts": [{"id": request.account_id, "api_key_env": pipe.recv()}]}
        key = account_key(config, request.account_id)
        if not key:
            pipe.send(
                Response(
                    None,
                    transport_error="provider credential unavailable",
                    context_diagnostics={"diagnostic_code": "worker_credentials_unavailable"},
                )
            )
            return
        # A copied Requests session must not inherit transport retries from a custom adapter.
        for adapter in getattr(session, "adapters", {}).values():
            adapter.max_retries = requests.adapters.Retry(total=0, redirect=0)
        pipe.send("ready")
        if pipe.recv() != "new":
            return
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        rules_for(request.provider)  # refuse unknown plugins before submission
        pipe.send(_collect_context(session, request, headers, timeout, time.monotonic))
    except (OSError, ValueError, KeyError, EOFError):
        try:
            pipe.send(
                Response(
                    None,
                    transport_error="context preflight unavailable",
                    context_diagnostics={"diagnostic_code": "worker_preflight_error"},
                )
            )
        except (OSError, EOFError):
            pass
    finally:
        pipe.close()
        session.close()


def measure_context(request, *, session, before_call, clock, timeout=120):
    """One hard-cancellable call; only a new typed admission releases the waiting subprocess."""
    import multiprocessing

    from citypods.compute.llm_dispatch_pause import ContextAdmission
    from citypods.provider_catalog.registry import rules_for

    if not 0 < timeout <= 120:
        raise ValueError("context timeout exceeds reviewed deadline")
    started = clock()
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe()
    process = ctx.Process(target=_context_http_worker, args=(child, request, session, timeout))
    observation = Response(
        None,
        timed_out=True,
        context_diagnostics={"diagnostic_code": "worker_response_timeout"},
    )
    admitted_at_ms = None
    try:
        process.start()
        child.close()
        # Only an environment variable name crosses the pipe; credentials remain in child memory.
        env_name = request.shaped_body.get("api_key_env")
        parent.send(env_name or "")
        remaining = timeout - (clock() - started)
        if remaining > 0 and parent.poll(remaining):
            ready = parent.recv()
            if isinstance(ready, Response):
                observation = ready
            elif ready == "ready":
                admission = before_call()
                if not isinstance(admission, ContextAdmission):
                    raise ValueError("context call lacks a new durable admission")
                admitted_at_ms = time.time_ns() // 1_000_000
                remaining = timeout - (clock() - started)
                received_response = False
                if remaining > 0:
                    parent.send("new")
                    if parent.poll(remaining):
                        received = parent.recv()
                        if isinstance(received, Response):
                            observation = replace(
                                received,
                                context_diagnostics=(
                                    received.context_diagnostics
                                    | {
                                        "admitted_at_ms": admitted_at_ms,
                                    }
                                ),
                            )
                            received_response = True
                if not received_response:
                    observation = replace(
                        observation,
                        context_diagnostics={
                            "diagnostic_code": "worker_response_timeout",
                            "admitted_at_ms": admitted_at_ms,
                        },
                    )
    except (OSError, EOFError):
        observation = Response(
            None,
            transport_error="context worker unavailable",
            context_diagnostics={
                "diagnostic_code": "worker_transport_error",
                "admitted_at_ms": admitted_at_ms,
            },
        )
    finally:
        parent.close()
        child.close()
        if process.pid is not None:
            if process.is_alive():
                process.terminate()
            process.join(0.5)
            if process.is_alive():
                process.kill()
                process.join(0.5)
            process.close()
    return rules_for(request.provider).context_observation(observation, request)
