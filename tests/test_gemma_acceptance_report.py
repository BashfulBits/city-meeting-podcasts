from __future__ import annotations

import copy
import json

import pytest

from scripts.gemma_acceptance_report import build_report, main, timestamp


def event(attempt, job, ordinal, status, *, start=1000, end=2000, version="deployed", **extra):
    return {
        "source": {
            "event": "gemma_provider_attempt",
            "job_id": job,
            "attempt_id": attempt,
            "attempt_ordinal": ordinal,
            "provider_status_code": status,
            "actual_start_at": start,
            "actual_end_at": end,
            "model": "gemma-4-31b-it",
            "route_id": "google-primary",
            "purpose": "judge:sibling",
            **extra,
        },
        "$workers": {"scriptVersion": {"id": version}},
    }


def report(*exports, **kwargs):
    return build_report(
        exports, since=1000, as_of=100_000, version="deployed", maturity_seconds=10, **kwargs
    )


def test_retries_and_duplicate_exports_do_not_repair_first_attempt_acceptance():
    rows = [event("a", "one", 1, 429), event("b", "one", 2, 200), event("c", "two", 1, 200)]
    result = report(rows, rows)
    group = result["groups"][0]
    assert result["observed_mature_attempts"] == 3
    assert result["discarded_events"]["duplicate_attempt_event"] == 3
    assert group["jobs"] == 2
    assert group["accepted_http_2xx"] == 1
    assert group["acceptance_fraction"] == 0.5
    assert group["wilson_95_interval"][0] < 0.5 < group["wilson_95_interval"][1]


def test_fast_recent_failures_and_unfinished_successes_share_the_maturity_exclusion():
    rows = [
        event("old", "old", 1, 200, start=999),
        event("version", "version", 1, 200, version="old"),
        event("fast", "fast", 1, 429, start=90_001, end=90_002),
        event("slow", "slow", 1, 200, start=90_001, end=99_000),
        event("boundary", "boundary", 1, 200, start=90_000, end=99_999),
    ]
    result = report(rows)
    assert result["groups"][0]["job_ids"] == ["boundary"]
    assert result["discarded_events"] == {
        "before_cutoff": 1,
        "different_or_missing_version": 1,
        "not_yet_mature": 2,
    }
    empty = build_report(
        [rows], since=90_001, as_of=100_000, version="deployed", maturity_seconds=10
    )
    assert not empty["has_mature_start_window"]
    assert empty["groups"] == []


def test_superseded_generations_and_conflicting_attempt_ids_are_excluded():
    rows = [
        event("a", "superseded", 1, 429),
        event("b", "superseded", 1, 200),
        event("c", "conflict", 1, 200),
        event("c", "conflict", 1, 429),
    ]
    result = report(rows)
    assert result["ambiguous_initial_jobs"] == 1
    assert result["conflicting_attempt_ids"] == 1
    assert result["groups"] == []


def test_google_routes_and_models_stay_separate_and_full_input_includes_cache():
    rows = [
        event(
            "a",
            "a",
            1,
            200,
            observed_input_tokens=10728,
            observed_cached_input_tokens=9901,
            observed_uncached_input_tokens=827,
            input_token_estimate=11000,
        ),
        event(
            "b",
            "b",
            1,
            429,
            route_id="google-secondary",
            quota={
                "message": "PRIVATE_ERROR",
                "violations": [
                    {
                        "quota_id": "GenerateContentInputTokensPerModelPerMinute-FreeTier",
                        "quota_value": 16000,
                        "secret": "PRIVATE_TOKEN",
                    }
                ],
            },
        ),
        event("c", "c", 1, 200, model="gemma-4-26b-a4b-it"),
    ]
    rows[0]["source"]["payload"] = {"messages": ["PRIVATE_PROMPT"]}
    result = report(rows)
    assert len(result["groups"]) == 2
    group = next(g for g in result["groups"] if g["model"] == "gemma-4-31b-it")
    assert group["jobs"] == 2
    assert group["observed_full_input_range"] == [10728, 10728]
    assert group["raw_input_estimate_range"] == [11000, 11000]
    assert group["first_routes"]["google-secondary"]["accepted_http_2xx"] == 0
    assert group["quota_limits"] == {"16000": 1}
    assert "PRIVATE" not in json.dumps(result)
    assert "not inferred" in result["coverage"]


def test_malformed_events_do_not_become_initial_attempts():
    rows = [
        event("future", "future", 1, 200, end=200_000),
        event("bool", "bool", True, 200),
        event("no-start", "no-start", 1, 200, start=None),
        event("no-id", "no-id", 1, 200),
    ]
    del rows[-1]["source"]["attempt_id"]
    result = report(rows)
    assert result["groups"] == []
    assert result["discarded_events"]["invalid_or_unobserved_end"] == 1
    with pytest.raises(ValueError):
        report({"success": False, "result": {"events": {"events": []}}})
    with pytest.raises(ValueError):
        report({"error": "not a log export"})
    with pytest.raises(ValueError):
        build_report([], since=1, as_of=0, version="deployed")
    with pytest.raises(ValueError):
        timestamp("2026-10-20T04:44:25")


def test_cli_reads_query_exports_and_requires_explicit_cutoffs(tmp_path, capsys):
    rows = [event("a", "a", 1, 200)]
    export = {"success": True, "result": {"events": {"events": copy.deepcopy(rows)}}}
    path = tmp_path / "events.json"
    path.write_text(json.dumps(export))
    assert (
        main(
            [
                str(path),
                "--since",
                "1970-01-01T00:00:01Z",
                "--as-of",
                "1970-01-01T00:01:40Z",
                "--version",
                "deployed",
                "--maturity-seconds",
                "10",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["groups"][0]["jobs"] == 1


@pytest.mark.parametrize("other_start,other_end", [(999, 2000), (90_001, 99_000)])
def test_reset_generations_outside_cutoff_or_maturity_still_make_job_ambiguous(
    other_start, other_end
):
    rows = [
        event("a", "reset", 1, 429),
        event("b", "reset", 1, 200, start=other_start, end=other_end),
    ]
    result = report(rows)
    assert result["ambiguous_initial_jobs"] == 1
    assert result["groups"] == []
