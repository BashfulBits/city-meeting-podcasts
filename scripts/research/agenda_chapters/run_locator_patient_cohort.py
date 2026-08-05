#!/usr/bin/env python
"""Run a slow, resumable one-at-a-time locator cohort.

This research-only wrapper isolates each model request in a child process.  It is useful for
routes whose HTTP response can take several minutes: completed episode results are persisted after
each child exits, and an interrupted run can resume without repeating completed UIDs.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def _load_uids(packets_path: Path) -> list[str]:
    payload = json.loads(packets_path.read_text(encoding="utf-8"))
    return [str(packet["uid"]) for packet in payload.get("packets", [])]


def _write(
    output_path: Path, *, model: str, uids: list[str], results: list[dict[str, Any]]
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(
            {
                "version": 1,
                "model": model,
                "prompt_variant": "full-transcript-with-merged-calibration-hints",
                "request_start_spacing_seconds": None,
                "patient_wrapper": True,
                "selected_uids": uids,
                "results": results,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packets", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--write", type=Path, required=True)
    parser.add_argument("--locator-model", required=True)
    parser.add_argument("--delay-seconds", type=float, default=45.0)
    parser.add_argument("--max-attempts", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=float, default=0.0)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)

    uids = _load_uids(args.packets)
    results: list[dict[str, Any]] = []
    if args.resume and args.write.exists():
        previous = json.loads(args.write.read_text(encoding="utf-8"))
        results = [result for result in previous.get("results", []) if result.get("uid") in uids]
    completed = {str(result.get("uid")) for result in results}

    for position, uid in enumerate(uids, start=1):
        if uid in completed:
            print(json.dumps({"index": position, "uid": uid, "status": "reused"}), flush=True)
            continue
        child_write = args.write.with_suffix(f".{uid}.json")
        command = [
            sys.executable,
            "-m",
            "scripts.research.agenda_chapters.run_locator_packet_shadow",
            "--packets",
            str(args.packets),
            "--manifest",
            str(args.manifest),
            "--write",
            str(child_write),
            "--locator-model",
            args.locator_model,
            "--routes",
            "full",
            "--hint-style",
            "merged",
            "--calibration-prompt",
            "--max-attempts",
            str(args.max_attempts),
            "--uid",
            uid,
        ]
        if args.cache_dir:
            command.extend(["--cache-dir", str(args.cache_dir)])
        if args.timeout_seconds > 0:
            command.extend(["--timeout-seconds", str(args.timeout_seconds)])
        try:
            completed_process = subprocess.run(command, check=False)
            if child_write.exists():
                child = json.loads(child_write.read_text(encoding="utf-8"))
                child_results = child.get("results") or []
                if child_results:
                    results.extend(child_results)
                else:
                    results.append({"uid": uid, "status": "failed", "error": "empty child result"})
            elif completed_process.returncode:
                results.append(
                    {
                        "uid": uid,
                        "status": "failed",
                        "error": f"child exited {completed_process.returncode}",
                    }
                )
        except KeyboardInterrupt:
            _write(args.write, model=args.locator_model, uids=uids, results=results)
            raise
        _write(args.write, model=args.locator_model, uids=uids, results=results)
        print(
            json.dumps(
                {
                    "index": position,
                    "uid": uid,
                    "status": results[-1].get("status") if results else "unknown",
                    "completed": len(results),
                }
            ),
            flush=True,
        )
        if position != len(uids) and args.delay_seconds > 0:
            time.sleep(args.delay_seconds)
    _write(args.write, model=args.locator_model, uids=uids, results=results)
    print(json.dumps({"completed": len(results), "total": len(uids)}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
