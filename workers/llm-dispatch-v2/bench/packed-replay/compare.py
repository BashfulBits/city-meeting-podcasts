#!/usr/bin/env python3
"""Compare a control replay with a candidate replay: identical selections and audited state."""

import json
import sys
from pathlib import Path


def main():
    control, candidate = (json.loads(Path(path).read_text()) for path in sys.argv[1:3])
    errors = []
    if len(control["ticks"]) != len(candidate["ticks"]):
        errors.append("tick count differs")
    if len(control["audits"]) != len(candidate["audits"]):
        errors.append("audit count differs")
    for left, right in zip(control["ticks"], candidate["ticks"], strict=False):
        if left["selection"] != right["selection"]:
            errors.append(f"tick {left['tick']}: selection differs")
    for left, right in zip(control["audits"], candidate["audits"], strict=False):
        for key in ("jobs", "routes", "estimates", "usage", "failures", "providers"):
            if left["hashes"][key] != right["hashes"][key]:
                errors.append(f"audit at tick {left['tick']}: {key} differs")
    for key in ("jobs", "routes", "estimates", "usage", "failures", "providers"):
        if control["final"]["hashes"][key] != candidate["final"]["hashes"][key]:
            errors.append(f"final {key} differs")
    if candidate["final"]["counts"]["leased"] != candidate["final"]["counts"]["registry"]:
        errors.append("lease registry count differs from leased jobs")
    print(
        json.dumps(
            {"control": control["summary"], "candidate": candidate["summary"], "errors": errors},
            indent=1,
        )
    )
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
