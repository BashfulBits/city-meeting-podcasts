"""Mistral La Plateforme (free "Experiment" account; every key shares one account tier).

Chat-capable entries (`capabilities.completion_chat`) are account-level free candidates; the
canary decides (live evidence 2026-09-24): `x-ratelimit-limit-req-minute: 0` on a 429 means the
account tier provisions nothing for that model (medium/small/devstral/magistral on all three
keys); 403 `tier_not_allowed` means outside the plan (mistral-large); 403 "is a Labs model" is an
account preference toggle. None of these is a retirement.
"""

from __future__ import annotations

from typing import Any

from citypods.provider_catalog.rules import (
    MODEL_DOES_NOT_EXIST,
    CatalogSpec,
    ProviderRules,
    Signal,
    account_level,
    body_contains,
    body_matches,
    default_chat_filter,
    header_equals,
)


def _chat_capable(model: str, record: Any) -> bool:
    capabilities = record.get("capabilities")
    return (
        isinstance(capabilities, dict)
        and capabilities.get("completion_chat") is True
        and default_chat_filter(model, record)
    )


RULES = ProviderRules(
    name="mistral",
    catalog=CatalogSpec(path="/v1/models"),
    free_evidence=account_level(),
    chat_filter=_chat_capable,
    creator="mistral",
    signals=(
        Signal(
            "account_blocked",
            429,
            header_equals("x-ratelimit-limit-req-minute", "0"),
            "429 with a provisioned limit of 0 rpm",
        ),
        Signal("not_entitled", 403, body_contains("tier_not_allowed"), "403 tier_not_allowed"),
        Signal("account_blocked", 403, body_contains("labs model"), "403 Labs model disabled"),
        Signal("retired", 400, body_matches(r"invalid model"), "400 invalid model"),
        MODEL_DOES_NOT_EXIST,
    ),
)
