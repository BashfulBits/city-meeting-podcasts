#!/usr/bin/env python3
"""Scan one Audio shard log and write redacted GH#353 finding metadata."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from citypods.h16_report import scan_h16_logs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    log_path = Path(args.log)
    output_path = Path(args.output)
    result = scan_h16_logs(log_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
