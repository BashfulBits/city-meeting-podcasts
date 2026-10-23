"""Score distinct-model pairs against frozen labels, offline; never change the gold labels.

Accept compact observations or a new eval_judge_contract.py output directory. For the latter,
replay saved HTTP-200 responses through today's schema/parser before scoring. Failed or missing
answers abstain. These are selection-set metrics, not held-out or population guarantees.
"""

import argparse
import hashlib
import itertools
import json
import math
import tarfile
import tempfile
from pathlib import Path

from scripts import eval_judge_contract as evaluator

ROOT = Path(__file__).resolve().parents[1]


def labeled_truth(gold):
    truth = {f"eval-{key}": value for key, value in gold["bundling"].items()}
    truth.update({f"eval-{key}": False for key in gold["context_ladder"]["controls"]})
    truth.update(
        {f"eval-{key}": value for key, value in gold["context_ladder"]["adjudicated"].items()}
    )
    return {key: value for key, value in truth.items() if value is not None}


def interval(successes, total):
    if not total:
        return None
    z = 1.959963984540054
    rate = successes / total
    divisor = 1 + z * z / total
    middle = (rate + z * z / (2 * total)) / divisor
    half = z * math.sqrt((rate * (1 - rate) + z * z / (4 * total)) / total) / divisor
    return [max(0, middle - half), min(1, middle + half)]


def score_pair(left, right, truth):
    accepted = [key for key in truth if left.get(key) is True and right.get(key) is True]
    false_ids = [key for key in accepted if truth[key] is False]
    negatives = sum(value is False for value in truth.values())
    positives = len(truth) - negatives
    return {
        "items": len(truth),
        "positives": positives,
        "negatives": negatives,
        "accepted": len(accepted),
        "true_accepts": len(accepted) - len(false_ids),
        "false_accepts": len(false_ids),
        "false_accept_ids": false_ids,
        "human_review": len(truth) - len(accepted),
        "false_accept_rate": len(false_ids) / negatives if negatives else None,
        "false_accept_wilson95": interval(len(false_ids), negatives),
    }


def compare(observations, gold):
    truth = labeled_truth(gold)
    pairs = []
    for left, right in itertools.combinations(observations, 2):
        if left["model"] == right["model"]:
            continue
        pairs.append(
            {
                "left": left["id"],
                "right": right["id"],
                **score_pair(left["decisions"], right["decisions"], truth),
            }
        )
    return {
        "policy": "Two distinct logical models must both answer true; otherwise human review.",
        "limits": "Selection-set Wilson intervals; not held-out or population guarantees.",
        "label_provenance": gold["context_ladder"].get("adjudicated_provenance"),
        "pairs": pairs,
    }


def replay(directory, manifest):
    evaluator.ensure_contract()
    items = {item.qid: item for unit in evaluator.frozen_units(manifest) for item in unit}
    observations = []
    for path in sorted(directory.glob("*/result.json")):
        summary = json.loads(path.read_text())
        decisions = {}
        hashes = []
        actual_routes = set()
        actual_credentials = set()
        for result in summary["results"]:
            if result.get("http_status") != 200:
                continue
            actual_routes.add(result.get("actual_route", summary["route"]))
            credential = result.get("credential_env_used")
            if credential:
                actual_credentials.add(credential)
            index = result["packet"]
            request_path = path.parent / f"{index:02d}-request.json"
            response_path = path.parent / f"{index:02d}-response.json"
            request = json.loads(request_path.read_text())
            submitted = json.loads(request["messages"][-1]["content"])["items"]
            packet = evaluator.Packet(
                f"judge:{summary['role']}",
                summary["role"],
                summary["model"],
                tuple(items[item["id"]] for item in submitted),
            )
            backend = evaluator.ChatJudgeBackend(max_total_tokens=4500, max_items=25)
            evaluation = evaluator.inspect_reply(
                backend, packet, json.loads(response_path.read_text())
            )
            hashes.append(hashlib.sha256(response_path.read_bytes()).hexdigest())
            for item in packet.items:
                if item.question.kind == "validate" and item.question_id == "supported":
                    subject = item.subjects[0].subject_id
                    if subject in decisions:
                        raise ValueError(f"Duplicate scored subject: {subject}")
                    decisions[subject] = None
            if evaluation["contract_pass"]:
                for row in evaluation["parsed_rows"]:
                    if row["kind"] == "validate" and row["question_id"] == "supported":
                        decisions[row["subject_id"]] = row["value"]
        observations.append(
            {
                "id": path.parent.name,
                "model": summary["model"],
                "route": summary["route"],
                "decisions": decisions,
                "response_sha256": hashes,
                "actual_routes": sorted(actual_routes),
                "actual_credentials": sorted(actual_credentials),
            }
        )
    return observations


