#!/usr/bin/env python3
"""Merge completed private model-arm runs into the reviewed answer key."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ANSWER_KEY = HERE / "answer-key.json"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def merge(run_paths: list[Path], *, answer_key_path: Path = ANSWER_KEY) -> int:
    answer_key = read_json(answer_key_path)
    cases = answer_key["cases"]
    case_ids_by_uid: dict[str, list[str]] = {}
    for case_id, case in cases.items():
        case_ids_by_uid.setdefault(str(case["episode_uid"]), []).append(case_id)

    merged = 0
    for run_path in run_paths:
        run = read_json(run_path)
        for result in run.get("results", []):
            uid = str(result["uid"])
            if uid not in case_ids_by_uid:
                raise ValueError(f"run UID {uid} is missing from the manual answer key")
            arm = {
                "model": run["model"],
                "provider": run["provider"],
                "route_id": run["route_id"],
                "prompt": run["prompt"],
                "valid": bool(result.get("valid")),
                "elapsed_seconds": result.get("elapsed_seconds"),
                "input_estimate_tokens": result.get("input_estimate_tokens"),
                "provider_prompt_tokens": result.get("prompt_tokens"),
                "completion_tokens": result.get("completion_tokens"),
                "error": result.get("error"),
                "anchors": result.get("anchors", []),
                "private_telemetry_dir": run.get("raw_telemetry_dir"),
            }
            for case_id in case_ids_by_uid[uid]:
                arms = cases[case_id].setdefault("model_arms", [])
                matching = [
                    index
                    for index, existing in enumerate(arms)
                    if existing.get("model") == arm["model"]
                    and existing.get("prompt") == arm["prompt"]
                    and existing.get("route_id") == arm["route_id"]
                ]
                if matching:
                    arms[matching[-1]] = arm
                else:
                    arms.append(arm)
                merged += 1

    write_json(answer_key_path, answer_key)
    return merged


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", nargs="+", type=Path, help="Private results.json run artifacts")
    parser.add_argument("--answer-key", type=Path, default=ANSWER_KEY)
    args = parser.parse_args()
    merged = merge(args.runs, answer_key_path=args.answer_key)
    print(f"Merged {merged} case/model-arm rows into {args.answer_key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
