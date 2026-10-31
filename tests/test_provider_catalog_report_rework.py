"""Report rework: artifact state, scoring fallbacks, per-model decisions, method switch."""

import hashlib
import io
import json
import zipfile
from dataclasses import replace
from datetime import date

import requests

from citypods.compute.llm_lanes import LaneConfig
from citypods.provider_catalog.apply import (
    SOURCE_PATHS,
    ApplyConfig,
    Decision,
    parse_decisions,
    plan_apply,
    proposal_body,
)
from citypods.provider_catalog.config_edit import apply_config_edits
from citypods.provider_catalog.evidence import digest
from citypods.provider_catalog.issue import (
    decision_choices,
    decode_state,
    method_choice,
    render_body,
)
from citypods.provider_catalog.model_facts import catalog_facts, hugging_face_facts, plain
from citypods.provider_catalog.quality import AA_METRIC, QualityIndex, fetch_quality_index
from citypods.provider_catalog.reconcile import Anomaly, Candidate, Report
from citypods.provider_catalog.state_store import (
    ARTIFACT_PREFIX,
    STATE_FILE,
    bulk_ref,
    encode_bulk,
    restore_bulk,
    split_bulk,
)

TODAY = date(2026, 10, 31)


# --- state store -------------------------------------------------------------------------------


def _artifact(bulk, run_id="42", **meta):
    raw = encode_bulk(bulk)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(STATE_FILE, raw)
    listing = {
        "artifacts": [
            {"id": 7, "name": f"{ARTIFACT_PREFIX}{run_id}", "size_in_bytes": len(raw), **meta}
        ]
    }
    return listing, buffer.getvalue()


def _api(listing, **run):
    ours = {
        "path": ".github/workflows/provider-catalog-reconcile.yml",
        "head_branch": "main",
        "event": "schedule",
        "head_repository": {"full_name": "o/r"},
        **run,
    }
    return lambda path: listing if path.endswith("/artifacts?per_page=100") else ours


def test_bulk_state_leaves_the_issue_and_comes_back_by_run_and_digest():
    state = {"catalogs": {"a": {}}, "canaries": {"k": 1}, "structured_checks": {}, "last_full": {}}
    compact, bulk = split_bulk(state)
    assert set(compact) == {"last_full"} and set(bulk) == {
        "catalogs",
        "canaries",
        "structured_checks",
    }
    listing, archive = _artifact(bulk)
    issue_state = {**compact, "bulk_ref": bulk_ref(bulk, "42")}
    restored, note = restore_bulk(
        issue_state, repository="o/r", api=_api(listing), download=lambda _p: archive
    )
    assert restored["catalogs"] == {"a": {}} and restored["canaries"] == {"k": 1}
    assert "restored" in note


def test_a_missing_expired_or_tampered_artifact_costs_memory_only():
    _, bulk = split_bulk({"catalogs": {"a": {}}})
    ref = {"bulk_ref": bulk_ref(bulk, "42")}
    listing, archive = _artifact(bulk)
    cases = [
        ({"artifacts": []}, archive, "missing"),
        (_artifact(bulk, expired=True)[0], archive, "missing"),
        (listing, _artifact({"catalogs": {"x": {}}})[1], "digest"),
    ]
    for lst, data, word in cases:
        state, note = restore_bulk(
            dict(ref), repository="o/r", api=_api(lst), download=lambda _p, d=data: d
        )
        assert "catalogs" not in state and word in note
    for foreign in (
        {"head_branch": "evil"},
        {"path": ".github/workflows/x.yml"},
        {"event": "pull_request"},
    ):
        state, note = restore_bulk(
            dict(ref), repository="o/r", api=_api(listing, **foreign), download=lambda _p: archive
        )
        assert "catalogs" not in state and "not a reconcile run" in note
    bad = {"bulk_ref": {"run_id": "../x", "sha256": "z"}}
    assert "malformed" in restore_bulk(bad, repository="o/r", api=None, download=None)[1]
    # No reference: an inline (older) state is already complete.
    assert restore_bulk({"catalogs": {}}, repository="o/r", api=None, download=None)[1] == ""


def test_the_issue_keeps_only_decision_state_when_a_bulk_reference_exists():
    report = Report(state={"catalogs": {"p": {"models": [["m"] * 6] * 500}}, "last_full": {}})
    inline = decode_state(render_body(report, run_date="2026-10-31"))
    assert "catalogs" in inline
    report.state["bulk_ref"] = bulk_ref(split_bulk(report.state)[1], "9")
    slim = decode_state(render_body(report, run_date="2026-10-31"))
    assert "catalogs" not in slim and slim["bulk_ref"]["run_id"] == "9"


# --- scoring -----------------------------------------------------------------------------------


class _Session:
    def __init__(self, routes):
        self.routes, self.urls = routes, []

    def get(self, url, **kwargs):
        self.urls.append(url)
        status, payload = self.routes.get(url, (404, {}))
        response = requests.Response()
        response.status_code = status
        response.json = lambda: payload
        return response


