"""Discovery acceptance uses deterministic limits, usage drift and failures, never live quota."""

from dataclasses import asdict, replace
from datetime import UTC, datetime

import pytest

from citypods.provider_catalog.discovery import (
    AXES,
    DEFAULT_COMBINED_SEED,
    DEFAULT_OUTPUT_SEED,
    INPUT_OUTPUT_COUNTERPART,
    MAX_INPUT,
    Bracket,
    DiscoveryBudget,
    DiscoveryJournal,
    DiscoveryObservation,
    DiscoveryRequest,
    discovery_identity,
    run_discovery,
)

STAMP = datetime.now(UTC).isoformat()


def request(axis="input", target=2048, attempt="a"):
    output_tokens = (
        INPUT_OUTPUT_COUNTERPART if axis in {"input", "total_input_heavy"} else DEFAULT_OUTPUT_SEED
    )
    return DiscoveryRequest(
        "session",
        attempt,
        "physical",
        axis,
        target,
        target,
        output_tokens,
        f"{axis}:v2"
        if axis in {"input", "total_input_heavy"}
        else "output:v3"
        if axis == "output"
        else f"{axis}:v1",
        "parser-v1",
    )


def observation(req, count, admitted=True, **kwargs):
    return DiscoveryObservation(
        req,
        STAMP,
        outcome="admitted" if admitted else "refused",
        coordinate=count,
        count_basis=("prompt_plus_requested_output" if req.axis.startswith("total_") else req.axis),
        verified=True,
        reported_output=count if req.axis == "output" and admitted else None,
        finish_reason="length" if req.axis == "output" and admitted else None,
        **kwargs,
    )


def execute(tmp_path, adapter, **kwargs):
    supported_axes = kwargs.pop("supported_axes", AXES)
    return run_discovery(
        session_id="session",
        identity="physical",
        parser_version="parser-v1",
        supported_axes=supported_axes,
        measure=adapter,
        renew=lambda: None,
        journal=DiscoveryJournal(tmp_path / "journal.json", {"identity": "physical"}),
        **kwargs,
    )


@pytest.mark.parametrize("axis", AXES)
@pytest.mark.parametrize("seed", [256, 20_000])
def test_each_axis_converges_without_more_than_64_calls(tmp_path, axis, seed):
    limits = dict(input=4000, output=1800, total_input_heavy=25_000, total_output_heavy=25_000)
    calls = []

    def measure(req, admit, timeout):
        receipt = admit()
        assert receipt.matches(req) and 0 < timeout <= 600
        actual_input = req.input_tokens
        count = (
            actual_input
            if req.axis == "input"
            else req.output_tokens
            if req.axis == "output"
            else actual_input + req.output_tokens
        )
        calls.append((req.axis, count))
        return observation(req, count, count <= limits[req.axis], reported_input=actual_input)

    result = execute(tmp_path, measure, supported_axes=(axis,), seeds={axis: seed})
    assert result.budget["calls"] <= 64
    assert len(result.brackets) == 4
    bracket = next(bracket for bracket in result.brackets if bracket["axis"] == axis)
    assert bracket["status"] == "converged"
    assert bracket["admitted"] <= limits[axis] < bracket["refused"]
    assert bracket["refused"] - bracket["admitted"] <= bracket["target_width"]
    if seed == 20_000 and axis == "input":
        assert [count for axis, count in calls if axis == "input"][:3] == [20000, 10000, 5000]


@pytest.mark.parametrize(
    ("axis", "expected_target", "expected_input", "expected_output"),
    [
        ("output", DEFAULT_OUTPUT_SEED, 2_048, DEFAULT_OUTPUT_SEED),
        (
            "total_input_heavy",
            DEFAULT_COMBINED_SEED,
            2_048,
            INPUT_OUTPUT_COUNTERPART,
        ),
        (
            "total_output_heavy",
            DEFAULT_COMBINED_SEED,
            2_048,
            INPUT_OUTPUT_COUNTERPART,
        ),
    ],
)
def test_default_output_and_combined_probes_leave_reasoning_headroom(
    tmp_path, axis, expected_target, expected_input, expected_output
):
    calls = []

    def measure(req, admit, timeout):
        admit()
        calls.append(req)
        return DiscoveryObservation(req, STAMP, outcome="outage", diagnostic="http_503")

    result = execute(tmp_path, measure, supported_axes=(axis,))

    assert result.reason == "outage"
    assert len(calls) == 1
    assert calls[0].target == expected_target
    assert calls[0].input_tokens == expected_input
    assert calls[0].output_tokens == expected_output


@pytest.mark.parametrize("remaining, expected", [(1000, 600), (300, 300)])
def test_discovery_timeout_uses_600_second_cap_and_session_deadline(tmp_path, remaining, expected):
    timeouts = []

    def measure(req, admit, timeout):
        admit()
        timeouts.append(timeout)
        return DiscoveryObservation(req, STAMP, outcome="outage", diagnostic="http_503")

    result = execute(
        tmp_path,
        measure,
        supported_axes=("input",),
        clock=lambda: 0,
        deadline=remaining,
    )

    assert timeouts == [expected]
    assert result.reason == "outage"


def test_slow_no_response_attempts_stop_at_session_deadline_with_partial_evidence(tmp_path):
    now = [0.0]
    timeouts = []

    def measure(req, admit, timeout):
        admit()
        timeouts.append(timeout)
        now[0] += timeout
        return DiscoveryObservation(req, STAMP, diagnostic="worker_response_timeout")

    result = execute(
        tmp_path,
        measure,
        supported_axes=("input",),
        clock=lambda: now[0],
        deadline=1200,
    )

    assert timeouts == pytest.approx([600, 600])
    assert result.reason == "uncertain_no_response_before_session_deadline"
    assert result.brackets[0]["admitted"] is None
    assert result.brackets[0]["refused"] is None
    assert len(result.transport_retries) == 1
    assert len(result.transport_retries[0]) == 2


def test_actual_counts_correct_input_estimate(tmp_path):
    inputs = []

    def measure(req, admit, timeout):
        admit()
        count = req.input_tokens * 2
        inputs.append(req.input_tokens)
        return observation(req, count, count <= 5000, reported_input=count)

    result = run_discovery(
        session_id="session",
        identity="physical",
        parser_version="parser-v1",
        supported_axes=("input",),
        measure=measure,
        renew=lambda: None,
        journal=DiscoveryJournal(tmp_path / "counts.json", {}),
        seeds={"input": 2048},
    )
    assert result.brackets[0]["status"] == "converged"
    assert inputs[:2] == [2048, 3072]
    assert result.budget["input_tokens"] >= sum(inputs) * 2


def test_default_input_search_starts_high_and_reports_hard_ceiling(tmp_path):
    calls = []

    def measure(req, admit, _timeout):
        admit()
        calls.append(req)
        count = min(req.input_tokens, MAX_INPUT - 13)
        return observation(req, count, reported_input=count)

    result = execute(tmp_path, measure, supported_axes=("input",))

    assert [req.input_tokens for req in calls] == [500_000, 750_000, 1_000_000]
    assert all(req.output_tokens == INPUT_OUTPUT_COUNTERPART for req in calls)
    assert all(req.control.startswith("input:v2") for req in calls)
    assert result.reason == "partial"
    assert result.brackets[0]["admitted"] == MAX_INPUT - 13
    assert result.brackets[0]["refused"] is None
    assert result.brackets[0]["reason"] == "hard_ceiling_lower_bound"


def test_input_search_stops_after_refusal_at_two_thousand_tokens(tmp_path):
    calls = []

    def measure(req, admit, _timeout):
        admit()
        calls.append(req.input_tokens)
        return observation(req, req.input_tokens, admitted=False)

    result = execute(tmp_path, measure, supported_axes=("input",))

    assert calls == [
        500_000,
        250_000,
        125_000,
        62_500,
        31_250,
        15_625,
        7_812,
        3_906,
        2_000,
    ]
    assert result.reason == "partial"
    assert result.brackets[0]["admitted"] is None
    assert result.brackets[0]["refused"] == 2_000
    assert result.brackets[0]["reason"] == "minimum_probe_refused"


def test_search_stops_cleanly_at_existing_smaller_budget(tmp_path):
    calls = []

    def measure(req, admit, _timeout):
        admit()
        calls.append(req.input_tokens)
        return observation(req, req.input_tokens, reported_input=req.input_tokens)

    result = execute(
        tmp_path,
        measure,
        supported_axes=("input",),
        budget=DiscoveryBudget(max_input=2_097_152),
    )

    assert calls == [500_000, 750_000]
    assert result.reason == "budget_exhausted"
    assert result.brackets[0]["admitted"] == 750_000
    assert result.brackets[0]["refused"] is None


def test_discovery_input_request_accepts_one_million_token_ceiling():
    from citypods.provider_catalog.discovery import DiscoveryRequest
    from citypods.provider_catalog.probe import build_discovery_request
    from citypods.provider_catalog.providers.groq import RULES

    cfg = dict(
        api_base="https://api.groq.com/openai/v1",
        accounts=[dict(id="a", api_key_env="KEY")],
    )
    route = dict(route_id="candidate", provider="groq", account_id="a", upstream_model="model")
    req = DiscoveryRequest(
        "session",
        "attempt",
        discovery_identity("groq", "a", "model", "https://api.groq.com/openai/v1/chat/completions"),
        "input",
        MAX_INPUT,
        MAX_INPUT,
        256,
        "input:v1",
        RULES.discovery_parser_version,
    )

    transient = build_discovery_request(req, route, cfg, RULES, nonce="f" * 32)
    assert transient.local_input_estimate <= MAX_INPUT

    with pytest.raises(ValueError, match="invalid discovery request"):
        replace(req, target=MAX_INPUT + 1, input_tokens=MAX_INPUT + 1)


def test_rate_rejection_never_establishes_upper(tmp_path):
    calls = []
    now = [0.0]

    def measure(req, admit, timeout):
        admit()
        calls.append(req)
        return DiscoveryObservation(req, STAMP, outcome="quota", retry_after_seconds=3)

    result = execute(
        tmp_path,
        measure,
        clock=lambda: now[0],
        sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
    )
    assert len(calls) == 2
    assert calls[0].input_tokens == calls[1].input_tokens
    assert result.reason == "quota_deferred"
    assert result.brackets[0]["refused"] is None


