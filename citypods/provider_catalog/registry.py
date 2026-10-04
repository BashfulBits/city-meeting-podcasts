"""Discover provider plugins: every module in ``providers/`` that exports ``RULES``.

Files whose name starts with ``_`` (``_template.py``) are skipped. A contract test keeps this
registry and ``config/provider_limits.yml`` ``providers:`` in exact agreement.
"""

from __future__ import annotations

import importlib
import pkgutil
from functools import cache

from citypods.provider_catalog import providers as providers_pkg
from citypods.provider_catalog.rules import ProviderRules


@cache
def all_rules() -> dict[str, ProviderRules]:
    found: dict[str, ProviderRules] = {}
    for module_info in pkgutil.iter_modules(providers_pkg.__path__):
        if module_info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{providers_pkg.__name__}.{module_info.name}")
        rules = getattr(module, "RULES", None)
        if not isinstance(rules, ProviderRules):
            raise TypeError(
                f"providers/{module_info.name}.py must export RULES = ProviderRules(...)"
            )
        if rules.name in found:
            raise ValueError(f"provider {rules.name!r} is defined twice")
        found[rules.name] = rules
    return found


def rules_for(provider: str) -> ProviderRules:
    try:
        return all_rules()[provider]
    except KeyError:
        raise KeyError(
            f"no rules for provider {provider!r}: add citypods/provider_catalog/providers/"
            f"{provider}.py (copy providers/_template.py)"
        ) from None
