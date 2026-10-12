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
5. For documented chat token/finish/error meanings, reuse `chat_discovery_observation` from
   `rules.py`, declare `discovery_axes` and a version, and test each supported axis. Set the
   included-reasoning basis only when verified by the endpoint contract. The shared helper
   supports exact chat/vLLM input errors and typed output-field validation; unknown forms defer.
   Do not enable combined axes or native/non-chat envelopes by assuming compatibility. See
   review/48's parser-onboarding checklist and the NVIDIA/OpenRouter adapters for examples.
"""

from citypods.provider_catalog.rules import ProviderRules

# model_identity defaults to unknown. Opt in only to an authoritative canonical publisher ID;
# AA scores/aliases never establish serving identity. limit_observations defaults to no headers;
# normalize only fixture-proven metrics and scopes, never treat account capacity as route capacity.
RULES = ProviderRules(name="example")