@pytest.mark.parametrize("outcome", ["daily", "outage"])
def test_account_errors_stop_all_axes(tmp_path, outcome):
    def measure(req, admit, timeout):
        admit()
        return DiscoveryObservation(req, STAMP, outcome=outcome)

    result = execute(tmp_path, measure)
    assert result.reason == outcome
    assert result.budget["calls"] == 1


def test_natural_output_never_converges(tmp_path):
    def measure(req, admit, timeout):
        admit()
        return DiscoveryObservation(
            req, STAMP, outcome="lower_only", reported_output=20, finish_reason="stop"
        )

    result = execute(tmp_path, measure)
    assert result.budget["calls"] == 12
    assert all(b["status"] == "partial" and b["refused"] is None for b in result.brackets)
    with pytest.raises(ValueError, match="generated"):
        replace(observation(request("output"), 20), finish_reason="stop")


def test_budget_exhaustion_and_unknown_response_stay_charged(tmp_path):
    def measure(req, admit, timeout):
        admit()
        raise TimeoutError("never persist a credential or prompt")

    result = execute(tmp_path, measure)
    assert result.budget["calls"] == 1
    assert "credential" not in (tmp_path / "journal.json").read_text()
    resumed = execute(tmp_path, measure)
    assert resumed.reason == "indeterminate_attempt_new_receipt"
    assert resumed.budget["calls"] == 1

    other = tmp_path / "new"
    other.mkdir()

    def successful(req, admit, timeout):
        admit()
        return observation(req, req.input_tokens, reported_input=req.input_tokens)

    result = execute(other, successful, budget=DiscoveryBudget(max_calls=1))
    assert result.budget["calls"] == 1
    assert result.reason == "budget_exhausted"


def test_no_response_retries_up_to_five_charged_calls_then_reports_uncertainty(tmp_path):
    calls = []

    def measure(req, admit, timeout):
        admit()
        calls.append(req)
        return DiscoveryObservation(
            req,
            STAMP,
            diagnostic="transport_timeout",
        )

    result = execute(
        tmp_path,
        measure,
        supported_axes=("input",),
        budget=DiscoveryBudget(max_calls=64),
    )

    assert len(calls) == result.budget["calls"] == 5
    assert len({request.attempt_id for request in calls}) == 5
    assert {request.target for request in calls} == {calls[0].target}
    assert result.reason == "uncertain_no_response_after_five_calls"
    assert result.brackets[0]["admitted"] is None
    assert result.brackets[0]["refused"] is None
    assert len(result.transport_retries) == 1
    assert len(result.transport_retries[0]) == 5
    journal = DiscoveryJournal(tmp_path / "journal.json", {"identity": "physical"})
    assert len(journal.data["attempts"]) == 5


def test_multiple_transport_attempts_are_reported_when_later_call_responds(tmp_path):
    calls = []

    def measure(req, admit, timeout):
        admit()
        calls.append(req)
        if len(calls) < 3:
            return DiscoveryObservation(req, STAMP, diagnostic="worker_response_timeout")
        return DiscoveryObservation(req, STAMP, outcome="outage", diagnostic="http_503")

    result = execute(tmp_path, measure, supported_axes=("input",))

    assert result.reason == "outage"
    assert result.budget["calls"] == 3
    assert len(result.transport_retries) == 1
    assert [row["diagnostic"] for row in result.transport_retries[0]] == [
        "worker_response_timeout",
        "worker_response_timeout",
        "http_503",
    ]


def test_safe_stop_after_charged_discovery_attempt_keeps_retryable_artifact(tmp_path, monkeypatch):
    from datetime import date

    import citypods.provider_catalog.discovery as discovery
    from citypods.provider_catalog.reconcile import Candidate, _discover_candidate_caps
    from citypods.provider_catalog.registry import rules_for

    rules = rules_for("groq")
    candidate = Candidate(
        "groq", "meta/llama-new", 20, False, 8192, (), (), "2026-10-12", evidence={"methods": []}
    )
    provider_cfg = {
        "api_base": "https://api.groq.com/openai/v1",
        "accounts": [{"id": "account", "api_key_env": "GROQ_DISCOVERY_TEST_KEY"}],
    }
    monkeypatch.setenv("GROQ_DISCOVERY_TEST_KEY", "test-only")

    def charged_then_stop(**kwargs):
        request = DiscoveryRequest(
            kwargs["session_id"],
            "charged-attempt",
            kwargs["identity"],
            "input",
            2048,
            2048,
            256,
            "input:v1",
            rules.discovery_parser_version,
        )
        kwargs["journal"].charge(request)
        raise RuntimeError("injected safe stop")

    monkeypatch.setattr(discovery, "run_discovery", charged_then_stop)
    result = _discover_candidate_caps(
        candidate,
        {"context_length": 8192},
        provider_cfg=provider_cfg,
        rules=rules,
        pause=type("Pause", (), {"renew": lambda _self: None})(),
        session=object(),
        today=date(2026, 10, 12),
        run_id="123",
        receipt_id="receipt",
        history=(),
        shared_budget=None,
        sleep=lambda _seconds: None,
    )

    assert result["summary"]["retryable"] is True
    assert result["summary"]["calls"] == 2
    assert result["artifact"]["attempts"][0]["observation"] is None


def test_shared_budget_validation_is_atomic(tmp_path):
    shared = DiscoveryBudget(max_calls=0)

    def measure(req, admit, timeout):
        admit()
        pytest.fail("must never release transport")

    result = execute(tmp_path, measure, shared_budget=shared)
    assert result.budget["calls"] == shared.calls == 0


def test_contradiction_and_control_mismatch(tmp_path):
    bracket = Bracket("input", 2048)
    bracket.observe(observation(request(), 2000))
    bracket.observe(observation(request(attempt="b"), 3000, False))
    bracket.observe(observation(request(attempt="c"), 4000))
    assert bracket.upper is None and bracket.lower == 4000
    assert bracket.reason == "contradictory_boundary_rebaseline"
    bracket.observe(observation(replace(request(), control="other"), 5000, False))
    assert bracket.upper is None
    assert bracket.reason == "incomparable_count_or_control"


def test_resume_requires_two_fresh_successes(tmp_path):
    history = [observation(request(), 4000), observation(request(attempt="b"), 4010, False)]
    seen = []

    def measure(req, admit, timeout):
        admit()
        seen.append(req.target)
        return observation(
            req, req.input_tokens, req.input_tokens <= 4000, reported_input=req.input_tokens
        )

    result = execute(tmp_path, measure, history=history)
    assert seen[:2] == [4000, 4000]
    assert result.brackets[0]["status"] == "converged"


def test_pause_loss_and_reported_overrun_stop(tmp_path):
    journal = DiscoveryJournal(tmp_path / "pause.json", {})
    result = run_discovery(
        session_id="s",
        identity="physical",
        parser_version="v",
        supported_axes=AXES,
        measure=lambda *args: pytest.fail("no transport"),
        journal=journal,
        renew=lambda: (_ for _ in ()).throw(RuntimeError("pause lost")),
    )
    assert result.budget["calls"] == 0

    def measure(req, admit, timeout):
        admit()
        return observation(req, 100000, reported_input=100000)

    result = execute(
        tmp_path,
        measure,
        budget=DiscoveryBudget(max_input=3000),
        seeds={"input": 2048},
    )
    assert result.budget["calls"] == 1
    assert result.reason == "deadline_or_reported_usage_overrun"


def test_journal_identity_and_observation_round_trip(tmp_path):
    journal = DiscoveryJournal(tmp_path / "checkpoint.json", {"session_id": "s"})
    req = request()
    journal.charge(req)
    with pytest.raises(ValueError, match="already_charged"):
        journal.charge(req)
    item = observation(req, 2000)
    journal.complete(item)
    assert DiscoveryObservation.from_dict(asdict(item)) == item
    assert DiscoveryJournal(journal.path, {"session_id": "s"}).data == journal.data
    with pytest.raises(ValueError, match="identity"):
        DiscoveryJournal(journal.path, {"session_id": "another"})


def test_retry_receipt_is_bound_to_authorized_result_and_next_utc_day():
    from citypods.provider_catalog.evidence import (
        discovery_retry_receipt,
        validate_discovery_retry_receipt,
    )

    now = datetime(2026, 10, 11, 23, 30, tzinfo=UTC)
    envelope = discovery_retry_receipt(
        repository="owner/repo",
        issue_number=2271,
        candidate_key="groq/model",
        result_revision="a" * 64,
        comment_id=123,
        comment_created_at="2026-10-11T23:00:00Z",
        command_run_id="987",
        command_head_sha="b" * 40,
        now=now,
    )
    assert envelope["payload"]["earliest_date"] == "2026-10-12"
    with pytest.raises(ValueError, match="date"):
        validate_discovery_retry_receipt(
            envelope,
            repository="owner/repo",
            now=datetime(2026, 10, 11, 23, 59, tzinfo=UTC),
        )
    assert (
        validate_discovery_retry_receipt(
            envelope,
            repository="owner/repo",
            now=datetime(2026, 10, 12, 8, 0, tzinfo=UTC),
        )["candidate_key"]
        == "groq/model"
    )

    # A slow command run must retain the date selected by the original comment.
    delayed = discovery_retry_receipt(
        repository="owner/repo",
        issue_number=2271,
        candidate_key="groq/model",
        result_revision="a" * 64,
        comment_id=123,
        comment_created_at="2026-10-11T23:00:00Z",
        command_run_id="987",
        command_head_sha="b" * 40,
        now=datetime(2026, 10, 12, 0, 5, tzinfo=UTC),
    )
    assert delayed["payload"]["earliest_date"] == "2026-10-12"
    tampered = {**envelope, "payload": {**envelope["payload"], "issue_number": 2272}}
    with pytest.raises(ValueError, match="checksum"):
        validate_discovery_retry_receipt(tampered, repository="owner/repo", now=now)


