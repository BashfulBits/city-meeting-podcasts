from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from citypods.provider_catalog.evidence import LimitObservation
from citypods.provider_catalog.limits import (
    effective_limit,
    merge_observations,
    plan_limit_changes,
)

NOW = datetime(2026, 10, 9, tzinfo=UTC)


def observation(value=70, *, run=1, days=0, **changes):
    return LimitObservation(
        metric="rpm",
        value=value,
        observed_at=(NOW - timedelta(days=days)).isoformat(),
        provider="host",
        account_id="primary",
        route_id="route",
        scope="route",
        run_id=str(run),
        **changes,
    )


def test_history_bounds_distinct_runs_and_deduplicates_a_run_conservatively():
    entries = [observation(run=run, days=run) for run in range(1, 9)]
    entries.extend([observation(75, run=1, days=1), observation(60, run=1, days=1)])
    history = merge_observations(entries, (), now=NOW)
    assert {entry.run_id for entry in history} == {str(run) for run in range(1, 7)}
    assert len(history) == 6
    assert next(entry.value for entry in history if entry.run_id == "1") == 75


def test_history_rejects_expired_future_unscoped_and_unidentified_observations():
    entries = [
        observation(days=91),
        observation(days=-1),
        replace(observation(), run_id=""),
        replace(observation(), scope="unknown"),
        replace(observation(), route_id=None),
        replace(observation(), observed_at="2026-10-09"),
        replace(observation(), run_id=True),
        replace(observation(), source=[]),
    ]
    assert merge_observations(entries, (), now=NOW) == ()
    assert merge_observations([observation(days=90)], (), now=NOW)


def test_history_bounds_each_scope_without_mixing_account_and_route_capacity():
    entries = [observation(run=run, days=run) for run in range(8)] + [
        replace(observation(run=run, days=run), scope="provider_account", route_id=None)
        for run in range(8)
    ]
    history = merge_observations(entries, (), now=NOW)
    assert len(history) == 12
    assert len([entry for entry in history if entry.scope == "route"]) == 6
    assert len([entry for entry in history if entry.scope == "provider_account"]) == 6


@pytest.mark.parametrize(
    "value,action,material",
    [
        (79, "tighten", False),
        (80, "unchanged", False),
        (120, "unchanged", False),
        (121, "offer_increase", False),
        (50, "tighten", False),
        (49, "tighten", True),
    ],
)
def test_exact_twenty_and_fifty_percent_boundaries(value, action, material):
    result = plan_limit_changes([observation(value, run=run) for run in range(3)], 100, now=NOW)
    assert result.action == action
    assert result.material is material


def test_repeated_one_run_cannot_establish_three_independent_runs():
    result = plan_limit_changes([observation()] * 3, 100, now=NOW)
    assert result.action == "unchanged"


def test_old_high_maximum_blocks_tightening_until_it_expires():
    low = [observation(run=run) for run in range(3)]
    assert plan_limit_changes([*low, observation(100, run=9, days=89)], 100, now=NOW).action == (
        "unchanged"
    )
    assert plan_limit_changes([*low, observation(100, run=9, days=91)], 100, now=NOW).action == (
        "tighten"
    )


def test_throughput_and_mixed_scopes_or_kinds_cannot_assert_a_ceiling():
    lower = observation(130, source="throughput")
    assert effective_limit([lower]) is None
    assert plan_limit_changes([lower], 100, now=NOW).action == "defer"
    assert effective_limit([observation(), replace(observation(), account_id="other")]) is None
    assert effective_limit([observation(), observation(source="documented")]) is None


