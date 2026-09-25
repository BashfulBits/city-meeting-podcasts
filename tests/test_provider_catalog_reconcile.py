"""Planner, issue and quality behavior for provider-catalog reconciliation (review/48 Slice 1).

Offline: fake catalogs, fake canaries and a fake Worker pause control drive the real planner.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date

import pytest

from citypods.compute.llm_lanes import parse_lanes
from citypods.provider_catalog.decisions import Decisions
from citypods.provider_catalog.issue import (
    MARKER,
    decision_choices,
    decode_state,
    render_body,
    sync_issue,
)
from citypods.provider_catalog.quality import QualityIndex
from citypods.provider_catalog.reconcile import reconcile
from citypods.provider_catalog.rules import Response

TODAY = date(2026, 9, 24)
OK = Response(status=200, body='data: {"choices":[{"delta":{"content":"ok"}}]}\n\n')
GONE = Response(status=404, body='{"error":{"message":"The model does not exist"}}')


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, catalogs):
        self.catalogs = catalogs

    def get(self, url, headers=None, params=None, timeout=None):
        for base, payload in self.catalogs.items():
            if url.startswith(base):
                return FakeResponse(payload)
        return FakeResponse({}, 404)


@dataclass
class FakeOutcome:
    contended: bool = False
    renewals: int = 0

    def renew(self):
        self.renewals += 1


class FakeControl:
    def __init__(self, quota=None, contended=False):
        self.quota, self.contended = quota or {}, contended
        self.paused_providers, self.reserved = [], []

    @contextmanager
    def paused(self, provider):
        self.paused_providers.append(provider)
        yield FakeOutcome(contended=self.contended)

    def route_quota(self, provider):
        return self.quota

    def reserve(self, route_id):
        self.reserved.append(route_id)


LIMITS = {
    "providers": {
        "groq": {"api_base": "https://groq.test/v1", "accounts": [{"id": "p", "api_key_env": "K"}]},
        "openrouter": {
            "api_base": "https://or.test/v1",
            "accounts": [{"id": "p", "api_key_env": "K"}],
        },
        "zai": {"api_base": "https://zai.test/v4", "accounts": [{"id": "p", "api_key_env": "K"}]},
    },
    "routes": [
        {
            "route_id": "groq_keep",
            "provider": "groq",
            "model": "meta/llama-keep",
            "upstream_model": "meta-llama/llama-keep",
            "account_id": "p",
        },
        {
            "route_id": "groq_gone",
            "provider": "groq",
            "model": "qwen/qwen-gone",
            "upstream_model": "qwen/qwen-gone",
            "account_id": "p",
        },
        {
            "route_id": "or_scarce",
            "provider": "openrouter",
            "model": "google/gem-scarce",
            "upstream_model": "google/gem-scarce:free",
            "account_id": "p",
            "rpd": 20,
        },
        {
            "route_id": "zai_flash",
            "provider": "zai",
            "model": "zai/glm-flash",
            "upstream_model": "glm-flash",
            "account_id": "p",
        },
    ],
}
CATALOGS = {
    "https://groq.test": {
        "data": [
            {"id": "meta-llama/llama-keep"},
            {"id": "meta-llama/llama-new"},
            {"id": "whisper-large-v3"},
        ]
    },
    "https://or.test": {
        "data": [
            {"id": "google/gem-scarce:free", "pricing": {"prompt": "0", "completion": "0"}},
            {
                "id": "kimi/k9:free",
                "pricing": {"prompt": "0", "completion": "0"},
                "context_length": 131072,
            },
            {"id": "kimi/k9", "pricing": {"prompt": "0.000001", "completion": "0.000002"}},
        ]
    },
    "https://zai.test": {"data": [{"id": "glm-paid"}]},
}
LANES = parse_lanes(
    {
        "tagger": {
            "models": ["meta/llama-keep"],
            "max_dispatches_per_run": 1,
            "daily_write_units": 100,
        },
        "fixed": {
            "models": ["meta/llama-keep"],
            "max_dispatches_per_run": 1,
            "daily_write_units": 100,
            "catalog_backup_candidates": False,
        },
    }
)
QUALITY = QualityIndex(
    scores={
        ("meta", "llama-keep"): 40.0,
        ("meta", "llama-new"): 55.0,
        ("kimi", "k9"): 30.0,
        ("openai", "gpt-oss-120b"): 33.0,
        ("nvidia", "nvidia-nemotron-3-super-120b-a12b"): 36.0,
    }
)


NO_DECISIONS = Decisions()


def _run(monkeypatch, *, canaries=None, state=None, decisions=NO_DECISIONS, control=None, **kw):
    monkeypatch.setenv("K", "test-key")
    canaries = {"qwen/qwen-gone": GONE, **(canaries or {})}
    sent = []

    def canary_fn(rules, cfg, model, key, session):
        sent.append(model)
        return canaries.get(model, OK)

    control = control or FakeControl()
    report = reconcile(
        LIMITS,
        LANES,
        decisions,
        QUALITY,
        state or {},
        session=FakeSession(CATALOGS),
        control=control,
        today=TODAY,
        canary_fn=canary_fn,
        **kw,
    )
    return report, sent, control


def test_a_proven_free_model_becomes_a_candidate_with_score_links_and_lane_suggestions(monkeypatch):
    report, sent, control = _run(monkeypatch)
    keys = {c.key: c for c in report.candidates}
    new = keys["groq/meta-llama/llama-new"]
    assert new.score == 55.0 and new.below_floor is False
    assert new.lanes == ("tagger",)  # 55 >= the lane's 40; the opted-out lane is never offered
    assert any("huggingface" in url for _, url in new.links)
    k9 = keys["openrouter/kimi/k9:free"]
    assert k9.below_floor is True and k9.lanes == () and k9.context_limit == 131072
    assert "kimi/k9" not in sent  # the paid variant has no free evidence
    assert "whisper-large-v3" not in sent  # non-chat entries are never canaried
    assert set(control.paused_providers) == {"groq", "openrouter", "zai"}


def test_observation_only_provider_is_health_checked_but_never_proposes(monkeypatch):
    report, sent, _ = _run(monkeypatch)
    assert "glm-paid" not in sent and "glm-flash" in sent
    assert not any(c.provider == "zai" for c in report.candidates)
    # z.ai's list omits its free models, so glm-flash's absence is not reported as drift.
    assert not any("zai_flash" in o and "absent" in o for o in report.observations)


def test_a_retired_configured_route_is_a_new_anomaly_then_a_known_one(monkeypatch):
    report, _, _ = _run(monkeypatch)
    [gone] = [a for a in report.anomalies if a.route_id == "groq_gone"]
    assert gone.verdict == "retired" and gone.absent_from_catalog and gone.new
    again, _, _ = _run(monkeypatch, state=report.state)
    assert [a.new for a in again.anomalies if a.route_id == "groq_gone"] == [False]


def test_an_acknowledged_state_moves_to_observations(monkeypatch):
    decisions = Decisions(
        acknowledged=(
            {
                "provider": "groq",
                "model_glob": "qwen/*",
                "verdict": "retired",
                "reason": "known",
                "decided_on": "2026-09-24",
            },
        )
    )
    report, _, _ = _run(monkeypatch, decisions=decisions)
    assert not report.anomalies
    assert any("acknowledged: known" in o for o in report.observations)


def test_ignored_candidates_are_not_canaried(monkeypatch):
    decisions = Decisions(
        ignored=(
            {
                "provider": "groq",
                "model": "meta-llama/llama-new",
                "reason": "no",
                "decided_on": "2026-09-01",
            },
        )
    )
    _, sent, _ = _run(monkeypatch, decisions=decisions)
    assert "meta-llama/llama-new" not in sent


def test_a_failed_candidate_is_not_retried_for_28_days_and_cannot_starve_others(monkeypatch):
    report, sent, _ = _run(monkeypatch, canaries={"kimi/k9:free": GONE})
    assert "kimi/k9:free" in sent and not any(c.model == "kimi/k9:free" for c in report.candidates)
    _, sent_again, _ = _run(monkeypatch, state=report.state)
    assert "kimi/k9:free" not in sent_again
    later = {
        **report.state,
        "canaries": {"openrouter/kimi/k9:free": {"verdict": "retired", "on": "2026-08-01"}},
    }
    _, sent_later, _ = _run(monkeypatch, state=later)
    assert "kimi/k9:free" in sent_later


def test_proven_candidates_stay_listed_without_being_re_canaried(monkeypatch):
    first, _, _ = _run(monkeypatch)
    second, sent, _ = _run(monkeypatch, state=first.state)
    assert "meta-llama/llama-new" not in sent
    assert "groq/meta-llama/llama-new" in {c.key for c in second.candidates}


def test_a_scarce_route_waits_for_its_cadence_and_its_quota(monkeypatch):
    # Out of quota: deferred to the reset, never canaried, never judged.
    control = FakeControl(quota={"or_scarce": {"rpd_remaining": 0, "rpd_resets_at": 123}})
    report, sent, _ = _run(monkeypatch, control=control)
    assert "google/gem-scarce:free" not in sent and report.state["deferred"] == {"or_scarce": "123"}
    # Quota left: checked and charged to the Worker's ledger.
    control = FakeControl(quota={"or_scarce": {"rpd_remaining": 5}})
    report, sent, control = _run(monkeypatch, control=control)
    assert "google/gem-scarce:free" in sent and "or_scarce" in control.reserved
    # Checked recently: skipped until the 28-day cadence comes round.
    _, sent, _ = _run(monkeypatch, state=report.state)
    assert "google/gem-scarce:free" not in sent


def test_due_only_runs_just_the_deferred_checks_and_keeps_the_last_full_report(monkeypatch):
    full, _, _ = _run(monkeypatch, control=FakeControl(quota={"or_scarce": {"rpd_remaining": 0}}))
    due, sent, _ = _run(
        monkeypatch,
        state=full.state,
        due_only=True,
        control=FakeControl(quota={"or_scarce": {"rpd_remaining": 3}}),
    )
    assert sent == ["google/gem-scarce:free"]
    assert {c.key for c in due.candidates} == {c.key for c in full.candidates}
    assert {a.route_id for a in due.anomalies} == {a.route_id for a in full.anomalies}
    assert due.state["deferred"] == {}


def test_unpaused_probes_are_reported_as_contended(monkeypatch):
    report, _, _ = _run(monkeypatch, control=FakeControl(contended=True))
    assert any("without a drained dispatch pause" in o for o in report.observations)


def test_a_new_lane_is_offered_with_no_code_change(monkeypatch):
    global LANES
    original = LANES
    try:
        LANES = parse_lanes(
            {
                **{
                    p: {
                        "models": list(lane.models),
                        "max_dispatches_per_run": 1,
                        "daily_write_units": 100,
                    }
                    for p, lane in original.items()
                },
                "brand-new-lane": {
                    "models": ["meta/llama-keep"],
                    "max_dispatches_per_run": 1,
                    "daily_write_units": 100,
                },
            }
        )
        report, _, _ = _run(monkeypatch)
    finally:
        LANES = original
    new = next(c for c in report.candidates if c.model == "meta-llama/llama-new")
    assert "brand-new-lane" in new.lanes
    assert "`groq/meta-llama/llama-new`: add as backup to `brand-new-lane`" in decision_choices(
        report
    )


def test_quality_matching_rules():
    q = QualityIndex(
        scores={
            ("google", "gemma-4-31b"): 40.0,
            ("nvidia", "nvidia-nemo-3"): 36.0,
            ("alibaba", "qwen3-8b"): 20.0,
        }
    )
    assert q.score("gemma-4-31b-it", creator="google") == 40.0  # instruct suffix is optional
    assert q.score("nvidia/nemo-3:free", free_suffix=":free") == 36.0  # AA creator prefix
    assert q.score("qwen/qwen3-8b") == 20.0  # publisher alias
    assert q.score("google/gemma-4-26b") is None  # never a nearby model's score
    assert QualityIndex().floor is None


# ---- issue --------------------------------------------------------------------------------------


def test_issue_body_round_trips_state_and_keeps_ticked_boxes(monkeypatch):
    report, _, _ = _run(monkeypatch)
    body = render_body(report, run_date="2026-09-24")
    assert MARKER in body
    assert decode_state(body) == json.loads(json.dumps(report.state))  # tuples become lists
    choice = "`groq/meta-llama/llama-new`: add as backup to `tagger`"
    ticked = body.replace(f"- [ ] {choice}", f"- [x] {choice}")
    rerendered = render_body(report, run_date="2026-10-01", previous_body=ticked)
    assert f"- [x] {choice}" in rerendered
    assert "<details><summary>Observations" in rerendered


class FakeGh:
    def __init__(self, issues):
        self.issues, self.calls = issues, []

    def __call__(self, args):
        self.calls.append(list(args))
        if args[:2] == ["issue", "list"]:
            return json.dumps(self.issues)
        return "https://github.test/issues/9" if args[:2] == ["issue", "create"] else ""


@pytest.mark.parametrize(
    ("issues", "actionable", "expected"),
    [
        ([], True, "created"),
        ([], False, "no issue"),
        ([{"number": 7, "state": "OPEN", "body": MARKER}], True, "updated #7"),
        ([{"number": 7, "state": "OPEN", "body": MARKER}], False, "closed #7"),
        ([{"number": 7, "state": "CLOSED", "body": MARKER}], True, "reopened #7"),
        ([{"number": 3, "state": "OPEN", "body": "unrelated"}], True, "created"),
    ],
)
def test_one_rolling_issue_is_created_updated_closed_or_reopened(
    monkeypatch, issues, actionable, expected
):
    report, _, _ = _run(monkeypatch)
    if not actionable:
        report.candidates, report.anomalies = [], []
    gh = FakeGh(issues)
    assert sync_issue(report, run_date="2026-09-24", run=gh).startswith(expected)


# ---- refinements from the 2026-09-24 dry run and discovery backtest ------------------------------

from citypods.provider_catalog.reconcile import (  # noqa: E402
    backtest_discovery,
    backtest_summary,
    other_lanes,
    suggest_lanes,
)


def _with(limits_routes=(), catalogs=None):
    limits = {**LIMITS, "routes": [*LIMITS["routes"], *limits_routes]}
    return limits, {**CATALOGS, **(catalogs or {})}


def _run_with(monkeypatch, limits, catalogs, state=None):
    monkeypatch.setenv("K", "test-key")
    sent, sleeps = [], []

    def canary_fn(rules, cfg, model, key, session):
        sent.append(model)
        return GONE if model == "qwen/qwen-gone" else OK

    report = reconcile(
        limits,
        LANES,
        NO_DECISIONS,
        QUALITY,
        state or {},
        session=FakeSession(catalogs),
        control=FakeControl(),
        today=TODAY,
        canary_fn=canary_fn,
        sleep=sleeps.append,
    )
    return report, sent, sleeps


def test_an_alias_of_a_configured_model_is_not_proposed(monkeypatch):
    # Dry run: gemini-3.1-flash-lite-preview was proposed although gemini-3.1-flash-lite is live.
    catalogs = {
        "https://groq.test": {
            "data": [
                {"id": "meta-llama/llama-keep"},
                {"id": "meta-llama/llama-keep-preview"},
            ]
        }
    }
    _, sent, _ = _run_with(monkeypatch, *_with(catalogs=catalogs))
    assert "meta-llama/llama-keep-preview" not in sent


def test_a_model_smaller_than_any_configured_free_route_is_not_proposed(monkeypatch):
    # Dry run: allam-2-7b (4k context) was proposed. The floor comes from config, not a constant.
    small = {
        "route_id": "groq_ctx",
        "provider": "groq",
        "model": "x/ctx",
        "free": True,
        "upstream_model": "meta-llama/llama-keep",
        "input_context_limit": 32768,
    }
    catalogs = {
        "https://groq.test": {
            "data": [
                {"id": "meta-llama/llama-keep"},
                {"id": "tiny/model-4k", "context_window": 4096},
                {"id": "big/model-128k", "context_window": 131072},
            ]
        }
    }
    _, sent, _ = _run_with(monkeypatch, *_with([small], catalogs))
    assert "tiny/model-4k" not in sent and "big/model-128k" in sent


def test_inconclusive_candidates_are_retried_but_after_fresh_ones(monkeypatch):
    # Dry run: Airforce canaries hit its 1 rps limit (quota_exhausted); remembering that for 28
    # days would have hidden three free models for a month.
    state = {
        "canaries": {"openrouter/kimi/k9:free": {"verdict": "quota_exhausted", "on": "2026-09-20"}}
    }
    report, sent, _ = _run(monkeypatch, state=state)
    assert "kimi/k9:free" in sent
    assert "openrouter/kimi/k9:free" in {c.key for c in report.candidates}


def test_provider_canary_spacing_is_honoured(monkeypatch):
    route = {
        "route_id": "af_1",
        "provider": "airforce",
        "model": "af/one",
        "upstream_model": "one",
        "account_id": "p",
    }
    limits = {
        "providers": {
            **LIMITS["providers"],
            "airforce": {
                "api_base": "https://af.test/v1",
                "accounts": [{"id": "p", "api_key_env": "K"}],
            },
        },
        "routes": [*LIMITS["routes"], route],
    }
    catalogs = {
        **CATALOGS,
        "https://af.test": {
            "data": [{"id": "one"}, {"id": "two", "tier": "free"}, {"id": "three", "tier": "free"}]
        },
    }
    _, sent, sleeps = _run_with(monkeypatch, limits, catalogs)
    assert {"one", "two", "three"} <= set(sent)
    assert sleeps.count(1.5) == 2  # between the three Airforce probes, never before the first


def test_lane_offer_uses_the_weakest_member_and_lists_the_rest():
    incumbents = {"locator": [43.6, 39.5], "tagger": [15.6], "empty": []}
    assert suggest_lanes(20.0, incumbents) == ("empty", "tagger")
    assert other_lanes(20.0, incumbents) == (("locator", 19.5),)
    # Backtest: a model's own score is excluded from the lanes it already sits in.
    assert suggest_lanes(39.5, {"locator": [43.6, 39.5]}, exclude=39.5) == ()
    assert suggest_lanes(None, incumbents) == ()


def test_quality_variant_suffixes_are_optional_but_never_guess_between_two():
    q = QualityIndex(
        scores={
            ("google", "gemini-3-1-flash-lite-preview"): 15.6,
            ("nvidia", "nemotron-3-nano-omni-30b-a3b"): 10.3,
            ("google", "gemini-3-flash"): 17.9,
            ("google", "gemini-3-flash-reasoning"): 26.3,
        }
    )
    assert q.score("gemini-3.1-flash-lite", creator="google") == 15.6
    assert q.score("nvidia/nemotron-3-nano-omni-30b-a3b-reasoning") == 10.3
    assert q.score("gemini-3-flash", creator="google") == 17.9  # exact match wins
    assert q.score("gemini-3-flash-preview", creator="google") is None  # two normalize equal
    # A bare ID with no creator: unique slug across all creators only.
    assert q.score("gemma-4-31B-it") is None  # not in this index
    assert QualityIndex(scores={("google", "gemma-4-31b"): 19.0}).score("gemma-4-31B-it") == 19.0
    shared = QualityIndex(scores={("a", "same-model"): 1.0, ("b", "same-model"): 2.0})
    assert shared.score("same-model") is None


def test_backtest_reports_the_first_gate_that_drops_each_configured_model(monkeypatch):
    monkeypatch.setenv("K", "test-key")
    rows = backtest_discovery(LIMITS, LANES, QUALITY, session=FakeSession(CATALOGS))
    gates = {r.route_id: r.gate for r in rows}
    assert gates == {
        "groq_keep": "discoverable",
        "groq_gone": "not in catalog",
        "or_scarce": "discoverable",
        "zai_flash": "observation-only provider",
    }
    summary = backtest_summary(rows)
    assert (
        summary.startswith("discovery self-check: 2/4")
        and "qwen/qwen-gone (not in catalog)" in summary
    )


def test_a_free_route_turned_paid_is_a_decision_with_its_lane_impact(monkeypatch):
    # Maintainer decision 2026-09-24: never auto-remove; offer remove / keep-as-paid, and show
    # which lanes use the route and what else still serves them.
    monkeypatch.setenv("K", "test-key")
    paid = Response(status=403, body='{"error":{"message":"only available on agentic harnesses"}}')
    lanes = parse_lanes(
        {
            "moments": {
                "models": ["google/gem-scarce", "meta/llama-keep"],
                "max_dispatches_per_run": 1,
                "daily_write_units": 100,
            },
            "solo": {
                "models": ["google/gem-scarce"],
                "max_dispatches_per_run": 1,
                "daily_write_units": 100,
            },
        }
    )
    report = reconcile(
        LIMITS,
        lanes,
        NO_DECISIONS,
        QUALITY,
        {},
        session=FakeSession(CATALOGS),
        control=FakeControl(quota={"or_scarce": {"rpd_remaining": 5}}),
        today=TODAY,
        canary_fn=lambda rules, cfg, model, key, session: (
            paid
            if model == "google/gem-scarce:free"
            else (GONE if model == "qwen/qwen-gone" else OK)
        ),
    )
    [anomaly] = [a for a in report.anomalies if a.route_id == "or_scarce"]
    assert anomaly.verdict == "not_entitled"
    assert anomaly.lane_usage == (("moments", ("meta/llama-keep",)), ("solo", ()))
    choices = decision_choices(report)
    assert "`or_scarce`: remove route" in choices
    assert "`or_scarce`: keep as a paid route" in choices
    body = render_body(report, run_date="2026-09-24")
    assert "used by `moments`; still served by: `meta/llama-keep`" in body
    assert "used by `solo`; still served by: **nothing else -- the lane stalls**" in body
    assert body.index("## Decisions") > body.index("## Configured-route anomalies")


def test_a_due_only_run_calls_only_providers_with_deferred_checks(monkeypatch):
    full, _, _ = _run(monkeypatch, control=FakeControl(quota={"or_scarce": {"rpd_remaining": 0}}))
    requested = []

    class RecordingSession(FakeSession):
        def get(self, url, headers=None, params=None, timeout=None):
            requested.append(url)
            return super().get(url, headers=headers, params=params, timeout=timeout)

    reconcile(
        LIMITS,
        LANES,
        NO_DECISIONS,
        QUALITY,
        full.state,
        session=RecordingSession(CATALOGS),
        control=FakeControl(quota={"or_scarce": {"rpd_remaining": 3}}),
        today=TODAY,
        canary_fn=lambda *args: OK,
        due_only=True,
    )
    assert requested and all(url.startswith("https://or.test") for url in requested)


@pytest.mark.parametrize("payload", ["oops", 7, {"data": "oops"}])
def test_a_malformed_catalog_is_that_providers_error_not_a_crash(monkeypatch, payload):
    from citypods.provider_catalog.probe import fetch_catalog
    from citypods.provider_catalog.registry import all_rules

    monkeypatch.setenv("K", "test-key")
    catalog = fetch_catalog(
        all_rules()["groq"],
        LIMITS["providers"]["groq"],
        FakeSession({"https://groq.test": payload}),
    )
    assert catalog.error == "catalog malformed response"


def test_the_decision_block_survives_a_truncated_body(monkeypatch):
    import citypods.provider_catalog.issue as issue

    report, _, _ = _run(monkeypatch)
    choice = "`groq/meta-llama/llama-new`: add as backup to `tagger`"
    ticked = render_body(report, run_date="2026-09-24").replace(
        f"- [ ] {choice}", f"- [x] {choice}"
    )
    report.observations.extend(f"observation {i} " + "x" * 200 for i in range(200))
    monkeypatch.setattr(issue, "_HUMAN_BODY_LIMIT", 2_500)
    body = render_body(report, run_date="2026-10-01", previous_body=ticked)
    assert "Truncated" in body or "Omitted" in body
    assert f"- [x] {choice}" in body  # the tick survives, so the next update keeps it
    assert set(decision_choices(report)) <= {
        line.split("] ", 1)[1] for line in body.splitlines() if line.startswith("- [")
    }
    assert decode_state(body) == json.loads(json.dumps(report.state))