def test_discovery_reference_scan_includes_manual_and_scheduled_runs():
    import io
    import json
    import zipfile
    from urllib.parse import parse_qs, urlparse

    from citypods.provider_catalog.evidence import discover_discovery_references

    now = datetime(2026, 10, 11, 12, tzinfo=UTC)
    envelope = {"payload_digest": "a" * 64}
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("provider-catalog-discovery-evidence.json", json.dumps(envelope))
    raw = stream.getvalue()
    events = []

    def api(path):
        if path.startswith("repos/owner/repo/actions/workflows/") and "/runs?" in path:
            query = parse_qs(urlparse(path).query)
            event = query["event"][0]
            events.append(event)
            runs = (
                [
                    {
                        "id": 15 if event == "schedule" else 16,
                        "created_at": "2026-10-11T10:00:00Z",
                    }
                ]
                if query["page"] == ["1"]
                else []
            )
            return {"workflow_runs": runs}
        if "/actions/runs/15/artifacts?" in path:
            run_id = "15"
        elif "/actions/runs/16/artifacts?" in path:
            run_id = "16"
        else:
            return {"default_branch": "main"}
        return {
            "artifacts": [
                {
                    "id": int(run_id),
                    "name": f"provider-catalog-discovery-evidence-{run_id}",
                    "expired": False,
                    "size_in_bytes": len(raw),
                }
            ]
        }

    refs = discover_discovery_references(
        repository="owner/repo", now=now, api=api, download=lambda _path: raw
    )
    assert [row["run_id"] for row in refs] == ["15", "16"]
    assert events == ["schedule", "workflow_dispatch"]


def test_authenticate_partial_artifacts_and_reject_forged_numeric_state(tmp_path):
    import io
    import json
    import zipfile

    from citypods.provider_catalog.evidence import discovery_artifact, verified_discovery_history

    now = datetime.now(UTC)
    req = replace(
        request(),
        identity=discovery_identity("groq", "a", "model", "https://api.groq.com/openai/v1"),
    )
    metadata = dict(
        session_id="session",
        identity=req.identity,
        provider="groq",
        account_id="a",
        upstream_model="model",
        endpoint="https://api.groq.com/openai/v1",
        parser_version="parser-v1",
        fixture_version="discovery-v1",
        catalog_digest="a" * 64,
        free_evidence_digest="b" * 64,
        created_at=now.isoformat(),
    )
    session = dict(
        session=metadata,
        attempts=[dict(request=asdict(req), observation=asdict(observation(req, 2000)))],
        reason="partial",
    )
    terminal_retry = {
        "session": {
            **metadata,
            "session_id": "terminal-session",
            "receipt_id": "authorized-retry",
        },
        "attempts": [],
        "reason": "reachability_not_proven",
    }
    envelope = discovery_artifact(
        [session, terminal_retry],
        repository="owner/repo",
        run_id="5",
        head_sha="a" * 40,
        now=now,
    )

    def archive(value):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as zipped:
            zipped.writestr("provider-catalog-discovery-evidence.json", json.dumps(value))
        return stream.getvalue()

    raw = [archive(envelope)]
    run = dict(
        id=5,
        status="completed",
        conclusion="failure",
        event="schedule",
        workflow_id=2,
        head_branch="main",
        head_repository=dict(full_name="owner/repo"),
        head_sha="a" * 40,
    )

    def api(path):
        if path.endswith("/provider-catalog-reconcile.yml"):
            return dict(id=2, path=".github/workflows/provider-catalog-reconcile.yml")
        if path.endswith("/artifacts?per_page=100"):
            return dict(
                artifacts=[
                    dict(
                        id=8,
                        name="provider-catalog-discovery-evidence-5",
                        expired=False,
                        size_in_bytes=len(raw[0]),
                    )
                ]
            )
        if path.endswith("/runs/5"):
            return run
        return dict(full_name="owner/repo", default_branch="main")

    def verify():
        return verified_discovery_history(
            [dict(run_id="5")],
            repository="owner/repo",
            now=now,
            api=api,
            download=lambda _: raw[0],
            ancestor=lambda _: True,
        )

    values, accepted, deferred = verify()
    assert len(values) == len(accepted) == 1 and not deferred
    assert accepted[0]["receipt_ids"] == ["authorized-retry"]

    # Six newer empty or duplicate-only sessions must not crowd out older useful evidence.
    import copy
    import re

    from citypods.provider_catalog.evidence import digest

    for duplicate in (False, True):
        archives = {}
        for rid in range(5, 12):
            candidate = copy.deepcopy(envelope)
            candidate["payload"]["run_id"] = str(rid)
            rows = candidate["payload"]["sessions"][0]["attempts"]
            if rid > 5:
                if duplicate:
                    rows[0]["request"]["attempt_id"] = "copied"
                    rows[0]["observation"]["request"]["attempt_id"] = "copied"
                else:
                    rows.clear()
            candidate["payload_digest"] = digest(candidate["payload"])
            archives[rid] = archive(candidate)

        def history_api(path, archives=archives):
            match = re.search(r"/runs/([0-9]+)", path)
            if match:
                rid = int(match[1])
                if "artifacts?" in path:
                    return dict(
                        artifacts=[
                            dict(
                                id=rid,
                                name=f"provider-catalog-discovery-evidence-{rid}",
                                expired=False,
                                size_in_bytes=len(archives[rid]),
                            )
                        ]
                    )
                return run | {"id": rid}
            return api(path)

        found, accepted_runs, failures = verified_discovery_history(
            [dict(run_id=str(rid)) for rid in range(5, 12)],
            repository="owner/repo",
            now=now,
            api=history_api,
            download=lambda path, archives=archives: archives[
                int(re.search(r"/artifacts/([0-9]+)", path)[1])
            ],
            ancestor=lambda _: True,
        )
        assert not failures and len(accepted_runs) == 7
        assert len(found) == (2 if duplicate else 1)
        assert any(item.request.attempt_id == "a" for item in found)
    run["event"] = "pull_request"
    assert verify()[2]
    run["event"] = "schedule"
    envelope["payload"]["sessions"][0]["attempts"][0]["observation"]["coordinate"] = 999999
    raw[0] = archive(envelope)
    assert verify()[2]


def test_unsupported_current_provider_endpoints_are_explicit():
    from citypods.provider_catalog.registry import all_rules
    from citypods.provider_catalog.rules import Response

    for name, rules in all_rules().items():
        if name not in {"groq", "openrouter", "nvidia"}:
            assert not rules.discovery_axes
            assert (
                rules.discovery_observation(Response(200, body="{}"), request(), None).outcome
                == "inconclusive"
            )
    assert not any(
        axis.startswith("total") for rules in all_rules().values() for axis in rules.discovery_axes
    )


def test_chat_usage_and_rate_numbers_have_separate_authority():
    import json

    from citypods.provider_catalog.probe import build_discovery_request
    from citypods.provider_catalog.providers.groq import RULES
    from citypods.provider_catalog.rules import Response

    cfg = dict(
        api_base="https://api.groq.com/openai/v1", accounts=[dict(id="a", api_key_env="KEY")]
    )
    route = dict(route_id="candidate", provider="groq", account_id="a", upstream_model="model")
    req = request()
    req = replace(
        req,
        identity=discovery_identity(
            "groq", "a", "model", "https://api.groq.com/openai/v1/chat/completions"
        ),
    )
    transient = build_discovery_request(req, route, cfg, RULES, nonce="f" * 32)
    import re

    expected = dict(
        re.findall(r"(start|middle|tail)_sentinel=([a-f0-9]{32})", transient.messages[0]["content"])
    )
    body = dict(
        object="chat.completion",
        usage=dict(prompt_tokens=2047, completion_tokens=100, total_tokens=2147),
        choices=[dict(message=dict(content=json.dumps(expected)), finish_reason="stop")],
    )
    result = RULES.discovery_observation(Response(200, body=json.dumps(body)), req, transient)
    assert result.outcome == "admitted" and result.coordinate == 2047
    assert result.reported_input == 2047 and result.reported_output == 100
    assert result.response_preview == ""
    error = Response(429, body='{"error":{"message":"TPM limit 8000, requested 9000"}}')
    result = RULES.discovery_observation(error, req, transient)
    assert result.outcome == "quota" and result.coordinate is None


@pytest.mark.parametrize(
    ("content_state", "finish_state", "diagnostic"),
    [
        ("missing", "null", "completion_content_missing"),
        ("null", "null", "completion_content_null"),
        ("empty", "missing", "completion_content_empty"),
        ("other", "other", "completion_content_not_text"),
    ],
)
def test_discovery_records_usage_and_response_shape_when_completion_is_invalid(
    content_state, finish_state, diagnostic
):
    import json

    from citypods.provider_catalog.probe import build_discovery_request
    from citypods.provider_catalog.providers.groq import RULES
    from citypods.provider_catalog.rules import Response

    cfg = dict(
        api_base="https://api.groq.com/openai/v1", accounts=[dict(id="a", api_key_env="KEY")]
    )
    route = dict(route_id="candidate", provider="groq", account_id="a", upstream_model="model")
    req = replace(
        request(),
        identity=discovery_identity(
            "groq", "a", "model", "https://api.groq.com/openai/v1/chat/completions"
        ),
    )
    transient = build_discovery_request(req, route, cfg, RULES, nonce="f" * 32)
    message = {} if content_state == "missing" else {"content": None}
    if content_state == "empty":
        message = {"content": ""}
    elif content_state == "other":
        message = {"content": ["not", "text"]}
    choice = {"message": message}
    if finish_state != "missing":
        choice["finish_reason"] = None if finish_state == "null" else "unsupported"
    body = {
        "object": "chat.completion",
        "id": "response-123",
        "usage": {"prompt_tokens": 2047, "completion_tokens": 100, "total_tokens": 2147},
        "choices": [choice],
    }
    response = Response(
        200,
        body=json.dumps(body),
        context_diagnostics={
            "response_media_type": "application/json",
            "response_bytes": len(json.dumps(body).encode()),
            "provider_request_id": "request-456",
            "response_preview": '"redacted short provider detail"',
        },
    )

    observed = RULES.discovery_observation(response, req, transient)

    assert observed.outcome == "inconclusive"
    assert observed.diagnostic == diagnostic
    assert observed.reported_input == 2047 and observed.reported_output == 100
    assert observed.usage_state == "valid"
    assert observed.message_content_state == content_state
    assert observed.finish_reason_state == finish_state
    assert observed.response_status == 200
    assert observed.response_media_type == "application/json"
    assert observed.response_bytes == len(json.dumps(body).encode())
    assert observed.provider_request_id == "request-456"
    assert observed.response_id == "response-123"
    assert observed.response_preview == '"redacted short provider detail"'


