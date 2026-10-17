"""Policy foundation stays local, deterministic and inert until reviewed proof is supplied."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from citypods.config import load_city_configs
from citypods.models import City
from citypods.records import source_key
from citypods.remedy_evaluation import RemedyConfig
from citypods.remedy_policy import (
    EXCLUSIONS,
    MIGRATIONS,
    PROOFS,
    PolicyIndex,
    PolicyTemplate,
    canonical_hash,
    instantiate_policies,
    load_policies,
    load_policy_templates,
    resolve_owner,
    validate_declaration,
)

APPROVAL = "https://github.com/example/catalog/issues/1#issuecomment-2"


def template():
    return {
        "id": "named-board",
        "version": 1,
        "approval_ref": APPROVAL,
        "scope": "cross_city",
        "city_source": None,
        "identity_type": "named_body",
        "aggregate_family": None,
        "permitted_transformations": ["normalized_exact_label"],
        "selector_forms": ["body_exact"],
        "official_proof_requirements": sorted(PROOFS),
        "exclusion_boundaries": sorted(EXCLUSIONS),
        "migration_boundaries": sorted(MIGRATIONS),
        "positive_case_ids": ["real-board"],
        "negative_case_ids": ["council-topic"],
        "transfer_case_ids": ["unseen-city-board"],
    }


def index(value=None):
    config = yaml.safe_load(Path("config/remedy.yml").read_text())
    config["policy_templates"] = [value or template()]
    return load_policy_templates(config)


def city():
    result = City(
        "example-tx-board",
        "granicus",
        {"body_exact": "Library Board"},
        "Board",
        "City",
        "",
        "Meetings",
        source_id="pinned-source",
        city_entity="example-tx",
    )
    result.extra["remedy_policy"] = {
        "identity_names": ["Library Board"],
        "policy_id": "example-board",
        "version": 1,
        "approval_ref": APPROVAL,
        "template_id": "named-board",
        "template_version": 1,
        "positive_case_ids": ["real-board"],
        "negative_case_ids": ["council-topic"],
    }
    return result


def packet():
    span = "Library Board convenes a public meeting; archive lists its recording."
    return {
        "schema_version": 1,
        "city": "example-tx",
        "source_key": "pinned-source",
        "approval_ref": APPROVAL,
        "policy_id": "example-board",
        "policy_version": 1,
        "template_id": "named-board",
        "template_version": 1,
        "identity_bindings": [
            {
                "identity_name": "Library Board",
                "evidence_ref_ids": ["agenda"],
                "recording_guids": ["clip-7"],
            }
        ],
        "evidence_refs": [
            {
                "id": "agenda",
                "url": "https://example.test/agenda",
                "retrieved_at": "2026-10-09",
                "source_span": span,
                "content_hash": canonical_hash(span),
            }
        ],
        "recordings": [
            {
                "uid": "saved-uid",
                "provider_guid": "clip-7",
                "body": "Library Board",
                "title": "Library Board Meeting",
                "date": "2026-10-01",
                "canonical_url": "https://example.test/video/7",
            }
        ],
        "completeness": {
            "status": "complete_available",
            "evidence_ref_ids": ["agenda"],
            "exception_refs": [],
        },
        "positive_case_ids": ["real-board"],
        "negative_case_ids": ["council-topic"],
        "transfer_case_ids": ["unseen-city-board"],
    }


@pytest.mark.parametrize(
    "key,value",
    [
        ("version", True),
        ("version", "1"),
        ("version", 0),
        ("unknown", "x"),
        ("approval_ref", "http://github.com/example/catalog/issues/1"),
        ("approval_ref", "https://github.com/example/catalog/tree/main"),
        ("scope", "world"),
        ("city_source", {"city": "example-tx", "source_key": "pinned"}),
        ("aggregate_family", "bond"),
        ("permitted_transformations", ["fuzzy"]),
        ("official_proof_requirements", ["official_body_identity"]),
        ("exclusion_boundaries", []),
        ("migration_boundaries", ["uid"]),
        ("positive_case_ids", ["repeat", "repeat"]),
        ("transfer_case_ids", []),
    ],
)
def test_template_rejects_invalid_metadata(key, value):
    raw = template()
    raw[key] = value
    with pytest.raises(ValueError):
        PolicyTemplate.model_validate(raw)


@pytest.mark.parametrize(
    "changes",
    [
        {"version": True},
        {"version": "1"},
        {"version": 0},
        {"policy_id": "missing-tuple"},
        {"extra": 1},
        {"template_id": "named-board"},
        {"identity_names": ["Board*"]},
        {"identity_names": []},
        {"positive_case_ids": []},
        {"positive_case_ids": [1]},
        {"aggregate_family": "unknown"},
    ],
)
def test_declaration_rejects_invalid_provenance(changes):
    value = {"identity_names": ["Board"]}
    value.update(changes)
    with pytest.raises(ValueError, match="test-feed"):
        validate_declaration(value, "test-feed")


def test_default_empty_and_immutable_canonical_template_index():
    config = yaml.safe_load(Path("config/remedy.yml").read_text())
    config.pop("policy_templates")
    assert RemedyConfig.model_validate(config).policy_templates == []
    assert load_policy_templates(config).entries == ()
    first = index()
    raw = template()
    raw["official_proof_requirements"].reverse()
    assert index(raw).hashes == first.hashes
    with pytest.raises((ValueError, TypeError, AttributeError)):
        first.entries[0].positive_case_ids += ("mutated",)
    config["policy_templates"] = [template(), template()]
    with pytest.raises(ValueError, match="duplicate"):
        load_policy_templates(config)


def test_official_packet_resolves_unseen_city_without_io_or_mutation(monkeypatch):
    import citypods.remedy_evaluation as evaluation

    monkeypatch.setattr(evaluation, "load_cases", lambda *_: pytest.fail("implicit gold IO"))
    local, proof = city(), packet()
    original = deepcopy(local)
    original_packet = deepcopy(proof)
    policies = instantiate_policies(local, source_key(local), index(), proof)
    assert not policies.unresolved
    result = resolve_owner("Library Board", "pinned-source", policies)
    assert result.status == "verified"
    assert result.owner_slugs == (local.slug,)
    assert result.evidence_refs == ("agenda",)
    assert local == original and proof == original_packet
    assert (
        resolve_owner("Council Library Board discussion", "pinned-source", policies).status
        == "unknown"
    )
    assert resolve_owner("Library Board", "other-source", policies).status == "unknown"
    # Maintenance and onboarding use the same source-local index, never a second resolver.
    assert resolve_owner("Library Board", "pinned-source", PolicyIndex(policies.resolved)) == (
        resolve_owner("Library Board", "pinned-source", policies)
    )


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p.update(source_key="different"),
        lambda p: p.update(city="other-tx"),
        lambda p: p.update(policy_version=2),
        lambda p: p.update(template_version=2),
        lambda p: p.update(approval_ref="https://github.com/example/catalog/issues/3"),
        lambda p: p["completeness"].update(status="incomplete"),
        lambda p: p["completeness"].update(evidence_ref_ids=["absent"]),
        lambda p: p["identity_bindings"][0].update(evidence_ref_ids=["absent"]),
        lambda p: p["identity_bindings"][0].update(recording_guids=["absent"]),
        lambda p: p["recordings"][0].update(body="Council"),
        lambda p: p["evidence_refs"][0].update(content_hash="wrong"),
        lambda p: p.update(positive_case_ids=["invented"]),
        lambda p: p.update(transfer_case_ids=["invented"]),
    ],
)
def test_missing_or_contradictory_packet_cannot_acquire_owner(change):
    proof = packet()
    change(proof)
    policies = instantiate_policies(city(), "pinned-source", index(), proof)
    assert not policies.resolved and policies.unresolved
    assert resolve_owner("Library Board", "pinned-source", policies).status == "ambiguous"
    assert not resolve_owner("Library Board", "pinned-source", policies).owner_slugs


def test_city_source_binding_does_not_transfer_and_unknown_revision_stays_unresolved():
    raw = template()
    raw.update(
        scope="city_source",
        city_source={"city": "example-tx", "source_key": "pinned-source"},
        transfer_case_ids=[],
    )
    proof = packet()
    proof["transfer_case_ids"] = []
    assert instantiate_policies(city(), "pinned-source", index(raw), proof).resolved
    raw["city_source"]["source_key"] = "other-source"
    assert instantiate_policies(city(), "pinned-source", index(raw), proof).unresolved
    local = city()
    local.extra["remedy_policy"]["template_version"] = 2
    assert instantiate_policies(local, "pinned-source", index(), proof).unresolved
    assert instantiate_policies(city(), "wrong-source", index(), proof).unresolved


def test_legacy_guard_semantics_joint_owner_and_exact_supersedes_holding():
    from citypods.remedy_policy import _declaration_instance

    named = _declaration_instance({"identity_names": ["Bond Open House"]}, "public", "city", "src")
    joint = replace(named, owner_slug="joint", policy_id="legacy:joint")
    holding = _declaration_instance({"aggregate_family": "bond"}, "bond", "city", "src")
    policies = PolicyIndex((named, joint, holding))
    result = resolve_owner("Bond Open House", "src", policies)
    assert result.owner_slugs == ("joint", "public")
    assert result.holding_owner_slugs == ("bond", "joint", "public")
    assert named.status == "legacy_unversioned" and named.version is None
    tif = _declaration_instance({"aggregate_family": "tif"}, "tif", "city", "src")
    assert resolve_owner("TIRZ Board", "src", PolicyIndex((tif,))).status == "verified"
    assert resolve_owner("Council TIRZ discussion", "src", PolicyIndex((tif,))).status == "unknown"
    assert resolve_owner("Bond Advisory Board", "src", policies).status == "unknown"


def test_load_current_config_and_shared_root_isolation():
    paths = {p.stem: p for p in Path("config/feeds").glob("*.yml") if not p.name.startswith("_")}
    policies = load_policies(paths)
    assert policies.instances
    assert all(p.status == "legacy_unversioned" for p in policies.instances)
    assert len(load_city_configs("config", {})) == len(paths)
    assert load_policies({}).instances == ()
    one = next(iter(paths))
    with pytest.raises(ValueError, match="shared config root"):
        load_policies({one: paths[one], "other": Path("other/feeds/other.yml")})
    with pytest.raises(ValueError, match="slug mismatch"):
        load_policies({"wrong": paths[one]})


def test_tif_marker_does_not_supersede_other_family_hold(tmp_path):
    from citypods.audit_remedy import BodyProposal, _aggregate_policy_reason

    paths = {}
    for slug, family in (("tif", "tif"), ("bond", "bond")):
        path = tmp_path / f"{slug}.yml"
        path.write_text(yaml.safe_dump({"remedy_policy": {"aggregate_family": family}}))
        paths[slug] = path
    proposal = BodyProposal(
        source_key="src",
        unexpected_body="TIRZ Bond Advisory",
        action="union",
        target_feeds=["tif"],
        rationale="test",
    )
    assert "requires the owning" in _aggregate_policy_reason(proposal, set(paths), paths)


def test_legacy_aggregate_empty_names_and_duplicate_names_remain_compatible():
    value = {"aggregate_family": "tif", "identity_names": [], "member_names": ["Board", "Board"]}
    assert validate_declaration(value) == value
    with pytest.raises(ValueError):
        validate_declaration({"aggregate_family": None, "identity_names": ["Board"]})


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p.update(schema_version=True),
        lambda p: p["identity_bindings"][0].update(evidence_ref_ids=["agenda", "agenda"]),
        lambda p: p["evidence_refs"][0].update(id=""),
    ],
)
def test_packet_cannot_use_coerced_or_empty_identity(change):
    proof = packet()
    change(proof)
    assert instantiate_policies(city(), "pinned-source", index(), proof).unresolved


def test_sanitized_transfer_fixture_is_not_loaded_as_evaluation_truth():
    import json

    fixture = json.loads(Path("evals/remedy/policies/p2a-foundation/transfer.json").read_text())
    assert "not independent" in fixture["provenance"]
    result = instantiate_policies(
        city(), "pinned-source", index(fixture["template"]), fixture["official_evidence"]
    )
    assert result.resolved and not result.unresolved


def test_unknown_template_reference_cannot_fall_back_to_legacy_guard(tmp_path):
    from citypods.audit_remedy import (
        BodyProposal,
        _aggregate_policy_reason,
        _target_feed_is_compatible,
    )

    path = tmp_path / "owner.yml"
    path.write_text(yaml.safe_dump({"remedy_policy": city().extra["remedy_policy"]}))
    assert not _target_feed_is_compatible("Library Board", path)
    proposal = BodyProposal(
        source_key="src",
        unexpected_body="Library Board",
        action="union",
        target_feeds=["owner"],
        rationale="test",
    )
    assert "manual identity" in _aggregate_policy_reason(proposal, {"owner"}, {"owner": path})


@pytest.mark.parametrize(
    "inclusions",
    [
        [{}],
        ["not-a-mapping"],
        {"provider_guid": "clip-7", "body": "Library Board"},
        [{"provider_guid": "", "body": "Library Board"}],
        [{"provider_guid": "clip-7"}],
        [{"provider_guid": "clip-7", "body": "Library Board"}] * 2,
    ],
)
def test_hand_built_source_invalid_inclusions_remain_unresolved(inclusions):
    local = city()
    local.source["body_includes"] = inclusions
    result = instantiate_policies(local, "pinned-source", index(), packet())
    assert not result.resolved
    assert result.unresolved[0].proof_diagnostics == ("invalid recording inclusion declaration",)
    assert not resolve_owner("Library Board", "pinned-source", result).owner_slugs


def test_hand_built_source_inclusions_use_shared_parser_and_official_packet():
    local = city()
    local.source["body_includes"] = [{"provider_guid": " clip-7 ", "body": "Library Board"}]
    raw = template()
    raw["permitted_transformations"].append("reviewed_recording_inclusion")
    raw["selector_forms"].append("body_includes")
    raw["official_proof_requirements"].append("exact_provider_guid")
    result = instantiate_policies(local, "pinned-source", index(raw), packet())
    assert result.resolved and not result.unresolved
    assert resolve_owner("Library Board", "pinned-source", result).owner_slugs == (local.slug,)


def test_coverage_preserves_conflicts_exact_guid_and_source_isolation():
    from citypods.remedy_policy import replay_coverage

    feed = city()
    feed.source["body_exact"] = ["Library Board"]
    key = source_key(feed)
    base = {
        "source_key": key,
        "uid": "stable",
        "provider_guid": "view=1&id=2",
        "body": "Library Board",
        "title": "Original",
        "date": "2020-01-01",
        "observation_refs": ["record:stable"],
    }
    fetched = {**base, "uid": None, "observation_refs": ["fetch:0"]}
    other_view = {**fetched, "provider_guid": "view=9&id=2"}
    other_source = {**base, "source_key": "different"}
    replay = replay_coverage([base, fetched, other_view, other_source], [feed])
    assert len(replay.rows) == 3
    merged = next(row for row in replay.rows if row.source_key == key and row.uid == "stable")
    assert merged.observation_refs == ("fetch:0", "record:stable")
    assert merged.configured_owner_slugs == (feed.slug,)
    assert merged.verified_owner_slugs == ()
    assert next(row for row in replay.rows if row.source_key == "different").status == "unknown"
    conflict = replay_coverage([base, {**fetched, "title": "Contradiction"}], [feed])
    assert len(conflict.rows) == 2
    assert all(row.status == "ambiguous" for row in conflict.rows)
    assert all("identity-conflict" in row.diagnostics for row in conflict.rows)


def test_coverage_hash_order_clocks_and_unproven_observations():
    from citypods.remedy_policy import material_evidence_hash, replay_coverage

    assert material_evidence_hash({"observed_at": "a", "observations": ["x", "y"]}) == (
        material_evidence_hash({"observed_at": "b", "observations": ["y", "x"]})
    )
    observation = {"source_key": "s", "body": None, "observation_refs": ["one"]}
    replay = replay_coverage([observation, observation], [])
    assert len(replay.rows) == 2
    assert all("uniqueness-unknown" in row.diagnostics for row in replay.rows)
    assert all("missing-or-malformed-label" in row.diagnostics for row in replay.rows)


def test_coverage_keeps_joint_and_city_aggregate_matches_separate_from_policy_proof():
    from citypods.remedy_policy import _declaration_instance, replay_coverage

    feed = city()
    feed.source = {"body": "Library"}
    joint = replace(feed, slug="joint", source={"body": "Council"})
    aggregate = replace(feed, slug="all", source={})
    label = "Council and Library Joint Meeting"
    policy = _declaration_instance({"identity_names": [label]}, "joint", "city", source_key(feed))
    observation = {"source_key": source_key(feed), "uid": "stable", "body": label}
    replay = replay_coverage([observation], [feed, joint, aggregate], PolicyIndex((policy,)))
    row = replay.rows[0]
    assert row.configured_owner_slugs == ("all", "example-tx-board", "joint")
    assert row.verified_owner_slugs == ("joint",)
    assert row.status == "selected"
    assert row.diagnostics == ("configured-verified-owners-differ",)
    assert dict(replay.totals)["selected"] == dict(replay.totals)["verified_policy"] == 1


def test_coverage_shared_guid_keeps_unproven_observations_separate():
    from citypods.remedy_policy import replay_coverage

    base = {"source_key": "s", "provider_guid": "shared", "body": "Council"}
    observations = [
        {**base, "uid": "one"},
        {**base, "uid": "two"},
        {**base, "observation_refs": ["fetch:a"]},
        {**base, "observation_refs": ["fetch:b"]},
    ]
    rows = replay_coverage(observations, []).rows
    assert len(rows) == 4
    retained = [row for row in rows if row.uid]
    fresh = [row for row in rows if not row.uid]
    assert {row.uid for row in retained} == {"one", "two"}
    assert all("uniqueness-unknown" not in row.diagnostics for row in retained)
    assert all("uniqueness-unknown" in row.diagnostics for row in fresh)
    assert all("identity-conflict" not in row.diagnostics for row in rows)
    assert {row.observation_refs for row in fresh} == {("fetch:a",), ("fetch:b",)}
