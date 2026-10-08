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
