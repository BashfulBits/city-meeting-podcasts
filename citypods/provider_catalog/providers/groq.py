"""Groq (free account: every chat model it lists is a free-route candidate).

Live evidence 2026-09-24: a model Groq withdrew returns 404 "does not exist or you do not have
access" and is gone from `/models` -- on this all-free account that is a retirement. Responses
carry `x-ratelimit-limit-requests` (per day) and `x-ratelimit-limit-tokens` (per minute).
"""

from citypods.provider_catalog.rules import MODEL_DOES_NOT_EXIST, ProviderRules, account_level

RULES = ProviderRules(
    name="groq",
    free_evidence=account_level(),
    signals=(MODEL_DOES_NOT_EXIST,),
)
