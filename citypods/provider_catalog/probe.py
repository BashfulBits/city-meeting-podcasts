"""Provider I/O: list a catalog and send one bounded canary. No provider names appear here.

Everything provider-specific comes from the plugin (`ProviderRules`) and the provider's block in
config/provider_limits.yml (`api_base`, `chat_path`, `accounts`).
"""

from __future__ import annotations

import base64
import json
import os
import platform
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

import requests

from citypods.compute.structured_shaping import shape_structured_request
from citypods.provider_catalog.discovery import MAX_RESPONSE_TIMEOUT_SECONDS
from citypods.provider_catalog.rules import ProviderRules, Response, strict_context_json

# The Worker's own response ceiling (wrangler.jsonc MAX_RESPONSE_SECONDS): a canary is never
# stricter than production. Live first bytes took up to 224 s (NVIDIA, 2026-09-24).
CANARY_TIMEOUT_SECONDS = 720
CANARY_PROMPT = "Reply exactly: ok"
_MAX_FIRST_EVENT_BYTES = 65_536
_MAX_STRUCTURED_EVENT_BYTES = 131_072
_MAX_STRUCTURED_PREVIEW_CHARS = 128
_MAX_STRUCTURED_PREVIEW_BYTES = 1_024
_MAX_STRUCTURED_ERROR_BYTES = 1_024
_SAFE_PROVIDER_ID = re.compile(r"[^A-Za-z0-9._:-]")


class _SSELineLimitError(ValueError):
    pass


def _iter_sse_byte_lines(response, result):
    """Frame LF, CRLF and CR on bytes, preserving boundaries across transport chunks."""
    pending = bytearray()
    skip_lf = False
    for chunk in response.iter_content(chunk_size=512):
        result["stream_chunks"] = result.get("stream_chunks", 0) + 1
        result["stream_bytes"] = result.get("stream_bytes", 0) + len(chunk)
        for byte in chunk:
            if skip_lf and byte == 10:
                skip_lf = False
                continue
            skip_lf = False
            if byte in (10, 13):
                line = bytes(pending)
                pending.clear()
                skip_lf = byte == 13
                yield line
            else:
                pending.append(byte)
                if len(pending) > _MAX_STRUCTURED_EVENT_BYTES:
                    raise _SSELineLimitError
    result["http_stream_eof"] = True
    if pending:
        yield bytes(pending)


def _iter_sse_data_events(lines, *, on_line):
    """Yield complete SSE data payloads while observing each streamed line."""
    data_lines: list[str] = []
    first_data_at = None
    for line in lines:
        if not on_line():
            return
        if isinstance(line, bytes):
            line = line.decode("utf-8")
        line = line.removesuffix("\r")
        if line == "":
            if data_lines:
                yield "\n".join(data_lines), first_data_at, len(data_lines), True
                data_lines.clear()
                first_data_at = None
            continue
        if line.startswith("data:"):
            if first_data_at is None:
                first_data_at = time.monotonic()
            value = line[5:]
            data_lines.append(value[1:] if value.startswith(" ") else value)
    if data_lines:
        # Some compatible gateways omit the final blank-line separator.
        yield "\n".join(data_lines), first_data_at, len(data_lines), False


def _stream_line_guard(started: float, result: dict[str, Any], renew_pause):
    """Enforce elapsed-time and pause-renewal checks for every streamed line."""
    next_renewal = started + 60

    def check() -> bool:
        nonlocal next_renewal
        now = time.monotonic()
        if now - started > CANARY_TIMEOUT_SECONDS:
            result["diagnostic"] = "stream_timeout"
            return False
        if now >= next_renewal:
            renew_pause()
            next_renewal = now + 60
        return True

    return check


def _safe_provider_id(value: str) -> str:
    """Keep a bounded correlation ID without allowing issue markup or control characters."""
    return _SAFE_PROVIDER_ID.sub("", value[:128])


def _safe_structured_preview(content: str, *, secret: str = "") -> str:
    """Return a short JSON-escaped preview safe to include in a GitHub issue."""
    if secret:
        content = content.replace(secret, "[redacted]")
    # Model output is untrusted issue content: prevent markdown/HTML/mention interpretation.
    replacements = (
        ("`", r"\u0060"),
        ("@", r"\u0040"),
        ("<", r"\u003c"),
        (">", r"\u003e"),
        ("[", r"\u005b"),
        ("]", r"\u005d"),
        ("(", r"\u0028"),
        (")", r"\u0029"),
        ("!", r"\u0021"),
        ("#", r"\u0023"),
        ("|", r"\u007c"),
        ("*", r"\u002a"),
        ("_", r"\u005f"),
        ("~", r"\u007e"),
    )
    for end in range(min(len(content), _MAX_STRUCTURED_PREVIEW_CHARS), -1, -1):
        preview = json.dumps(content[:end], ensure_ascii=True)
        for char, escaped in replacements:
            preview = preview.replace(char, escaped)
        if len(preview.encode("ascii")) <= _MAX_STRUCTURED_PREVIEW_BYTES:
            return preview
    return '""'


