"""Run and score the open-weight decision evaluation (review/60 Phase 3).

    python -m scripts.open_weight_eval run-api perplexity --split dev --out ANSWERS.json
    python -m scripts.open_weight_eval run-api gemini --split dev --out ANSWERS.json
    python -m scripts.open_weight_eval score --split dev --reference perplexity.json \
        --answers karpo-9b-fp8=a.json,b.json --out SUMMARY.json

``run-api`` reads only ``manifest.json``. API keys come from the environment (run it under
``citypods-env``) and are never printed or written. ``score`` is the only command that reads
``gold.json``. Answer documents are ``{"model", "split", "answers": {key: {p, status, tokens,
request, seconds}}}``; a model's lanes may be spread over several files and are merged.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from citypods.judging import open_weight_eval as ev

OPENROUTER_URL = "https://openrouter.ai/api/alpha/decisions"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/decisions"
API = {
    "perplexity": {
        "model": "perplexity/pplx-decider-v1.3:free",
        "label": "perplexity/pplx-decider-v1.3",
        "url": OPENROUTER_URL,
        "env": "OPENROUTER_API_KEY",
        "pace": 7.0,  # the route is rpm 10 in production config; stay under it
    },
    "gemini": {
        "model": "gemini-decision-1",
        "label": "gemini/gemini-decision-1",
        "url": GEMINI_URL,
        "env": "GEMINI_API_KEY",
        "pace": 4.0,
    },
}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the key header must not leave the configured origin."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _post(spec: dict[str, Any], key: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """(status, payload); a transport failure is status 0, an unreadable body a non-dict error."""
    headers = {"Content-Type": "application/json"}
    if spec["env"] == "GEMINI_API_KEY":
        headers["x-goog-api-key"] = key
    else:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(spec["url"], json.dumps(body).encode(), headers)
    try:
        with _OPENER.open(req, timeout=120) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as err:
        status, raw = err.code, err.read()[:2000]
    except (urllib.error.URLError, TimeoutError, ConnectionError) as err:
        return 0, {"error": type(err).__name__}
    try:
        payload = json.loads(raw)
    except ValueError:
        return (status if status != 200 else 0), {"error": "unreadable body"}
    if not isinstance(payload, dict):
        return (status if status != 200 else 0), {"error": "unexpected body"}
    return status, payload


def _write_atomic(path: Path, doc: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, sort_keys=True))
    os.replace(tmp, path)


def _probability(entry: Any) -> float | None:
    """A yes probability in [0, 1] from a ``noul`` answer, else None."""
    if not isinstance(entry, dict):
        return None
    value = entry.get("noul")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        p = float(value)
    except OverflowError:
        return None
    return p if 0.0 <= p <= 1.0 else None


def _wire(name: str, request: ev.Request) -> dict[str, Any]:
    if name == "perplexity":
        folded = ev.fold_for_openrouter(request)
        return {"state": folded["state"], "questions": folded["questions"]}
    # The production Worker sends an empty state as a fixed sentence and keeps the evidence in the
    # question; a shared state (a chapter) goes in `input` as text.
    if len(request["questions"]) == 1:
        return {
            "input": "Answer each question using only the evidence given in that question's "
            "instructions.",
            "questions": ev.fold_for_openrouter(request)["questions"],
        }
    return {"input": request["state"], "questions": request["questions"]}


def run_api(args: argparse.Namespace) -> int:
    spec = API[args.provider]
    key = os.environ.get(spec["env"], "")
    if not key:
        print(f"{spec['env']} is not set", file=sys.stderr)
        return 2
    manifest = ev.load_manifest()
    lanes = set(args.lanes.split(","))
    requests = {
        k: r
        for k, r in ev.build_requests(manifest, args.split, chapter_mode=args.chapter_mode).items()
        if k.split("/")[0] in lanes
    }
    out_path = Path(args.out)
    doc: dict[str, Any] = {
        "model": spec["label"],
        "provider_model": spec["model"],
        "split": args.split,
        "chapter_mode": args.chapter_mode,
        "answers": {},
        "failures": [],
        "wall_seconds": 0.0,
    }
    if out_path.exists():  # resume, but only from this run's own file
        prior = json.loads(out_path.read_text())
        if (prior.get("model"), prior.get("split"), prior.get("chapter_mode")) != (
            doc["model"],
            args.split,
            args.chapter_mode,
        ):
            print(f"{out_path} belongs to another run; refusing to resume", file=sys.stderr)
            return 2
        doc = prior
    # a request is done only when every one of its answers is valid; others are asked again
    by_request: dict[str, list[bool]] = {}
    for k, v in doc["answers"].items():
        by_request.setdefault(v.get("request", k), []).append(v.get("status") == "ok")
    done = {r for r, oks in by_request.items() if all(oks)}
    doc["failures"] = [
        f for f in doc["failures"] if doc["answers"].get(f["key"], {}).get("request") in done
    ]
    started = time.monotonic()
    prior_wall = float(doc.get("wall_seconds") or 0.0)
    for n, (rkey, request) in enumerate(requests.items(), 1):
        if rkey in done:
            continue
        body = {"model": spec["model"], **_wire(args.provider, request)}
        status, payload = 0, {}
        seconds = 0.0
        for attempt in range(5):
            t0 = time.monotonic()
            status, payload = _post(spec, key, body)
            seconds = time.monotonic() - t0
            if status == 200 or status in (400, 404, 413, 422):
                break
            time.sleep(min(60, 10 * 2**attempt) if status in (429, 0) else 5 * (attempt + 1))
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        tokens = usage.get("input_tokens", usage.get("total_input_tokens"))
        answers = payload.get("answers") if status == 200 else None
        if not isinstance(answers, dict):
            answers = {}
        for akey in ev.answer_keys(rkey, request):
            qid = akey.rsplit("/", 1)[-1] if rkey.startswith("chapters/") else "supported"
            p = _probability(answers.get(qid))
            record: dict[str, Any] = {"request": rkey, "seconds": round(seconds, 2)}
            if isinstance(tokens, int) and not isinstance(tokens, bool):
                record["tokens"] = tokens
            if p is not None:
                record.update(p=p, status="ok")
            else:
                record.update(p=None, status=f"http_{status}" if status != 200 else "missing")
                doc["failures"].append({"key": akey, "status": status})
            doc["answers"][akey] = record
        if n % 10 == 0 or n == len(requests):
            doc["wall_seconds"] = round(prior_wall + time.monotonic() - started, 1)
            _write_atomic(out_path, doc)
            print(f"{n}/{len(requests)} requests, {time.monotonic() - started:.0f}s", flush=True)
        time.sleep(spec["pace"])
    if time.monotonic() - started > 1.0 or not done:
        doc["wall_seconds"] = round(prior_wall + time.monotonic() - started, 1)
    _write_atomic(out_path, doc)
    return 0


def export_local(args: argparse.Namespace) -> int:
    """Inputs for the local screen runners (review/evidence/*-local-screen-run.py): one run per
    request, each ``{"state", "questions"}``."""
    lanes = set(args.lanes.split(","))
    requests = {
        k: r
        for k, r in ev.build_requests(ev.load_manifest(), args.split).items()
        if k.split("/")[0] in lanes
    }
    Path(args.out).write_text(json.dumps({"runs": requests}))
    print(f"{len(requests)} runs")
    return 0


def import_local(args: argparse.Namespace) -> int:
    """Turn a local screen runner's result into an answers document."""
    result = json.loads(Path(args.result).read_text())
    answers: dict[str, Any] = {}
    manifest_requests = ev.build_requests(ev.load_manifest(), args.split)
    for rkey, run in result["runs"].items():
        request = manifest_requests[rkey]
        per_second = run["seconds"] / max(1, len(run["answers"]) + run["invalid"])
        for akey in ev.answer_keys(rkey, request):
            qid = akey.rsplit("/", 1)[-1] if rkey.startswith("chapters/") else "supported"
            p = run["answers"].get(qid)
            answers[akey] = {
                "request": rkey,
                "seconds": round(per_second, 3),
                "p": p,
                "status": "ok" if p is not None else "invalid",
            }
    doc = {
        "model": args.name,
        "split": args.split,
        "chapter_mode": "grouped",
        "answers": answers,
        "telemetry": {k: v for k, v in result.items() if k != "runs"},
        "run_seconds": {k: v["seconds"] for k, v in result["runs"].items()},
    }
    Path(args.out).write_text(json.dumps(doc, sort_keys=True))
    return 0


def merge_answers(paths: list[str], split: str | None = None) -> dict[str, dict[str, Any]]:
    """Merge answer files; with ``split`` set, a file from another split is an error."""
    merged: dict[str, dict[str, Any]] = {}
    for path in paths:
        doc = json.loads(Path(path).read_text())
        if split not in (None, "all") and doc.get("split") != split:
            raise SystemExit(f"{path} is for split {doc.get('split')!r}, not {split!r}")
        merged.update(doc["answers"])
    return merged


def score(args: argparse.Namespace) -> int:
    manifest, gold, reference = ev.load_manifest(), ev.load_gold(), ev.load_reference()
    ref_answers = merge_answers(args.reference.split(","), args.split) if args.reference else None
    summary: dict[str, Any] = {"split": args.split, "models": {}}
    models = dict(m.split("=", 1) for m in args.answers)
    for name, paths in models.items():
        answers = merge_answers(paths.split(","), args.split)
        entry: dict[str, Any] = {}
        for lane in ev.LANES:
            rows = ev.gold_rows(lane, manifest, gold, reference, args.split)
            if not any(r["key"] in answers for r in rows):
                continue
            is_ref = ref_answers is not None and answers is ref_answers
            entry[lane] = ev.score_lane(
                lane, rows, answers, None if is_ref else ref_answers, reps=args.reps
            )
            if lane == "chapters":
                entry[lane]["tokens_per_tag"] = ev.tokens_per_tag(answers, len(rows))
        summary["models"][name] = entry
    Path(args.out).write_text(json.dumps(summary, indent=1, sort_keys=True))
    print(json.dumps(summary, indent=1, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    api = sub.add_parser("run-api")
    api.add_argument("provider", choices=sorted(API))
    api.add_argument("--split", choices=ev.SPLITS, required=True)
    api.add_argument("--lanes", default="tags,moments,chapters")
    api.add_argument("--chapter-mode", choices=("grouped", "single"), default="grouped")
    api.add_argument("--out", required=True)
    exp = sub.add_parser("export-local")
    exp.add_argument("--split", choices=ev.SPLITS, required=True)
    exp.add_argument("--lanes", default="tags,moments,chapters")
    exp.add_argument("--out", required=True)
    imp = sub.add_parser("import-local")
    imp.add_argument("--split", choices=ev.SPLITS, required=True)
    imp.add_argument("--result", required=True)
    imp.add_argument("--name", required=True)
    imp.add_argument("--out", required=True)
    sc = sub.add_parser("score")
    sc.add_argument("--split", choices=ev.SCORE_SPLITS, required=True)
    sc.add_argument("--reference", help="comma-separated answer files of the reference model")
    sc.add_argument("--answers", nargs="+", required=True, help="NAME=file[,file...]")
    sc.add_argument("--reps", type=int, default=2000)
    sc.add_argument("--out", required=True)
    args = parser.parse_args()
    return {
        "run-api": run_api,
        "score": score,
        "export-local": export_local,
        "import-local": import_local,
    }[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
