"""Selections need fresh safe proofs; commands and publication fail closed."""

import json
from dataclasses import replace
from datetime import date

import pytest

from citypods.compute.llm_lanes import LaneConfig
from citypods.provider_catalog.apply import (
    PR_MARKER,
    SOURCE_PATHS,
    ApplyConfig,
    Decision,
    EditPlan,
    parse_decisions,
    plan_apply,
    proposal_body,
)
from citypods.provider_catalog.evidence import RouteEvidence, candidate_digest
from citypods.provider_catalog.issue import MARKER, _discovery_attempt_diagnostics, render_body
from citypods.provider_catalog.reconcile import Anomaly, Candidate, Report
from scripts.provider_catalog_commands import process_event, publish

TODAY = date(2026, 10, 8)
PROOF = RouteEvidence(
    "host",
    "creator/new",
    "primary",
    TODAY.isoformat(),
    "catalog",
    True,
    True,
    10000,
    4000,
    "creator/new",
    "proven",
    "prompt_only",
    TODAY.isoformat(),
)
CANDIDATE = Candidate(
    "host",
    "creator/new",
    42,
    False,
    10000,
    (),
    ("lane",),
    TODAY.isoformat(),
    structured_output_method="prompt_only",
    evidence=PROOF.to_dict(),
    evidence_digest=candidate_digest(PROOF, ("lane",)),
)
DISCOVERY = {
    "profile": "history",
    "axes": [
        {
            "axis": "input",
            "admitted": 12000,
            "count_basis": "input",
            "control": "input:v2",
            "source": "https://catalog.example/models",
        }
    ],
    "input_characterization": {
        "admitted_input": 12000,
        "generated_output": 234,
        "count_basis": "input",
        "control": "input:v2",
        "identity": "physical-route-digest",
        "parser_version": "provider-parser-v1",
        "fixture_version": "discovery-v1",
        "evidence_digest": "a" * 64,
    },
}


def config(routes=()):
    return ApplyConfig(
        {
            "providers": {"host": {"accounts": [{"id": "primary"}]}},
            "routes": list(routes),
            "structured_output_methods": {"prompt_only": {}},
        },
        {"lane": LaneConfig("lane", ("old",), 1, 1, 1)},
        dict.fromkeys(SOURCE_PATHS, "source"),
        "main-sha",
        TODAY,
    )


def plan(proof=PROOF, decision=None):
    report = Report(candidates=[replace(CANDIDATE, evidence=proof.to_dict(), discovery=DISCOVERY)])
    return plan_apply(
        report,
        (
            decision
            or Decision(
                "host",
                "creator/new",
                "add",
                ("lane",),
                PROOF,
                discovery_digest=DISCOVERY["input_characterization"]["evidence_digest"],
            ),
        ),
        config(),
    )


def test_selected_addition_preserves_primary_and_records_route_method():
    result = plan()
    assert len(result.routes) == 1
    route = dict(result.routes[0])
    assert route["model"] == "creator/new" and route["free"] is True
    assert route["structured_output_method"] == "prompt_only"
    assert route["hard_input_ceiling"] == 12000
    assert route["input_context_limit"] == 12234
    assert result.route_notes[0][0] == route["route_id"]
    assert "12000 provider-counted input tokens admitted" in result.route_notes[0][1][0]
    assert route["rpm"] == route["concurrency"] == 1
    assert result.backups == (("lane", "creator/new"),)
    assert not result.deferred and not result.rejected


@pytest.mark.parametrize(
    "changes",
    [
        {"catalog_complete": False},
        {"free": False},
        {"contended": True},
        {"observed_at": "2026-10-07"},
        {"structured_output_verified_on": "2026-10-07"},
        {"structured_output_method": None},
        {"verdict": "inconclusive"},
        {"input_context_limit": 20000},
        {"model_key": "other"},
        {"account_id": "other"},
    ],
)
def test_incomplete_stale_changed_or_deferred_proof_cannot_write(changes):
    result = plan(replace(PROOF, **changes))
    assert not result.routes and result.deferred


def test_an_unrelated_catalog_change_does_not_defer_a_selection():
    # Stored evidence predates a listing change elsewhere; this model itself is unchanged.
    stored = replace(PROOF, catalog_digest="older-listing")
    result = plan(
        PROOF,
        Decision(
            "host",
            "creator/new",
            "add",
            ("lane",),
            stored,
            discovery_digest=DISCOVERY["input_characterization"]["evidence_digest"],
        ),
    )
    assert len(result.routes) == 1 and not result.deferred


def test_a_changed_entry_for_the_model_itself_still_defers():
    stored = replace(PROOF, model_digest="a" * 64)
    fresh = replace(PROOF, model_digest="b" * 64)
    result = plan(
        fresh,
        Decision(
            "host",
            "creator/new",
            "add",
            ("lane",),
            stored,
            discovery_digest=DISCOVERY["input_characterization"]["evidence_digest"],
        ),
    )
    assert not result.routes and "candidate changed" in result.deferred[0]
    # Evidence recorded before the per-model digest existed is compared on its other fields only.
    legacy = replace(PROOF, model_digest="")
    ok = plan(
        fresh,
        Decision(
            "host",
            "creator/new",
            "add",
            ("lane",),
            legacy,
            discovery_digest=DISCOVERY["input_characterization"]["evidence_digest"],
        ),
    )
    assert len(ok.routes) == 1


def test_model_digest_ignores_other_models_and_old_evidence_keeps_its_candidate_digest():
    from citypods.provider_catalog.evidence import (
        CatalogEvidence,
        candidate_digest,
        digest,
        model_catalog_digest,
    )

    def entry(name, free=True):
        return (name, True, free, 1000, 100, None)

    one = CatalogEvidence("host", "t", True, (entry("a"), entry("b")))
    two = CatalogEvidence("host", "t", True, (entry("a"), entry("c"), entry("b")))
    assert model_catalog_digest(one, "a") == model_catalog_digest(two, "a")
    changed = CatalogEvidence("host", "t", True, (entry("a", free=False), entry("b")))
    assert model_catalog_digest(one, "a") != model_catalog_digest(changed, "a")
    # Without a model digest the hash is byte-for-byte what it was before the field existed.
    legacy = {k: v for k, v in PROOF.to_dict().items() if k != "model_digest"}
    assert candidate_digest(PROOF, ("lane",)) == digest({"evidence": legacy, "lanes": ["lane"]})