def _bounded_error_preview(response: Any, *, secret: str = "") -> tuple[int, str, bool]:
    """Read at most one small provider error excerpt, without retaining the full body."""
    chunks: list[bytes] = []
    size = 0
    truncated = False
    try:
        for chunk in response.iter_content(chunk_size=_MAX_STRUCTURED_ERROR_BYTES):
            if not chunk:
                continue
            remaining = _MAX_STRUCTURED_ERROR_BYTES - size
            chunks.append(chunk[:remaining])
            size += min(len(chunk), remaining)
            if len(chunk) > remaining or size == _MAX_STRUCTURED_ERROR_BYTES:
                truncated = True
                break
    except (requests.RequestException, AttributeError, TypeError, ValueError):
        return size, "", truncated
    body = b"".join(chunks).decode("utf-8", errors="replace")
    return size, _safe_structured_preview(body, secret=secret), truncated


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
    mismatch cannot establish support. Results exclude prompts and credentials and include only a
    short escaped completion preview when the fixture is not verified.
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
        result: dict[str, Any] = {
            "outcome": "inconclusive",
            "diagnostic": "no_terminal_result",
            "finish_reason": None,
            "stream_terminal": False,
            "events": 0,
            "event_bytes": 0,
            "content_bytes": 0,
        }
        response = None
        content: list[str] = []
        try:
            response = session.post(
                chat_url(rules, provider_cfg),
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=(20, CANARY_TIMEOUT_SECONDS),
                stream=True,
            )
            result["status"] = response.status_code
            headers = getattr(response, "headers", {})
            result["content_type"] = str(headers.get("content-type", ""))[:128] or None
            result["content_encoding"] = str(headers.get("content-encoding", ""))[:64] or None
            result["response_encoding"] = str(getattr(response, "encoding", "") or "")[:64] or None
            if not response.ok:
                result["diagnostic"] = f"http_{response.status_code}"
                error_bytes, error_preview, truncated = _bounded_error_preview(
                    response, secret=api_key
                )
                result["error_body_bytes"] = error_bytes
                result["error_body_truncated"] = truncated
                if error_preview:
                    result["error_preview"] = error_preview
                for header in ("x-request-id", "request-id", "x-openrouter-id", "cf-ray"):
                    value = headers.get(header)
                    if isinstance(value, str) and value.isascii():
                        if safe_id := _safe_provider_id(value):
                            result["provider_request_id"] = safe_id
                            break
                # Capacity failures are not evidence against a method. Stop instead of spending
                # the same exhausted account's quota on the remaining methods.
                if response.status_code in (402, 403, 404, 410, 429):
                    results[method] = result
                    break
                if response.status_code == 400:
                    result["outcome"] = "rejected"
                results[method] = result
                continue
            result["stream_decoder"] = "utf8_byte_lines_v1"
            result["http_stream_eof"] = False
            result["runtime"] = {
                "python": platform.python_version(),
                "requests": requests.__version__,
                "urllib3": requests.packages.urllib3.__version__,
                "system": platform.system(),
            }
            finished, error = False, False
            # SSE is UTF-8. Unicode splitlines() with an inferred Latin-1 charset
            # mistakes continuation byte 0x85 for a line separator, truncating JSON.
            for data, first_data_at, data_line_count, delimited in _iter_sse_data_events(
                _iter_sse_byte_lines(response, result),
                on_line=_stream_line_guard(started, result, renew_pause),
            ):
                result["events"] += 1
                result["last_event_delimited"] = delimited
                result["last_event_data_lines"] = data_line_count
                raw_data = data.encode("utf-8")
                encoding = getattr(response, "encoding", None)
                if encoding:
                    try:
                        legacy_lines = raw_data.decode(encoding).splitlines()
                    except (LookupError, UnicodeError):
                        pass
                    else:
                        extra = max(0, len(legacy_lines) - data_line_count)
                        result["legacy_unicode_extra_line_breaks"] = (
                            result.get("legacy_unicode_extra_line_breaks", 0) + extra
                        )
                result["sse_data_lines"] = result.get("sse_data_lines", 0) + data_line_count
                for header in ("x-request-id", "request-id", "x-openrouter-id", "cf-ray"):
                    value = headers.get(header)
                    if isinstance(value, str) and value.isascii():
                        if safe_id := _safe_provider_id(value):
                            result["provider_request_id"] = safe_id
                            break
                if "first_byte_seconds" not in result and first_data_at is not None:
                    result["first_byte_seconds"] = round(first_data_at - started, 3)
                if data == "[DONE]":
                    result["stream_terminal"] = True
                    break
                result["event_bytes"] += len(data.encode("utf-8"))
                if result["event_bytes"] > _MAX_STRUCTURED_EVENT_BYTES:
                    result["diagnostic"] = "event_data_limit_exceeded"
                    break
                try:
                    event = json.loads(data)
                except json.JSONDecodeError as exc:
                    result["diagnostic"] = "malformed_sse_json"
                    result["json_error"] = exc.msg
                    result["json_error_line"] = exc.lineno
                    result["json_error_column"] = exc.colno
                    result["json_error_offset"] = exc.pos
                    # Preserve exact bytes independently of issue escaping, with a bounded
                    # window and secret redaction before encoding. Never retain full bodies.
                    safe_data = data.replace(api_key, "[redacted]") if api_key else data
                    safe_prefix = data[: exc.pos]
                    if api_key:
                        safe_prefix = safe_prefix.replace(api_key, "[redacted]")
                    raw_safe = safe_data.encode("utf-8")
                    byte_start = max(0, len(safe_prefix.encode("utf-8")) - 64)
                    result["malformed_event_utf8_bytes"] = len(raw_data)
                    result["malformed_event_byte_preview_start"] = byte_start
                    result["malformed_event_byte_preview_base64"] = base64.b64encode(
                        raw_safe[byte_start : byte_start + 128]
                    ).decode("ascii")
                    preview_source = data.replace(api_key, "[redacted]") if api_key else data
                    before_error = data[: exc.pos]
                    if api_key:
                        before_error = before_error.replace(api_key, "[redacted]")
                    preview_offset = len(before_error)
                    preview_start = max(
                        0,
                        min(
                            max(0, len(preview_source) - _MAX_STRUCTURED_PREVIEW_CHARS),
                            preview_offset - _MAX_STRUCTURED_PREVIEW_CHARS // 2,
                        ),
                    )
                    result["malformed_event_preview_start"] = preview_start
                    result["malformed_event_preview"] = _safe_structured_preview(
                        preview_source[
                            preview_start : preview_start + _MAX_STRUCTURED_PREVIEW_CHARS
                        ]
                    )
                    encoding = getattr(response, "encoding", None)
                    result["malformed_event_source_bytes"] = None
                    result["utf8_redecode_differs"] = None
                    result["utf8_redecode_parses"] = None
                    if encoding:
                        try:
                            source_bytes = data.encode(encoding)
                            result["malformed_event_source_bytes"] = len(source_bytes)
                            utf8_text = source_bytes.decode("utf-8")
                        except (LookupError, UnicodeError):
                            result["utf8_redecode_differs"] = None
                            result["utf8_redecode_parses"] = False
                        else:
                            result["utf8_redecode_differs"] = utf8_text != data
                            try:
                                json.loads(utf8_text)
                            except ValueError:
                                result["utf8_redecode_parses"] = False
                            else:
                                result["utf8_redecode_parses"] = True
                    error = True
                    break
                if not isinstance(event, dict) or event.get("error"):
                    result["diagnostic"] = "provider_stream_error"
                    if isinstance(event, dict) and event.get("error"):
                        try:
                            error_text = json.dumps(event["error"], ensure_ascii=True)
                        except (TypeError, ValueError):
                            error_text = str(type(event["error"]).__name__)
                        result["provider_error_preview"] = _safe_structured_preview(
                            error_text, secret=api_key
                        )
                    error = True
                    break
                response_id = event.get("id")
                if isinstance(response_id, str) and response_id.isascii():
                    if safe_id := _safe_provider_id(response_id):
                        result["response_id"] = safe_id
                usage = event.get("usage")
                if isinstance(usage, dict):
                    counts = {}
                    for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
                        value = usage.get(name)
                        if type(value) is int and 0 <= value <= 2**53 - 1:
                            counts[name] = value
                    details = usage.get("completion_tokens_details")
                    if isinstance(details, dict):
                        value = details.get("reasoning_tokens")
                        if type(value) is int and 0 <= value <= 2**53 - 1:
                            counts["reasoning_tokens"] = value
                    prompt_details = usage.get("prompt_tokens_details")
                    if isinstance(prompt_details, dict):
                        value = prompt_details.get("cached_tokens")
                        if type(value) is int and 0 <= value <= 2**53 - 1:
                            counts["cached_tokens"] = value
                    if counts:
                        result["usage"] = counts
                for choice in event.get("choices") or []:
                    if choice.get("index", 0) != 0:
                        continue
                    delta = choice.get("delta") or {}
                    if isinstance(delta.get("content"), str):
                        content.append(delta["content"])
                    finish_reason = choice.get("finish_reason")
                    if isinstance(finish_reason, str):
                        safe_finish_reason = _safe_provider_id(finish_reason)[:64]
                        result["finish_reason"] = safe_finish_reason
                        result["stream_terminal"] = True
                    if finish_reason == "stop":
                        finished = True
                    elif finish_reason:
                        reason_label = (
                            _safe_provider_id(finish_reason)[:48]
                            if isinstance(finish_reason, str)
                            else type(finish_reason).__name__
                        )
                        result["diagnostic"] = f"finish_reason_{reason_label}"
                        error = True
                if error:
                    break
            if result["diagnostic"] == "stream_timeout":
                error = True
            text = "".join(content)
            if finished and not error:
                if not text:
                    result["outcome"] = "empty"
                    result["diagnostic"] = "empty_content"
                else:
                    try:
                        valid = json.loads(text) == {"answer": "ok"}
                    except ValueError:
                        valid = False
                    result["outcome"] = "valid" if valid else "invalid"
                    result["diagnostic"] = (
                        "fixture_json_valid" if valid else "fixture_json_mismatch"
                    )
            elif not error and result["diagnostic"] == "no_terminal_result":
                result["diagnostic"] = (
                    "stream_done_without_finish_reason"
                    if result["stream_terminal"]
                    else "stream_ended_without_finish_reason"
                )
        except _SSELineLimitError:
            result["diagnostic"] = "sse_line_data_limit_exceeded"
        except UnicodeDecodeError as exc:
            result["diagnostic"] = "invalid_sse_utf8"
            result["utf8_error_start"] = exc.start
            result["utf8_error_end"] = exc.end
            result["utf8_error_reason"] = exc.reason
            safe_bytes = (
                exc.object.replace(api_key.encode(), b"[redacted]") if api_key else exc.object
            )
            safe_prefix = exc.object[: exc.start]
            if api_key:
                safe_prefix = safe_prefix.replace(api_key.encode(), b"[redacted]")
            byte_start = max(0, len(safe_prefix) - 32)
            result["invalid_utf8_line_bytes"] = len(exc.object)
            result["invalid_utf8_preview_start"] = byte_start
            result["invalid_utf8_preview_base64"] = base64.b64encode(
                safe_bytes[byte_start : byte_start + 64]
            ).decode("ascii")
        except requests.Timeout:
            result["diagnostic"] = "transport_timeout"
        except requests.RequestException as exc:
            result["diagnostic"] = f"transport_{type(exc).__name__}"
        except (ValueError, TypeError, AttributeError) as exc:
            # Neither malformed transport data nor parser errors establish lack of support.
            result["diagnostic"] = f"probe_{type(exc).__name__}"
        finally:
            text = "".join(content)
            result["content_bytes"] = len(text.encode("utf-8"))
            if text and result["outcome"] != "valid":
                result["completion_preview"] = _safe_structured_preview(text, secret=api_key)
            result["completion_seconds"] = round(time.monotonic() - started, 3)
            if response is not None:
                response.close()
        results[method] = result
    return results


