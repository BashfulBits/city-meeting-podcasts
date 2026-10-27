import json

import pytest

from scripts import score_judge_pairs as scorer


def test_pair_requires_two_positive_decisions():
    result = scorer.score_pair(
        {"yes": True, "bad": True, "missing": None, "no": False},
        {"yes": True, "bad": True, "missing": True, "no": True},
        {"yes": True, "bad": False, "missing": True, "no": False},
    )
    assert result["true_accepts"] == 1
    assert result["false_accept_ids"] == ["bad"]
    assert result["human_review"] == 2
    assert result["false_accept_rate"] == 0.5


def test_unknown_labels_excluded_and_same_model_not_paired():
    gold = {
        "bundling": {"a": True},
        "context_ladder": {"controls": ["b"], "adjudicated": {"c": None}},
    }
    observations = [
        {"id": "a", "model": "same", "decisions": {}},
        {"id": "b", "model": "same", "decisions": {}},
    ]
    assert len(scorer.labeled_truth(gold)) == 2
    assert scorer.compare(observations, gold)["pairs"] == []
    assert scorer.interval(0, 0) is None
    assert scorer.interval(0, 35)[1] == pytest.approx(0.09890099)


@pytest.mark.parametrize("contract_pass", [False, True])
def test_replay_calls_actual_inspection_boundary(tmp_path, monkeypatch, contract_pass):
    manifest = json.loads((scorer.ROOT / "evals/judge/manifest.json").read_text())
    item = scorer.evaluator.frozen_units(manifest)[0][0]
    folder = tmp_path / "route"
    folder.mkdir()
    (folder / "result.json").write_text(
        json.dumps(
            {
                "role": "sibling",
                "model": "google/gemma-4-31b-it",
                "route": "route",
                "results": [{"packet": 0, "http_status": 200}],
            }
        )
    )
    (folder / "00-request.json").write_text(
        json.dumps({"messages": [{"content": json.dumps({"items": [{"id": item.qid}]})}]})
    )
    (folder / "00-response.json").write_text('{"raw": true}')
    calls = []

    def inspect(backend, packet, raw):
        calls.append((packet, raw))
        return {
            "contract_pass": contract_pass,
            "parsed_rows": [
                {
                    "kind": "validate",
                    "question_id": "supported",
                    "subject_id": item.subjects[0].subject_id,
                    "value": True,
                    "reason": "No usable quote",
                }
            ],
        }

    monkeypatch.setattr(scorer.evaluator, "inspect_reply", inspect)
    monkeypatch.setattr(scorer, "quote_valid", lambda reason, evidence: False)
    result = scorer.replay(tmp_path, manifest)
    assert calls[0][1] == {"raw": True}
    subject_id = item.subjects[0].subject_id
    assert result[0]["decisions"] == {subject_id: True if contract_pass else None}
    assert result[0]["evidence_valid"] == ({subject_id: False} if contract_pass else {})


def test_archive_replays_first_response_without_extracting_paths(tmp_path, monkeypatch):
    import io
    import tarfile

    manifest = json.loads((scorer.ROOT / "evals/judge/manifest.json").read_text())
    item = scorer.evaluator.frozen_units(manifest)[0][0]
    route = "nvidia_gemma_4_31b_it_free"
    packet = {
        "request": {"messages": [{"content": json.dumps({"items": [{"id": item.qid}]})}]},
        "response": {"raw": "first"},
    }
    prefix = "evals/judge/results/2026-10-20-typed-chat-contract-evidence/"
    entries = {
        prefix + route + ".json": {"packets": [packet]},
        prefix + "http200-retries/" + route + ".json": {
            "route": route,
            "role": "sibling",
            "retry_attempts": [],
            "first_http200": [
                {
                    "packet": 0,
                    "source": "initial",
                    "actual_route": "actual_nvidia",
                    "credential_env_used": "PRIMARY_KEY_ENV",
                }
            ],
        },
    }
    archive = tmp_path / "raw.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        for name, value in entries.items():
            raw = json.dumps(value).encode()
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            bundle.addfile(info, io.BytesIO(raw))
    seen = []

    def inspect(backend, packet, raw):
        seen.append(raw)
        return {"contract_pass": False, "parsed_rows": []}

    monkeypatch.setattr(scorer.evaluator, "inspect_reply", inspect)
    observations = scorer.replay_archive(archive, manifest)
    assert seen == [{"raw": "first"}]
    assert observations[0]["actual_routes"] == ["actual_nvidia"]
    assert observations[0]["actual_credentials"] == ["PRIMARY_KEY_ENV"]
    assert observations[0]["decisions"][item.subjects[0].subject_id] is None