def artifact_fixture():
    import io
    import json
    import zipfile

    from citypods.provider_catalog.evidence import RATE_ARTIFACT_FILE, RATE_WORKFLOW, rate_artifact

    limits = {
        "providers": {"host": {"accounts": [{"id": "primary"}]}},
        "routes": [{"route_id": "route", "provider": "host", "account_id": "primary", "rpm": 100}],
    }
    envelope = rate_artifact(
        [observation()],
        limits,
        repository="owner/repo",
        run_id="1",
        head_sha="a" * 40,
        now=NOW,
        attempted_routes=("route",),
    )
    run = {
        "id": 1,
        "status": "completed",
        "conclusion": "success",
        "event": "schedule",
        "workflow_id": 7,
        "head_branch": "main",
        "head_sha": "a" * 40,
        "head_repository": {"full_name": "owner/repo"},
    }
    artifact = {
        "id": 9,
        "name": "provider-catalog-rate-evidence-1",
        "expired": False,
        "size_in_bytes": 1000,
    }

    def api(path):
        if path.endswith("/repo"):
            return {"full_name": "owner/repo", "default_branch": "main"}
        if "/workflows/" in path:
            return {"id": 7, "path": RATE_WORKFLOW}
        if "/artifacts?" in path:
            return {"artifacts": [artifact]}
        return run

    def download(path):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr(RATE_ARTIFACT_FILE, json.dumps(envelope))
        return stream.getvalue()

    reference = {"run_id": "1", "payload_digest": envelope["payload_digest"]}
    return limits, envelope, run, artifact, reference, api, download


def test_verified_history_rehydrates_artifact_and_rejects_changed_route_digest():
    from citypods.provider_catalog.evidence import verified_rate_history

    limits, _, _, _, reference, api, download = artifact_fixture()
    kwargs = dict(
        repository="owner/repo", now=NOW, api=api, download=download, ancestor=lambda _: True
    )
    values, accepted, deferred = verified_rate_history([reference], limits, **kwargs)
    assert (
        values == (observation(),)
        and accepted
        == ({**reference, "observed_at": NOW.isoformat(), "attempted_routes": ["route"]},)
        and not deferred
    )
    limits["routes"][0]["rpm"] = 99
    values, _, _ = verified_rate_history([reference], limits, **kwargs)
    assert not values


@pytest.mark.parametrize(
    "field,value",
    [
        ("conclusion", "failure"),
        ("status", "in_progress"),
        ("event", "workflow_dispatch"),
        ("workflow_id", 8),
        ("head_branch", "fork"),
        ("head_repository", {"full_name": "other/repo"}),
    ],
)
def test_rate_artifact_run_provenance_cannot_be_forged(field, value):
    from citypods.provider_catalog.evidence import verified_rate_history

    limits, _, run, _, reference, api, download = artifact_fixture()
    run[field] = value
    values, accepted, deferred = verified_rate_history(
        [reference],
        limits,
        repository="owner/repo",
        now=NOW,
        api=api,
        download=download,
        ancestor=lambda _: True,
    )
    assert not values and not accepted and deferred


@pytest.mark.parametrize("fault", ["expired", "tampered", "not_main_ancestor"])
def test_rate_artifact_expiry_digest_and_ancestry_fail_closed(fault):
    from citypods.provider_catalog.evidence import verified_rate_history

    limits, envelope, _, artifact, reference, api, download = artifact_fixture()
    if fault == "expired":
        artifact["expired"] = True
    if fault == "tampered":
        envelope["payload"]["observations"][0]["value"] = 1
    values, accepted, deferred = verified_rate_history(
        [reference],
        limits,
        repository="owner/repo",
        now=NOW,
        api=api,
        download=download,
        ancestor=lambda _: fault != "not_main_ancestor",
    )
    assert not values and not accepted and deferred


def test_empty_successful_run_interrupts_consecutive_tightening():
    low = [observation(run=run) for run in range(1, 4)]
    assert plan_limit_changes(low, 100, now=NOW, recent_runs=("4", "3", "2")).action == "unchanged"
    assert plan_limit_changes(low, 100, now=NOW, recent_runs=("3", "2", "1")).action == "tighten"