def replay_archive(archive, manifest):
    """Read the external task archive without extracting untrusted paths."""
    with tarfile.open(archive) as bundle, tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        prefix = "evals/judge/results/2026-10-20-typed-chat-contract-evidence/"
        files = {member.name: member for member in bundle.getmembers() if member.isfile()}

        def load(name):
            return json.load(bundle.extractfile(files[name]))

        for name in files:
            if Path(name).name.startswith("._"):
                continue
            if not name.startswith(prefix + "http200-retries/") or not name.endswith(".json"):
                continue
            data = load(name)
            if "first_http200" not in data:
                continue
            route = data["route"]
            if Path(route).name != route:
                raise ValueError("Invalid route path in archive")
            folder = root / route
            folder.mkdir()
            initial = load(prefix + route + ".json")
            history = {entry["attempt_file"]: entry for entry in data["retry_attempts"]}
            results = []
            for entry in data["first_http200"]:
                index = entry["packet"]
                if not isinstance(index, int) or index < 0:
                    raise ValueError("Invalid packet index")
                source = (
                    initial["packets"][index]
                    if entry["source"] == "initial"
                    else history[entry["source"]]
                )
                for kind in ("request", "response"):
                    (folder / f"{index:02d}-{kind}.json").write_text(json.dumps(source[kind]))
                results.append(
                    {
                        "packet": index,
                        "http_status": 200,
                        "actual_route": entry.get("actual_route", route),
                        "credential_env_used": entry.get("credential_env_used"),
                    }
                )
            model = evaluator.ROUTE_REGISTRY[route].model
            (folder / "result.json").write_text(
                json.dumps(
                    {
                        "role": data["role"],
                        "model": model,
                        "route": route,
                        "results": results,
                    }
                )
            )
        return replay(root, manifest)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--archive", type=Path, help="External raw-evidence tar.gz to re-parse")
    inputs.add_argument("--observations", type=Path, help="Compact saved decisions JSON")
    inputs.add_argument("--results", type=Path, help="Saved direct harness output to re-parse")
    parser.add_argument("--gold", type=Path, default=ROOT / "evals/judge/gold.json")
    parser.add_argument("--manifest", type=Path, default=ROOT / "evals/judge/manifest.json")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.out.exists():
        parser.error("Output already exists; preserve previous scoring results")
    if args.observations:
        observations = json.loads(args.observations.read_text())["observations"]
    elif args.archive:
        observations = replay_archive(args.archive, json.loads(args.manifest.read_text()))
    else:
        observations = replay(args.results, json.loads(args.manifest.read_text()))
    if not observations:
        parser.error("No observations found; check the input path and format")
    result = compare(observations, json.loads(args.gold.read_text()))
    result["gold_sha256"] = hashlib.sha256(args.gold.read_bytes()).hexdigest()
    result["manifest_sha256"] = hashlib.sha256(args.manifest.read_bytes()).hexdigest()
    result["observations"] = observations
    args.out.write_text(json.dumps(result, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
