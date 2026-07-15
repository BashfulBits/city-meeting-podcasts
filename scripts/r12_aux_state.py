"""Read/write the public auxiliary eligibility state carried by the rolling digest issue."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

MARKER = "citypods:r12:auxiliary-state"


def parse(body: str) -> dict[str, Any]:
    prefix = f"<!-- {MARKER} "
    for line in body.splitlines():
        if not line.startswith(prefix) or not line.endswith(" -->"):
            continue
        try:
            value = json.loads(line[len(prefix) : -4])
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}
    return {}


def marker(state: dict[str, Any]) -> str:
    return f"<!-- {MARKER} {json.dumps(state, sort_keys=True, separators=(',', ':'))} -->"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--body")
    parser.add_argument("--out")
    parser.add_argument("--render-state")
    args = parser.parse_args(argv)
    if args.render_state:
        print(marker(json.loads(Path(args.render_state).read_text())))
        return 0
    if not args.body or not args.out:
        raise SystemExit("--body and --out are required unless --render-state is used")
    Path(args.out).write_text(json.dumps(parse(Path(args.body).read_text()), indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
