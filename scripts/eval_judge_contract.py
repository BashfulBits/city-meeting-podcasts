"""Direct, route-by-route judge-contract evaluation on the frozen evals/judge manifest.

No dispatcher, registry, production ledger, or automatic corrective retries. Save requests without
credentials, raw replies, strict contract validation, actual backend parsing, and quote checks.
Run with --live to spend free provider quota; default is a local plan only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from citypods.compute.llm import _lane_reasoning_level
from citypods.compute.llm_policy import (
    ROUTE_REGISTRY,
    route_output_tokens,
    route_reasoning_controls,
    route_request_params,
)
from citypods.compute.structured import response_model
from citypods.compute.structured_shaping import shape_for_route
from citypods.config import load_site_config
from citypods.judging.backends import (
    CHAT_REQUEST_REVISION,
    JUDGE_REQUEST_CONTRACT,
    AdjudicatorBackend,
    ChatJudgeBackend,
    Item,
    Packet,
    choose_options,
    ensure_contract,
    parse_chat_response,
)
from citypods.judging.moment_task import BEST, PUBLISHABLE, SUPPORTED, USEFULNESS
from citypods.judging.quotes import quote_valid
from citypods.judging.tag_task import SUPPORTED as TAG_SUPPORTED
from citypods.judging.tasks import QuestionSpec, Subject, make_evidence

ROOT = Path(__file__).resolve().parents[1]


def frozen_units(manifest):
    """Every frozen item: synthetic claims, T1 tag controls, and six full moment meetings."""
    units = []
    for row in manifest["bundling"]["items"]:
        subject = Subject("tag", f"eval-{row['id']}", "synthetic", None, None, {})
        question = QuestionSpec(
            "supported", "validate", f"Does this evidence support {row['subject']}?"
        )
        units.append((Item(question, "T1", make_evidence("T1", row["text"]), (subject,)),))
    for row in manifest["context_ladder"]["items"]:
        subject = Subject("tag", f"eval-{row['id']}", row["uid"], None, None, {})
        text = f"Proposed tag: {row['label']}: {row['desc']}\nTranscript:\n{row['T1']}"
        units.append((Item(TAG_SUPPORTED, "T1", make_evidence("T1", text), (subject,)),))
    for meeting in manifest["question_types"]["meetings"]:
        subjects = tuple(
            Subject(
                "moment", f"eval-{meeting['uid']}-{i}", meeting["uid"], meeting["uid"], None, row
            )
            for i, row in enumerate(meeting["quotes"])
        )
        items = []
        for subject in subjects:
            row = subject.payload
            text = f"Quote: {row['quote']}\nStated reason: {row['why']}\nTranscript: {row['quote']}"
            for question in (SUPPORTED, PUBLISHABLE, USEFULNESS):
                items.append(Item(question, "T1", make_evidence("T1", text), (subject,)))
        for reverse in (False, True):
            options = choose_options(subjects, reverse=reverse)
            by_id = {s.subject_id: s for s in subjects}
            text = "\n".join(f"{letter}: {by_id[sid].payload['quote']}" for letter, sid in options)
            items.append(
                Item(
                    BEST,
                    "T1",
                    make_evidence("T1", text),
                    (subjects[0],),
                    options=options,
                    order="_r" if reverse else "",
                )
            )
        units.append(tuple(items))
    count = len(manifest["question_types"]["meetings"])
    return units[-count:] + units[:-count]


def routes():
    site = load_site_config(ROOT / "config/site_config.yml")
    roles = {}
    for role in ("sibling", "adjudicator"):
        lane = site["llm_lanes"][f"judge:{role}"]
        for model in [*lane["models"], *lane.get("eligible_models", [])]:
            roles.setdefault(model, set()).add(role)
    selected = {}
    for route in ROUTE_REGISTRY.values():
        if not route.free:
            continue
        for model, model_roles in roles.items():
            if model == route.model or model in route.also_serves:
                for role in sorted(model_roles):
                    selected[(route.route_id, role)] = (route, role)
    return list(selected.values())


def build_packets(manifest, route, role):
    backend = (AdjudicatorBackend if role == "adjudicator" else ChatJudgeBackend)(
        max_total_tokens=4500, max_items=25
    )
    packets, current = [], []
    for unit in frozen_units(manifest):
        trial = Packet(f"judge:{role}", role, route.model, tuple([*current, *unit]))
        job = backend.build(trial)
        # Include the schema and framing in the conservative local budget. A meeting is a unit;
        # both choose orders always travel together.
        size = (
            len(json.dumps(job.inputs["messages"])) / 4
            + len(json.dumps(response_model(JUDGE_REQUEST_CONTRACT).model_json_schema())) / 4
        )
        if current and (size > 4500 or len(trial.items) > 25):
            packets.append(Packet(f"judge:{role}", role, route.model, tuple(current)))
            current = []
        current.extend(unit)
    if current:
        packets.append(Packet(f"judge:{role}", role, route.model, tuple(current)))
    return backend, packets


def inspect_reply(backend, packet, raw):
    result = {
        "expected_items": len(packet.items),
        "schema_valid": False,
        "parsed_items": 0,
        "quote_valid": 0,
        "choose_expected": sum(i.question.kind == "choose" for i in packet.items),
    }
    try:
        content = parse_chat_response(raw)
        typed = response_model(JUDGE_REQUEST_CONTRACT).model_validate(content)
        result["schema_valid"] = True
        expected = {item.qid for item in packet.items}
        result["ids_exact"] = {answer.id for answer in typed.answers} == expected
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        # Never log an exception's arbitrary text: provider bodies can contain request metadata.
        result["validation_error"] = type(exc).__name__
    parsed = backend.parse(packet, raw)
    by_id = {item.question_id + ":" + item.subjects[0].subject_id: item for item in packet.items}
    result["parsed_items"] = len(parsed)
    result["parsed_choose"] = sum(row["kind"] == "choose" for _, row in parsed)
    for subject, row in parsed:
        item = by_id[row["question_id"] + ":" + subject.subject_id]
        result["quote_valid"] += quote_valid(row.get("reason"), item.evidence.text)
    result["parsed_rows"] = [row for _, row in parsed]
    result["contract_pass"] = (
        result["schema_valid"]
        and result.get("ids_exact", False)
        and len(parsed) == len(packet.items)
    )
    result["quotes_all_valid"] = result["quote_valid"] == len(packet.items)
    result["pass"] = result["contract_pass"] and (
        backend.role != "adjudicator" or result["quotes_all_valid"]
    )
    return result


def evaluate_route(route, role, manifest, out, live, packet_limit=None, output_cap=None):
    import requests

    backend, packets = build_packets(manifest, route, role)
    timeout_seconds = json.loads(
        (ROOT / "workers/llm-dispatch-v2/src/dispatch_tuning.json").read_bytes()
    )["values"]["MAX_RESPONSE_SECONDS"]
    if packet_limit:
        packets = packets[:packet_limit]
    directory = out / f"{route.route_id}-{role}"
    directory.mkdir(parents=True, exist_ok=True)
    key = os.environ.get(route.api_key_env, "").strip()
    if live and not key:
        try:
            secret = subprocess.run(
                [
                    "security",
                    "find-generic-password",
                    "-a",
                    os.environ.get("USER", ""),
                    "-s",
                    f"citypods/{route.api_key_env}",
                    "-w",
                ],
                capture_output=True,
                text=True,
            )
            if secret.returncode == 0:
                key = secret.stdout.strip()
        except FileNotFoundError:
            pass  # Non-macOS hosts can evaluate routes with environment credentials.
    summary = {
        "route": route.route_id,
        "model": route.model,
        "role": role,
        "planned_packets": len(packets),
        "read_timeout_seconds": timeout_seconds,
        "results": [],
    }
    stopped = None
    last_end = 0.0
    for index, packet in enumerate(packets):
        job = backend.build(packet)
        messages, response_format = shape_for_route(
            job.inputs["messages"],
            name="judge_answers_v3",
            schema=response_model(JUDGE_REQUEST_CONTRACT).model_json_schema(),
            route=route,
        )
        body = {
            "model": route.upstream_model,
            "messages": messages,
            "max_tokens": route_output_tokens(
                route, job.inputs["max_tokens"], max(1, len(json.dumps(messages)) // 4)
            ),
            **route_request_params(route),
            **route_reasoning_controls(route, _lane_reasoning_level(job, route)),
        }
        if output_cap:
            body["max_tokens"] = min(body["max_tokens"], output_cap)
        if response_format:
            body["response_format"] = response_format
        (directory / f"{index:02d}-request.json").write_text(json.dumps(body, indent=2))
        if not live:
            continue
        if not key or stopped:
            summary["results"].append({"packet": index, "status": stopped or "missing_key"})
            continue
        # Be conservative on the shared BeatAPI window; direct calls have no dispatcher retries.
        delay = max(60 / (route.quota.rpm or 1), 66 if route.provider.startswith("beatapi") else 0)
        while (remaining := last_end + delay - time.monotonic()) > 0:
            time.sleep(min(remaining, 30))
        started = datetime.now(UTC).isoformat()
        raw = {}
        try:
            response = requests.post(
                route.api_base.rstrip("/") + route.chat_path,
                headers={"Authorization": f"Bearer {key}"},
                json=body,
                timeout=(20, timeout_seconds),
            )
            try:
                raw = response.json()
            except ValueError:
                raw = {"non_json_body": response.text[:2000]}
            (directory / f"{index:02d}-response.json").write_text(json.dumps(raw, indent=2))
            result = {
                "packet": index,
                "http_status": response.status_code,
                "started_at": started,
                "finish_reason": (
                    (raw.get("choices") or [{}])[0].get("finish_reason")
                    if isinstance(raw, dict)
                    else None
                ),
            }
            if response.ok:
                result.update(inspect_reply(backend, packet, raw))
            else:
                result["status"] = "transport_error"
                if response.status_code in (400, 401, 403, 404, 429, 503):
                    stopped = f"route_stopped_after_http_{response.status_code}"
        except requests.RequestException as exc:
            result = {"packet": index, "started_at": started, "status": type(exc).__name__}
            stopped = "route_stopped_after_transport_failure"
        last_end = time.monotonic()
        # A large packet can consume an entire rolling TPM window even at a low RPM.
        # Pacing from completion adds headroom for provider arrival-time variation.
        if route.quota.tpm:
            usage = raw.get("usage", {}) if isinstance(raw, dict) else {}
            tokens = usage.get("total_tokens") or max(1, len(json.dumps(messages)) // 4)
            fraction = float(tokens) / route.quota.tpm
            window = 66 if fraction > 0.5 else 66 * fraction
            last_end += max(0, window - delay)
        summary["results"].append(result)
        (directory / "result.json").write_text(json.dumps(summary, indent=2))
        print(
            json.dumps(
                {
                    "route": route.route_id,
                    "packet": index,
                    **{k: v for k, v in result.items() if k not in ("parsed_rows",)},
                }
            ),
            flush=True,
        )
    (directory / "result.json").write_text(json.dumps(summary, indent=2))
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--packet-limit", type=int)
    parser.add_argument("--output-cap", type=int, help="Diagnostic cap; does not edit route config")
    parser.add_argument("--route", action="append", default=[])
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.out.exists() and any(args.out.iterdir()):
        parser.error("Use a new empty output directory; existing evaluation evidence is immutable")
    ensure_contract()
    manifest_path = ROOT / "evals/judge/manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    args.out.mkdir(parents=True, exist_ok=True)
    selected = [(r, role) for r, role in routes() if not args.route or r.route_id in args.route]
    metadata = {
        "request_revision": CHAT_REQUEST_REVISION,
        "contract": JUDGE_REQUEST_CONTRACT,
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "git_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "backend_sha256": hashlib.sha256(
            (ROOT / "citypods/judging/backends.py").read_bytes()
        ).hexdigest(),
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "routes": [r.route_id for r, _ in selected],
        "live": args.live,
        "diagnostic_output_cap": args.output_cap,
    }
    (args.out / "manifest.json").write_text(json.dumps(metadata, indent=2))
    # Serialize routes sharing a provider credential, while independent providers can progress.
    groups = {}
    for route, role in selected:
        groups.setdefault(route.api_key_env, []).append((route, role))

    def group_run(group):
        return [
            evaluate_route(
                r, role, manifest, args.out, args.live, args.packet_limit, args.output_cap
            )
            for r, role in group
        ]

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = [r for group in pool.map(group_run, groups.values()) for r in group]
    counts = Counter(
        "pass" if x.get("pass") else "failed_or_inconclusive" for r in results for x in r["results"]
    )
    (args.out / "summary.json").write_text(json.dumps(dict(counts), indent=2))
    return int(args.live and counts["failed_or_inconclusive"] > 0)


if __name__ == "__main__":
    raise SystemExit(main())
