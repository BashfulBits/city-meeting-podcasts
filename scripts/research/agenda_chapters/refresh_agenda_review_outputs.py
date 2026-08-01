#!/usr/bin/env python
"""Replace a local review packet's anonymous item sets from completed shadow outputs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-data", type=Path, required=True)
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--outputs-dir", type=Path, required=True)
    parser.add_argument("--write", type=Path, required=True)
    args = parser.parse_args(argv)

    key_by_id = {entry["adjudication_id"]: entry for entry in json.loads(args.key.read_text())}
    packet = json.loads(args.review_data.read_text())
    for meeting in packet["meetings"]:
        mapping = key_by_id[meeting["adjudication_id"]]["label_to_model"]
        for title_set in meeting["generated_title_sets"]:
            model_directory = mapping[title_set["label"]]
            uid = key_by_id[meeting["adjudication_id"]]["episode"][2]
            slug = key_by_id[meeting["adjudication_id"]]["episode"][1]
            output_path = args.outputs_dir / model_directory / f"{slug}--{uid}.json"
            output = json.loads(output_path.read_text())
            if output.get("status") != "completed":
                raise RuntimeError(f"incomplete revised output: {output_path}")
            title_set["items"] = [
                {
                    "title": item["title"],
                    "evidence_quote": item["evidence_quote"],
                    "line_start": item["line_start"],
                    "line_end": item["line_end"],
                }
                for item in output["items"]
            ]
    args.write.parent.mkdir(parents=True, exist_ok=True)
    args.write.write_text(json.dumps(packet, indent=2) + "\n")
    print(f"refreshed {len(packet['meetings'])} anonymous meetings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