def test_scoped_targets_require_all_shared_accounts_and_existing_caps():
    from citypods.provider_catalog.limits import configured_limit_changes

    limits = {
        "providers": {"host": {"rpm": 100, "accounts": [{"id": "primary"}, {"id": "other"}]}},
        "routes": [{"route_id": "route", "provider": "host", "account_id": "primary", "rpm": 100}],
    }
    low = [observation(run=run) for run in range(1, 4)]
    shared = [replace(o, scope="provider_account", route_id=None) for o in low]
    kwargs = dict(now=NOW, recent_runs=("3", "2", "1"))
    changes, deferred = configured_limit_changes([*low, *shared], limits, **kwargs)
    assert len(changes) == 1 and changes[0].scope == "route" and deferred
    other = [replace(o, account_id="other") for o in shared]
    changes, deferred = configured_limit_changes([*low, *shared, *other], limits, **kwargs)
    assert {c.scope for c in changes} == {"route", "provider"} and not deferred
    changes, deferred = configured_limit_changes(
        [*low, *shared, *[replace(o, value=60) for o in other]], limits, **kwargs
    )
    assert len(changes) == 1 and deferred
    del limits["providers"]["host"]["rpm"]
    assert len(configured_limit_changes([*low, *shared, *other], limits, **kwargs)[0]) == 1


def test_provider_rpd_is_not_invented_as_a_new_compiler_cap():
    from citypods.provider_catalog.limits import configured_limit_changes

    limits = {"providers": {"host": {"rpd": 100, "accounts": [{"id": "primary"}]}}, "routes": []}
    samples = [
        replace(observation(run=run), scope="provider_account", route_id=None, metric="rpd")
        for run in range(1, 4)
    ]
    changes, deferred = configured_limit_changes(
        samples, limits, now=NOW, recent_runs=("3", "2", "1")
    )
    assert not changes and deferred


@pytest.mark.parametrize("metadata", [None, [], "bad"])
def test_malformed_github_metadata_defers(metadata):
    from citypods.provider_catalog.evidence import verified_rate_history

    limits, _, _, _, reference, _, download = artifact_fixture()
    values, accepted, deferred = verified_rate_history(
        [reference],
        limits,
        repository="owner/repo",
        now=NOW,
        api=lambda _: metadata,
        download=download,
        ancestor=lambda _: True,
    )
    assert not values and not accepted and deferred


def test_early_rate_trigger_uses_today_and_never_counts_duplicate_rows_as_failures():
    from citypods.provider_catalog.limits import early_rate_routes

    limits = {"routes": [{"route_id": "route"}]}
    row = {"route_id": "route", "failure_class": "own_rpm", "count": 2, "utc_day": "2026-10-09"}
    assert not early_rate_routes([row, row], limits, {}, today=NOW.date())
    assert early_rate_routes(
        [row, {**row, "failure_class": "own_tpm", "count": 1}], limits, {}, today=NOW.date()
    ) == {"route"}
    assert not early_rate_routes(
        [{**row, "count": 3}], limits, {"route_checks": {"route": "2026-10-09"}}, today=NOW.date()
    )
    assert not early_rate_routes(
        [{**row, "count": 3, "utc_day": "2026-10-08"}], limits, {}, today=NOW.date()
    )


def test_rate_increase_requires_exact_selection_and_automatic_writer_only_tightens():
    from citypods.provider_catalog.apply import ApplyConfig
    from citypods.provider_catalog.config_edit import load_config
    from citypods.provider_catalog.limits import configured_limit_changes, rate_edit_plan
    from tests.test_provider_catalog_config_edit import rate_source

    texts = rate_source()
    limits = load_config(next(iter(texts.values())))
    samples = [replace(observation(130), route_id="old")]
    changes, _ = configured_limit_changes(samples, limits, now=NOW, recent_runs=("1",))
    config = ApplyConfig(limits, {}, texts, "base", NOW.date())
    assert not rate_edit_plan(changes, config, automatic=True).rate_changes
    assert not rate_edit_plan(changes, config).rate_changes
    assert not rate_edit_plan(
        changes, config, selected=(changes[0].choice + " forged",)
    ).rate_changes
    assert rate_edit_plan(changes, config, selected=(changes[0].choice,)).rate_changes == changes


def test_history_discovery_is_independent_of_issue_marker_and_bounds_archive_contents():
    from citypods.provider_catalog.evidence import discover_rate_references

    _, _, run, artifact, reference, api, download = artifact_fixture()

    def history_api(path):
        if "/runs?" in path:
            return {"workflow_runs": [{**run, "created_at": NOW.isoformat()}]}
        return api(path)

    refs = discover_rate_references(
        repository="owner/repo", now=NOW, api=history_api, download=download
    )
    assert refs == (reference,)
    artifact["size_in_bytes"] = 1_000_001
    with pytest.raises(ValueError, match="artifact"):
        discover_rate_references(
            repository="owner/repo", now=NOW, api=history_api, download=download
        )