def _discovery_transport_worker(
    pipe, transient, session, timeout, retain_error_body, retain_response_preview=False
):
    """Spawn-safe IPC fixture; no credentials, URLs or provider network calls."""
    import json
    import re

    from citypods.provider_catalog.rules import Response

    try:
        pipe.recv()
        pipe.send("ready")
        if pipe.recv() != "new":
            return
        expected = dict(
            re.findall(
                r"(start|middle|tail)_sentinel=([a-f0-9]{32})", transient.messages[0]["content"]
            )
        )
        pipe.send(
            Response(
                200,
                body=json.dumps(
                    dict(
                        object="chat.completion",
                        usage=dict(prompt_tokens=2048, completion_tokens=100, total_tokens=2148),
                        choices=[
                            dict(message=dict(content=json.dumps(expected)), finish_reason="stop")
                        ],
                    )
                ),
            )
        )
    finally:
        pipe.close()
        session.close()


@pytest.mark.parametrize("receipt_kind", ["new", "wrong_identity", "wrong_type", "too_long"])
def test_transport_requires_distinct_matching_receipt(monkeypatch, receipt_kind):
    import time

    import requests

    from citypods.provider_catalog.discovery import DiscoveryAdmission
    from citypods.provider_catalog.probe import build_discovery_request, measure_discovery
    from citypods.provider_catalog.providers.groq import RULES

    monkeypatch.setattr(
        "citypods.provider_catalog.probe._context_http_worker", _discovery_transport_worker
    )
    req = request()
    cfg = dict(
        api_base="https://api.groq.com/openai/v1", accounts=[dict(id="a", api_key_env="TEST_ONLY")]
    )
    route = dict(route_id="candidate", provider="groq", account_id="a", upstream_model="model")
    req = replace(
        req,
        identity=discovery_identity(
            "groq", "a", "model", "https://api.groq.com/openai/v1/chat/completions"
        ),
    )
    transient = build_discovery_request(req, route, cfg, RULES, nonce="e" * 32)
    receipt = DiscoveryAdmission(
        req.session_id,
        req.attempt_id,
        req.identity,
        req.input_tokens,
        req.output_tokens,
        time.monotonic() + 700,
    )
    if receipt_kind == "wrong_identity":
        receipt = replace(receipt, identity="different")
    if receipt_kind == "wrong_type":
        receipt = object()
    timeout = 601 if receipt_kind == "too_long" else 600 if receipt_kind == "new" else 5

    def measure():
        return measure_discovery(
            req,
            transient,
            RULES,
            session=requests.Session(),
            before_call=lambda: receipt,
            clock=time.monotonic,
            timeout=timeout,
        )

    if receipt_kind == "new":
        assert measure().coordinate == 2048
    elif receipt_kind == "too_long":
        with pytest.raises(ValueError, match="response timeout exceeds reviewed deadline"):
            measure()
    else:
        with pytest.raises(ValueError, match="admission"):
            measure()


def test_envelopes_cannot_be_raised_by_caller(tmp_path):
    with pytest.raises(ValueError, match="reviewed ceiling"):
        execute(
            tmp_path,
            lambda *args: pytest.fail("no transport"),
            budget=DiscoveryBudget(max_calls=65),
        )


def test_pause_renewal_crossing_deadline_does_not_release_transport(tmp_path):
    now = [0.0]

    def renew():
        now[0] += 10

    def measure(req, admit, timeout):
        admit()
        pytest.fail("expired admission must not release transport")

    result = run_discovery(
        session_id="s",
        identity="physical",
        parser_version="v",
        supported_axes=("input",),
        measure=measure,
        renew=renew,
        clock=lambda: now[0],
        deadline=15,
        journal=DiscoveryJournal(tmp_path / "deadline.json", {}),
    )
    assert result.budget["calls"] == 0


def test_input_only_refusal_cannot_bound_combined_capacity():
    bracket = Bracket("total_input_heavy", 6000)
    req = replace(request(), axis="total_input_heavy", control="total_input_heavy:v1")
    item = replace(observation(req, 5000), count_basis="prompt_plus_requested_output")
    bracket.observe(item)
    refused_input = replace(observation(req, 5200, False), count_basis="input")
    bracket.observe(refused_input)
    assert bracket.lower == 5000 and bracket.upper is None
    assert bracket.reason == "conditional_or_unverified_count_basis"


def test_output_fixture_ceiling_and_reasoning_are_endpoint_specific():
    import json

    from citypods.provider_catalog.probe import build_discovery_request
    from citypods.provider_catalog.providers.groq import RULES as GROQ
    from citypods.provider_catalog.providers.openrouter import RULES
    from citypods.provider_catalog.rules import Response

    cfg = dict(
        api_base="https://openrouter.ai/api/v1", accounts=[dict(id="a", api_key_env="TEST_ONLY")]
    )
    route = dict(route_id="candidate", provider="openrouter", account_id="a", upstream_model="m")
    req = replace(
        request("output"),
        output_tokens=131072,
        identity=discovery_identity(
            "openrouter", "a", "m", "https://openrouter.ai/api/v1/chat/completions"
        ),
    )
    transient = build_discovery_request(req, route, cfg, RULES, nonce="a" * 32)
    assert transient.shaped_body["body"]["max_tokens"] == 131072
    body = dict(
        object="chat.completion",
        usage=dict(
            prompt_tokens=25,
            completion_tokens=10,
            total_tokens=35,
            completion_tokens_details=dict(reasoning_tokens=2),
            prompt_tokens_details=dict(cached_tokens=0),
        ),
        choices=[dict(message=dict(content="1\n1\n1\n1"), finish_reason="length")],
    )
    response = Response(200, body=json.dumps(body))
    result = RULES.discovery_observation(response, req, transient)
    assert result.outcome == "admitted" and result.reasoning_tokens == 2
    assert result.count_basis == "completion_including_reasoning"
    assert GROQ.discovery_observation(response, req, transient).outcome == "inconclusive"
    body["usage"] = []
    assert (
        RULES.discovery_observation(Response(200, body=json.dumps(body)), req, transient).outcome
        == "inconclusive"
    )


def test_invalid_history_references_fail_closed_without_network():
    from citypods.provider_catalog.evidence import verified_discovery_history

    for rows in (["forged"], [dict(run_id="9" * 5000)], [dict(run_id=True)]):
        observations, accepted, deferred = verified_discovery_history(
            rows,
            repository="owner/repo",
            now=datetime.now(UTC),
            api=lambda _: pytest.fail("malformed identity must not make an API call"),
        )
        assert not observations and not accepted and deferred


@pytest.mark.parametrize("current_limit", [4005, 9000])
def test_resumed_boundaries_are_merged_once_and_fresh_evidence_wins(tmp_path, current_limit):
    history = [observation(request(), 4000), observation(request(attempt="b"), 4500, False)]
    seen = []

    def measure(req, admit, timeout):
        admit()
        seen.append(req.input_tokens)
        # An altered tokenizer makes the old lower-bound shape count 4600 tokens.
        count = req.input_tokens + (600 if current_limit == 9000 else 0)
        return observation(req, count, count <= current_limit, reported_input=count)

    result = run_discovery(
        session_id="session",
        identity="physical",
        parser_version="parser-v1",
        supported_axes=("input",),
        measure=measure,
        renew=lambda: None,
        history=history,
        journal=DiscoveryJournal(tmp_path / "resume.json", {}),
    )
    assert result.brackets[0]["status"] == "converged"
    assert result.brackets[0]["admitted"] <= current_limit < result.brackets[0]["refused"]
    if current_limit == 9000:
        assert result.brackets[0]["admitted"] > 4500
    # A wide old bracket must not stall after only the two baseline successes.
    assert len(seen) > 3


def test_fixed_counterpart_failure_reduces_it_without_creating_input_upper(tmp_path):
    seen = []

    def measure(req, admit, timeout):
        admit()
        seen.append((req.input_tokens, req.output_tokens, req.control))
        if req.output_tokens > INPUT_OUTPUT_COUNTERPART // 2:
            return replace(
                observation(req, req.output_tokens, False), count_basis="requested_output"
            )
        return observation(
            req, req.input_tokens, req.input_tokens <= 4000, reported_input=req.input_tokens
        )

    result = run_discovery(
        session_id="session",
        identity="physical",
        parser_version="parser-v1",
        supported_axes=("input",),
        measure=measure,
        renew=lambda: None,
        journal=DiscoveryJournal(tmp_path / "counterpart.json", {}),
    )
    assert seen[0][1] == INPUT_OUTPUT_COUNTERPART
    assert seen[1][1] == INPUT_OUTPUT_COUNTERPART // 2
    assert seen[0][2] != seen[1][2]
    assert result.brackets[0]["status"] == "converged"
    assert result.brackets[0]["admitted"] <= 4000 < result.brackets[0]["refused"]
    assert result.brackets[0]["refused"] != 256


def test_local_restart_does_not_restart_wall_time_budget(tmp_path, monkeypatch):
    from datetime import timedelta

    import citypods.provider_catalog.discovery as discovery

    journal = DiscoveryJournal(tmp_path / "expired.json", {})
    start = datetime.fromisoformat(journal.data["started_at"])

    class LaterDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return start + timedelta(seconds=1201)

    monkeypatch.setattr(discovery, "datetime", LaterDateTime)
    result = run_discovery(
        session_id="s",
        identity="physical",
        parser_version="v",
        supported_axes=("input",),
        measure=lambda *args: pytest.fail("expired session cannot submit"),
        renew=lambda: None,
        journal=journal,
    )
    assert result.budget["calls"] == 0
    assert result.reason == "deadline_or_reported_usage_overrun"


def test_newest_first_history_cannot_restore_an_old_refusal(tmp_path):
    from datetime import timedelta

    stamp = datetime.now(UTC)
    oldest = replace(
        observation(request(attempt="old"), 4500, False),
        observed_at=(stamp - timedelta(days=2)).isoformat(),
    )
    admitted = replace(
        observation(request(attempt="new-admit"), 5000),
        observed_at=(stamp - timedelta(days=1)).isoformat(),
    )
    refused = replace(
        observation(request(attempt="new-refuse"), 5100, False), observed_at=stamp.isoformat()
    )

    def measure(req, admit, timeout):
        admit()
        return observation(
            req, req.input_tokens, req.input_tokens <= 5050, reported_input=req.input_tokens
        )

    result = run_discovery(
        session_id="session",
        identity="physical",
        parser_version="parser-v1",
        supported_axes=("input",),
        measure=measure,
        renew=lambda: None,
        history=(refused, admitted, oldest),
        journal=DiscoveryJournal(tmp_path / "ordered-history.json", {}),
    )
    bracket = result.brackets[0]
    assert bracket["status"] == "converged"
    assert bracket["admitted"] <= 5050 < bracket["refused"]


