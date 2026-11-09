"""Production proposal permission never implies model qualification or auto-merge permission."""

import hashlib
import json
import shutil
from pathlib import Path

import pytest
import yaml

from citypods.compute.llm_policy import ROUTE_REGISTRY
from citypods.remedy_evaluation import RemedyConfig, validate_admission
from citypods.remedy_p3_runner import p3_enabled

ROOT = Path(__file__).parents[1]


def config():
    return RemedyConfig.model_validate(yaml.safe_load((ROOT / "config/remedy.yml").read_text()))


def test_only_the_two_human_proposers_are_operationally_authorized():
    selected = config()
    assert selected.mode == "manual" and selected.publication_mode == "live"
    assert selected.qualified_alias_types == []
    assert p3_enabled(selected, repo_root=ROOT)
    assert len(selected.admissions) == 2
    for entry in selected.admissions:
        assert entry.aa is None and entry.project_evidence is None
        assert not validate_admission(entry, ROUTE_REGISTRY).usable


@pytest.mark.parametrize("change", ["scope", "limits", "route", "profile", "hash"])
def test_authorization_fails_closed_when_approved_scope_changes(change):
    selected = config()
    if change == "scope":
        selected.qualified_alias_types = [{"type_id": "unapproved"}]
    elif change == "limits":
        selected.limits.open_prs -= 1
    elif change == "route":
        selected.p3_role_plan = "flash-deepseek-v2"
    elif change == "profile":
        selected.admissions[0].request_params_hash = "0" * 64
    else:
        selected.manual_review_authorization.sha256 = "0" * 64
    assert not p3_enabled(selected, repo_root=ROOT)


def test_even_hash_matched_authorization_cannot_allow_automatic_merge(tmp_path):
    selected = config()
    ref = selected.manual_review_authorization
    artifact = json.loads((ROOT / ref.path).read_text())
    for source in artifact["source_artifacts"]:
        path = tmp_path / source["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / source["path"], path)
    artifact["automatic_merge_allowed"] = True
    path = tmp_path / ref.path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(artifact))
    selected.manual_review_authorization.sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    assert not p3_enabled(selected, repo_root=tmp_path)


def test_publisher_uses_repository_trust_root_before_processing_live_evidence(tmp_path):
    from scripts.remedy_p3_publish import publish

    report = tmp_path / "report.json"
    evidence = tmp_path / "evidence.json"
    report.write_text("{}")
    evidence.write_text('{"sources": []}')
    # Real authorized config must reach evidence validation, without storage or GitHub calls.
    with pytest.raises(ValueError, match="missing its collection timestamp"):
        publish(report, evidence, ROOT)


def test_authorization_survives_route_and_provider_changes_inside_a_model_pool(monkeypatch):
    """The authorization binds models, so catalog churn inside a pool must not invalidate it."""
    from dataclasses import replace

    from citypods.compute import llm_policy

    selected = config()
    assert p3_enabled(selected, repo_root=ROOT)
    reference = "orcarouter_zai_glm_5_3_flash_free"
    # The exact change that invalidated the route-bound authorization: a provider concurrency edit.
    monkeypatch.setitem(
        ROUTE_REGISTRY, reference, replace(ROUTE_REGISTRY[reference], provider_concurrency=3)
    )
    pool = llm_policy.ROUTE_CANDIDATES["zai/glm-5.3-flash"]
    extra = replace(ROUTE_REGISTRY[reference], route_id="extra_glm_leg", provider="other")
    monkeypatch.setitem(llm_policy.ROUTE_CANDIDATES, "zai/glm-5.3-flash", (*pool, extra))
    assert p3_enabled(selected, repo_root=ROOT)


def test_authorization_requires_a_free_direct_route_in_each_model_pool(monkeypatch):
    from dataclasses import replace

    from citypods.compute import llm_policy

    selected = config()
    pool = llm_policy.ROUTE_CANDIDATES["deepseek/deepseek-v4.1-flash"]
    paid = tuple(replace(route, free=False) for route in pool)
    monkeypatch.setitem(llm_policy.ROUTE_CANDIDATES, "deepseek/deepseek-v4.1-flash", paid)
    assert not p3_enabled(selected, repo_root=ROOT)
