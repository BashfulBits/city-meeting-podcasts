#!/usr/bin/env python3
"""List the meeting bodies on a Swagit view page, ranked by meeting count.

Use this to choose ``body:`` values when adding Swagit cities (one feed per body):

    python scripts/discover_swagit.py https://dallastx.new.swagit.com/views/default/city-council

Each printed line is a candidate ``body:`` filter (substring-matched by the adapter).
"""

from __future__ import annotations

import collections
import html
import re
import sys

from citypods.http import DEFAULT_TIMEOUT, make_session
from citypods.providers.swagit import ROW_RE


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    list_url = argv[1]
    with make_session() as session:
        resp = session.get(list_url, timeout=DEFAULT_TIMEOUT)
    resp.raise_for_status()
    bodies = collections.Counter(
        html.unescape(b).strip() for _, b, _ in re.findall(ROW_RE, resp.text)
    )
    print(f"{sum(bodies.values())} meetings across {len(bodies)} bodies:\n")
    for body, n in bodies.most_common():
        print(f"  {n:5}  {body}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