def test_discovery_defers_when_bounded_run_enumeration_cannot_cover_window():
    from citypods.provider_catalog.evidence import discover_rate_references

    def api(path):
        if path.endswith("/repo"):
            return {"default_branch": "main"}
        if "/runs?" in path:
            return {"workflow_runs": [{"id": i, "created_at": NOW.isoformat()} for i in range(100)]}
        return {"artifacts": []}

    with pytest.raises(ValueError, match="bounded enumeration"):
        discover_rate_references(
            repository="owner/repo",
            now=NOW,
            api=api,
            download=lambda _: pytest.fail("unexpected download"),
        )


def test_scoped_attempt_history_ignores_unrelated_runs_but_missing_sample_interrupts():
    from citypods.provider_catalog.limits import configured_limit_changes, scope_recent_runs

    limits = {
        "providers": {"host": {"accounts": [{"id": "primary"}]}},
        "routes": [
            {"route_id": "route", "provider": "host", "account_id": "primary", "rpm": 100},
            {"route_id": "other", "provider": "host", "account_id": "secondary"},
        ],
    }
    runs = [
        {
            "run_id": str(i),
            "observed_at": (NOW - timedelta(days=i)).isoformat(),
            "attempted_routes": ["route" if i % 2 else "other"],
        }
        for i in range(1, 6)
    ]
    recent = scope_recent_runs(runs, limits)
    assert recent[("route", "route")] == ("1", "3", "5")
    samples = [observation(run=i, days=i) for i in (1, 3, 5)]
    changes, _ = configured_limit_changes(samples, limits, now=NOW, recent_runs=recent)
    assert len(changes) == 1 and changes[0].action == "tighten"
    runs[1]["attempted_routes"] = ["route"]
    recent = scope_recent_runs(runs, limits)
    changes, _ = configured_limit_changes(samples, limits, now=NOW, recent_runs=recent)
    assert not changes


def test_route_evidence_rejects_changed_provider_binding_and_malformed_attempts():
    from citypods.provider_catalog.evidence import digest, verified_rate_history

    limits, envelope, _, _, reference, api, download = artifact_fixture()
    limits["providers"]["host"]["api_base"] = "https://different.test"
    values, _, _ = verified_rate_history(
        [reference],
        limits,
        repository="owner/repo",
        now=NOW,
        api=api,
        download=download,
        ancestor=lambda _: True,
    )
    assert not values
    limits, envelope, _, _, reference, api, download = artifact_fixture()
    envelope["payload"]["attempted_routes"] = [False]
    envelope["payload_digest"] = digest(envelope["payload"])
    reference["payload_digest"] = envelope["payload_digest"]
    values, accepted, deferred = verified_rate_history(
        [reference],
        limits,
        repository="owner/repo",
        now=NOW,
        api=api,
        download=download,
        ancestor=lambda _: True,
    )
    assert not values and not accepted and deferred


def test_manual_report_carries_unchecked_rate_offers_and_advisory_history():
    from dataclasses import asdict

    from citypods.provider_catalog.limits import RateChange
    from citypods.provider_catalog.reconcile import Report
    from scripts.reconcile_provider_routes import _carry_rate_state

    change = RateChange("route", "host", "route", "rpm", 100, 150, "offer_increase", "stamp")
    state = {
        "last_full": {"rate_changes": [asdict(change)]},
        "rate_changes": [asdict(change)],
        "rate_evidence_refs": [{"run_id": "1"}],
        "rate_observations": [asdict(observation())],
    }
    report = Report(state={"last_full": {}})
    _carry_rate_state(report, state)
    assert report.rate_changes == [change]
    assert report.state["last_full"]["rate_changes"] == state["rate_changes"]
    assert all(report.state[key] == value for key, value in state.items())
    del state["rate_changes"]
    _carry_rate_state(report, state)
    assert report.rate_changes == [change]
