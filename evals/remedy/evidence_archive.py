"""Retain unique experiment evidence; rebuild disposable packets without network or inference."""

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PAIRS = ROOT / "evals/remedy/results/accepted-pairs-2026-10-24"


def read(path):
    return json.loads(Path(path).read_text())


def digest(value):
    return hashlib.sha256(value).hexdigest()


def line(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def save_rows(path, header, collection, rows):
    """One independently reviewable observation/source per line, without repeated profiles."""
    text = json.dumps(header, indent=2)[:-2]
    with Path(path).open("x") as stream:
        stream.write(text + ',\n  "' + collection + '": [\n')
        stream.write(",\n".join("    " + line(row) for row in rows))
        stream.write("\n  ]\n}\n")


def load_observations(path):
    """Read original ledgers or normalized answers with shared physical request profiles."""
    saved = read(path)
    if saved.get("format") != "remedy-observations-v1":
        return saved
    rows = []
    for row in saved["observations"]:
        profile = saved["profiles"][row["profile"]]
        rows.append({**profile, **{k: v for k, v in row.items() if k != "profile"}})
    return {**saved["metadata"], "observed_results": rows}


def archive_observations(source, destination):
    """Discard derived scoring views; retain every actual answer and its original provenance."""
    saved = read(source)
    profiles, rows = {}, []
    fields = {
        "route_id",
        "provider",
        "requested_model",
        "output_cap",
        "transport",
        "timeout_seconds",
    }
    for original in saved["observed_results"]:
        row = dict(original)
        profile = {key: row.pop(key) for key in sorted(fields) if key in row}
        key = digest(line(profile).encode())[:16]
        if key in profiles and profiles[key] != profile:
            raise ValueError("profile digest collision")
        profiles[key] = profile
        rows.append({"profile": key, **row})
    derived = {
        "observed_results",
        "case_outcomes",
        "summary",
        "cumulative_summary",
        "unattempted_cases",
        "partial_case",
    }
    metadata = {key: value for key, value in saved.items() if key not in derived}
    metadata["original_artifact_sha256"] = digest(Path(source).read_bytes())
    save_rows(
        destination,
        {"format": "remedy-observations-v1", "metadata": metadata, "profiles": profiles},
        "observations",
        rows,
    )
    if load_observations(destination)["observed_results"] != saved["observed_results"]:
        raise ValueError("normalization changed model evidence")


def delta(base, value):
    if isinstance(base, dict) and isinstance(value, dict):
        return {
            "map": {
                key: delta(base[key], item) if key in base else {"value": item}
                for key, item in value.items()
                if key not in base or base[key] != item
            },
            "delete": [key for key in base if key not in value],
        }
    if isinstance(base, list) and isinstance(value, list):
        return {
            "list": {
                str(i): delta(base[i], value[i])
                for i in range(min(len(base), len(value)))
                if base[i] != value[i]
            },
            "length": len(value),
            "append": value[len(base) :],
        }
    return {"value": value}


def apply_delta(base, change):
    if "value" in change:
        return change["value"]
    if "map" in change:
        result = {key: value for key, value in base.items() if key not in change["delete"]}
        for key, value in change["map"].items():
            result[key] = apply_delta(base.get(key), value)
        return result
    result = list(base[: change["length"]])
    for index, value in change["list"].items():
        result[int(index)] = apply_delta(base[int(index)], value)
    return result + change["append"]


def cases_by_id():
    from evals.remedy.accepted_pairs import load_manifest

    manifest, cases = load_manifest()
    return manifest, {case.case_id: case.model_dump(mode="json") for case in cases}


def archive_packets(destination):
    """Factor packets into the original cohort, observed proposals and unique retrieved evidence."""
    _, cases = cases_by_id()
    evidence = read(PAIRS / "evidence.json")["initial"]
    approval = read(PAIRS / "adjudication-approval.json")
    unique, packets = {}, {}
    for pair, ref in approval["packets"].items():
        original = ROOT / ref["path"]
        packet, entries = read(original), []
        for entry in packet["cases"]:
            cid = entry["case"]["case_id"]
            source = {
                "case_id": cid,
                "changes": delta(cases[cid], entry["case"]),
                **{key: value for key, value in entry.items() if key not in {"case", "proposals"}},
            }
            key = digest(line(source).encode())
            unique[key] = source
            proposals = {}
            for letter, answer in entry["proposals"].items():
                matches = [
                    f"{model}/{cid}"
                    for model in pair.split("-")
                    if evidence.get(f"{model}/{cid}", {}).get("answer") == answer
                ]
                if len(matches) != 1:
                    raise ValueError("proposal does not uniquely bind retained model evidence")
                proposals[letter] = matches[0]
            entries.append({"source": key, "proposals": proposals})
        packets[pair] = {
            "path": ref["path"],
            "sha256": digest(original.read_bytes()),
            "header": {key: value for key, value in packet.items() if key != "cases"},
            "entries": entries,
        }
    save_rows(
        destination,
        {"version": 1, "packets": packets},
        "sources",
        [{"id": key, **value} for key, value in unique.items()],
    )
    for pair, payload in load_packets(destination).items():
        if digest(packet_bytes(payload)) != packets[pair]["sha256"]:
            raise ValueError("rebuilt packet bytes differ from original approved packet")


def packet_bytes(packet):
    return (json.dumps(packet, indent=2, sort_keys=True) + "\n").encode()


def load_packets(path=None):
    archive = read(path or PAIRS / "judging-inputs.json")
    _, cases = cases_by_id()
    answers = read(PAIRS / "evidence.json")["initial"]
    sources = {row["id"]: row for row in archive["sources"]}
    packets = {}
    approved = read(PAIRS / "adjudication-approval.json")["packets"]
    for pair, spec in archive["packets"].items():
        if spec["sha256"] != approved[pair]["sha256"] or spec["path"] != approved[pair]["path"]:
            raise ValueError("archive changed the original packet approval binding")
        relative = Path(spec["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("packet path must stay repository-relative")
        entries = []
        for row in spec["entries"]:
            source = sources[row["source"]]
            entries.append(
                {
                    "case": apply_delta(cases[source["case_id"]], source["changes"]),
                    **{
                        key: value
                        for key, value in source.items()
                        if key not in {"id", "case_id", "changes"}
                    },
                    "proposals": {
                        letter: answers[ref]["answer"] for letter, ref in row["proposals"].items()
                    },
                }
            )
        packet = {**spec["header"], "cases": entries}
        if digest(packet_bytes(packet)) != spec["sha256"]:
            raise ValueError("retained inputs no longer reproduce the approved packet")
        packets[pair] = packet
    return packets


def rebuild_packets(output):
    archive = read(PAIRS / "judging-inputs.json")
    output.mkdir(parents=True, exist_ok=False)
    for pair, packet in load_packets().items():
        path = output / archive["packets"][pair]["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(packet_bytes(packet))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["packets", "archive-packets", "archive-observations"])
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "packets":
        rebuild_packets(args.output)
    elif args.command == "archive-packets":
        archive_packets(args.output)
    else:
        if args.source is None:
            parser.error("archive-observations requires --source")
        archive_observations(args.source, args.output)
