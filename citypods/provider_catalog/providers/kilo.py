"""Kilo Gateway: an OpenRouter-compatible catalog with the same `:free` + zero-price convention."""

from citypods.provider_catalog.rules import (
    END_OF_LIFE,
    MODEL_DOES_NOT_EXIST,
    ProviderRules,
    all_free,
    id_suffix,
    zero_price,
)

RULES = ProviderRules(
    name="kilo",
    free_evidence=all_free(id_suffix(":free"), zero_price("prompt", "completion")),
    free_suffix=":free",
    signals=(END_OF_LIFE, MODEL_DOES_NOT_EXIST),
)
