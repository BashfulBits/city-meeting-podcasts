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
    chat_context_observation,
    id_suffix,
    namespaced_identity,
    zero_price,
)


def _links(model: str) -> tuple[tuple[str, str], ...]:
    return (
        ("OpenRouter", f"https://openrouter.ai/{model.removesuffix(':free')}"),
        ("Artificial Analysis models", "https://artificialanalysis.ai/models"),
    )


def context_observation(response, request):
    """Native tokenizer counts; completion details are a breakdown of completion_tokens.

    https://openrouter.ai/docs/api_reference/overview
    """
    return chat_context_observation(response, request, reasoning_basis="included")


RULES = ProviderRules(
    context_observation=context_observation,
    model_identity=namespaced_identity(":free"),
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
