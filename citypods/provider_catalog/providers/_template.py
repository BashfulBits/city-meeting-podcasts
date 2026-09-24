"""Template for a new provider plugin (not loaded: files starting with `_` are skipped).

1. Copy to `providers/<name>.py`, where <name> is the key under `providers:` in
   config/provider_limits.yml.
2. Start observation-only (`free_evidence=None`): the reconciler lists the catalog and
   health-checks configured routes, but never proposes additions.
3. Record real responses with
   `scripts/reconcile_provider_routes.py --evidence-report --provider <name>`, then add one
   `Signal` per response shape you have actually seen, dated in its `note`.
4. Record those shapes in a fixture under tests/fixtures/provider_catalog/ and pin their verdicts
   in tests/test_provider_catalog_signals.py.
"""

from citypods.provider_catalog.rules import ProviderRules

RULES = ProviderRules(name="example")