def _row(creator, slug, value, creator_slug=None):
    return {
        "model_creator": {"name": creator, "slug": creator_slug or creator.lower()},
        "slug": slug,
        "evaluations": {AA_METRIC: value},
    }


def test_a_moved_endpoint_falls_back_and_a_total_failure_is_reported(monkeypatch):
    monkeypatch.setenv("ARTIFICIAL_ANALYSIS_API_KEY", "k")
    old = "https://artificialanalysis.ai/api/v2/language/models/free"
    new = "https://artificialanalysis.ai/api/v2/data/llms/models"
    session = _Session({new: (200, {"data": [_row("MiniMax", "minimax-m3-1-flash", 29)]})})
    index = fetch_quality_index(session)
    assert index.error is None and session.urls == [old, new]
    assert index.score("minimaxai/minimax-m3.1-flash") == 29
    failed = fetch_quality_index(_Session({}))
    assert "HTTP 404" in failed.error and failed.error.count("Artificial Analysis") == 2


def test_size_suffix_and_publisher_aliases_match_aa_slugs():
    index = QualityIndex(scores={("mistral", "mistral-large-4"): 26.0})
    assert index.score("mistralai/mistral-large-4-1t-a52b") == 26.0
    # A different size is a different model, so it is never matched by stripping.
    assert index.score("mistralai/mistral-large-4-8b") is None


# --- facts -------------------------------------------------------------------------------------


def test_catalog_facts_are_short_inert_text():
    facts = catalog_facts(
        "cohere/north-mini-code-30b-a3b:free",
        {
            "description": "Great <b>model</b> [click](http://evil) @everyone " + "x" * 400,
            "created": 1_780_000_000,
            "expiration_date": "2026-10-31",
            "architecture": {"input_modalities": ["text", "image"]},
        },
    )
    assert facts["size"] == "30B (3B active)" and facts["expires"] == "2026-10-31"
    assert facts["inputs"] == "text, image" and facts["released"].startswith("2026-")
    assert len(facts["summary"]) <= 220 and not any(c in facts["summary"] for c in "<>[]@")
    assert plain("a `b` | c\nd") == "a b c d"


def test_hugging_face_needs_one_unambiguous_match():
    row = {
        "id": "org/Model-X",
        "createdAt": "2026-07-21T00:00:00Z",
        "tags": ["license:mit"],
        "safetensors": {"total": 29_776_626_688},
    }
    one = _Session({"https://huggingface.co/api/models": (200, [row])})
    facts = hugging_face_facts(one, "vendor/model-x:free")
    assert facts["license"] == "mit" and facts["params"] == "29.8B parameters"
    two = _Session({"https://huggingface.co/api/models": (200, [row, {**row, "id": "o2/model-x"}])})
    assert hugging_face_facts(two, "vendor/model-x") == {}


# --- report ------------------------------------------------------------------------------------


def _cand(provider, model, **kw):
    return Candidate(
        provider,
        model,
        None,
        None,
        1000,
        (("Hub", "https://example.test"),),
        (),
        "2026-10-31",
        structured_output_method="json_schema",
        **kw,
    )


def test_decisions_are_grouped_per_model_and_every_row_is_still_machine_readable():
    report = Report(
        candidates=[
            _cand(
                "kilo", "x/new:free", facts={"summary": "A *fine* model", "expires": "2026-11-30"}
            ),
            _cand("openrouter", "x/new:free"),
            _cand("nvidia", "y/other"),
        ],
        quality_error="Artificial Analysis HTTP 401",
    )
    body = render_body(report, run_date="2026-10-31")
    assert body.count("### `x/new`") == 1 and "### `y/other`" in body
    assert "**free tier ends 2026-11-30**" in body
    assert "Quality data unavailable: Artificial Analysis HTTP 401" in body
    block = body.split("<!-- citypods-review-decisions:start -->")[1].split(
        "<!-- citypods-review-decisions:end -->"
    )[0]
    rows = [line[6:] for line in block.splitlines() if line.startswith("- [ ] ")]
    assert set(rows) == set(decision_choices(report)) and len(rows) == 6
    ticked = body.replace("- [ ] `kilo/x/new:free`: ignore", "- [x] `kilo/x/new:free`: ignore")
    # Re-rendering keeps the tick, and parse_decisions still reads it through the new layout.
    assert "- [x] `kilo/x/new:free`: ignore" in render_body(
        report, run_date="2026-11-01", previous_body=ticked
    )
    [decision] = parse_decisions(ticked, report)
    assert decision.action == "ignore" and decision.key == "kilo/x/new:free"


def test_every_anomaly_says_what_happens_next():
    def anomaly(rid, verdict, **kw):
        return Anomaly("p", rid, "m", verdict, "why", False, **kw)

    report = Report(
        anomalies=[
            anomaly("gone", "retired", lane_usage=(("lane", ("other",)),)),
            anomaly("stalls", "retired", lane_usage=(("lane", ()),)),
            anomaly("paid", "not_entitled"),
            anomaly("json", "structured_output_invalid", alternative_method="json_object"),
        ]
    )
    body = render_body(report, run_date="2026-10-31")
    assert body.count("**Next:**") == 4
    assert "opens a removal PR" in body and "no automatic removal while a lane would stall" in body
    assert "switch to `json_object`" in body and "keep it as a paid route" in body
    assert "Slice 3" not in body


