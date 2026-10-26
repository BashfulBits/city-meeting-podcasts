"""Prepare new model inputs and rescore compact remedy evidence without provider access."""

import argparse
import hashlib
import itertools
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "evals/remedy/results"
DEFAULT = RESULTS / "accepted-pairs-2026-10-24/evidence.json"
sys.path.insert(0, str(RESULTS / "body-selection-2026-10-13"))
from body_selection import (  # noqa: E402
    PROMPT,
    BodySelectionAnswer,
    load_manifest,
    messages,
    score_answer,
    validate_answer,
)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def passed(answer, gold):
    verdict = score_answer(answer, gold)
    return verdict.get("correct", verdict.get("guardrail_pass", False))


def signature(answer):
    return answer.publication_action, tuple(sorted(answer.feed_slugs))


def compact(destination):
    """One-time extraction from the retained local archive; never overwrites its sources."""
    index = read(RESULTS / "glm-full-2026-10-24/all-pairs-final.json")
    fields = {
        "case_id",
        "answer",
        "status",
        "requested_model",
        "returned_model",
        "identity_matches",
        "upstream_model",
        "upstream_model_requested",
        "identity_held",
        "serving_family_held",
        "route_id",
        "output_token_cap",
        "request_timeout",
        "reasoning",
        "schema_error",
        "http_status",
        "elapsed_seconds",
        "end_to_end_seconds",
        "started_at",
        "finished_at",
        "pair",
        "role",
        "parsed",
    }

    def extract(path):
        row = read(path)
        result = {k: v for k, v in row.items() if k in fields}
        result.update(source_path=str(path.relative_to(ROOT)), source_sha256=digest(path))
        intent = path.parent.parent / "intents" / path.name
        if intent.exists():
            result["output_token_cap"] = read(intent).get("request", {}).get("max_tokens")
        raw = row.get("raw_response", {})
        if isinstance(raw, dict):
            if raw.get("usage") is not None:
                result["usage"] = {
                    k: v
                    for k, v in raw["usage"].items()
                    if k.endswith("_tokens") and isinstance(v, (int, float))
                }
            result["transport"] = "streaming" if "stream_events" in raw else "nonstream"
            events = raw.get("stream_events", [])
            choices = raw.get("choices", []) or [c for e in events for c in e.get("choices", [])]
            reasons = [c["finish_reason"] for c in choices if c.get("finish_reason")]
            if reasons:
                result["finish_reason"] = reasons[-1]
            if row.get("schema_error") and reasons and reasons[-1] == "stop":
                result["rejected_model_content"] = "".join(
                    c.get("message", c.get("delta", {})).get("content", "") or "" for c in choices
                )
        return result

    for ref in index["selected_sources"].values():
        assert digest(ROOT / ref["path"]) == ref["sha256"], "Original source changed"
    initial = {key: extract(ROOT / ref["path"]) for key, ref in index["selected_sources"].items()}
    base = RESULTS / "accepted-pairs-2026-10-24"
    judges = [extract(p) for p in sorted((base / "adjudication").glob("*-result.json"))]
    attempts = []
    for provider, folder in (
        ("beatapi", RESULTS / "glm-full-2026-10-24"),
        ("nvidia", RESULTS / "glm-full-2026-10-24/nvidia"),
        ("beatapi", RESULTS / "gemini-replacement-2026-10-24"),
    ):
        for path in sorted((folder / "results").glob("*.json")):
            row = extract(path)
            row.pop("answer", None)  # Selected answers above; completed invalid content retained.
            row["provider"] = provider
            attempts.append(row)
    gold = ROOT / "evals/remedy/holdout/body-selection-2026-10-13/gold.json"
    payload = {
        "version": 1,
        "scope": "Normalized model outputs and attempt metadata, not raw transport receipts. "
        "Source SHA256 values identify local originals; normalized bytes have different hashes.",
        "manifest_sha256": digest(
            ROOT / "evals/remedy/holdout/body-selection-2026-10-13/manifest.json"
        ),
        "gold_sha256": digest(gold),
        "prompt_sha256": hashlib.sha256(PROMPT.encode()).hexdigest(),
        "schema_sha256": hashlib.sha256(
            json.dumps(BodySelectionAnswer.model_json_schema(), sort_keys=True).encode()
        ).hexdigest(),
        "initial": initial,
        "judgments": judges,
        "attempts": attempts,
    }

    # One observed outcome per line keeps the evidence diff readable without transport noise.
    def encode(value):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    sections = []
    for key, value in payload.items():
        if key == "initial":
            encoded = (
                "{\n"
                + ",\n".join("    " + encode(k) + ":" + encode(v) for k, v in value.items())
                + "\n  }"
            )
        elif isinstance(value, list):
            encoded = "[\n" + ",\n".join("    " + encode(v) for v in value) + "\n  ]"
        else:
            encoded = encode(value)
        sections.append("  " + encode(key) + ":" + encoded)
    with destination.open("x") as stream:
        stream.write("{\n" + ",\n".join(sections) + "\n}\n")


