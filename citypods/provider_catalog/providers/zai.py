"""Z.ai. Observation-only for additions: its `/models` omits the free flash models entirely.

Live evidence 2026-09-24: `glm-4.5-flash` is absent from `/models` yet completes (200), while the
listed models answer 429 code 1113 "Insufficient balance" (paid). So catalog absence is not
evidence here, and discovery from the catalog would only ever find paid models. Configured routes
are still health-checked; code 1305 "temporarily overloaded" is inconclusive.
"""

from citypods.provider_catalog.rules import ProviderRules, Signal, json_error_code

RULES = ProviderRules(
    name="zai",
    catalog_is_complete=False,
    signals=(Signal("not_entitled", 429, json_error_code("1113"), "429 code 1113 paid model"),),
)
