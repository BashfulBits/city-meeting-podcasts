"""BeatAPI: free evidence is the `-free` id suffix; `jev-1.13-free` is not a chat model.

`GET /v1/models` is OpenAI-shaped (41 models, live 2026-10-07). Six ids end in `-free`: five chat
models and `jev-1.13-free`, whose `owned_by` is "task plugin" and which is called through
`POST /v1/systemone` (P1 breakout), so the chat filter drops it. Free requests share one
account-wide limit of 1 successful request per minute, hence the canary spacing.

Signals below come from docs.beatapi.io/decisions (429 `rate_limit_exceeded`, 402
`insufficient_credits`, 403 `forbidden`), not yet from a captured response; pin them with a
fixture once the reconcile workflow has recorded real ones.
"""

from citypods.provider_catalog.rules import (
    MODEL_DOES_NOT_EXIST,
    ProviderRules,
    Signal,
    default_chat_filter,
    id_suffix,
    json_error_code,
)

RULES = ProviderRules(
    name="beatapi",
    free_evidence=id_suffix("-free"),
    free_suffix="-free",
    chat_filter=lambda model, record: (
        default_chat_filter(model, record)
        and "image" not in model.lower()
        and record.get("owned_by") != "task plugin"
    ),
    canary_interval_seconds=65.0,
    signals=(
        Signal("quota_exhausted", 429, json_error_code("rate_limit_exceeded"), "documented 429"),
        Signal("not_entitled", 402, json_error_code("insufficient_credits"), "documented 402"),
        Signal("not_entitled", 403, json_error_code("forbidden"), "documented 403"),
        MODEL_DOES_NOT_EXIST,
    ),
)
