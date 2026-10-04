"""api.airforce: the record's own `tier: free` (not the broader `access_tiers` list).

Live evidence 2026-09-24: 402 "requires an active subscription or a positive Pay-as-you-Go
balance" means the model is paid for this account; 503 "every channel behind it is ..." and the
429 global 1 rps limit are capacity noise.
"""

from citypods.provider_catalog.rules import (
    MODEL_DOES_NOT_EXIST,
    ProviderRules,
    Signal,
    body_contains,
    field_equals,
)

RULES = ProviderRules(
    name="airforce",
    free_evidence=field_equals("tier", "free"),
    # api_base already ends in /v1; the compiled chat_path repeats it for the AI Gateway form.
    canary_path="/chat/completions",
    canary_interval_seconds=1.5,
    signals=(
        Signal("not_entitled", 402, body_contains("subscription"), "402 subscription required"),
        Signal("quota_exhausted", 429, body_contains("global rate limit"), "429 global rate limit"),
        MODEL_DOES_NOT_EXIST,
    ),
)