def test_unknown_identity_is_deferred_and_legacy_state_is_advisory():
    unknown = replace(PROOF, model_key=None)
    result = plan(unknown, Decision("host", "creator/new", "add", evidence=unknown))
    assert "identity_required" in result.deferred[0]
    assert plan(decision=Decision("host", "creator/new", "add")).deferred


def test_measured_window_raises_operational_context_and_retains_provider_claim():
    discovery = {
        **DISCOVERY,
        "input_characterization": {
            **DISCOVERY["input_characterization"],
            "admitted_input": 12000,
            "generated_output": 234,
        },
    }
    report = Report(candidates=[replace(CANDIDATE, discovery=discovery)])
    decision = Decision(
        "host",
        "creator/new",
        "add",
        ("lane",),
        PROOF,
        discovery_digest=discovery["input_characterization"]["evidence_digest"],
    )
    result = plan_apply(report, (decision,), config())
    route = dict(result.routes[0])
    assert route["hard_input_ceiling"] == 12000
    assert route["input_context_limit"] == 12234


def test_stale_or_missing_discovery_evidence_cannot_prepare_an_addition():
    report = Report(candidates=[replace(CANDIDATE, discovery=DISCOVERY)])
    decision = Decision("host", "creator/new", "add", ("lane",), PROOF)
    result = plan_apply(report, (decision,), config())
    assert not result.routes
    assert "authenticated provider-counted input evidence" in result.deferred[0]


def test_non_provider_counted_or_stale_discovery_cap_is_not_accepted():
    for field, value in (
        ("count_basis", "local_estimate"),
        ("parser_version", ""),
        ("fixture_version", "old-fixture"),
    ):
        discovery = {
            **DISCOVERY,
            "input_characterization": {
                **DISCOVERY["input_characterization"],
                field: value,
            },
        }
        report = Report(candidates=[replace(CANDIDATE, discovery=discovery)])
        decision = Decision(
            "host",
            "creator/new",
            "add",
            ("lane",),
            PROOF,
            discovery_digest=DISCOVERY["input_characterization"]["evidence_digest"],
        )
        result = plan_apply(report, (decision,), config())
        assert not result.routes


def test_route_addition_accepts_one_authenticated_discovery_run():
    report = Report(candidates=[replace(CANDIDATE, discovery=DISCOVERY)])
    decision = Decision(
        "host",
        "creator/new",
        "add",
        ("lane",),
        PROOF,
        discovery_digest=DISCOVERY["input_characterization"]["evidence_digest"],
    )
    result = plan_apply(report, (decision,), config())
    assert result.routes
    assert not result.deferred


def test_shared_provider_capacity_is_not_copied_to_routes():
    from citypods.provider_catalog.evidence import LimitObservation

    shared = LimitObservation(
        "rpm", 30, TODAY.isoformat(), "host", "primary", scope="provider_account"
    )
    proof = replace(PROOF, limits=(shared,))
    assert dict(plan(proof).routes[0])["rpm"] == 1


def test_mixed_ignore_and_unverified_add_keeps_partial_plan():
    choices = (
        Decision("host", "creator/ignored", "ignore"),
        Decision("host", "creator/new", "add"),
    )
    result = plan_apply(Report(), choices, config())
    assert result.ignored and result.deferred and not result.routes


def test_existing_physical_route_never_gets_duplicated():
    existing = {
        "route_id": "existing",
        "model": "creator/new",
        "provider": "host",
        "upstream_model": "creator/new",
        "free": True,
    }
    result = plan_apply(
        Report(),
        (Decision("host", "creator/new", "add", ("lane",)),),
        replace(
            config((existing,)),
            lanes={"lane": replace(config().lanes["lane"], backup_models=("creator/new",))},
        ),
    )
    assert not result.routes and not result.backups and not result.deferred


@pytest.mark.parametrize(
    "selection",
    [
        "`host/creator/new`: ignore\n- [x] `host/creator/new`: add route only",
        "`host/creator/new`: add route only\n- [x] `host/creator/new`: add as backup to `lane`",
        "`host/creator/new`: add as backup to `unknown`",
        "`forged`: ignore",
        "`host/creator/new`: ignore\n- [x] `host/creator/new`: ignore",
    ],
)
def test_unknown_conflicting_duplicate_decisions_rejected(selection):
    report = Report(candidates=[CANDIDATE])
    body = render_body(report, run_date=TODAY.isoformat())
    body = body.replace("- [ ] `host/creator/new`: ignore", "- [x] " + selection)
    with pytest.raises(ValueError):
        parse_decisions(body, report)


def test_discovery_test_again_is_a_single_retry_choice_not_an_addition():
    candidate = replace(
        CANDIDATE,
        discovery={
            "retryable": True,
            "reason": "daily",
            "calls": 3,
            "input_tokens": 12000,
            "output_tokens": 768,
            "axes": [
                {
                    "axis": "input",
                    "published": 8000,
                    "source": "https://provider.test/limits",
                    "admitted": 7900,
                    "refused": 8000,
                    "target_width": 40,
                    "status": "partial",
                }
            ],
        },
    )
    report = Report(candidates=[candidate])
    body = render_body(report, run_date=TODAY.isoformat())
    assert "## Request-cap discovery" in body
    assert "| input | 8,000 ([source](https://provider.test/limits))" in body
    assert "Count basis / control" in body and "unverified" in body
    selected = body.replace(
        "- [ ] `host/creator/new`: test again", "- [x] `host/creator/new`: test again"
    )
    [decision] = parse_decisions(selected, report)
    assert decision.action == "test_again"
    assert decision.key == "host/creator/new"

    conflicting = selected.replace(
        "- [ ] `host/creator/new`: add route only",
        "- [x] `host/creator/new`: add route only",
    )
    with pytest.raises(ValueError, match="conflicts"):
        parse_decisions(conflicting, report)


