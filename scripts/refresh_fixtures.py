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
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "granicus"


def main() -> None:
    site_config = load_site_config(ROOT / "site_config.yml")
    cities = load_city_configs(ROOT / "cities", site_config.get("defaults", {}))
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)

    with make_session() as session:
        for city in cities:
            if city.provider != "granicus":
                continue
            url = city.source["feed_url"]
            resp = session.get(url, timeout=DEFAULT_TIMEOUT)
            resp.raise_for_status()
            out = FIXTURE_DIR / f"{city.slug}.xml"
            out.write_bytes(resp.content)
            print(f"  wrote {out.relative_to(ROOT)} ({len(resp.content)} bytes)")


if __name__ == "__main__":
    main()
