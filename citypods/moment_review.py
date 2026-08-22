"""Small authenticated-review adapter for R6's immutable calibration ledger."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from citypods.moment_evaluation import load_state, record_review, save_state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="citypods r6-review")
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--label", choices=("Good", "Borderline", "Reject"), required=True)
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--review-id", required=True)
    parser.add_argument("--start", type=float)
    parser.add_argument("--end", type=float)
    parser.add_argument("--title")
    parser.add_argument("--caption")
    parser.add_argument("--crop-anchor", help="JSON object with normalized x/y crop anchor")
    parser.add_argument("--composition", dest="output_profile")
    args = parser.parse_args(argv)
    candidate = json.loads(args.candidate.read_text())
    if not isinstance(candidate, dict):
        raise SystemExit("--candidate must contain one JSON candidate object")
    state = load_state(args.state)
    overrides = {
        key: value
        for key, value in {
            "start": args.start,
            "end": args.end,
            "title": args.title,
            "caption": args.caption,
            "output_profile": args.output_profile,
        }.items()
        if value is not None
    }
    if args.crop_anchor:
        overrides["crop_anchor"] = json.loads(args.crop_anchor)
    record_review(
        state,
        candidate,
        args.label,
        reviewer=args.reviewer,
        review_id=args.review_id,
        overrides=overrides,
    )
    save_state(args.state, state)
    return 0


__all__ = ["main"]