def test_discovery_telemetry_and_edited_spend_counters_render_safely():
    candidate = replace(
        CANDIDATE,
        discovery={
            "retryable": False,
            "calls": "bad",
            "input_tokens": "bad",
            "output_tokens": None,
            "axes": [],
            "attempt_diagnostics": [
                {
                    "axis": "input",
                    "target": 500000,
                    "reserved_input": 500000,
                    "requested_output": 256,
                    "outcome": "inconclusive",
                    "diagnostic": "completion_content_null",
                    "response_status": 200,
                    "response_media_type": "application/json",
                    "response_bytes": 900,
                    "message_content_state": "null",
                    "finish_reason_state": "missing",
                    "finish_reason": "",
                    "usage_state": "valid",
                    "reported_input": 490000,
                    "reported_output": 0,
                    "provider_request_id": "request-123",
                    "response_id": "response-456",
                    "response_preview": '"Provider suggests reduce the request"',
                }
            ],
            "transport_retries": [
                [
                    {
                        "attempt_id": "a" * 64,
                        "axis": "input",
                        "target": 1000,
                        "reserved_input": 1000,
                        "requested_output": 256,
                        "diagnostic": "transport_timeout",
                        "outcome": "inconclusive",
                    },
                    {
                        "attempt_id": "b" * 64,
                        "axis": "input",
                        "target": 1000,
                        "reserved_input": 1000,
                        "requested_output": 256,
                        "diagnostic": "verified_input_fixture",
                        "outcome": "admitted",
                    },
                ]
            ],
        },
    )

    body = render_body(Report(candidates=[candidate]), run_date=TODAY.isoformat())

    assert "— reserved input" in body
    assert "— requested output tokens" in body
    assert "No-response attempt telemetry" in body
    assert "`aaaaaaaaaaaa`" in body and "`bbbbbbbbbbbb`" in body
    assert "Recent request diagnostics" in body
    assert "completion_content_null" in body
    assert "490,000 / 0" in body
    assert "request-123" in body and "response-456" in body
    assert '"Provider suggests reduce the request"' in body

    malformed = replace(
        candidate,
        discovery={"transport_retries": "malformed"},
    )
    assert "No-response attempt telemetry" not in render_body(
        Report(candidates=[malformed]), run_date=TODAY.isoformat()
    )


def test_edited_discovery_preview_cannot_inject_issue_markup():
    preview = json.dumps("](https://example.test)|@everyone`*_[x]~")

    rendered = _discovery_attempt_diagnostics([{"response_preview": preview}])[0][
        "response_preview"
    ]

    assert all(char not in rendered for char in "[]()|@`*_~")


def test_discovery_retry_choice_cannot_be_forged_without_retryable_evidence():
    report = Report(candidates=[replace(CANDIDATE, discovery={"retryable": False})])
    body = render_body(report, run_date=TODAY.isoformat()).replace(
        "- [ ] `host/creator/new`: add route only",
        "- [x] `host/creator/new`: test again",
    )
    with pytest.raises(ValueError, match="unknown or duplicate"):
        parse_decisions(body, report)


def test_explicit_removal_is_parsed_and_needs_a_fresh_proof():
    report = Report(anomalies=[Anomaly("host", "route", "model", "not_entitled", "paid", False)])
    body = render_body(report, run_date=TODAY.isoformat()).replace(
        "- [ ] `route`: remove route", "- [x] `route`: remove route"
    )
    [decision] = parse_decisions(body, report)
    assert decision.action == "remove" and decision.route_id == "route"
    configured = [{"route_id": "route", "provider": "host", "upstream_model": "model"}]
    result = plan_apply(Report(), (decision,), config(configured))
    assert result.deferred and "not_entitled proof" in result.deferred[0]
    assert not result.removed_routes and not result.routes and not result.paid_routes


def test_remove_decision_drops_a_route_that_has_a_sibling_and_repairs_nothing():
    conf, proof, _ = paid_fixture()
    decision = Decision(
        "host", "creator/old", "remove", route_id="paid", route_digest=proof.config_digest
    )
    result = plan_apply(Report(anomalies=[proof]), (decision,), conf)
    assert result.removed_routes == ("paid",) and not result.last_routes


def test_removing_a_models_last_route_flags_it_with_alias_targets():
    from citypods.provider_catalog.evidence import digest

    conf, proof, _ = paid_fixture()
    newer = {**conf.limits["routes"][0], "route_id": "newer", "model": "old-2", "provider": "host"}
    paid = {**conf.limits["routes"][0], "route_id": "paid"}
    limits = {**conf.limits, "routes": [paid, newer]}
    lanes = {"lane": LaneConfig("lane", ("old",), 1, 1, 1, backup_models=("old-2",))}
    proof = replace(proof, config_digest=digest(paid))
    decision = Decision("host", "creator/old", "remove", route_id="paid", route_digest=digest(paid))
    result = plan_apply(
        Report(anomalies=[proof]), (decision,), replace(conf, limits=limits, lanes=lanes)
    )
    assert result.removed_routes == ("paid",)
    assert result.last_routes == (("old", ("lane",), ("old-2",)),)
    body = proposal_body(result)
    assert "Last route for `old` removed" in body and "`old` → `old-2`" in body


def test_alias_candidates_prefer_same_family_then_shared_lanes():
    from citypods.provider_catalog.apply import alias_candidates

    routes = [
        {"model": "zai/glm-5.3-flash", "free": True},
        {"model": "deepseek/v4", "free": True},
        {"model": "zai/glm-4.5-flash", "free": True, "rpd": 0},
    ]
    lanes = {"a": LaneConfig("a", ("zai/glm-4.5-flash", "deepseek/v4"), 1, 1, 1)}
    assert alias_candidates("zai/glm-4.5-flash", routes, lanes) == (
        "zai/glm-5.3-flash",
        "deepseek/v4",
    )


def event(**changes):
    value = {
        "action": "created",
        "comment": {"body": "/apply"},
        "issue": {"number": 1, "body": MARKER},
    }
    value.update(changes)
    return value


@pytest.mark.parametrize("permission", ["none", "read", "triage"])
def test_command_permission_is_current_write_permission(permission):
    assert not process_event(event(), {"permission": permission})["accepted"]


@pytest.mark.parametrize(
    "value",
    [
        event(action="edited"),
        event(comment={"body": "/apply extra"}),
        event(issue={"number": 1, "body": MARKER, "pull_request": {}}),
        event(issue={"number": True, "body": MARKER}),
        event(issue={"number": 1, "body": "ordinary"}),
    ],
)
def test_spoofed_or_nonexact_commands_rejected(value):
    assert not process_event(value, {"permission": "write"})["accepted"]


def test_authorized_command_accepts_legacy_markers():
    assert process_event(
        event(issue={"number": 1, "body": MARKER.replace("v=3", "v=2")}), {"permission": "write"}
    )["accepted"]


