"""Create a GitHub-safe auxiliary discovery issue body from the full workflow output."""

from __future__ import annotations

import argparse
from pathlib import Path

from citypods.review_issues import SAFE_BODY_LIMIT_BYTES, bounded_body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--artifact-url", required=True)
    args = parser.parse_args(argv)

    body, _ = bounded_body(
        args.input.read_text(encoding="utf-8"),
        limit=SAFE_BODY_LIMIT_BYTES,
        artifact_url=args.artifact_url,
    )
    args.output.write_text(body, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
