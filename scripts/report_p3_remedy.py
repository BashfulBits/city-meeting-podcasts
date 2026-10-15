#!/usr/bin/env python3
"""Replay recorded P3 votes into a new local report; never call models or publish work."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from citypods.audit_remedy_report import (  # noqa: E402
    build_p3_report,
    load_report_input,
    write_report_exclusive,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Recorded P3 vote JSON")
    parser.add_argument("--out", required=True, type=Path, help="New report JSON path")
    args = parser.parse_args(argv)
    record = load_report_input(args.input)
    # Local replay has no trusted runtime admission context, so all routes fail closed.
    report = build_p3_report(record)
    write_report_exclusive(args.out, report, input_path=args.input)
    print(
        f"Wrote report-only P3 replay for {record.finding_id}: {report['decision']['disposition']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
