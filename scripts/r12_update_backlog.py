"""Apply approved unsupported-provider dispositions to the canonical R12 tracker."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from citypods.discovery.backlog import (
    assign_provider,
    dump_pending_providers,
    load_pending_providers,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tracker", default="config/discovery/pending-providers.yml")
    parser.add_argument("--records", required=True)
    args = parser.parse_args(argv)
    records = json.loads(Path(args.records).read_text())
    if not isinstance(records, list):
        raise SystemExit("backlog records must be a list")
    tracker = load_pending_providers(args.tracker)
    for record in records:
        if not isinstance(record, dict):
            continue
        tracker = assign_provider(tracker, **record)
    Path(args.tracker).write_text(dump_pending_providers(tracker))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
