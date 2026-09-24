"""Provider I/O: list a catalog and send one bounded canary. No provider names appear here.

Everything provider-specific comes from the plugin (`ProviderRules`) and the provider's block in
config/provider_limits.yml (`api_base`, `chat_path`, `accounts`).
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import requests

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
            if spec.style == "google":
                for item in payload.get("models") or []:
                    name = str(item.get("name") or "").removeprefix("models/")
                    if name:
                        models[name] = item
                token = payload.get("nextPageToken")
                if not token:
                    break
                params = {"pageSize": 1000, "pageToken": token}
                continue
            items = payload if isinstance(payload, list) else payload.get("data") or []
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
