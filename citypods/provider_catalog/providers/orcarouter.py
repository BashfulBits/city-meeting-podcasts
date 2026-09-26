"""OrcaRouter: a `-free` ID suffix, a "(Free)" display name, and zero per-request pricing."""

from citypods.provider_catalog.rules import (
    END_OF_LIFE,
    MODEL_DOES_NOT_EXIST,
    ProviderRules,
    all_free,
    id_suffix,
    name_contains,
    zero_price,
)

RULES = ProviderRules(
    name="orcarouter",
    free_evidence=all_free(id_suffix("-free"), name_contains("(free)"), zero_price("request")),
    free_suffix="-free",
    signals=(END_OF_LIFE, MODEL_DOES_NOT_EXIST),
)