@pytest.mark.parametrize("field", ["identity", "session_id", "observation"])
def test_checkpoint_foreign_evidence_rejected_before_any_charge(tmp_path, field):
    journal = DiscoveryJournal(tmp_path / "foreign.json", {})
    req = request()
    journal.charge(req)
    journal.complete(observation(req, 2000))
    if field == "observation":
        journal.data["attempts"][0]["observation"]["request"]["identity"] = "foreign"
    else:
        journal.data["attempts"][0]["request"][field] = "foreign"
    budget, shared = DiscoveryBudget(), DiscoveryBudget(max_calls=128)
    with pytest.raises(ValueError, match="identity mismatch"):
        run_discovery(
            session_id="session",
            identity="physical",
            parser_version="parser-v1",
            supported_axes=AXES,
            measure=lambda *args: pytest.fail("no transport"),
            renew=lambda: None,
            journal=journal,
            budget=budget,
            shared_budget=shared,
        )
    assert budget.calls == shared.calls == 0


def _chat_discovery_fixture(provider, axis="input"):
    from citypods.provider_catalog.probe import build_discovery_request
    from citypods.provider_catalog.registry import all_rules

    rules = all_rules()[provider]
    cfg = dict(api_base="https://provider.test/v1", accounts=[dict(id="a", api_key_env="KEY")])
    route = dict(
        route_id="candidate", provider=provider, account_id="a", upstream_model="org/model"
    )
    req = replace(
        request(axis),
        control="output:v3" if axis == "output" else request(axis).control,
        identity=discovery_identity(
            provider, "a", "org/model", cfg["api_base"] + "/chat/completions"
        ),
        parser_version=rules.discovery_parser_version,
    )
    transient = build_discovery_request(req, route, cfg, rules, nonce="f" * 32)
    return rules, req, transient


@pytest.mark.parametrize("provider", ["groq", "openrouter", "nvidia"])
@pytest.mark.parametrize("nested", [True, False])
@pytest.mark.parametrize("shape", ["combined", "classic", "input_only"])
def test_shared_chat_size_refusals_use_exact_provider_input_counts(provider, nested, shape):
    import json

    from citypods.provider_catalog.rules import Response

    rules, req, transient = _chat_discovery_fixture(provider)
    prefix = "This model's maximum context length is 16384 tokens. However, "
    messages = {
        "combined": prefix + f"you requested {req.output_tokens} output tokens and your prompt "
        f"contains 20000 input tokens, for a total of {20000 + req.output_tokens} tokens.",
        "classic": prefix + f"you requested {20000 + req.output_tokens} tokens "
        f"(20000 in the messages, {req.output_tokens} in the completion).",
        "input_only": prefix + "your request has 20000 input tokens.",
    }
    error = dict(type="BadRequestError", code=400, message=messages[shape])
    body = {"error": error} if nested else error
    observed = rules.discovery_observation(Response(400, body=json.dumps(body)), req, transient)
    assert observed.outcome == "refused" and observed.verified
    assert observed.coordinate == observed.reported_input == 20000
    assert observed.count_basis == "input"
    bracket = Bracket("input", 500000)
    bracket.observe(observed)
    assert bracket.upper == 20000 and bracket.target() == 10000


@pytest.mark.parametrize("provider", ["openrouter", "nvidia"])
@pytest.mark.parametrize("axis", ["input", "output"])
def test_shared_chat_success_uses_usage_counts_and_correct_reasoning_basis(provider, axis):
    import json
    import re

    from citypods.provider_catalog.rules import Response

    rules, req, transient = _chat_discovery_fixture(provider, axis)
    sentinels = dict(
        re.findall(r"(start|middle|tail)_sentinel=([a-f0-9]{32})", transient.messages[0]["content"])
    )
    content = json.dumps(sentinels) if axis == "input" else "1\n1\n1"
    body = {
        "object": "chat.completion",
        "usage": {"prompt_tokens": 1900, "completion_tokens": 64, "total_tokens": 1964},
        "choices": [
            {
                "message": {"content": content},
                "finish_reason": "stop" if axis == "input" else "length",
            }
        ],
    }
    observed = rules.discovery_observation(Response(200, body=json.dumps(body)), req, transient)
    assert observed.outcome == "admitted" and observed.verified
    assert observed.coordinate == (1900 if axis == "input" else 64)
    assert observed.count_basis == (
        "input" if axis == "input" else "completion_including_reasoning"
    )


@pytest.mark.parametrize("provider", ["openrouter", "nvidia"])
@pytest.mark.parametrize("field", ["max_tokens", "max_completion_tokens"])
def test_shared_chat_typed_output_validation_proves_refused_reservation(provider, field):
    import json

    from citypods.provider_catalog.rules import Response

    rules, req, transient = _chat_discovery_fixture(provider, "output")
    body = {
        "detail": [
            {
                "type": "less_than_equal",
                "loc": ["body", field],
                "input": req.output_tokens,
                "ctx": {"le": req.output_tokens - 1},
            }
        ]
    }
    observed = rules.discovery_observation(Response(422, body=json.dumps(body)), req, transient)
    assert observed.outcome == "refused" and observed.coordinate == req.output_tokens
    assert observed.reported_output is None  # Rejected reservations did not generate these tokens.
    assert observed.count_basis == "completion_including_reasoning"


@pytest.mark.parametrize(
    "bad",
    [
        "quota",
        "characters",
        "at_least",
        "wrong_output",
        "wrong_total",
        "huge_integer",
        "array_type",
        "non_overflow",
        "wrong_axis",
    ],
)
def test_shared_chat_size_parser_rejects_ambiguous_and_conflicting_authority(bad):
    import json

    from citypods.provider_catalog.rules import Response

    rules, req, transient = _chat_discovery_fixture("nvidia")
    prefix = "This model's maximum context length is 16384 tokens. However, "
    text = prefix + f"you requested {req.output_tokens} output tokens and your prompt "
    text += f"contains 20000 input tokens, for a total of {20000 + req.output_tokens} tokens."
    error = dict(type="BadRequestError", code=400, message=text)
    status = 400
    if bad == "quota":
        status = 429
    elif bad == "characters":
        error["message"] = text.replace("20000 input tokens", "20000 characters")
    elif bad == "at_least":
        error["message"] = text.replace("20000 input", "at least 20000 input")
    elif bad == "wrong_output":
        error["message"] = text.replace(f"{req.output_tokens} output", "1 output")
    elif bad == "wrong_total":
        error["message"] = text.replace(f"{20000 + req.output_tokens} tokens.", "20001 tokens.")
    elif bad == "huge_integer":
        error["message"] = text.replace("20000 input", "9" * 5000 + " input")
    elif bad == "array_type":
        error["type"] = []
    elif bad == "non_overflow":
        error["message"] = text.replace("16384 tokens.", "100000 tokens.")
    elif bad == "wrong_axis":
        req = replace(req, axis="output")
    observed = rules.discovery_observation(
        Response(status, body=json.dumps({"error": error})), req, transient
    )
    assert observed.coordinate is None and not observed.verified
    assert observed.outcome == ("quota" if bad == "quota" else "inconclusive")


