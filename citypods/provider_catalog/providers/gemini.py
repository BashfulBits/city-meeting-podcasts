"""Google AI Studio (Gemini API, OpenAI-compatible chat; native model list).

The whole project is on the free tier, so free evidence is account-level and the canary decides
(live evidence 2026-09-24): 200 = in the free tier; 404 "no longer available to new users" =
retired for this project; a 429 whose free-tier quota violations carry NO quotaValue on models
production never dispatches (gemini-pro-latest, gemini-3.1-pro-preview) = outside the free tier;
a 429 WITH a numeric quotaValue (e.g. '20' requests/day on gemini-3.5-flash) = a real quota that
production spent -- defer, never judge. 503 "high demand" is inconclusive.

Chat uses Bearer auth; `x-goog-api-key` is rejected (400) on the OpenAI-compatible endpoint. The
native list pages at 50 by default, so the catalog follows `nextPageToken`.
"""

from __future__ import annotations

from typing import Any

from citypods.provider_catalog.rules import (
    CatalogSpec,
    ProviderRules,
    Response,
    Signal,
    account_level,
    body_contains,
    default_chat_filter,
)


def _free_tier_violations(response: Response) -> list[dict[str, Any]]:
    data = response.json()
    error = data.get("error") if isinstance(data, dict) else None
    details = error.get("details") if isinstance(error, dict) else None
    return [
        violation
        for detail in details or []
        if isinstance(detail, dict)
        for violation in detail.get("violations") or []
        if isinstance(violation, dict) and "FreeTier" in str(violation.get("quotaId", ""))
    ]


def free_tier_quota_spent(response: Response) -> bool:
    return any(v.get("quotaValue") not in (None, "", "0") for v in _free_tier_violations(response))


def free_tier_not_provisioned(response: Response) -> bool:
    violations = _free_tier_violations(response)
    return bool(violations) and all(v.get("quotaValue") in (None, "", "0") for v in violations)


def _chat_capable(model: str, record: Any) -> bool:
    methods = record.get("supportedGenerationMethods")
    return (methods is None or "generateContent" in methods) and default_chat_filter(model, record)


RULES = ProviderRules(
    name="gemini",
    catalog=CatalogSpec(
        style="google",
        auth="google_api_key",
        url="https://generativelanguage.googleapis.com/v1beta/models",
    ),
    free_evidence=account_level(),
    chat_filter=_chat_capable,
    creator="google",
    signals=(
        Signal("retired", 404, body_contains("no longer available"), "404 no longer available"),
        Signal("quota_exhausted", 429, free_tier_quota_spent, "429 free-tier quota spent"),
        Signal("not_entitled", 429, free_tier_not_provisioned, "429 no free-tier quota"),
    ),
)
