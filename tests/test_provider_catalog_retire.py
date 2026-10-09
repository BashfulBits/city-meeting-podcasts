"""Retirement requires current complete absence and agreement across serving accounts."""

from dataclasses import replace
from datetime import date

import pytest

from citypods.provider_catalog.apply import SOURCE_PATHS, ApplyConfig, EditPlan
from citypods.provider_catalog.config_edit import apply_config_edits, load_config
from citypods.provider_catalog.evidence import digest
from citypods.provider_catalog.reconcile import Anomaly, Report
from citypods.provider_catalog.retire import _retirement_proven

TODAY = date(2026, 10, 8)
ROUTE = dict(
    route_id="old", provider="host", upstream_model="creator/old", account_id="first", model="old"
)


def evidence(routes=(ROUTE,)):
    config = ApplyConfig(
        {
            "providers": {"host": {"accounts": [{"id": "first"}, {"id": "second"}]}},
            "routes": list(routes),
        },
        {},
        {},
        "main",
        TODAY,
    )
    report = Report(
        anomalies=[
            Anomaly(
                "host",
                r["route_id"],
                "creator/old",
                "retired",
                "end of life",
                True,
                observed_on=TODAY.isoformat(),
                config_digest=digest(r),
                contended=False,
            )
            for r in routes
        ],
        state={
            "catalogs": {
                "host": dict(
                    provider="host", observed_at=TODAY.isoformat(), complete=True, models=[]
                )
            }
        },
    )
    return report, config


@pytest.mark.parametrize(
    "verdict",
    [
        "inconclusive",
        "quota_exhausted",
        "not_entitled",
        "account_blocked",
        "structured_output_invalid",
        "proven",
    ],
)
def test_non_retirement_signals_never_authorize_removal(verdict):
    report, config = evidence()
    report.anomalies[0].verdict = verdict
    assert not _retirement_proven(ROUTE, report, config)


@pytest.mark.parametrize(
    "change",
    [
        dict(complete=False),
        dict(observed_at="2026-10-07"),
        dict(models=[("creator/old", True, True, 1, 1, "old")]),
        dict(models=[("malformed",)]),
    ],
)
def test_partial_stale_present_or_malformed_catalog_defers(change):
    report, config = evidence()
    report.state["catalogs"]["host"].update(change)
    assert not _retirement_proven(ROUTE, report, config)


@pytest.mark.parametrize(
    "change",
    [
        dict(contended=True),
        dict(config_digest="old"),
        dict(observed_on="2026-10-07"),
        dict(absent_from_catalog=False),
        dict(provider="other"),
        dict(model="other"),
    ],
)
def test_stale_or_mismatched_probe_defers(change):
    report, config = evidence()
    report.anomalies[0] = replace(report.anomalies[0], **change)
    assert not _retirement_proven(ROUTE, report, config)


def test_all_serving_accounts_must_agree():
    second = {**ROUTE, "route_id": "second-route", "account_id": "second"}
    report, config = evidence((ROUTE, second))
    assert _retirement_proven(ROUTE, report, config)
    report.anomalies[1].verdict = "not_entitled"
    assert not _retirement_proven(ROUTE, report, config)
    report.anomalies.pop()
    assert not _retirement_proven(ROUTE, report, config)


def test_unknown_account_and_duplicate_proofs_fail_closed():
    report, config = evidence()
    config.limits["routes"][0] = {**ROUTE, "account_id": "missing"}
    assert not _retirement_proven(ROUTE, report, config)
    report, config = evidence()
    report.anomalies.append(report.anomalies[0])
    assert not _retirement_proven(ROUTE, report, config)


def test_route_removal_preserves_unrelated_pool_and_yaml_comments():
    texts = dict(
        zip(
            SOURCE_PATHS,
            [
                "# catalog\nroutes:\n  - route_id: old\n    model: shared\n    free: true\n"
                "# surviving pool\n  - route_id: surviving\n    model: shared\n"
                "    free: true\n# tail\n",
                "llm_lanes: {}\n",
                "ignored: []\n",
            ],
            strict=True,
        )
    )
    plan = EditPlan(
        "main", tuple((p, digest(t)) for p, t in texts.items()), removed_routes=("old",)
    )
    output = apply_config_edits(texts, plan)
    assert load_config(output[SOURCE_PATHS[0]])["routes"] == [
        dict(route_id="surviving", model="shared", free=True)
    ]
    assert "# catalog" in output[SOURCE_PATHS[0]]
    assert "# surviving pool\n  - route_id: surviving" in output[SOURCE_PATHS[0]]
    assert "# tail" in output[SOURCE_PATHS[0]]
    assert output[SOURCE_PATHS[1]] == texts[SOURCE_PATHS[1]]
    assert output[SOURCE_PATHS[2]] == texts[SOURCE_PATHS[2]]
    changed = dict(texts)
    changed[SOURCE_PATHS[0]] += "# concurrent main edit\n"
    with pytest.raises(ValueError, match="changed"):
        apply_config_edits(changed, plan)