def publication_runner(main="main-sha", prs=None, remote="", email="", commit="", changed=True):
    calls = []

    def run(args):
        calls.append(args)
        if args[:3] == ["git", "rev-parse", "origin/main"]:
            return main
        if args[:3] == ["gh", "pr", "list"]:
            return json.dumps(prs or [])
        if args[:2] == ["git", "ls-remote"]:
            return remote
        if args[:3] == ["git", "show", "-s"]:
            return email if "--format=%ae" in args else commit
        if args[:4] == ["git", "diff", "--cached", "--name-only"]:
            return "config/provider_limits.yml" if changed else ""
        if args[:3] in (["gh", "pr", "view"], ["gh", "pr", "create"]):
            return "https://github.test/pr/1"
        return ""

    return calls, run


def test_changed_main_rebuilds_before_any_push():
    calls, run = publication_runner(main="new-sha")
    assert publish(EditPlan("main-sha", ()), run_fn=run) is None
    assert not any(c[:2] == ["git", "push"] for c in calls)


def test_managed_pr_is_updated_with_a_lease_and_body_file():
    prs = [
        {
            "number": 1,
            "body": PR_MARKER,
            "author": {"login": "github-actions[bot]"},
            "baseRefName": "main",
            "isCrossRepository": False,
        }
    ]
    calls, run = publication_runner(
        prs=prs,
        remote="oldsha refs/heads/automation",
        commit=PR_MARKER,
        email="41898282+github-actions[bot]@users.noreply.github.com",
    )
    assert publish(EditPlan("main-sha", ()), run_fn=run) == "https://github.test/pr/1"
    push = next(c for c in calls if c[:2] == ["git", "push"])
    assert "--force-with-lease=refs/heads/automation/provider-catalog-additions:oldsha" in push
    assert any(c[:3] == ["gh", "pr", "edit"] and "--body-file" in c for c in calls)
    assert not any(c[:3] == ["gh", "pr", "create"] for c in calls)


def test_human_branch_is_never_replaced():
    calls, run = publication_runner(
        remote="human refs/heads/automation", email="human@example.test"
    )
    with pytest.raises(ValueError, match="human-owned"):
        publish(EditPlan("main-sha", ()), run_fn=run)
    assert not any(c[:2] == ["git", "push"] for c in calls)


def test_tampered_digest_cannot_supply_an_apply_selection():
    report = Report(candidates=[replace(CANDIDATE, evidence_digest="forged")])
    body = render_body(report, run_date=TODAY.isoformat()).replace(
        "- [ ] `host/creator/new`: add route only", "- [x] `host/creator/new`: add route only"
    )
    with pytest.raises(ValueError, match="digest"):
        parse_decisions(body, report)


def test_paused_beatapi_physical_route_is_not_reenabled_or_added_to_lanes():
    limits = config().limits
    limits["providers"]["beatapi"] = {"accounts": [{"id": "primary"}], "rpm": 1, "concurrency": 1}
    limits["routes"] = [
        {
            "route_id": "beatapi",
            "provider": "beatapi",
            "model": "deepseek/v4",
            "upstream_model": "deepseek-v4-free",
            "free": True,
            "rpd": 0,
            "tier": "backup",
        }
    ]
    result = plan_apply(
        Report(),
        (Decision("beatapi", "deepseek-v4-free", "add", ("lane",)),),
        replace(config(), limits=limits),
    )
    assert result.deferred and not result.routes and not result.backups
    assert limits["routes"][0]["rpd"] == 0 and limits["routes"][0]["tier"] == "backup"


def test_unknown_lane_still_rejected_for_an_existing_route():
    route = {
        "model": "creator/new",
        "provider": "host",
        "upstream_model": "creator/new",
        "free": True,
    }
    result = plan_apply(
        Report(), (Decision("host", "creator/new", "add", ("missing",)),), config((route,))
    )
    assert result.rejected and not result.backups


def test_publication_creates_one_pr_and_repeated_noop_does_not_push():
    calls, run = publication_runner()
    assert publish(EditPlan("main-sha", ()), run_fn=run) == "https://github.test/pr/1"
    assert sum(c[:3] == ["gh", "pr", "create"] for c in calls) == 1
    calls, run = publication_runner(changed=False)
    assert "already present" in publish(EditPlan("main-sha", ()), run_fn=run)
    assert not any(c[:2] == ["git", "push"] or c[:3] == ["gh", "pr", "create"] for c in calls)


@pytest.mark.parametrize("field", ["input_context_limit", "output_context_limit"])
def test_unknown_compiler_required_bound_defers_only_that_candidate(field):
    proof = replace(PROOF, **{field: None})
    result = plan(
        proof,
        Decision(
            "host",
            "creator/new",
            "add",
            evidence=proof,
            discovery_digest=DISCOVERY["input_characterization"]["evidence_digest"],
        ),
    )
    if field == "input_context_limit":
        assert result.routes
        assert dict(result.routes[0])["input_context_limit"] == 12234
    else:
        assert result.deferred and not result.routes
        assert "output-context value" in result.deferred[0]


def test_old_selection_cannot_place_an_already_configured_task_plugin_in_a_chat_lane():
    limits = config().limits
    limits["providers"]["beatapi"] = {"accounts": [{"id": "primary"}]}
    limits["routes"] = [
        {
            "provider": "beatapi",
            "upstream_model": "jev-1.13-free",
            "model": "task/jev",
            "free": True,
        }
    ]
    result = plan_apply(
        Report(),
        (Decision("beatapi", "jev-1.13-free", "add", ("lane",)),),
        replace(config(), limits=limits),
    )
    assert result.deferred and not result.routes and not result.backups


def paid_fixture(routes=None):
    from citypods.provider_catalog.evidence import digest

    route = {
        "route_id": "paid",
        "model": "old",
        "provider": "host",
        "upstream_model": "creator/old",
        "account_id": "primary",
        "free": True,
    }
    alternate = {
        **route,
        "route_id": "alternate",
        "provider": "other",
        "upstream_model": "other/old",
    }
    conf = config(routes or [route, alternate])
    conf = replace(conf, texts={**conf.texts, SOURCE_PATHS[2]: "ignored: []\nacknowledged: []\n"})
    proof = Anomaly(
        "host",
        "paid",
        "creator/old",
        "not_entitled",
        "paid",
        False,
        observed_on=TODAY.isoformat(),
        config_digest=digest(route),
        contended=False,
    )
    decision = Decision(
        "host", "creator/old", "keep_paid", route_id="paid", route_digest=digest(route)
    )
    return conf, proof, decision


