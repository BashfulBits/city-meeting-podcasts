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
            req = _fixture_request(city)
            if req is None:
                continue
            url, params, ext = req
            resp = session.get(url, params=params, timeout=DEFAULT_TIMEOUT)
            resp.raise_for_status()
            out = FIXTURE_DIR / city.provider / f"{city.slug}.{ext}"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(resp.content)
            print(f"  wrote {out.relative_to(ROOT)} ({len(resp.content)} bytes)")


def _fixture_request(city):
    """Return (url, params, extension) for recording a city's list response, or None."""
    src = city.source
    if city.provider in ("granicus", "civicplus"):
        if not src.get("feed_url"):
            return None
        return src["feed_url"], None, "xml"
    if city.provider == "civicclerk":
        base = src["api_base"].rstrip("/")
        params = {
            "$filter": "hasMedia eq true",
            "$orderby": "startDateTime desc",
            "$top": str(src.get("max_fetch", 100)),
        }
        return f"{base}/v1/Events", params, "json"
    return None


if __name__ == "__main__":
    main()