def score(evidence, additional=()):
    manifest, cases = load_manifest()
    cases = {c.case_id: c for c in cases}
    gold_path = ROOT / "evals/remedy/holdout/body-selection-2026-10-13/gold.json"
    assert evidence["gold_sha256"] == digest(gold_path), "Gold changed"
    assert evidence["manifest_sha256"] == digest(
        ROOT / "evals/remedy/holdout/body-selection-2026-10-13/manifest.json"
    ), "Frozen cohort changed"
    assert evidence["prompt_sha256"] == hashlib.sha256(PROMPT.encode()).hexdigest()
    assert (
        evidence["schema_sha256"]
        == hashlib.sha256(
            json.dumps(BodySelectionAnswer.model_json_schema(), sort_keys=True).encode()
        ).hexdigest()
    )
    gold = read(gold_path)["cases"]
    models = {}
    for key, row in evidence["initial"].items():
        model, cid = key.split("/", 1)
        models.setdefault(model, {})[cid] = validate_answer(row["answer"], cases[cid])
    for model, rows in additional:
        if model in models:
            raise ValueError("use a distinct model/profile label; never overwrite old answers")
        models[model] = {}
        for row in rows:
            cid = row["case_id"]
            if cid in models[model]:
                raise ValueError("duplicate additional-model case")
            models[model][cid] = validate_answer(row["answer"], cases[cid])
    pairs = []
    for left, right in itertools.combinations(sorted(models), 2):
        ids = sorted(set(models[left]) & set(models[right]))
        counts = Counter(correct_agreement=0, wrong_agreement=0, disagreement=0)
        wrong = []
        for cid in ids:
            a, b = models[left][cid], models[right][cid]
            outcome = "disagreement"
            if signature(a) == signature(b):
                outcome = "correct_agreement" if passed(a, gold[cid]) else "wrong_agreement"
            counts[outcome] += 1
            if outcome == "wrong_agreement":
                wrong.append(cid)
        pairs.append(
            dict(
                models=[left, right],
                answered=len(ids),
                holds=60 - len(ids),
                counts=counts,
                wrong_agreement_case_ids=wrong,
            )
        )
    base = RESULTS / "accepted-pairs-2026-10-24"
    approval = read(base / "adjudication-approval.json")
    cascades = []
    for pair, roles in approval["judge_mapping"].items():
        packet = read(ROOT / approval["packets"][pair]["path"])
        chat = next(r for r in roles if r != "jev")
        counts = Counter()
        for entry in packet["cases"]:
            cid = entry["case"]["case_id"]
            rows = {
                r["role"]: r
                for r in evidence["judgments"]
                if r["pair"] == pair and r["case_id"] == cid
            }
            if set(rows) != set(roles):
                raise ValueError("missing or unexpected judgment role")
            if any(not r.get("identity_matches") or r.get("schema_error") for r in rows.values()):
                counts["manual_identity_or_contract_hold"] += 1
                continue
            answer = validate_answer(rows[chat]["answer"], cases[cid].model_validate(entry["case"]))
            choice = next(v["value"] for v in rows["jev"]["parsed"] if v["question_id"] == "choice")
            if choice not in {"A", "B"} or signature(answer) != signature(
                BodySelectionAnswer.model_validate(entry["proposals"][choice])
            ):
                counts["manual_disagreement_or_abstention"] += 1
            else:
                counts[
                    "correct_resolution" if passed(answer, gold[cid]) else "wrong_resolution"
                ] += 1
        cascades.append(dict(pair=pair, counts=counts))
    return dict(
        cohort=len(manifest["cases"]),
        coverage={k: len(v) for k, v in models.items()},
        pairs=pairs,
        cascades=cascades,
    )


