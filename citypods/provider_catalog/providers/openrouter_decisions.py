"""OpenRouter's decisions endpoint (``POST /api/alpha/decisions``, review/58 12.9).

Same account and key as ``openrouter``, but its own provider block: the endpoint sits under
``https://openrouter.ai/api``, outside the ``/api/v1`` base of the AI Gateway ``openrouter`` slug,
so it goes through the ``custom-openrouter-api`` gateway provider. Its routes speak
``api_shape: openrouter_decisions``, never chat, so chat-only tooling skips them. Observation-only:
the reconciler proposes nothing for this block.
"""

from citypods.provider_catalog.rules import ProviderRules

RULES = ProviderRules(name="openrouter_decisions")