def test_discovery_sse_normalizer_preserves_unicode_inside_reasoning_json():
    import json

    from citypods.provider_catalog.probe import _context_envelope

    frames = [
        {"choices": [{"delta": {"reasoning_content": "ⅅ\u2028\u2029"}}]},
        {
            "choices": [{"delta": {"content": '{"answer":"ok"}'}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        },
    ]
    raw = "".join("data: " + json.dumps(frame, ensure_ascii=False) + "\n\n" for frame in frames)
    normalized = json.loads(_context_envelope(raw + "data: [DONE]\n\n", "text/event-stream"))
    assert normalized["choices"][0]["message"]["content"] == '{"answer":"ok"}'
    assert normalized["usage"]["total_tokens"] == 30


@pytest.mark.parametrize(
    "bad",
    [
        "wrong_field",
        "wrong_value",
        "boolean_limit",
        "not_exceeded",
        "mixed_errors",
        "unknown_reasoning",
    ],
)
def test_typed_output_refusals_require_unambiguous_matching_field_authority(bad):
    import json

    from citypods.provider_catalog.rules import Response, chat_discovery_observation

    _, req, transient = _chat_discovery_fixture("nvidia", "output")
    issue = {
        "type": "less_than_equal",
        "loc": ["body", "max_tokens"],
        "input": req.output_tokens,
        "ctx": {"le": req.output_tokens - 1},
    }
    body = {"detail": [issue]}
    if bad == "wrong_field":
        issue["loc"] = ["body", "rpm"]
    elif bad == "wrong_value":
        issue["input"] -= 1
    elif bad == "boolean_limit":
        issue["ctx"]["le"] = True
    elif bad == "not_exceeded":
        issue["ctx"]["le"] = req.output_tokens
    elif bad == "mixed_errors":
        body["error"] = {"code": "rate_limit_exceeded"}
    observed = chat_discovery_observation(
        Response(422, body=json.dumps(body)),
        req,
        transient,
        reasoning_basis="unknown" if bad == "unknown_reasoning" else "included",
    )
    assert observed.outcome == "inconclusive" and observed.coordinate is None


@pytest.mark.parametrize("provider", ["groq", "openrouter", "nvidia"])
@pytest.mark.parametrize("typed", [True, False])
@pytest.mark.parametrize("nested", [True, False])
@pytest.mark.parametrize("suffix", ["", " Please reduce the length of the messages."])
def test_recorded_messages_resulted_token_error_supplies_comparable_input_refusal(
    provider, typed, nested, suffix
):
    import json

    from citypods.provider_catalog.rules import Response

    rules, req, transient = _chat_discovery_fixture(provider)
    # Redacted exact prefix from run 38296813993, without guessing the truncated suffix.
    error = {
        "message": "This model's maximum context length is 1000000 tokens. "
        "However, your messages resulted in 1290360 tokens."
    }
    if typed:
        error.update(type="BadRequestError", code=400)
    error["message"] += suffix
    body = {"error": error} if nested else error
    observed = rules.discovery_observation(Response(400, body=json.dumps(body)), req, transient)
    assert observed.outcome == "refused" and observed.coordinate == 1290360
    assert observed.reported_input == 1290360 and observed.count_basis == "input"
    assert observed.reported_output is None


@pytest.mark.parametrize(
    "bad",
    [
        "characters",
        "at_least",
        "below_limit",
        "wrong_axis",
        "quota",
        "extra_fields",
        "untyped_other_wording",
        "approximate_suffix",
        "outer_conflict",
    ],
)
def test_message_only_size_refusal_is_narrow_and_fail_closed(bad):
    import json

    from citypods.provider_catalog.rules import Response

    rules, req, transient = _chat_discovery_fixture("nvidia")
    text = "This model's maximum context length is 1000000 tokens. "
    text += "However, your messages resulted in 1290360 tokens."
    error = {"message": text}
    status = 400
    if bad == "characters":
        error["message"] = text.replace("1290360 tokens", "1290360 characters")
    elif bad == "at_least":
        error["message"] = text.replace("in 1290360", "in at least 1290360")
    elif bad == "below_limit":
        error["message"] = text.replace("1290360", "900000")
    elif bad == "wrong_axis":
        req = replace(req, axis="output")
    elif bad == "quota":
        status = 429
    elif bad == "extra_fields":
        error["rpd"] = 1000
    elif bad == "untyped_other_wording":
        error["message"] = text.replace("your messages resulted in", "your request has").replace(
            "1290360 tokens", "1290360 input tokens"
        )
    elif bad == "approximate_suffix":
        error["message"] += " This count is approximate."
    elif bad == "outer_conflict":
        error = {"error": error, "quota_exhausted": True}
    observed = rules.discovery_observation(Response(status, body=json.dumps(error)), req, transient)
    assert observed.coordinate is None and not observed.verified


def test_count_mapping_is_retained_at_ceiling_and_refines_actual_input_boundary(tmp_path):
    calls = []
    actual_limit = 980000

    def measure(req, admit, _timeout):
        admit()
        calls.append(req)
        actual = (req.input_tokens * 129) // 100
        return observation(req, actual, actual <= actual_limit, reported_input=actual)

    result = execute(tmp_path, measure, supported_axes=("input",))
    ceiling_calls = [req for req in calls if req.target == MAX_INPUT]
    assert ceiling_calls and all(req.input_tokens < MAX_INPUT for req in ceiling_calls)
    assert result.brackets[0]["status"] == "converged"
    assert result.brackets[0]["admitted"] <= actual_limit < result.brackets[0]["refused"]
    assert all(req.input_tokens <= MAX_INPUT for req in calls)
    assert len(calls) < 20


@pytest.mark.parametrize("provider", ["nvidia", "openrouter"])
def test_discovery_output_fixture_has_concrete_long_goal_and_preserves_request_bound(provider):
    _, req, transient = _chat_discovery_fixture(provider, "output")
    assert transient.messages[0]["role"] == "system"
    assert "partial prefix is the intended result" in transient.messages[0]["content"]
    text = transient.messages[1]["content"]
    assert "131072 times" in text
    assert "server truncates output" in text
    assert text.index("</padding>") < text.index("Repeat the line")
    assert "1000000" not in text
    assert transient.shaped_body["body"]["max_tokens"] == req.output_tokens
    assert transient.local_input_estimate <= req.input_tokens


def test_changed_output_control_excludes_old_prompt_history(tmp_path):
    old = replace(request("output"), control="output:v2")
    previous = observation(old, 16384)
    seen = []

    def measure(req, admit, _timeout):
        admit()
        seen.append(req)
        return DiscoveryObservation(req, STAMP)

    result = execute(tmp_path, measure, supported_axes=("output",), history=(previous,))
    assert all(req.control == "output:v3" for req in seen)
    assert result.brackets[1]["admitted"] is None


@pytest.mark.parametrize("provider", ["groq", "openrouter", "nvidia"])
@pytest.mark.parametrize("accurate", [True, False])
def test_api_input_admission_is_separate_from_sentinel_accuracy(provider, accurate):
    import json
    import re

    from citypods.provider_catalog.rules import Response

    rules, req, transient = _chat_discovery_fixture(provider)
    expected = dict(
        re.findall(r"(start|middle|tail)_sentinel=([a-f0-9]{32})", transient.messages[0]["content"])
    )
    content = json.dumps(expected if accurate else {"start": "wrong", "middle": None})
    body = {
        "object": "chat.completion",
        "usage": {"prompt_tokens": 1900, "completion_tokens": 64, "total_tokens": 1964},
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
    }
    observed = rules.discovery_observation(Response(200, body=json.dumps(body)), req, transient)
    assert observed.outcome == "admitted" and observed.coordinate == 1900
    assert observed.diagnostic == "api_input_admitted"
    assert observed.sentinel_verification == ("passed" if accurate else "failed")
    assert observed.verified and observed.count_basis == "input"


@pytest.mark.parametrize(
    "bad",
    [
        "missing_usage",
        "missing_total",
        "inconsistent_total",
        "boolean_count",
        "wrong_object",
        "refusal",
        "tool_call",
        "missing_content",
        "empty_content",
        "invalid_reasoning",
        "array_finish",
        "transport",
    ],
)
def test_api_input_capacity_stays_inconclusive_without_complete_consistent_evidence(bad):
    import json

    from citypods.provider_catalog.rules import Response

    rules, req, transient = _chat_discovery_fixture("nvidia")
    body = {
        "object": "chat.completion",
        "usage": {"prompt_tokens": 1900, "completion_tokens": 64, "total_tokens": 1964},
        "choices": [{"message": {"content": "wrong sentinels"}, "finish_reason": "stop"}],
    }
    choice = body["choices"][0]
    if bad == "missing_usage":
        del body["usage"]
    elif bad == "missing_total":
        del body["usage"]["total_tokens"]
    elif bad == "inconsistent_total":
        body["usage"]["total_tokens"] = 1963
    elif bad == "boolean_count":
        body["usage"]["prompt_tokens"] = True
    elif bad == "wrong_object":
        body["object"] = "other"
    elif bad == "refusal":
        choice["message"]["refusal"] = "refused"
    elif bad == "tool_call":
        choice["message"]["tool_calls"] = ["tool"]
    elif bad == "missing_content":
        del choice["message"]["content"]
    elif bad == "empty_content":
        choice["message"]["content"] = ""
    elif bad == "invalid_reasoning":
        body["usage"]["completion_tokens_details"] = {"reasoning_tokens": 65}
    elif bad == "array_finish":
        choice["finish_reason"] = []
    observed = rules.discovery_observation(
        Response(200, body=json.dumps(body), timed_out=bad == "transport"), req, transient
    )
    assert observed.outcome != "admitted" and observed.coordinate is None


def test_output_only_session_resumes_output_history_without_input_probes(tmp_path):
    previous_req = replace(
        request("output"), output_tokens=24576, target=24576, control="output:v3"
    )
    previous = observation(previous_req, 24576)
    requests = []

    def measure(req, admit, _timeout):
        admit()
        requests.append(req)
        return observation(req, req.output_tokens, req.output_tokens <= 40000)

    result = execute(
        tmp_path,
        measure,
        supported_axes=("input", "output"),
        selected_axes=("output",),
        history=(previous,),
    )
    assert requests and all(req.axis == "output" for req in requests)
    assert [req.output_tokens for req in requests[:2]] == [24576, 24576]
    assert result.brackets[0]["reason"] == "not_selected"
    assert result.brackets[1]["status"] == "converged"


def test_sentinel_metadata_is_backward_compatible_and_rejects_untrusted_types():
    from dataclasses import asdict

    row = asdict(DiscoveryObservation(request(), STAMP))
    del row["sentinel_verification"]
    assert DiscoveryObservation.from_dict(row).sentinel_verification == "unknown"
    row["sentinel_verification"] = ["passed"]
    with pytest.raises(ValueError, match="sentinel verification"):
        DiscoveryObservation.from_dict(row)


def test_output_selection_cannot_enable_an_unsupported_provider_axis(tmp_path):
    def measure(*args):
        raise AssertionError("unsupported output must never send a size request")

    result = execute(tmp_path, measure, supported_axes=("input",), selected_axes=("output",))
    assert result.brackets[0]["reason"] == "not_selected"
    assert result.brackets[1]["reason"] == "unsupported_endpoint_axis"


def test_issue_reports_sentinel_failure_separately_from_admitted_api_capacity():
    from citypods.provider_catalog.issue import _discovery_attempt_diagnostics
    from citypods.provider_catalog.reconcile import _discovery_attempt_diagnostics as collect

    req = request()
    observed = replace(
        observation(req, 1900), diagnostic="api_input_admitted", sentinel_verification="failed"
    )
    row = _discovery_attempt_diagnostics(collect([observed]))[0]
    assert row["outcome"] == "admitted"
    assert row["sentinel_verification"] == "failed"
    row["sentinel_verification"] = {"passed": True}
    assert _discovery_attempt_diagnostics([row])[0]["sentinel_verification"] == "unknown"


@pytest.mark.parametrize("provider", ["groq", "openrouter", "nvidia"])
def test_discovery_natural_output_stop_is_explicitly_inconclusive(provider):
    import json

    from citypods.provider_catalog.rules import Response

    rules, req, transient = _chat_discovery_fixture(provider, "output")
    payload = {
        "object": "chat.completion",
        "usage": {
            "prompt_tokens": 2390,
            "completion_tokens": 4241,
            "total_tokens": 6631,
            "completion_tokens_details": {"reasoning_tokens": 3548},
        },
        "choices": [{"finish_reason": "stop", "message": {"content": "1\n2\n3\n4"}}],
    }
    result = rules.discovery_observation(Response(200, body=json.dumps(payload)), req, transient)
    assert result.diagnostic == "output_stopped_early"
    assert result.outcome == "inconclusive"
    assert result.coordinate is None and not result.verified
    assert result.reported_output == 4241


def test_discovery_failed_output_preview_includes_redacted_ending():
    import json

    from citypods.provider_catalog.probe import _discovery_response_excerpt

    _, _, transient = _chat_discovery_fixture("nvidia", "output")
    secret = "provider-secret-value"
    text = "1\n2\n3\n" + "123\n" * 1000 + "Stopping at a reasonable point. " + secret
    raw = json.dumps({"choices": [{"message": {"content": text}}]}).encode()
    preview = _discovery_response_excerpt(raw, transient, {"Authorization": "Bearer " + secret})
    decoded = json.loads(preview)
    assert decoded.startswith("1\n2\n3")
    assert "[tail]" in decoded and "reasonable point" in decoded
    assert secret not in preview
    assert len(decoded) <= 128


@pytest.mark.parametrize(
    "endpoint,model,expected",
    [
        (
            "https://integrate.api.nvidia.com/v1/chat/completions",
            "nvidia/nemotron-3.5-lightning-30b-a3b",
            True,
        ),
        (
            "https://integrate.api.nvidia.com/v1/chat/completions",
            "nvidia/nemotron-3-super-120b-a12b",
            False,
        ),
        ("https://other.test/v1/chat/completions", "nvidia/nemotron-3.5-lightning-30b-a3b", False),
    ],
)
def test_minimum_generation_is_an_exact_served_route_capability(endpoint, model, expected):
    from citypods.provider_catalog.registry import rules_for

    assert rules_for("nvidia").discovery_output_min_tokens(endpoint, model) is expected
    assert not rules_for("openrouter").discovery_output_min_tokens(endpoint, model)


def minimum_generation_fixture(axis="output", minimum=True):
    from citypods.provider_catalog.probe import build_discovery_request
    from citypods.provider_catalog.registry import rules_for

    rules = rules_for("nvidia")
    cfg = dict(
        api_base="https://integrate.api.nvidia.com/v1",
        accounts=[dict(id="a", api_key_env="KEY")],
    )
    route = dict(
        route_id="candidate",
        provider="nvidia",
        account_id="a",
        upstream_model="nvidia/nemotron-3.5-lightning-30b-a3b",
    )
    req = replace(
        request(axis),
        identity=discovery_identity(
            "nvidia", "a", route["upstream_model"], cfg["api_base"] + "/chat/completions"
        ),
        control="output:v3:min_tokens" if minimum else "output:v3",
    )
    transient = build_discovery_request(req, route, cfg, rules, nonce="f" * 32)
    return req, transient


def test_minimum_generation_is_bounded_and_never_added_to_input_probes():
    req, transient = minimum_generation_fixture()
    body = transient.shaped_body["body"]
    assert body["min_tokens"] == body["max_tokens"] == req.output_tokens
    assert transient.local_input_estimate <= req.input_tokens
    _, transient = minimum_generation_fixture("input")
    assert "min_tokens" not in transient.shaped_body["body"]
    with pytest.raises(ValueError, match="generation control mismatch"):
        minimum_generation_fixture(minimum=False)


def test_unsupported_route_rejects_a_forged_minimum_control():
    from citypods.provider_catalog.probe import build_discovery_request
    from citypods.provider_catalog.registry import rules_for

    rules, req, _ = _chat_discovery_fixture("openrouter", "output")
    req = replace(req, control="output:v3:min_tokens")
    cfg = dict(api_base="https://provider.test/v1", accounts=[dict(id="a", api_key_env="KEY")])
    route = dict(provider="openrouter", account_id="a", upstream_model="org/model", route_id="c")
    with pytest.raises(ValueError, match="generation control mismatch"):
        build_discovery_request(req, route, cfg, rules, nonce="f" * 32)
    assert not rules_for("groq").discovery_output_min_tokens("any", "any")


def test_minimum_generation_history_is_fenced_from_prompt_only_results(tmp_path):
    previous = observation(request("output"), 16384)
    seen = []

    def measure(req, admit, _timeout):
        admit()
        seen.append(req)
        return DiscoveryObservation(req, STAMP)

    result = execute(
        tmp_path,
        measure,
        supported_axes=("output",),
        output_min_tokens=True,
        history=(previous,),
    )
    assert all(req.control == "output:v3:min_tokens" for req in seen)
    assert result.brackets[1]["admitted"] is None
    with pytest.raises(ValueError, match="minimum output capability"):
        execute(tmp_path, measure, output_min_tokens="yes")


def test_reduced_output_input_control_preserves_minimum_generation_flag(tmp_path):
    seen = []

    def measure(req, admit, _timeout):
        admit()
        seen.append(req)
        if len(seen) == 1:
            return DiscoveryObservation(
                req,
                STAMP,
                outcome="refused",
                coordinate=req.input_tokens,
                count_basis="input",
                verified=True,
            )
        return DiscoveryObservation(req, STAMP)

    execute(tmp_path, measure, supported_axes=("output",), output_min_tokens=True)
    assert seen[0].control == "output:v3:min_tokens"
    assert all(req.control == "output:v3:min_tokens:opposite=1024" for req in seen[1:])


def test_minimum_parameter_rejection_is_not_an_output_boundary():
    import json

    from citypods.provider_catalog.registry import rules_for
    from citypods.provider_catalog.rules import Response

    req, transient = minimum_generation_fixture()
    response = Response(
        400,
        body=json.dumps({"error": {"message": "Unsupported parameter: min_tokens", "code": 400}}),
    )
    result = rules_for("nvidia").discovery_observation(response, req, transient)
    assert result.outcome == "inconclusive"
    assert result.coordinate is None and not result.verified


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "ascending",
        "abbreviated",
        "prose",
        "empty",
        "reasoning_only",
        "early_stop",
        "tool_call",
        "total_missing",
        "total_invalid",
        "negative_output",
    ],
)
def test_repeated_output_capacity_requires_real_usage_length_and_exact_text(bad):
    import json

    from citypods.provider_catalog.registry import rules_for
    from citypods.provider_catalog.rules import Response

    req, transient = minimum_generation_fixture()
    usage = dict(prompt_tokens=2390, completion_tokens=24576, total_tokens=26966)
    usage["completion_tokens_details"] = dict(reasoning_tokens=5273)
    message = dict(content="1\n" * 100)
    choice = dict(message=message, finish_reason="length")
    if bad == "ascending":
        message["content"] = "1\n2\n3"
    elif bad == "abbreviated":
        message["content"] = "1\n1\n..."
    elif bad == "prose":
        message["content"] += "Done"
    elif bad == "empty":
        message["content"] = ""
    elif bad == "reasoning_only":
        usage["completion_tokens_details"]["reasoning_tokens"] = usage["completion_tokens"]
    elif bad == "early_stop":
        choice["finish_reason"] = "stop"
    elif bad == "tool_call":
        message["tool_calls"] = [dict(id="call")]
    elif bad == "total_missing":
        del usage["total_tokens"]
    elif bad == "total_invalid":
        usage["total_tokens"] += 1
    elif bad == "negative_output":
        usage["completion_tokens"] = -1
    body = dict(object="chat.completion", usage=usage, choices=[choice])
    observed = rules_for("nvidia").discovery_observation(
        Response(200, body=json.dumps(body)), req, transient
    )
    if bad is None:
        assert observed.outcome == "admitted" and observed.verified
        assert observed.coordinate == 24576
        assert observed.diagnostic == "verified_repeated_output_fixture"
    else:
        assert observed.outcome == "inconclusive" and observed.coordinate is None


