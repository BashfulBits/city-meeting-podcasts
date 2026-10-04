"""Committed, task-scoped configuration for R12 discovery inference."""

from __future__ import annotations

from dataclasses import replace

from citypods.compute.llm import LLMBackendConfig

# These are the only alternate routes approved for same-run civic-platform classification. They are
# all Google AI Studio routes with native structured-output support and use the Gemini secrets
# already supplied to the city-discovery workflow. Keep this list narrow: an unset credential on a
# nominally free route otherwise becomes a confusing LiteLLM "missing credentials" deferral.
DISCOVERY_FALLBACK_MODELS = (
    "gemini/gemini-3.6-flash",
    "gemini/gemini-3.8-flash",
    "gemini/gemini-3.5-flash",
)


def discovery_llm_config(site_config: dict) -> LLMBackendConfig:
    """Resolve R12's route from YAML while retaining only secret dispatch fields from env."""
    defaults = LLMBackendConfig()
    environment = LLMBackendConfig.from_env()
    configured = site_config.get("city_discovery", {})
    if configured is None:
        configured = {}
    if not isinstance(configured, dict):
        raise ValueError("city_discovery must be a mapping")
    return replace(
        environment,
        model=str(configured.get("llm_model") or defaults.model),
        mode=str(configured.get("llm_mode") or defaults.mode),
    )


def discovery_allowed_models(model: str | None = None) -> tuple[str, ...]:
    """Return the configured discovery model plus its vetted same-provider fallbacks."""
    primary = model or LLMBackendConfig.model
    return tuple(dict.fromkeys((primary, *DISCOVERY_FALLBACK_MODELS)))