def retirement_config(routes, models=("old",), backups=("new",)):
    import yaml

    from citypods.compute.llm_lanes import parse_lanes

    lane = dict(
        models=list(models),
        max_dispatches_per_run=1,
        daily_write_units=20,
        telemetry=dict(producer="test", unit="job", completion="consumed", scope="sample"),
        reasoning={m: "off" for m in (*models, *backups)},
    )
    if backups:
        lane.update(backup_models=list(backups), backup_after_attempts=2)
    limits = dict(
        providers={"host": {"accounts": [{"id": "first"}, {"id": "second"}]}}, routes=routes
    )
    texts = dict(
        zip(
            SOURCE_PATHS,
            [
                yaml.safe_dump(limits, sort_keys=False),
                yaml.safe_dump({"llm_lanes": {"lane": lane}}, sort_keys=False),
                "ignored: []\n",
            ],
            strict=True,
        )
    )
    return ApplyConfig(limits, parse_lanes({"lane": lane}), texts, "main-sha", TODAY)


def live_route(model="new", **changes):
    return {
        **ROUTE,
        "route_id": "surviving",
        "model": model,
        "upstream_model": "creator/new",
        "free": True,
        **changes,
    }


def test_primary_promotion_preserves_backup_retry_contract_and_prunes_reasoning():
    from citypods.compute.llm_lanes import parse_lanes
    from citypods.provider_catalog.retire import plan_retirements

    report, _ = evidence()
    config = retirement_config([ROUTE, live_route()])
    plan = plan_retirements(report, config)
    assert plan.removed_routes == ("old",) and plan.primary_changes == ("lane",)
    output = apply_config_edits(config.texts, plan)
    lane = load_config(output[SOURCE_PATHS[1]])["llm_lanes"]["lane"]
    assert lane["models"] == ["new"] and lane["reasoning"] == {"new": "off"}
    assert lane["backup_models"] == ["new"] and lane["backup_after_attempts"] == 2
    parse_lanes({"lane": lane})
    assert plan.proposal_kind == "removals"


@pytest.mark.parametrize("changes", [dict(free=False), dict(rpd=0)])
def test_paid_only_or_explicitly_paused_replacement_rejects_removal(changes):
    from citypods.provider_catalog.retire import plan_retirements

    report, _ = evidence()
    plan = plan_retirements(report, retirement_config([ROUTE, live_route(**changes)]))
    assert not plan.removed_routes and not plan.lane_repairs
    assert "no safe replacement" in plan.rejected[0]


def test_surviving_pool_route_keeps_primary_and_all_lane_settings():
    from citypods.provider_catalog.retire import plan_retirements

    report, _ = evidence()
    config = retirement_config([ROUTE, live_route(model="old")])
    plan = plan_retirements(report, config)
    assert plan.removed_routes == ("old",) and not plan.lane_repairs
    assert apply_config_edits(config.texts, plan)[SOURCE_PATHS[1]] == config.texts[SOURCE_PATHS[1]]


def test_backup_removal_retains_primary_and_other_backups_in_order():
    from citypods.provider_catalog.retire import plan_retirements

    report, _ = evidence()
    config = retirement_config([ROUTE, live_route()], models=("new",), backups=("old", "new"))
    plan = plan_retirements(report, config)
    output = apply_config_edits(config.texts, plan)
    lane = load_config(output[SOURCE_PATHS[1]])["llm_lanes"]["lane"]
    assert lane["models"] == ["new"] and lane["backup_models"] == ["new"]
    assert lane["backup_after_attempts"] == 2
    assert not plan.primary_changes and lane["reasoning"] == {"new": "off"}


def test_batch_never_removes_last_safe_replacement():
    from citypods.provider_catalog.retire import plan_retirements

    second = {**ROUTE, "route_id": "second", "model": "new"}
    report, _ = evidence((ROUTE, second))
    config = retirement_config([ROUTE, second, live_route(model="unrelated")])
    second["free"] = True
    report.anomalies[1].config_digest = digest(second)
    plan = plan_retirements(report, config)
    assert not plan.removed_routes and not plan.lane_repairs
    assert len(plan.rejected) == 2
    assert any("second" in item for item in plan.rejected)


