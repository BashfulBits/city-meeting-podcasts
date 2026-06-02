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
    site_config = load_site_config(ROOT / "config" / "site_config.yml")
    cities = load_city_configs(ROOT / "config", site_config.get("defaults", {}))

    with make_session() as session:
        for city in cities:
            if city.provider == "swagit":
                content, ext = _swagit_fixture(session, city)
            else:
                req = _fixture_request(city)
                if req is None:
                    continue
                url, params, ext = req
                resp = session.get(url, params=params, timeout=DEFAULT_TIMEOUT)
                resp.raise_for_status()
                content = resp.content
            out = FIXTURE_DIR / city.provider / f"{city.slug}.{ext}"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(content)
            print(f"  wrote {out.relative_to(ROOT)} ({len(content)} bytes)")


def _swagit_fixture(session, city, max_rows: int = 20):
    """The Swagit view page is multi-MB; trim the committed fixture to the matching
    body's first rows so it stays small and deterministic."""
    import re

    from citypods.providers.swagit import ROW_RE

    resp = session.get(city.source["list_url"], timeout=DEFAULT_TIMEOUT)
    resp.raise_for_status()
    text = resp.text
    needle = city.source["body"].lower()
    rows = []
    for m in re.finditer(ROW_RE, text):
        if needle in m.group(2).lower():
            rows.append(m.group(0))
        if len(rows) >= max_rows:
            break
    table = "<table>\n" + "\n".join(f"<tr><td>{r}<td></tr>" for r in rows) + "\n</table>\n"
    return table.encode(), "html"


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