def test_keep_paid_preserves_pool_and_records_exact_acknowledgement():
    conf, proof, decision = paid_fixture()
    result = plan_apply(Report(anomalies=[proof]), (decision,), conf)
    assert result.paid_routes == ("paid",)
    assert result.acknowledged == (("host", "creator/old", TODAY.isoformat()),)
    assert "lane" in result.applied[0]
    assert not result.routes and not result.backups and not result.rejected


@pytest.mark.parametrize(
    "change",
    [
        {"contended": True},
        {"observed_on": "2026-10-07"},
        {"config_digest": "changed"},
        {"verdict": "quota_exhausted"},
        {"verdict": "retired"},
    ],
)
def test_paid_decision_defers_missing_changed_or_contended_proof(change):
    conf, proof, decision = paid_fixture()
    result = plan_apply(Report(anomalies=[replace(proof, **change)]), (decision,), conf)
    assert result.deferred and not result.paid_routes


def test_paid_decision_requires_unchanged_snapshot_and_live_route():
    conf, proof, decision = paid_fixture()
    for choice in [
        replace(decision, route_digest=None),
        replace(decision, route_digest="old"),
        replace(decision, model="swapped"),
    ]:
        result = plan_apply(Report(anomalies=[proof]), (choice,), conf)
        assert result.deferred and not result.paid_routes


def test_paid_decision_rejects_last_free_primary_even_with_backup():
    conf, proof, decision = paid_fixture()
    conf = replace(
        conf,
        limits={**conf.limits, "routes": conf.limits["routes"][:1]},
        lanes={"lane": replace(conf.lanes["lane"], backup_models=("backup",))},
    )
    result = plan_apply(Report(anomalies=[proof]), (decision,), conf)
    assert "empty free lane pool" in result.rejected[0]
    assert not result.paid_routes


def test_multiple_paid_choices_cannot_collectively_empty_pool():
    from citypods.provider_catalog.evidence import digest

    conf, proof, decision = paid_fixture()
    second = {**conf.limits["routes"][1], "provider": "host"}
    conf = replace(conf, limits={**conf.limits, "routes": [conf.limits["routes"][0], second]})
    other_proof = replace(
        proof, route_id="alternate", model="other/old", config_digest=digest(second)
    )
    other_choice = replace(
        decision, route_id="alternate", model="other/old", route_digest=digest(second)
    )
    result = plan_apply(Report(anomalies=[proof, other_proof]), (decision, other_choice), conf)
    assert result.paid_routes == ("paid",)
    assert result.rejected and "alternate" in result.rejected[0]


def test_acknowledgement_cannot_hide_another_free_account_route():
    conf, proof, decision = paid_fixture()
    second = {**conf.limits["routes"][0], "route_id": "same_upstream", "account_id": "secondary"}
    conf = replace(conf, limits={**conf.limits, "routes": [*conf.limits["routes"], second]})
    result = plan_apply(Report(anomalies=[proof]), (decision,), conf)
    assert result.deferred and "other free routes" in result.deferred[0]
    assert not result.acknowledged


def test_paid_checkbox_conflict_and_exact_identity():
    conf, proof, decision = paid_fixture()
    report = Report(anomalies=[proof])
    body = render_body(report, run_date=TODAY.isoformat()).replace(
        "- [ ] `paid`: keep as a paid route", "- [x] `paid`: keep as a paid route"
    )
    assert parse_decisions(body, report) == (decision,)
    both = body.replace("- [ ] `paid`: remove route", "- [x] `paid`: remove route")
    with pytest.raises(ValueError, match="conflicting"):
        parse_decisions(both, report)


def test_already_fulfilled_paid_choice_is_noop_without_fresh_probe():
    conf, proof, decision = paid_fixture()
    routes = [{**r, "free": False} if r["route_id"] == "paid" else r for r in conf.limits["routes"]]
    conf = replace(
        conf,
        limits={**conf.limits, "routes": routes},
        texts={
            **conf.texts,
            SOURCE_PATHS[2]: "acknowledged:\n  - provider: host\n    model_glob: creator/old\n"
            "    verdict: not_entitled\n",
        },
    )
    result = plan_apply(Report(), (decision,), conf)
    assert not result.paid_routes and not result.acknowledged and not result.deferred


def test_prepare_rechecks_paid_route_then_skips_probes_after_fulfillment(tmp_path, monkeypatch):
    from dataclasses import asdict

    import yaml

    from citypods.provider_catalog.decisions import Decisions
    from scripts import provider_catalog_commands as commands

    conf, proof, decision = paid_fixture()
    for path, text in zip(
        SOURCE_PATHS,
        [yaml.safe_dump(conf.limits), lane_yaml(), conf.texts[SOURCE_PATHS[2]]],
        strict=True,
    ):
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    snapshot = Report(anomalies=[proof], state={"last_full": {"anomalies": [asdict(proof)]}})
    body = render_body(snapshot, run_date=TODAY.isoformat()).replace(
        "- [ ] `paid`: keep as a paid route", "- [x] `paid`: keep as a paid route"
    )
    from datetime import datetime

    class FixedDatetime:
        @staticmethod
        def now(tz):
            return datetime(2026, 10, 8, tzinfo=tz)

    monkeypatch.setattr(commands, "datetime", FixedDatetime)
    monkeypatch.setattr(commands, "ROOT", tmp_path)
    monkeypatch.setattr(commands, "load_decisions", lambda: Decisions())
    monkeypatch.setattr(commands, "_control", lambda _: object())
    monkeypatch.setattr(commands, "fetch_quality_index", lambda _: object())
    calls = []

    def fresh(*args, **kwargs):
        calls.append(kwargs)
        return Report(anomalies=[proof])

    monkeypatch.setattr(commands, "reconcile", fresh)
    result = commands.prepare(body, "base")
    assert result.paid_routes == ("paid",)
    assert calls[0]["candidate_keys"] == set() and calls[0]["route_ids"] == {"paid"}
    assert calls[0]["providers"] == {"host"}
    monkeypatch.setattr(
        commands, "_control", lambda _: pytest.fail("fulfilled choice must not probe")
    )
    result = commands.prepare(body, "base")
    assert not result.paid_routes and not result.deferred