def build_context_request(
    route,
    provider_cfg,
    rules,
    *,
    dimension,
    target,
    ratio,
    attempt_ordinal,
    nonce,
    max_input_tokens=524_288,
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
    input_ceiling = max_input_tokens if dimension == "input" else 32768
    if target > input_ceiling:
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
_CONTEXT_ERROR_BODY_BYTES = 4096


def _manual_error_body_excerpt(raw, request, request_headers):
    """Retain bounded manual error detail after removing the canary credential and prompt."""
    raw = bytes(raw)
    truncated = len(raw) > _CONTEXT_ERROR_BODY_BYTES
    text = raw.decode("utf-8", errors="replace")
    authorization = next(
        (value for key, value in request_headers.items() if key.lower() == "authorization"), ""
    )
    if isinstance(authorization, str) and authorization:
        text = text.replace(authorization, "[redacted authorization]")
        if authorization.lower().startswith("bearer "):
            text = text.replace(authorization[7:], "[redacted credential]")
    for message in request.messages:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str) and content:
            text = text.replace(content, "[generated probe prompt redacted]")
            escaped = json.dumps(content, ensure_ascii=False)[1:-1]
            if escaped != content:
                text = text.replace(escaped, "[generated probe prompt redacted]")
    encoded = text.encode("utf-8")
    if len(encoded) > _CONTEXT_ERROR_BODY_BYTES:
        text = encoded[:_CONTEXT_ERROR_BODY_BYTES].decode("utf-8", errors="ignore")
        truncated = True
    return text, truncated