def materialize(evidence, destination):
    """Recreate disposable scorer inputs in a new tree, using normalized receipt bytes."""
    destination.mkdir(parents=True, exist_ok=False)
    sources = {}
    for key, row in evidence["initial"].items():
        relative = Path("evals/remedy/results/accepted-pairs-2026-10-24/normalized") / (
            key + ".json"
        )
        path = destination / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(row, indent=2) + "\n")
        sources[key] = dict(path=str(relative), sha256=digest(path))
    base = destination / "evals/remedy/results"
    index = base / "glm-full-2026-10-24/all-pairs-final.json"
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text(json.dumps(dict(selected_sources=sources), indent=2) + "\n")
    for row in evidence["judgments"]:
        path = base / "accepted-pairs-2026-10-24/adjudication" / Path(row["source_path"]).name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(row, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    archive = sub.add_parser("compact", help="extract retained local originals once; no network")
    archive.add_argument("--output", type=Path, required=True)
    replay = sub.add_parser("score", help="offline original-gold pairs and judge resolutions")
    replay.add_argument("--evidence", type=Path, default=DEFAULT)
    replay.add_argument("--additional-model", action="append", default=[], metavar="LABEL=JSON")
    restore = sub.add_parser("materialize", help="rebuild disposable historical scorer inputs")
    restore.add_argument("--evidence", type=Path, default=DEFAULT)
    restore.add_argument("--output-dir", type=Path, required=True)
    prepare = sub.add_parser("prepare", help="generate original-prompt inputs; never execute")
    prepare.add_argument("--model", required=True)
    prepare.add_argument("--output-dir", type=Path, required=True)
    prepare.add_argument("--output-cap", type=int, choices=[16384, 32768], default=16384)
    args = parser.parse_args()
    if args.command == "compact":
        compact(args.output)
    elif args.command == "materialize":
        materialize(read(args.evidence), args.output_dir)
    elif args.command == "score":
        extra = [s.split("=", 1) for s in args.additional_model]
        print(json.dumps(score(read(args.evidence), [(m, read(p)) for m, p in extra]), indent=2))
    else:
        args.output_dir.mkdir(parents=True, exist_ok=False)
        refs = []
        for case in load_manifest()[1]:
            payload = dict(
                case_id=case.case_id,
                upstream_model=args.model,
                messages=messages(case),
                response_schema=BodySelectionAnswer.model_json_schema(),
                max_tokens=args.output_cap,
                reasoning="provider-default",
                temperature_override=None,
                timeout_seconds=720,
                client_retries=0,
            )
            path = args.output_dir / (case.case_id + ".json")
            path.write_text(json.dumps(payload, indent=2) + "\n")
            refs.append(dict(path=str(path), sha256=digest(path)))
        (args.output_dir / "manifest.json").write_text(json.dumps(dict(requests=refs), indent=2))


if __name__ == "__main__":
    main()