def test_paid_acknowledgement_escapes_glob_characters():
    from fnmatch import fnmatchcase

    from citypods.provider_catalog.evidence import digest

    conf, proof, decision = paid_fixture()
    name = "creator/model[*]?"
    route = {**conf.limits["routes"][0], "upstream_model": name}
    conf = replace(conf, limits={**conf.limits, "routes": [route, conf.limits["routes"][1]]})
    proof = replace(proof, model=name, config_digest=digest(route))
    decision = replace(decision, model=name, route_digest=digest(route))
    result = plan_apply(Report(anomalies=[proof]), (decision,), conf)
    pattern = result.acknowledged[0][1]
    assert fnmatchcase(name, pattern) and not fnmatchcase("creator/modelX", pattern)


@pytest.mark.parametrize("value", [None, "true", 1])
def test_unsupported_free_field_rejects_only_paid_selection(value):
    conf, proof, decision = paid_fixture()
    conf = replace(
        conf,
        limits={
            **conf.limits,
            "routes": [
                {**r, "free": value} if r["route_id"] == "paid" else r
                for r in conf.limits["routes"]
            ],
        },
    )
    result = plan_apply(
        Report(anomalies=[proof]), (decision, Decision("host", "skip", "ignore")), conf
    )
    assert result.rejected and result.ignored and not result.paid_routes


def test_paused_alternate_cannot_keep_free_pool_usable():
    conf, proof, decision = paid_fixture()
    conf = replace(
        conf,
        limits={
            **conf.limits,
            "routes": [
                r if r["route_id"] == "paid" else {**r, "rpd": 0} for r in conf.limits["routes"]
            ],
        },
    )
    result = plan_apply(Report(anomalies=[proof]), (decision,), conf)
    assert result.rejected and not result.paid_routes


def test_route_alias_can_preserve_an_affected_free_pool():
    conf, proof, decision = paid_fixture()
    conf = replace(
        conf,
        limits={
            **conf.limits,
            "routes": [
                r
                if r["route_id"] == "paid"
                else {**r, "model": "different", "also_serves": ["old"]}
                for r in conf.limits["routes"]
            ],
        },
    )
    assert plan_apply(Report(anomalies=[proof]), (decision,), conf).paid_routes == ("paid",)


@pytest.mark.parametrize("model", ["creator/old", "creator/model[1]*?"])
def test_paid_fulfillment_requires_literal_ack_in_planner_and_issue(model):
    from dataclasses import asdict
    from glob import escape

    import yaml

    from citypods.provider_catalog.apply import paid_fulfilled
    from citypods.provider_catalog.decisions import Decisions
    from citypods.provider_catalog.issue import fulfilled_choices

    conf, proof, decision = paid_fixture()
    decision = replace(decision, model=model)
    proof = replace(proof, model=model)
    routes = [
        {**r, "free": False, "upstream_model": model} if r["route_id"] == "paid" else r
        for r in conf.limits["routes"]
    ]
    conf = replace(conf, limits={**conf.limits, "routes": routes})
    for pattern, expected in [("*", False), ("creator/*", False), (escape(model), True)]:
        entry = {"provider": "host", "model_glob": pattern, "verdict": "not_entitled"}
        conf = replace(
            conf, texts={**conf.texts, SOURCE_PATHS[2]: yaml.safe_dump({"acknowledged": [entry]})}
        )
        assert paid_fulfilled(decision, conf) is expected
        choices = fulfilled_choices(
            render_body(
                Report(anomalies=[proof], state={"last_full": {"anomalies": [asdict(proof)]}}),
                run_date=TODAY.isoformat(),
            ),
            conf.limits,
            conf.lanes,
            Decisions(acknowledged=(entry,)),
            TODAY,
        )
        assert ("`paid`: keep as a paid route" in choices) is expected
        plan = plan_apply(Report(), (decision,), conf)
        assert bool(plan.deferred) is not expected


def lane_yaml(**changes):
    import yaml

    lane = dict(
        models=["old"],
        backup_models=["old"],
        backup_after_attempts=2,
        max_dispatches_per_run=1,
        daily_write_units=20,
        telemetry=dict(producer="test", unit="job", completion="consumed", scope="sample"),
    )
    lane.update(changes)
    return yaml.safe_dump({"llm_lanes": {"lane": lane}}, sort_keys=False)


def prepare_fixture(tmp_path, monkeypatch, limits, report, site_text):
    from datetime import datetime

    import yaml

    from citypods.compute import llm_lanes
    from citypods.provider_catalog.decisions import Decisions
    from scripts import provider_catalog_commands as commands

    class FixedDatetime:
        @staticmethod
        def now(tz):
            return datetime(2026, 10, 8, tzinfo=tz)

    texts = dict(
        zip(
            SOURCE_PATHS,
            [yaml.safe_dump(limits), site_text, "ignored: []\nacknowledged: []\n"],
            strict=True,
        )
    )
    for p, text in texts.items():
        path = tmp_path / p
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    # The process cache deliberately retains the first main snapshot through the rebuild.
    cached = llm_lanes.parse_lanes(yaml.safe_load(site_text)["llm_lanes"])
    monkeypatch.setitem(llm_lanes._CACHE, str(llm_lanes.DEFAULT_SITE_CONFIG_PATH), cached)
    monkeypatch.setattr(commands, "datetime", FixedDatetime)
    monkeypatch.setattr(commands, "ROOT", tmp_path)
    monkeypatch.setattr(commands, "_control", lambda _: object())
    monkeypatch.setattr(commands, "load_decisions", lambda: Decisions())
    monkeypatch.setattr(commands, "fetch_quality_index", lambda _: object())
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/repo")
    monkeypatch.setattr(commands, "discover_discovery_references", lambda **_: ())
    monkeypatch.setattr(commands, "verified_discovery_history", lambda *_, **__: ((), (), ()))
    seen = []

    def fresh(limits, lanes, *args, **kwargs):
        seen.append(lanes["lane"])
        return report

    monkeypatch.setattr(commands, "reconcile", fresh)
    return commands, texts, seen


