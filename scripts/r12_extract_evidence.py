"""Extract the exact hidden R12 evidence artifact from a GitHub issue/comment body."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from citypods.discovery.render import parse_evidence_marker


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--body", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    evidence = parse_evidence_marker(Path(args.body).read_text())
    if evidence is None:
        raise SystemExit("no R12 evidence marker found")
    Path(args.out).write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