def _discovery_response_excerpt(raw, request, request_headers):
    """Prefer a provider's short error/message text over JSON envelope boilerplate."""
    text = bytes(raw).decode("utf-8", errors="replace")
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        payload = None
    excerpt = None
    model_text = False
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            for key in ("message", "detail", "type"):
                if isinstance(error.get(key), str) and error[key]:
                    excerpt = error[key]
                    break
        choices = payload.get("choices")
        if excerpt is None and isinstance(choices, list) and choices:
            choice = choices[0]
            message = choice.get("message") if isinstance(choice, dict) else None
            if isinstance(message, dict):
                candidate = message.get("content") or message.get("refusal")
                if isinstance(candidate, str) and candidate:
                    excerpt = candidate
                    model_text = True
    if excerpt is None:
        excerpt = text
    redacted, _ = _manual_error_body_excerpt(excerpt.encode("utf-8"), request, request_headers)
    if model_text and request.dimension == "output" and len(redacted) > 128:
        # The prefix alone cannot reveal deliberate abbreviation or a closing remark. Keep
        # both ends within the same redacted/escaped 128-character diagnostic allowance.
        redacted = redacted[:56] + " ... [tail] ... " + redacted[-56:]
    return _safe_structured_preview(redacted)


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


def _context_provider_request_id(headers):
    """Keep a short upstream correlation ID without retaining arbitrary header text."""
    for key in ("x-request-id", "request-id", "x-openrouter-id", "cf-ray"):
        value = next(
            (v for name, v in headers.items() if str(name).lower() == key),
            None,
        )
        if isinstance(value, str) and value.isascii():
            if safe_id := _safe_provider_id(value):
                return safe_id
    return None


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
        # JSON may contain Unicode line separators in reasoning strings. Only wire LF
        # separates SSE lines; str.splitlines() would split those strings into invalid JSON.
        lines = [line[5:].strip() for line in frame.split("\n") if line.startswith("data:")]
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