def test_checkpoint_charges_remain_but_old_output_fixture_is_not_boundary_proof(tmp_path):
    journal = DiscoveryJournal(tmp_path / "old-output.json", {"identity": "physical"})
    old = replace(request("output"), control="output:v2")
    journal.charge(old)
    journal.complete(observation(old, 1000))
    seen = []

    def measure(req, admit, _timeout):
        admit()
        seen.append(req)
        return DiscoveryObservation(req, STAMP)

    result = run_discovery(
        session_id="session",
        identity="physical",
        parser_version="parser-v1",
        supported_axes=("output",),
        output_min_tokens=True,
        measure=measure,
        renew=lambda: None,
        journal=journal,
    )
    assert all(req.target == DEFAULT_OUTPUT_SEED for req in seen)
    assert result.brackets[1]["admitted"] is None
    assert result.budget["calls"] == 1 + len(seen)


def stream_fixture(content="1\n1\n1", usage=None):
    import json

    usage = usage or dict(prompt_tokens=2048, completion_tokens=4096, total_tokens=6144)
    frames = [
        dict(object="chat.completion.chunk", choices=[dict(index=0, delta=dict(content=content))]),
        dict(object="chat.completion.chunk", choices=[dict(delta={}, finish_reason="length")]),
        dict(object="chat.completion.chunk", choices=[], usage=usage),
    ]
    return (
        "".join("data: " + json.dumps(f, ensure_ascii=False) + "\r\n\r\n" for f in frames)
        + "data: [DONE]\r\n\r\n"
    ).encode()


@pytest.mark.parametrize("width", [1, 3, 16384])
def test_incremental_stream_handles_split_utf8_and_crlf(width):
    import json

    from citypods.provider_catalog.probe import DiscoverySSECollector

    raw = stream_fixture("1\n😀\u2028\n1")
    stream = DiscoverySSECollector()
    for offset in range(0, len(raw), width):
        stream.feed(raw[offset : offset + width])
    body = json.loads(stream.complete())
    assert body["choices"][0]["message"]["content"] == "1\n😀\u2028\n1"
    assert body["usage"]["completion_tokens"] == 4096
    assert stream.wire_bytes == len(raw)


@pytest.mark.parametrize(
    "bad",
    [
        "missing_done",
        "unfinished",
        "duplicate_usage",
        "post_done",
        "error",
        "bad_object",
        "conflicting_finish",
    ],
)
def test_incremental_stream_rejects_incomplete_or_conflicting_evidence(bad):
    from citypods.provider_catalog.probe import DiscoverySSECollector

    raw = stream_fixture()
    if bad == "missing_done":
        raw = raw.replace(b"data: [DONE]\r\n\r\n", b"")
    elif bad == "unfinished":
        raw = raw[:-1]
    elif bad == "duplicate_usage":
        frame = b'data: {"object":"chat.completion.chunk","choices":[],"usage":{}}\r\n\r\n'
        raw = raw.replace(b"data: [DONE]", frame + b"data: [DONE]")
    elif bad == "post_done":
        raw += b"data: {}\r\n\r\n"
    elif bad == "error":
        raw = b'data: {"error":{"message":"no proof"}}\n\n' + raw
    elif bad == "bad_object":
        raw = raw.replace(b"chat.completion.chunk", b"other")
    elif bad == "conflicting_finish":
        raw = (
            b'data: {"object":"chat.completion.chunk",'
            b'"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n' + raw
        )
    stream = DiscoverySSECollector()
    from citypods.provider_catalog.probe import ContextEnvelopeError

    expected = {
        "missing_done": "incomplete_sse",
        "unfinished": "incomplete_sse",
        "duplicate_usage": "conflicting_sse_usage",
        "post_done": "invalid_sse_envelope",
        "error": "provider_sse_error",
        "bad_object": "invalid_sse_object",
        "conflicting_finish": "invalid_sse_envelope",
    }
    with pytest.raises(ContextEnvelopeError) as error:
        stream.feed(raw)
        stream.complete()
    assert error.value.code == expected[bad]


