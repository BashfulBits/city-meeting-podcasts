"""Provider registry.

A new platform is added by implementing :class:`MeetingProvider` and registering
an instance here. Lookup is by the ``provider:`` key in a city's YAML.
"""

from __future__ import annotations

from citypods.providers.base import MeetingProvider, ProviderError
from citypods.providers.civicclerk import CivicClerkProvider
from citypods.providers.civicplus import CivicPlusProvider
from citypods.providers.granicus import GranicusProvider
from citypods.providers.swagit import SwagitProvider

_REGISTRY: dict[str, MeetingProvider] = {}


def register(provider: MeetingProvider) -> None:
    # CR2-CP-29: a copy-paste/typo registering the same name twice used to silently overwrite
    # the earlier entry instead of surfacing the collision.
    if provider.name in _REGISTRY and _REGISTRY[provider.name] is not provider:
        raise ProviderError(f"provider {provider.name!r} is already registered")
    _REGISTRY[provider.name] = provider


def get_provider(name: str) -> MeetingProvider:
    try:
        return _REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "(none)"
        raise ProviderError(f"Unknown provider {name!r}. Registered: {known}") from None


def provider_names() -> list[str]:
    return sorted(_REGISTRY)


# Built-in providers.
register(GranicusProvider())
register(CivicPlusProvider())
register(CivicClerkProvider())
register(SwagitProvider())

__all__ = ["MeetingProvider", "ProviderError", "get_provider", "provider_names", "register"]