class DiscoverySSECollector:
    """Incremental discovery-only SSE; transport overhead never becomes retained answer text."""

    MAX_WIRE = 64 * 1024 * 1024
    MAX_FRAME = 128 * 1024
    MAX_EVENTS = 1_000_000

    def __init__(self):
        self.pending = bytearray()
        self.content = bytearray()
        self.content_bytes = 0
        self.wire_bytes = 0
        self.events = 0
        self.usage = None
        self.response_id = None
        self.finish = None
        self.done = False
        self.error_preview = b""
        self.http_eof = False
        self.last_event = "none"

    def feed(self, chunk):
        self.wire_bytes += len(chunk)
        if self.wire_bytes > self.MAX_WIRE:
            raise ContextEnvelopeError("stream_bytes_exceeded")
        self.pending.extend(chunk)
        self.pending = bytearray(self.pending.replace(b"\r\n", b"\n"))
        while (end := self.pending.find(b"\n\n")) >= 0:
            if end > self.MAX_FRAME:
                raise ContextEnvelopeError("stream_frame_exceeded")
            frame = bytes(self.pending[:end]).decode("utf-8", errors="strict")
            del self.pending[: end + 2]
            self._frame(frame)
        if len(self.pending) > self.MAX_FRAME:
            raise ContextEnvelopeError("stream_frame_exceeded")

    def _frame(self, frame):
        self.events += 1
        if self.events > self.MAX_EVENTS:
            raise ContextEnvelopeError("stream_events_exceeded")
        lines = [line[5:].strip() for line in frame.split("\n") if line.startswith("data:")]
        if not lines:
            self.last_event = "comment"
            return
        self.last_event = "data"
        if self.done:
            raise ContextEnvelopeError("invalid_sse_envelope")
        value = "\n".join(lines)
        if value == "[DONE]":
            self.last_event = "done"
            self.done = True
            return
        try:
            event = strict_context_json(value)
        except (TypeError, ValueError) as exc:
            raise ContextEnvelopeError("invalid_sse_json") from exc
        if not isinstance(event, dict):
            raise ContextEnvelopeError("invalid_sse_event_type")
        if event.get("error"):
            # Retain only the provider error, never sibling generated/reasoning fields.
            self.error_preview = json.dumps({"error": event["error"]}).encode("utf-8")
            raise ContextEnvelopeError("provider_sse_error")
        if event.get("object") != "chat.completion.chunk":
            raise ContextEnvelopeError("invalid_sse_object")
        response_id = event.get("id")
        if response_id is not None:
            if (
                not isinstance(response_id, str)
                or not 1 <= len(response_id) <= 128
                or (self.response_id is not None and self.response_id != response_id)
            ):
                raise ContextEnvelopeError("invalid_sse_envelope")
            self.response_id = response_id
        incoming_usage = event.get("usage")
        choices = event.get("choices")
        if not isinstance(choices, list) or len(choices) > 1:
            raise ContextEnvelopeError("invalid_sse_choice")
        if not choices:
            self._usage(incoming_usage)
            return
        if self.finish is not None or self.usage is not None:
            raise ContextEnvelopeError("invalid_sse_envelope")
        choice = choices[0]
        if not isinstance(choice, dict) or choice.get("error") or choice.get("index", 0) != 0:
            raise ContextEnvelopeError("invalid_sse_choice")
        delta = choice.get("delta")
        if not isinstance(delta, dict) or delta.get("tool_calls") or delta.get("refusal"):
            raise ContextEnvelopeError("invalid_sse_delta")
        for key in ("reasoning_content", "reasoning", "content"):
            if delta.get(key) is not None and not isinstance(delta[key], str):
                raise ContextEnvelopeError("invalid_sse_content")
        if delta.get("content") is not None:
            content = delta["content"]
            self.content_bytes += len(content.encode("utf-8"))
            if self.content_bytes > _CONTEXT_BYTES:
                raise ContextEnvelopeError("stream_content_exceeded")
            self.content.extend(content.encode("utf-8"))
        if choice.get("finish_reason") is not None:
            if self.finish is not None and self.finish != choice["finish_reason"]:
                raise ContextEnvelopeError("conflicting_sse_finish_reason")
            self.finish = choice["finish_reason"]

        self._usage(incoming_usage)

    def _usage(self, value):
        if value is None:
            return
        if self.usage is not None:
            raise ContextEnvelopeError("conflicting_sse_usage")
        if self.finish is None or not isinstance(value, dict):
            raise ContextEnvelopeError("invalid_final_sse_usage")
        self.usage = value

    def terminal_state(self):
        """Compact structural evidence within the existing 128-character preview bound."""
        finish = (
            self.finish
            if isinstance(self.finish, str) and self.finish in {"length", "stop"}
            else "missing"
            if self.finish is None
            else "other"
        )
        return (
            f"eof={int(self.http_eof)} done={int(self.done)} finish={finish} "
            f"usage={int(self.usage is not None)} "
            f"pending={min(len(self.pending), self.MAX_FRAME + 1)} "
            f"content={min(self.content_bytes, _CONTEXT_BYTES + 1)} event={self.last_event}"
        )

    def complete(self):
        if self.pending.strip() or not self.done or self.usage is None or self.finish is None:
            raise ContextEnvelopeError("incomplete_sse")
        body = json.dumps(
            {
                "object": "chat.completion",
                "id": self.response_id,
                "usage": self.usage,
                "choices": [
                    {
                        "finish_reason": self.finish,
                        "message": {"content": self.content.decode("utf-8")},
                    }
                ],
            }
        )
        if len(body.encode("utf-8")) > _CONTEXT_BYTES:
            raise ContextEnvelopeError("stream_content_exceeded")
        return body