def test_apply_rebuild_honors_current_free_primary_pool_despite_warm_lane_cache(
    tmp_path, monkeypatch
):
    from dataclasses import asdict

    conf, proof, _ = paid_fixture()
    limits = {
        **conf.limits,
        "routes": [conf.limits["routes"][0], {**conf.limits["routes"][1], "model": "spare"}],
    }
    snapshot = Report(anomalies=[proof], state={"last_full": {"anomalies": [asdict(proof)]}})
    body = render_body(snapshot, run_date=TODAY.isoformat()).replace(
        "- [ ] `paid`: keep as a paid route", "- [x] `paid`: keep as a paid route"
    )
    commands, texts, seen = prepare_fixture(
        tmp_path,
        monkeypatch,
        limits,
        Report(anomalies=[proof]),
        lane_yaml(models=["old", "spare"]),
    )
    assert commands.prepare(body, "first-main").paid_routes == ("paid",)
    # A bounded publication rebuild resets source files to the newly fetched main.
    for p, text in texts.items():
        (tmp_path / p).write_text(text)
    (tmp_path / SOURCE_PATHS[1]).write_text(lane_yaml(models=["old"]))
    plan = commands.prepare(body, "second-main")
    assert not plan.paid_routes and "empty free lane pool" in plan.rejected[0]
    assert [lane.models for lane in seen] == [("old", "spare"), ("old",)]
    assert plan.base_commit == "second-main"
    assert (tmp_path / SOURCE_PATHS[0]).read_text() == texts[SOURCE_PATHS[0]]


def test_apply_rebuild_honors_current_backup_opt_out_despite_warm_lane_cache(tmp_path, monkeypatch):
    from dataclasses import asdict

    conf = config()
    candidate = replace(CANDIDATE, discovery=DISCOVERY)
    snapshot = Report(
        candidates=[candidate], state={"last_full": {"candidates": [asdict(candidate)]}}
    )
    body = render_body(snapshot, run_date=TODAY.isoformat()).replace(
        "- [ ] `host/creator/new`: add as backup to `lane`",
        "- [x] `host/creator/new`: add as backup to `lane`",
    )
    commands, texts, seen = prepare_fixture(
        tmp_path, monkeypatch, conf.limits, Report(candidates=[candidate]), lane_yaml()
    )
    assert commands.prepare(body, "first-main").backups == (("lane", "creator/new"),)
    for p, text in texts.items():
        (tmp_path / p).write_text(text)
    site = lane_yaml(catalog_backup_candidates=False)
    (tmp_path / SOURCE_PATHS[1]).write_text(site)
    plan = commands.prepare(body, "second-main")
    assert not plan.routes and not plan.backups
    assert "no longer eligible" in plan.rejected[0]
    assert [lane.accepts_catalog_backups for lane in seen] == [True, False]
    assert (tmp_path / SOURCE_PATHS[1]).read_text() == site


def test_rate_choices_preserve_selection_and_reject_forged_value():
    from dataclasses import asdict

    from citypods.provider_catalog.limits import RateChange
    from citypods.provider_catalog.reconcile import _merge_into_last_full

    change = RateChange("route", "host", "old", "rpm", 100, 130, "offer_increase", "stamp")
    report = Report(rate_changes=[change], state={"last_full": {"rate_changes": [asdict(change)]}})
    body = render_body(report, run_date=TODAY.isoformat()).replace("- [ ]", "- [x]")
    assert parse_decisions(body, report)[0].action == "increase_rate"
    with pytest.raises(ValueError, match="unknown"):
        parse_decisions(body.replace("to 130", "to 10000"), report)
    refreshed = render_body(
        Report(state={"last_full": {}}), run_date=TODAY.isoformat(), previous_body=body
    )
    assert f"- [x] {change.choice}" in refreshed
    from citypods.provider_catalog.issue import decode_state

    restored = Report()
    _merge_into_last_full(restored, decode_state(refreshed)["last_full"], set())
    assert restored.rate_changes == [change]


def test_due_only_merge_keeps_current_candidate_without_duplicate_choice():
    from dataclasses import asdict

    from citypods.provider_catalog.reconcile import _merge_into_last_full

    previous = replace(CANDIDATE, discovery={"retryable": True, "reason": "partial"})
    current = replace(CANDIDATE, discovery={"retryable": False, "reason": "complete"})
    report = Report(candidates=[current])

    _merge_into_last_full(report, {"candidates": [asdict(previous)]}, set())

    assert report.candidates == [current]
    body = render_body(report, run_date=TODAY.isoformat())
    assert body.count("## Request-cap discovery") == 1
    assert "complete" in body and "partial" not in body


def test_prepare_rate_increase_uses_verified_artifact_values_not_issue_observations(
    tmp_path, monkeypatch
):
    from datetime import UTC, datetime
    from pathlib import Path

    from citypods.provider_catalog.config_edit import load_config
    from citypods.provider_catalog.evidence import LimitObservation
    from citypods.provider_catalog.limits import configured_limit_changes
    from scripts import provider_catalog_commands as commands

    original = Path(__file__).resolve().parents[1]
    for path in SOURCE_PATHS:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((original / path).read_text())
    monkeypatch.setattr(commands, "ROOT", tmp_path)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    limits = load_config((tmp_path / SOURCE_PATHS[0]).read_text())
    route = next(r for r in limits["routes"] if r["provider"] == "groq" and r.get("rpm"))
    now = datetime.now(UTC)
    observed = LimitObservation(
        "rpm",
        int(route["rpm"] * 2),
        now.isoformat(),
        "groq",
        route["account_id"],
        route["route_id"],
        "route",
        run_id="42",
    )
    monkeypatch.setattr(commands, "discover_rate_references", lambda **kwargs: ("actual",))

    def history(refs, *args, **kwargs):
        assert refs == ("actual",)
        return ((observed,), ({"run_id": "42", "observed_at": now.isoformat()},), ())

    monkeypatch.setattr(commands, "verified_rate_history", history)
    actual, _ = configured_limit_changes([observed], limits, now=now, recent_runs=("42",))
    report = Report(
        rate_changes=list(actual),
        state={
            "last_full": {"rate_changes": [__import__("dataclasses").asdict(c) for c in actual]},
            "rate_observations": [{"value": 1000000}],
            "rate_evidence_refs": ["forged"],
        },
    )
    body = render_body(report, run_date=now.date().isoformat()).replace("- [ ]", "- [x]")
    plan = commands.prepare(body, "base")
    assert plan.rate_changes == actual and plan.proposal_kind == "limits"
    changed = load_config((tmp_path / SOURCE_PATHS[0]).read_text())
    updated = next(r for r in changed["routes"] if r["route_id"] == route["route_id"])
    assert updated["rpm"] == observed.value and updated["rpm"] != 1000000


