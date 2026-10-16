"""BeatAPI second free account: the same provider rules as ``beatapi`` on its own key and window.

The account's limits are per account, so it is configured as its own provider block in
``config/provider_limits.yml`` (the Worker scopes provider-level rpm/concurrency by provider
name). It shares BeatAPI's free evidence, chat filter, canary spacing and error signals.
"""

from dataclasses import replace

from citypods.provider_catalog.providers.beatapi import RULES as _BEATAPI_RULES

RULES = replace(_BEATAPI_RULES, name="beatapi_secondary")