def _collect_context(
    session,
    request,
    headers,
    timeout,
    clock,
    *,
    retain_error_body=False,
    retain_response_preview=False,
    discovery_stream=False,
    progress=None,
):
    """Byte/deadline collector inside the cancellable child; never reads response.text."""
    from citypods.provider_catalog.registry import rules_for

    started = clock()
    started_at_ms = time.time_ns() // 1_000_000
    response = None
    sse = None
    normalized_bytes = 0
    raw = bytearray()
    raw_size = 0
    first_byte_ms = None
    last_progress_at = None
    last_progress_phase = None

    def report_progress(phase):
        nonlocal last_progress_at, last_progress_phase
        now = clock()
        if (
            not discovery_stream
            or progress is None
            or (
                phase == last_progress_phase
                and last_progress_at is not None
                and now - last_progress_at < 5
            )
        ):
            return
        last_progress_at = now
        last_progress_phase = phase
        state = f"phase={phase}" + (" " + sse.terminal_state() if sse is not None else "")
        diagnostics = {
            "response_preview": _safe_structured_preview(state),
            "response_media_type": _response_media_type(response.headers)
            if response is not None
            else "unknown",
            "duration_ms": min(round(timeout * 1000), max(0, round((now - started) * 1000))),
            "first_byte_ms": first_byte_ms,
        }
        if sse is not None:
            diagnostics["stream_bytes"] = min(sse.wire_bytes, sse.MAX_WIRE)
            diagnostics["stream_events"] = min(sse.events, sse.MAX_EVENTS)
        progress(
            Response(
                response.status_code if response is not None else None,
                timed_out=True,
                context_diagnostics=diagnostics,
            )
        )

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
            "response_bytes": normalized_bytes
            if sse is not None
            else min(raw_size, _CONTEXT_BYTES),
            "duration_ms": min(round(timeout * 1000), max(0, round((clock() - started) * 1000))),
            "first_byte_ms": first_byte_ms,
            "request_started_at_ms": started_at_ms,
            "request_finished_at_ms": finished_at_ms,
            **rules_for(request.provider).context_rate_diagnostics(response_headers),
        }
        if sse is not None:
            diagnostics["stream_bytes"] = min(sse.wire_bytes, sse.MAX_WIRE)
            diagnostics["stream_events"] = min(sse.events, sse.MAX_EVENTS)
        if request_id := _context_provider_request_id(response_headers):
            diagnostics["provider_request_id"] = request_id
        if retain_response_preview and sse is not None and (body or sse.error_preview):
            diagnostics["response_preview"] = _discovery_response_excerpt(
                sse.error_preview or body.encode("utf-8"), request, headers
            )
        if (
            retain_response_preview
            and sse is not None
            and code != "response_received"
            and not diagnostics.get("response_preview")
        ):
            # Prefer a provider error explanation when present; otherwise preserve the
            # structural terminal state, without generated or reasoning text.
            diagnostics["response_preview"] = _discovery_response_excerpt(
                sse.terminal_state().encode(), request, headers
            )
        if retain_response_preview and raw:
            diagnostics["response_preview"] = _discovery_response_excerpt(raw, request, headers)
        if retain_error_body and status is not None and status >= 400:
            excerpt, truncated = _manual_error_body_excerpt(raw, request, headers)
            diagnostics["error_response_body"] = excerpt
            diagnostics["error_response_body_truncated"] = truncated or (
                bool(excerpt) and code != "response_received"
            )
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
        report_progress("requesting")
        response = session.post(
            request.shaped_body["url"],
            headers=headers,
            json=request.shaped_body["body"],
            stream=True,
            allow_redirects=False,
            timeout=(min(20, timeout), timeout),
        )
        if discovery_stream and response.status_code == 200:
            if _response_media_type(response.headers) != "text/event-stream":
                return result(response.status_code, code="stream_media_type_mismatch")
            sse = DiscoverySSECollector()
        report_progress("headers")
        for chunk in response.iter_content(chunk_size=16384):
            if first_byte_ms is None:
                first_byte_ms = min(
                    round(timeout * 1000), max(0, round((clock() - started) * 1000))
                )
            if clock() - started >= timeout:
                raw_size = len(raw)
                return result(response.status_code, timed_out=True, code="transport_timeout")
            if sse is not None:
                sse.feed(chunk)
                report_progress("streaming")
                if sse.done:
                    break
                continue
            if len(raw) + len(chunk) > _CONTEXT_BYTES:
                raw_size = len(raw) + len(chunk)
                return result(
                    response.status_code,
                    timed_out=True,
                    code="response_too_large",
                )
            raw.extend(chunk)
            raw_size = len(raw)
        else:
            if sse is not None:
                sse.http_eof = True
        if clock() - started >= timeout:
            return result(response.status_code, timed_out=True, code="transport_timeout")
        if sse is not None:
            body = sse.complete()
            normalized_bytes = len(body.encode("utf-8"))
            return result(response.status_code, body=body, code="response_received")
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
    except ContextEnvelopeError as exc:
        return result(response.status_code if response is not None else None, code=exc.code)
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


