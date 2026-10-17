"""Evidence is content-addressed, scope-aware and independent of listing order or scores."""

from dataclasses import replace

import pytest

from citypods.provider_catalog.evidence import (
    CatalogEvidence,
    LimitObservation,
    RouteEvidence,
    candidate_digest,
    catalog_digest,
    logical_identity,
    positive_bound,
)
from citypods.provider_catalog.rules import ProviderRules, namespaced_identity


def test_catalog_digest_ignores_time_order_but_tracks_used_fields():
    a = ("creator/a", True, True, 100, 20, "creator/a")
    b = ("creator/b", True, False, 200, 30, "creator/b")
    evidence = CatalogEvidence("host", "2026-10-08", True, (a, b))
    assert catalog_digest(evidence) == catalog_digest(
        replace(evidence, observed_at="2026-10-09", models=(b, a))
    )
    assert catalog_digest(evidence) != catalog_digest(replace(evidence, complete=False))
    for index, value in ((1, False), (2, False), (3, 101), (4, 21), (5, None)):
        changed = list(a)
        changed[index] = value
        assert catalog_digest(evidence) != catalog_digest(
            replace(evidence, models=(tuple(changed), b))
        )


def test_route_evidence_roundtrip_and_lane_binding():
    proof = RouteEvidence(
        "host",
        "creator/a",
        "primary",
        "2026-10-08",
        "digest",
        True,
        True,
        100,
        20,
        "creator/a",
        "proven",
        "prompt_only",
        "2026-10-08",
        (LimitObservation("rpm", 1, "2026-10-08", "host", "primary", scope="provider_account"),),
    )
    assert RouteEvidence.from_dict(proof.to_dict()) == proof
    assert candidate_digest(proof, ("a", "b")) == candidate_digest(proof, ("b", "a"))
    assert candidate_digest(proof, ("a",)) != candidate_digest(proof, ("b",))


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "2"])
def test_observation_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        LimitObservation("rpm", value, "today", "host", "a")


def test_identity_does_not_guess_bare_names_or_aa_aliases():
    rules = ProviderRules("host", model_identity=namespaced_identity(":free"))
    assert logical_identity("host", "creator/new:free", rules, {}, []) == "creator/new"
    assert logical_identity("host", "gpt-brand:free", rules, {}, []) is None
    routes = [{"upstream_model": "gpt-brand:free", "model": "existing/pool"}]
    assert logical_identity("host", "gpt-brand:free", rules, {}, routes) == "existing/pool"
    routes += [{"upstream_model": "gpt-brand:free", "model": "different/pool"}]
    assert logical_identity("host", "gpt-brand:free", rules, {}, routes) is None


@pytest.mark.parametrize(
    "value,want", [(True, None), (0, None), (-1, None), (100, 100), ("100", 100), ("nan", None)]
)
def test_context_bound(value, want):
    assert positive_bound({"context": value}, ("context",)) == want