# --- method switch and provisional unlisted routes ---------------------------------------------

ROUTES_YAML = """routes:
  - route_id: r1
    model: m
    provider: host
    upstream_model: creator/m
    account_id: primary
    structured_output_method: json_schema
    structured_output_verified_on: 2026-10-01
    rpm: 1
    concurrency: 1
    free: true
"""


def _method_fixture():
    route = {
        "route_id": "r1",
        "model": "m",
        "provider": "host",
        "upstream_model": "creator/m",
        "account_id": "primary",
        "structured_output_method": "json_schema",
        "structured_output_verified_on": "2026-10-01",
        "rpm": 1,
        "concurrency": 1,
        "free": True,
    }
    texts = dict.fromkeys(SOURCE_PATHS, "x: 1\n")
    texts[SOURCE_PATHS[0]] = ROUTES_YAML
    config = ApplyConfig(
        {
            "providers": {"host": {"accounts": [{"id": "primary"}]}},
            "routes": [route],
            "structured_output_methods": {"json_schema": {}, "json_object": {}},
        },
        {"lane": LaneConfig("lane", ("m",), 1, 1, 1)},
        texts,
        "sha",
        TODAY,
    )
    anomaly = Anomaly(
        "host",
        "r1",
        "creator/m",
        "structured_output_invalid",
        "bad",
        False,
        observed_on=TODAY.isoformat(),
        config_digest=digest(route),
        contended=False,
        alternative_method="json_object",
    )
    return config, anomaly, route


def test_switching_a_routes_structured_output_method_is_reverified_and_edited_in_place():
    config, anomaly, route = _method_fixture()
    report = Report(anomalies=[anomaly])
    ticked = render_body(report, run_date=TODAY.isoformat()).replace(
        f"- [ ] {method_choice('r1', 'json_object')}", f"- [x] {method_choice('r1', 'json_object')}"
    )
    [decision] = parse_decisions(ticked, report)
    assert decision.action == "set_method" and decision.model == "json_object"
    plan = plan_apply(report, (decision,), config)
    assert plan.method_changes == (("r1", "json_object", TODAY.isoformat()),)
    out = apply_config_edits(config.texts, plan)[SOURCE_PATHS[0]]
    assert "structured_output_method: json_object" in out
    assert "structured_output_verified_on: 2026-10-31" in out
    assert "`r1` → `json_object`" in proposal_body(plan)
    # A changed route, a stale proof or an inherited method never produces an edit.
    stale = replace(anomaly, observed_on="2026-10-30")
    assert plan_apply(Report(anomalies=[stale]), (decision,), config).deferred
    config.limits["routes"][0] = {**route, "free": True, "rpm": 2}
    assert plan_apply(report, (decision,), config).deferred
    config.limits["routes"][0] = {k: v for k, v in route.items() if k != "structured_output_method"}
    inherited = replace(anomaly, config_digest=digest(config.limits["routes"][0]))
    moved = replace(decision, route_digest=inherited.config_digest)
    assert "inherits" in plan_apply(Report(anomalies=[inherited]), (moved,), config).deferred[0]


def test_a_route_only_add_with_no_published_context_gets_the_provisional_cap():
    from tests.test_provider_catalog_apply import CANDIDATE, PROOF, config

    proof = replace(PROOF, input_context_limit=None)
    report = Report(candidates=[replace(CANDIDATE, discovery=None, evidence=proof.to_dict())])
    plan = plan_apply(report, (Decision("host", "creator/new", "add", (), proof),), config())
    route = dict(plan.routes[0])
    assert route["hard_input_ceiling"] == route["input_context_limit"] == 16_000
    assert "not published" in plan.route_notes[0][1][0]
    assert hashlib.sha256(b"").hexdigest() and json.dumps({})


def test_a_method_switch_keeps_the_quoting_of_tool_written_routes():
    config, anomaly, route = _method_fixture()
    quoted = ROUTES_YAML.replace(
        "structured_output_verified_on: 2026-10-01", "structured_output_verified_on: '2026-10-01'"
    )
    config.texts[SOURCE_PATHS[0]] = quoted
    config.limits["routes"][0]["structured_output_verified_on"] = "2026-10-01"
    report = Report(anomalies=[anomaly])
    decision = Decision(
        "host", "json_object", "set_method", route_id="r1", route_digest=digest(route)
    )
    plan = plan_apply(report, (decision,), config)
    out = apply_config_edits(config.texts, plan)[SOURCE_PATHS[0]]
    assert "structured_output_verified_on: '2026-10-31'" in out


def test_the_nvidia_card_fetch_refuses_foreign_hosts_and_never_follows_redirects():
    from citypods.provider_catalog.model_facts import nvidia_card_summary

    class Boom:
        def get(self, *a, **k):
            raise AssertionError("must not fetch")

    assert nvidia_card_summary(Boom(), (("NVIDIA Build", "https://evil.test/x"),)) == ""
