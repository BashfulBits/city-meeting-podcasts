"""The direct judge-contract evaluator exercises the real backend, without network calls."""

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "eval_judge_contract", ROOT / "scripts/eval_judge_contract.py"
)
eval_contract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(eval_contract)


def test_frozen_packet_has_all_kinds_and_both_choice_orders():
    manifest = json.loads((ROOT / "evals/judge/manifest.json").read_bytes())
    units = eval_contract.frozen_units(manifest)
    assert len(units) == 30 + 44 + 6
    first = units[0]
    assert {i.question.kind for i in first} == {"validate", "gate", "grade", "choose"}
    choose = [i for i in first if i.question.kind == "choose"]
    assert [i.order for i in choose] == ["", "_r"]
    assert [sid for _, sid in choose[0].options] == [sid for _, sid in choose[1].options][::-1]


def test_evaluator_rejects_missing_choice_and_bad_quote_even_when_json_parses():
    manifest = json.loads((ROOT / "evals/judge/manifest.json").read_bytes())
    route, role = eval_contract.routes()[0]
    backend, packets = eval_contract.build_packets(manifest, route, role)
    packet = packets[0]
    answers = []
    for item in packet.items:
        answer = {
            "id": item.qid,
            "kind": item.question.kind,
            "reason": '"passage that is deliberately fabricated"',
        }
        answer.update(
            {"verdict": True}
            if item.question.kind in ("validate", "gate")
            else {"level": 1}
            if item.question.kind == "grade"
            else {"choice": "A"}
        )
        answers.append(answer)

    def output():
        return {"choices": [{"message": {"content": json.dumps({"answers": answers})}}]}

    result = eval_contract.inspect_reply(backend, packet, output())
    assert result["schema_valid"] and result["parsed_items"] == len(packet.items)
    assert not result["pass"] and result["quote_valid"] == 0
    next(a for a in answers if a["kind"] == "choose").pop("choice")
    result = eval_contract.inspect_reply(backend, packet, output())
    assert not result["schema_valid"] and not result["pass"]
    assert result["parsed_items"] == 0


def test_non_object_provider_error_is_saved_without_crashing(tmp_path, monkeypatch):
    import requests

    route, role = eval_contract.routes()[0]
    monkeypatch.setenv(route.api_key_env, "not-a-real-key")

    class Unavailable:
        ok = False
        status_code = 503

        def json(self):
            return [{"error": {"message": "Temporarily unavailable"}}]

    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: Unavailable())
    manifest = json.loads((ROOT / "evals/judge/manifest.json").read_bytes())
    result = eval_contract.evaluate_route(route, role, manifest, tmp_path, True, 1)
    assert result["results"][0]["status"] == "transport_error"
    request = (tmp_path / route.route_id / "00-request.json").read_text()
    assert "not-a-real-key" not in request
    assert json.loads((tmp_path / route.route_id / "00-response.json").read_text())[0]["error"]


def test_real_gemma_reply_with_a_lone_closing_fence_reaches_the_production_parser():
    manifest = json.loads((ROOT / "evals/judge/manifest.json").read_bytes())
    route = eval_contract.ROUTE_REGISTRY["gemma_4_31b_secondary"]
    backend, packets = eval_contract.build_packets(manifest, route, "sibling")
    raw = json.loads((ROOT / "tests/fixtures/judging/gemma-v3-closing-fence.json").read_bytes())
    result = eval_contract.inspect_reply(backend, packets[1], raw)
    assert result["schema_valid"] and result["parsed_items"] == 14
    assert result["parsed_choose"] == 2 and result["contract_pass"]


def test_judge_fence_normalization_still_rejects_trailing_prose():
    import pytest

    for content in ('{"answers": []} trailing prose```', '{"answers": [```'):
        with pytest.raises(ValueError):
            eval_contract.parse_chat_response({"choices": [{"message": {"content": content}}]})