def _context_http_worker(
    pipe,
    request,
    session,
    timeout,
    retain_error_body,
    retain_response_preview,
    discovery_stream=False,
):
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
        pipe.send(
            _collect_context(
                session,
                request,
                headers,
                timeout,
                time.monotonic,
                retain_error_body=retain_error_body,
                retain_response_preview=retain_response_preview,
                discovery_stream=discovery_stream,
                **(
                    {"progress": lambda snapshot: pipe.send(("progress", snapshot))}
                    if discovery_stream
                    else {}
                ),
            )
        )
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


def _measure_response(
    request,
    *,
    session,
    before_call,
    clock,
    admission_type,
    admission_check,
    timeout=120,
    max_timeout=120,
    retain_error_body=False,
    retain_response_preview=False,
    discovery_stream=False,
):
    """One hard-cancellable call; only a new typed admission releases the waiting subprocess."""
    import multiprocessing

    if not 0 < timeout <= max_timeout:
        raise ValueError("response timeout exceeds reviewed deadline")
    started = clock()
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe()
    process = ctx.Process(
        target=_context_http_worker,
        args=(child, request, session, timeout, retain_error_body, retain_response_preview)
        + ((True,) if discovery_stream else ()),
    )
    observation = Response(
        None,
        timed_out=True,
        context_diagnostics={"diagnostic_code": "worker_response_timeout"},
    )
    admitted_at_ms = None
    last_progress = None
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
                if not isinstance(admission, admission_type) or not admission_check(admission):
                    raise ValueError("context call lacks a new durable admission")
                admitted_at_ms = time.time_ns() // 1_000_000
                remaining = timeout - (clock() - started)
                received_response = False
                if remaining > 0:
                    parent.send("new")
                    while remaining > 0 and parent.poll(remaining):
                        received = parent.recv()
                        if (
                            discovery_stream
                            and isinstance(received, tuple)
                            and len(received) == 2
                            and received[0] == "progress"
                            and isinstance(received[1], Response)
                            and not received[1].body
                        ):
                            last_progress = received[1]
                        elif isinstance(received, Response):
                            observation = replace(
                                received,
                                context_diagnostics=received.context_diagnostics
                                | {"admitted_at_ms": admitted_at_ms},
                            )
                            received_response = True
                            break
                        else:
                            raise ValueError("invalid transport IPC message")
                        remaining = timeout - (clock() - started)
                if not received_response:
                    observation = replace(
                        last_progress or observation,
                        timed_out=True,
                        context_diagnostics=(
                            last_progress.context_diagnostics if last_progress else {}
                        )
                        | {
                            "diagnostic_code": "worker_response_timeout",
                            "admitted_at_ms": admitted_at_ms,
                            "duration_ms": min(
                                round(timeout * 1000), max(0, round((clock() - started) * 1000))
                            ),
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
    return observation


def measure_context(request, *, session, before_call, clock, timeout=120, retain_error_body=False):
    """Configured-route calls still require the existing durable Worker admission."""
    from citypods.compute.llm_dispatch_pause import ContextAdmission
    from citypods.provider_catalog.registry import rules_for

    response = _measure_response(
        request,
        session=session,
        before_call=before_call,
        clock=clock,
        admission_type=ContextAdmission,
        admission_check=lambda _a: True,
        timeout=timeout,
        retain_error_body=retain_error_body,
    )
    return rules_for(request.provider).context_observation(response, request)


def build_discovery_request(request, route, provider_cfg, rules, *, nonce):
    """Distinct discovery authority; never expands configured-route context builder ceilings."""
    import json

    from citypods.compute.llm_policy import estimate_tokens
    from citypods.provider_catalog.discovery import (
        MAX_INPUT,
        MAX_OUTPUT,
        discovery_identity,
        output_discovery_control,
    )
    from citypods.provider_catalog.evidence import digest

    physical = discovery_identity(
        route["provider"],
        route["account_id"],
        route["upstream_model"],
        chat_url(rules, provider_cfg),
    )
    if request.identity != physical:
        raise ValueError("discovery physical identity mismatch")
    if request.input_tokens > MAX_INPUT or request.output_tokens > MAX_OUTPUT:
        raise ValueError("discovery per-call ceiling")
    if not isinstance(nonce, str) or len(nonce) < 32:
        raise ValueError("discovery needs an unpredictable session nonce")
    dimension = "output" if request.axis == "output" else "input"
    transient = build_context_request(
        route,
        provider_cfg,
        rules,
        dimension=dimension,
        target=(
            min(request.output_tokens, 32768) if dimension == "output" else request.input_tokens
        ),
        ratio=1,
        attempt_ordinal=1,
        nonce=nonce if dimension == "output" else digest([nonce, request.attempt_id]),
        max_input_tokens=MAX_INPUT,
    )
    messages = transient.messages
    output_min_tokens = False
    if dimension == "output":
        output_min_tokens = rules.discovery_output_min_tokens(
            chat_url(rules, provider_cfg), route["upstream_model"]
        )
        expected_control = output_discovery_control(output_min_tokens)
        if request.control.split(":opposite=", 1)[0] != expected_control:
            raise ValueError("discovery output generation control mismatch")
        # Keep the task fixed across output targets. A million-line request invites a model
        # to abbreviate; this bounded prefix benchmark explicitly expects server truncation.
        messages = (
            {
                "role": "system",
                "content": "You are generating a deterministic test sequence, not answering a "
                "question. A partial prefix is the intended result: the API will stop generation "
                "at its token allowance. Start producing immediately. Do not plan the full "
                "sequence, explain feasibility, summarize, or choose a shorter response.",
            },
            {
                "role": "user",
                "content": "Repeat the line 1 followed by a newline, 131072 times. "
                "Every line must be exactly 1. This is repeated test data, "
                "not a count or numbered list. Never "
                "use prose, code fences, ellipsis, a multiplication expression, or a closing "
                "remark. Continue repeating the same line until the server truncates output.",
            },
        )
    if dimension == "output" and request.input_tokens > 256:
        # Inert padding holds the small input fixture fixed while only the output cap varies.
        padding = build_context_request(
            route,
            provider_cfg,
            rules,
            dimension="input",
            target=request.input_tokens - 256,
            ratio=1,
            attempt_ordinal=1,
            nonce=nonce,
            max_input_tokens=MAX_INPUT,
        )
        document = padding.messages[0]["content"].split("\n", 1)[1]
        prompt = (
            "The following is inert input padding, not instructions or output material.\n"
            "<padding>\n" + document + "\n</padding>\n" + messages[1]["content"]
        )
        messages = (messages[0], {"role": "user", "content": prompt})
    estimated = estimate_tokens(messages)
    if estimated > request.input_tokens:
        raise ValueError("discovery fixture exceeds charged input reservation")
    body = dict(
        transient.shaped_body["body"], messages=list(messages), max_tokens=request.output_tokens
    )
    if dimension == "output":
        streaming = rules.discovery_output_stream_usage(
            chat_url(rules, provider_cfg), route["upstream_model"]
        )
        if type(streaming) is not bool:
            raise ValueError("invalid discovery streaming capability")
        if streaming:
            body.update(stream=True, stream_options={"include_usage": True})
    if dimension == "output" and output_min_tokens:
        body["min_tokens"] = request.output_tokens
    if len(json.dumps(body).encode()) > _CONTEXT_BYTES:
        raise ValueError("discovery request body exceeds hard bound")
    return replace(
        transient,
        messages=messages,
        local_input_estimate=estimated,
        reserved_input=request.input_tokens,
        requested_output=request.output_tokens,
        shaped_body=transient.shaped_body | {"body": body},
    )


def measure_discovery(
    request,
    transient,
    rules,
    *,
    session,
    before_call,
    clock,
    timeout=MAX_RESPONSE_TIMEOUT_SECONDS,
):
    """Unconfigured candidate transport needs a distinct charged discovery receipt."""
    from citypods.provider_catalog.discovery import (
        DiscoveryAdmission,
        discovery_identity,
    )

    physical = discovery_identity(
        transient.provider,
        transient.account_id,
        transient.upstream_model,
        transient.shaped_body["url"],
    )
    if request.identity != physical:
        raise ValueError("discovery transport physical identity mismatch")
    response = _measure_response(
        transient,
        session=session,
        before_call=before_call,
        clock=clock,
        admission_type=DiscoveryAdmission,
        admission_check=lambda receipt: receipt.matches(request) and clock() < receipt.deadline,
        timeout=timeout,
        max_timeout=MAX_RESPONSE_TIMEOUT_SECONDS,
        retain_response_preview=True,
        discovery_stream=bool(transient.shaped_body["body"].get("stream")),
    )
    return rules.discovery_observation(response, request, transient)