def test_stream_discards_large_metadata_but_keeps_small_answer():
    from citypods.provider_catalog.probe import DiscoverySSECollector

    stream = DiscoverySSECollector()
    frame = (
        b'data: {"object":"chat.completion.chunk","choices":[],"padding":"'
        + b"x" * 1000
        + b'"}\n\n'
    )
    for _ in range(5000):
        stream.feed(frame)
    stream.feed(stream_fixture())
    assert stream.wire_bytes > 4 * 1024 * 1024
    assert len(stream.complete().encode()) < 4096
    assert not stream.pending


@pytest.mark.parametrize("bound", ["MAX_WIRE", "MAX_FRAME", "MAX_EVENTS"])
def test_stream_enforces_each_hard_bound(monkeypatch, bound):
    from citypods.provider_catalog.probe import DiscoverySSECollector

    monkeypatch.setattr(DiscoverySSECollector, bound, 1)
    stream = DiscoverySSECollector()
    with pytest.raises(ValueError, match="exceeded"):
        stream.feed(stream_fixture())


def test_stream_normalized_expansion_remains_within_four_mib(monkeypatch):
    import citypods.provider_catalog.probe as probe

    stream = probe.DiscoverySSECollector()
    stream.feed(stream_fixture("😀" * 100))
    monkeypatch.setattr(probe, "_CONTEXT_BYTES", 500)
    with pytest.raises(ValueError, match="content exceeded"):
        stream.complete()


def test_stream_opt_in_is_scoped_and_output_only():
    _, transient = minimum_generation_fixture()
    assert transient.shaped_body["body"]["stream"] is True
    assert transient.shaped_body["body"]["stream_options"] == {"include_usage": True}
    _, transient = minimum_generation_fixture("input")
    assert transient.shaped_body["body"]["stream"] is False
    _, _, transient = _chat_discovery_fixture("nvidia", "output")
    assert transient.shaped_body["body"]["stream"] is False


def test_stream_collector_handles_large_wire_without_retaining_it():
    import time
    from types import SimpleNamespace

    from citypods.provider_catalog.probe import _collect_context

    _, transient = minimum_generation_fixture()
    frame = (
        b'data: {"object":"chat.completion.chunk","choices":[],"padding":"'
        + b"x" * 1000
        + b'"}\n\n'
    )

    class Response:
        status_code = 200
        headers = {"Content-Type": "text/event-stream"}
        closed = False

        def iter_content(self, chunk_size):
            for _ in range(5000):
                yield frame
            yield stream_fixture()

        def close(self):
            self.closed = True

    response = Response()
    session = SimpleNamespace(post=lambda *args, **kwargs: response)
    captured = _collect_context(session, transient, {}, 600, time.monotonic, discovery_stream=True)
    assert response.closed
    assert captured.context_diagnostics["stream_bytes"] > 4 * 1024 * 1024
    assert captured.context_diagnostics["response_bytes"] < 4096
    assert captured.context_diagnostics["stream_events"] == 5004
    assert captured.context_diagnostics["diagnostic_code"] == "response_received"


def test_stream_diagnostics_are_optional_and_strictly_bounded():
    from citypods.provider_catalog.issue import _discovery_attempt_diagnostics

    row = asdict(DiscoveryObservation(request(), STAMP))
    del row["stream_bytes"]
    del row["stream_events"]
    assert DiscoveryObservation.from_dict(row).stream_bytes is None
    for name, bad in (("stream_bytes", 64 * 1024 * 1024 + 1), ("stream_events", True)):
        with pytest.raises(ValueError, match="stream diagnostic"):
            DiscoveryObservation.from_dict(row | {name: bad})
    parsed = _discovery_attempt_diagnostics([dict(stream_bytes=12_000_000, stream_events=1000)])[0]
    assert parsed["stream_bytes"] == 12_000_000 and parsed["stream_events"] == 1000


def test_verified_stream_output_does_not_retain_failure_preview():
    import json

    from citypods.provider_catalog.registry import rules_for
    from citypods.provider_catalog.rules import Response

    req, transient = minimum_generation_fixture()
    body = dict(
        object="chat.completion",
        usage=dict(prompt_tokens=2048, completion_tokens=4096, total_tokens=6144),
        choices=[dict(message=dict(content="1\n1\n1"), finish_reason="length")],
    )
    response = Response(200, body=json.dumps(body), context_diagnostics={"response_preview": '"1"'})
    observed = rules_for("nvidia").discovery_observation(response, req, transient)
    assert observed.outcome == "admitted" and observed.response_preview == ""


@pytest.mark.parametrize("bad", ["early_usage", "late_content"])
def test_stream_usage_must_be_final_and_content_cannot_follow_finish(bad):
    import json

    from citypods.provider_catalog.probe import DiscoverySSECollector

    raw = stream_fixture()
    if bad == "early_usage":
        frame = dict(object="chat.completion.chunk", choices=[], usage=dict(total_tokens=1))
        raw = ("data: " + json.dumps(frame) + "\n\n").encode() + raw
    else:
        frame = dict(object="chat.completion.chunk", choices=[dict(delta=dict(content="1\n"))])
        raw = raw.replace(
            b"data: [DONE]", ("data: " + json.dumps(frame) + "\n\ndata: [DONE]").encode()
        )
    with pytest.raises(ValueError):
        stream = DiscoverySSECollector()
        stream.feed(raw)
        stream.complete()


def test_stream_done_finishes_without_waiting_for_http_eof():
    import time
    from types import SimpleNamespace

    from citypods.provider_catalog.probe import _collect_context

    _, transient = minimum_generation_fixture()

    class Response:
        status_code = 200
        headers = {"Content-Type": "text/event-stream"}
        closed = False

        def iter_content(self, chunk_size):
            yield stream_fixture()
            raise AssertionError("must not wait for HTTP EOF after complete DONE")

        def close(self):
            self.closed = True

    response = Response()
    captured = _collect_context(
        SimpleNamespace(post=lambda *args, **kwargs: response),
        transient,
        {},
        600,
        time.monotonic,
        discovery_stream=True,
    )
    assert captured.context_diagnostics["diagnostic_code"] == "response_received"
    assert response.closed


def test_stream_keeps_consistent_response_id_and_rejects_cross_response_data():
    import json

    from citypods.provider_catalog.probe import DiscoverySSECollector

    prefix = b'data: {"object":"chat.completion.chunk","id":"chatcmpl-test","choices":[]}\n\n'
    stream = DiscoverySSECollector()
    stream.feed(prefix + stream_fixture())
    assert json.loads(stream.complete())["id"] == "chatcmpl-test"
    other = prefix.replace(b"chatcmpl-test", b"other")
    with pytest.raises(ValueError):
        DiscoverySSECollector().feed(prefix + other + stream_fixture())


def test_stream_accepts_multiline_json_data_without_unicode_line_splitting():
    import json

    from citypods.provider_catalog.probe import DiscoverySSECollector

    event = dict(object="chat.completion.chunk", choices=[])
    prefix = "\n".join("data: " + line for line in json.dumps(event, indent=2).split("\n"))
    stream = DiscoverySSECollector()
    stream.feed((prefix + "\n\n").encode() + stream_fixture())
    assert json.loads(stream.complete())["usage"]["completion_tokens"] == 4096


def test_stream_timings_use_the_authorized_deadline_not_a_two_minute_clip():
    from types import SimpleNamespace

    from citypods.provider_catalog.probe import _collect_context

    _, transient = minimum_generation_fixture()
    tick = SimpleNamespace(now=0)

    class Response:
        status_code = 200
        headers = {"Content-Type": "text/event-stream"}

        def iter_content(self, chunk_size):
            tick.now = 200
            yield stream_fixture()

        def close(self):
            pass

    observed = _collect_context(
        SimpleNamespace(post=lambda *args, **kwargs: Response()),
        transient,
        {},
        600,
        lambda: tick.now,
        discovery_stream=True,
    )
    assert observed.context_diagnostics["duration_ms"] == 200_000
    assert observed.context_diagnostics["first_byte_ms"] == 200_000


@pytest.mark.parametrize(
    ("event", "code"),
    [([], "invalid_sse_event_type"), ({"object": "other"}, "invalid_sse_object")],
)
def test_stream_identifies_invalid_event_shape(event, code):
    import json

    from citypods.provider_catalog.probe import ContextEnvelopeError, DiscoverySSECollector

    collector = DiscoverySSECollector()
    with pytest.raises(ContextEnvelopeError) as error:
        collector.feed(("data: " + json.dumps(event) + "\n\n").encode())
    assert error.value.code == code
    assert collector.error_preview == b""


def test_stream_provider_error_is_diagnostic_only_and_excludes_generated_siblings():
    import json
    import time
    from types import SimpleNamespace

    from citypods.provider_catalog.probe import _collect_context

    _, transient = minimum_generation_fixture()
    event = {
        "error": {"message": "upstream timed out", "code": 504},
        "reasoning": "private reasoning",
        "content": "generated text",
    }

    class Response:
        status_code = 200
        headers = {"Content-Type": "text/event-stream"}
        closed = False

        def iter_content(self, chunk_size):
            yield ("data: " + json.dumps(event) + "\n\n").encode()

        def close(self):
            self.closed = True

    response = Response()
    captured = _collect_context(
        SimpleNamespace(post=lambda *args, **kwargs: response),
        transient,
        {},
        600,
        time.monotonic,
        discovery_stream=True,
        retain_response_preview=True,
    )
    assert captured.context_diagnostics["diagnostic_code"] == "provider_sse_error"
    assert "upstream timed out" in captured.context_diagnostics["response_preview"]
    assert "private reasoning" not in captured.context_diagnostics["response_preview"]
    assert "generated text" not in captured.context_diagnostics["response_preview"]
    assert captured.body == ""
    assert response.closed


@pytest.mark.parametrize("code", ["provider_sse_error", "stream_bytes_exceeded"])
def test_long_discovery_failure_survives_configured_context_evidence_validation(code):
    from citypods.provider_catalog.rules import Response, chat_discovery_observation

    request, transient = minimum_generation_fixture()
    response = Response(
        200,
        context_diagnostics={
            "diagnostic_code": code,
            "duration_ms": 600216,
            "first_byte_ms": 359,
            "response_preview": "upstream timeout",
            "stream_bytes": 7537360,
            "stream_events": 22977,
        },
    )
    observed = chat_discovery_observation(response, request, transient, reasoning_basis="included")
    assert not observed.verified
    assert observed.diagnostic == code
    assert observed.response_preview == "upstream timeout"
    assert observed.stream_events == 22977
