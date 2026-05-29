#!/usr/bin/env python3
"""Record live provider responses into tests/fixtures/ for offline tests.

Run intentionally (not in CI) when you want to update the recorded feeds:

    python scripts/refresh_fixtures.py

Snapshot tests compare generated output against committed goldens built from
these frozen fixtures, so re-recording will surface as a snapshot diff to review.
"""

from __future__ import annotations

from pathlib import Path

from citypods.config import load_city_configs, load_site_config
from citypods.http import DEFAULT_TIMEOUT, make_session

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = ROOT / "tests" / "fixtures"


def main() -> None:
    site_config = load_site_config(ROOT / "site_config.yml")
    cities = load_city_configs(ROOT / "cities", site_config.get("defaults", {}))

    with make_session() as session:
        for city in cities:
            # Both Granicus and CivicPlus expose the episode list as an RSS feed_url.
            url = city.source.get("feed_url")
            if not url:
                continue
            resp = session.get(url, timeout=DEFAULT_TIMEOUT)
            resp.raise_for_status()
            out = FIXTURE_DIR / city.provider / f"{city.slug}.xml"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(resp.content)
            print(f"  wrote {out.relative_to(ROOT)} ({len(resp.content)} bytes)")


if __name__ == "__main__":
    main()
