"""Committed, task-scoped configuration for R12 discovery inference."""

from __future__ import annotations

from dataclasses import replace

from citypods.compute.llm import LLMBackendConfig


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
