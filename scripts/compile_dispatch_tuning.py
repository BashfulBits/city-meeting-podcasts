#!/usr/bin/env python3
"""Compile ``config/dispatch_tuning.yml`` into the v2 Worker's tuning map.

Writes ``workers/llm-dispatch-v2/src/dispatch_tuning.json``. Deterministic, offline and runnable
with nothing but PyYAML installed -- the same contract as ``scripts/compile_llm_limits.py`` and
``scripts/compile_llm_lanes.py``, whose ``--check`` drift pattern
(``llm-dispatch-v2-worker-deploy.yml``) this mirrors: recompile, and fail the deploy if the
committed JSON differs from what the YAML produces.

Why this exists: these values were Cloudflare ``vars`` in ``wrangler.jsonc``. Workers Free allows 64
variables per Worker, secrets included, and 37 of them were tunables. See the header of the YAML.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
INPUT_YAML = REPO_ROOT / "config" / "dispatch_tuning.yml"
OUTPUT_JSON = REPO_ROOT / "workers" / "llm-dispatch-v2" / "src" / "dispatch_tuning.json"

_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
# Names the Worker treats as credentials or account/URL specifics; none may be committed here.
_FORBIDDEN_SUBSTRINGS = (
    "KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "ENDPOINT",
    "ACCOUNT",
    "URL",
    "GATEWAY",
)


def _display(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def load_tuning(path: Path = INPUT_YAML) -> dict[str, int]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise SystemExit(f"{path} must be a mapping of NAME: number")
    values: dict[str, int] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not _NAME.match(name):
            raise SystemExit(f"{path}: {name!r} is not an UPPER_SNAKE_CASE name")
        if any(part in name for part in _FORBIDDEN_SUBSTRINGS):
            raise SystemExit(
                f"{path}: {name} looks like a credential or account/URL setting; "
                "those stay out of the repo"
            )
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise SystemExit(f"{path}: {name} must be a non-negative integer, got {value!r}")
        values[name] = value
    return values


def render(values: dict[str, int]) -> str:
    compiled = {
        "_generated_by": "scripts/compile_dispatch_tuning.py",
        "_source": "config/dispatch_tuning.yml",
        "_readme": (
            "Do not edit by hand. Defaults for non-secret Worker tuning; a Cloudflare variable of "
            "the same name overrides a value here (src/tuning.js)."
        ),
        "values": dict(sorted(values.items())),
    }
    return json.dumps(compiled, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="Exit non-zero if the committed JSON is stale."
    )
    args = parser.parse_args(argv)
    values = load_tuning()
    rendered = render(values)
    if args.check:
        current = OUTPUT_JSON.read_text(encoding="utf-8") if OUTPUT_JSON.exists() else ""
        if current != rendered:
            print(
                f"::error::{_display(OUTPUT_JSON)} is out of date -- recompile with "
                "'python scripts/compile_dispatch_tuning.py' and commit the YAML and JSON "
                "together.",
                file=sys.stderr,
            )
            return 1
        print(f"{_display(OUTPUT_JSON)} is up to date ({len(values)} values)")
        return 0
    OUTPUT_JSON.write_text(rendered, encoding="utf-8")
    print(f"wrote {_display(OUTPUT_JSON)}: {len(values)} values")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
