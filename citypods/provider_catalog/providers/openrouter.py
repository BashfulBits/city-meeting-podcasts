"""OpenRouter: the literal `:free` variant plus explicit zero prompt/completion pricing.

Live evidence 2026-09-24: 403 "only available on agentic harnesses" is a model the API key cannot
use; 429 "temporarily rate-limited upstream" is shared-capacity noise (inconclusive).
"""

from citypods.provider_catalog.rules import (
    END_OF_LIFE,
    MODEL_DOES_NOT_EXIST,
    ProviderRules,
    Signal,
    all_free,
    body_contains,
    id_suffix,
    zero_price,
)


def _links(model: str) -> tuple[tuple[str, str], ...]:
    return (
        ("OpenRouter", f"https://openrouter.ai/{model.removesuffix(':free')}"),
        ("Artificial Analysis models", "https://artificialanalysis.ai/models"),
    )


RULES = ProviderRules(
    name="openrouter",
    free_evidence=all_free(id_suffix(":free"), zero_price("prompt", "completion")),
    free_suffix=":free",
    research_links=_links,
    signals=(
        END_OF_LIFE,
        MODEL_DOES_NOT_EXIST,
        Signal("not_entitled", 403, body_contains("agentic harness"), "403 agentic-only model"),
    ),
)