def test_lane_comments_survive_primary_backup_and_reasoning_cleanup():
    from citypods.provider_catalog.retire import plan_retirements

    report, _ = evidence()
    config = retirement_config([ROUTE, live_route()])
    texts = dict(config.texts)
    texts[SOURCE_PATHS[1]] = texts[SOURCE_PATHS[1]].replace("- old", "- old # old model note")
    texts[SOURCE_PATHS[1]] = texts[SOURCE_PATHS[1]].replace(
        "backup_after_attempts: 2", "backup_after_attempts: 2 # retry note"
    )
    config = replace(config, texts=texts)
    output = apply_config_edits(texts, plan_retirements(report, config))
    assert "# old model note" in output[SOURCE_PATHS[1]]
    assert "# retry note" in output[SOURCE_PATHS[1]]


def test_activation_gate_prevents_probes_and_publication(monkeypatch):
    from scripts import provider_catalog_commands as commands

    monkeypatch.setattr(commands, "_control", lambda *a: pytest.fail("must not acquire live pause"))
    with pytest.raises(ValueError, match="canary"):
        commands.prepare_retirements("main")
    with pytest.raises(ValueError, match="canary"):
        commands.publish(
            EditPlan("main", (), proposal_kind="removals"),
            run_fn=lambda *a: pytest.fail("must not publish"),
        )


def test_removal_writer_rebuilds_on_changed_main_and_flags_primary(monkeypatch):
    from citypods.provider_catalog import retire
    from scripts.provider_catalog_commands import publish
    from tests.test_provider_catalog_apply import publication_runner

    monkeypatch.setattr(retire, "RETIREMENTS_ENABLED", True)
    plan = EditPlan("main-sha", (), proposal_kind="removals", primary_changes=("lane",))
    calls, run = publication_runner(main="changed")
    assert publish(plan, run_fn=run) is None
    assert not any(c[:2] == ["git", "push"] for c in calls)
    calls, run = publication_runner()
    publish(plan, run_fn=run)
    push = next(c for c in calls if c[:2] == ["git", "push"])
    assert "HEAD:refs/heads/automation/provider-catalog-removals" in push
    create = next(c for c in calls if c[:3] == ["gh", "pr", "create"])
    assert "--label" in create and "needs:human-verification" in create


def test_real_beatapi_removal_preserves_logical_pool_and_compiles(tmp_path, monkeypatch):
    from pathlib import Path

    from citypods.compute.llm_lanes import load_lanes
    from citypods.provider_catalog.retire import plan_retirements
    from scripts import compile_llm_lanes, compile_llm_limits

    root = Path(__file__).resolve().parents[1]
    texts = {p: (root / p).read_text() for p in SOURCE_PATHS}
    limits = load_config(texts[SOURCE_PATHS[0]])
    route = next(r for r in limits["routes"] if r["route_id"] == "beatapi_deepseek_v4_flash_free")
    config = ApplyConfig(limits, load_lanes(), texts, "main-sha", TODAY)
    report = Report(
        anomalies=[
            Anomaly(
                "beatapi",
                route["route_id"],
                route["upstream_model"],
                "retired",
                "end of life",
                True,
                observed_on=TODAY.isoformat(),
                config_digest=digest(route),
                contended=False,
            )
        ],
        state={
            "catalogs": {
                "beatapi": dict(
                    provider="beatapi", observed_at=TODAY.isoformat(), complete=True, models=[]
                )
            }
        },
    )
    plan = plan_retirements(report, config)
    assert plan.removed_routes == (route["route_id"],) and not plan.lane_repairs
    output = apply_config_edits(texts, plan)
    for p in SOURCE_PATHS:
        path = tmp_path / p
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output[p])
    monkeypatch.setattr(compile_llm_limits, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(compile_llm_limits, "INPUT_YAML", tmp_path / SOURCE_PATHS[0])
    compiled = compile_llm_limits.compile_limits()
    assert route["route_id"] not in compiled["routes_by_id"]
    assert compiled["model_routes_map"][route["model_key"]]
    for lane in config.lanes.values():
        assert all(m in compiled["model_routes_map"] for m in (*lane.models, *lane.backup_models))
    monkeypatch.setattr(compile_llm_lanes, "INPUT_YAML", tmp_path / SOURCE_PATHS[1])
    assert compile_llm_lanes.compile_reservations(
        load_lanes(path=tmp_path / SOURCE_PATHS[1]), compile_llm_lanes._global_ingress_budget()
    )


def test_last_backup_retirement_removes_dependent_threshold():
    from citypods.compute.llm_lanes import parse_lanes
    from citypods.provider_catalog.retire import plan_retirements

    report, _ = evidence()
    config = retirement_config([ROUTE, live_route()], models=("new",), backups=("old",))
    output = apply_config_edits(config.texts, plan_retirements(report, config))
    lane = load_config(output[SOURCE_PATHS[1]])["llm_lanes"]["lane"]
    assert lane["models"] == ["new"]
    assert "backup_models" not in lane and "backup_after_attempts" not in lane
    assert lane["reasoning"] == {"new": "off"}
    parse_lanes({"lane": lane})