def test_rate_publication_uses_own_managed_branch_and_preserves_open_reviewed_increase():
    from citypods.provider_catalog.limits import RateChange

    marker = "<!-- citypods:provider-catalog-limits -->"
    change = RateChange("route", "host", "old", "rpm", 100, 70, "tighten", "stamp")
    plan = EditPlan("main-sha", (), proposal_kind="limits", rate_changes=(change,))
    calls, run = publication_runner()
    assert publish(plan, run_fn=run) == "https://github.test/pr/1"
    assert any("HEAD:refs/heads/automation/provider-catalog-limits" in c for c in calls)
    prs = [
        {
            "number": 1,
            "body": marker + "\nSource: explicit `/apply` rate increase",
            "author": {"login": "github-actions[bot]"},
            "baseRefName": "main",
            "isCrossRepository": False,
        }
    ]
    calls, run = publication_runner(prs=prs)
    assert publish(plan, run_fn=run).startswith("Waiting:")
    assert not any(c[:2] == ["git", "push"] for c in calls)


def test_rate_increase_waits_for_open_tightening_and_merged_branch_does_not_block():
    from citypods.provider_catalog.limits import RateChange

    marker = "<!-- citypods:provider-catalog-limits -->"
    tighten = RateChange("route", "host", "old", "rpm", 100, 70, "tighten", "stamp")
    increase = replace(tighten, new=150, action="offer_increase")
    prs = [
        {
            "number": 1,
            "body": marker,
            "author": {"login": "github-actions[bot]"},
            "baseRefName": "main",
            "isCrossRepository": False,
        }
    ]
    calls, run = publication_runner(prs=prs)
    plan = EditPlan("main-sha", (), proposal_kind="limits", rate_changes=(increase,))
    assert publish(plan, run_fn=run).startswith("Waiting:")
    assert not any(c[:2] in (["git", "push"], ["git", "commit"]) for c in calls)
    calls, run = publication_runner(
        remote="oldsha refs/heads/automation/provider-catalog-limits",
        email="41898282+github-actions[bot]@users.noreply.github.com",
        commit=marker + "\nSource: explicit `/apply` rate increase",
    )
    plan = replace(plan, rate_changes=(tighten,))
    assert publish(plan, run_fn=run) == "https://github.test/pr/1"
    assert any(
        "--force-with-lease=refs/heads/automation/provider-catalog-limits:oldsha" in c
        for c in calls
    )


@pytest.mark.parametrize("profile", ["input_priority", "selected_axes", "history"])
def test_input_characterization_report_separates_provider_usage_and_estimates(profile):
    candidate = replace(
        CANDIDATE,
        discovery={
            "profile": profile,
            "input_characterization": {
                "admitted_input": 10000,
                "admitted_local_estimate": 11000,
                "requested_output": 2048,
                "generated_output": 234,
                "rejected_estimates": [
                    {"local_input_estimate": 12100, "provider_scale_estimate": 11000}
                ],
            },
        },
    )
    body = render_body(Report(candidates=[candidate]), run_date=TODAY.isoformat())
    assert "confirmed 10,000 provider-counted input" in body
    assert "2,048 output requested and 234 generated" in body
    assert "Passing request local input estimate: 11,000" in body
    assert "Rejected input-size request: local estimate 12,100; approximately 11,000" in body
    assert "not a provider-counted upper bound" in body
    assert ("Output and combined checks are supporting examples, not maxima" in body) == (
        profile == "input_priority"
    )


def test_issue_shows_measured_operational_window_and_preserves_published_claim():
    discovery = {
        "profile": "history",
        "axes": [
            {
                "axis": "input",
                "published": 8000,
                "source": "https://provider.test/models",
                "count_basis": "input",
                "control": "input:v2",
            },
            {"axis": "output", "published": 4096},
        ],
        "input_characterization": {
            "admitted_input": 9000,
            "generated_output": 100,
            "count_basis": "input",
            "control": "input:v2",
        },
    }
    body = render_body(
        Report(candidates=[replace(CANDIDATE, discovery=discovery)]),
        run_date=TODAY.isoformat(),
    )
    assert "`hard_input_ceiling` = 9,000" in body
    assert "`input_context_limit` = 9,100" in body
    assert "Proposed `output_context_limit`: 4,096" in body
    assert "provider claim 8,000" in body
    assert "one successful authenticated discovery run" in body


def test_input_priority_supporting_counts_do_not_claim_full_requested_output():
    candidate = replace(
        CANDIDATE,
        discovery={
            "profile": "input_priority",
            "attempt_diagnostics": [
                {
                    "axis": "total_input_heavy",
                    "outcome": "inconclusive",
                    "reported_input": 130463,
                    "reported_output": 609,
                    "requested_output": 2444,
                    "finish_reason_state": "length",
                    "usage_state": "valid",
                }
            ],
            "input_characterization": {"admitted_input": 130625, "rejected_estimates": 42},
        },
    )
    body = render_body(Report(candidates=[candidate]), run_date=TODAY.isoformat())
    assert "reported input 130,463, generated output 609, requested output 2,444" in body
    assert "input plus generated output 131,072" in body
    assert "Consumed totals are separate from requested-output capacity proof" in body


def test_route_only_addition_without_measured_input_is_provisional_and_listed():
    report = Report(candidates=[replace(CANDIDATE, discovery=None)])
    decision = Decision("host", "creator/new", "add", (), PROOF)
    result = plan_apply(report, (decision,), config())
    route = dict(result.routes[0])
    assert not result.deferred
    assert route["hard_input_ceiling"] == 10000 and route["input_context_limit"] == 10000
    assert "Provisional input cap" in result.route_notes[0][1][0]
    body = proposal_body(result)
    assert "| `host_creator_new` | `creator/new` | `primary` | `creator/new` | none |" in body


def test_lane_addition_still_requires_measured_input():
    report = Report(candidates=[replace(CANDIDATE, discovery=None)])
    decision = Decision("host", "creator/new", "add", ("lane",), PROOF)
    result = plan_apply(report, (decision,), config())
    assert not result.routes and "input evidence" in result.deferred[0]
