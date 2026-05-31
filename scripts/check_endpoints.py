#!/usr/bin/env python
"""Probe live provider endpoints and report a PASS/FAIL matrix; optionally file GitHub issues.

Complements the feed-health audit: that checks feed *output* (empty/stale/dead enclosures); this
checks the *input* contracts (does the provider's list/media/chapters endpoint still return the
shape we parse?). When a platform changes, this names the exact broken endpoint.

    PYTHONPATH=. python scripts/check_endpoints.py             # representatives (1 per provider)
    PYTHONPATH=. python scripts/check_endpoints.py --all       # every configured city
    PYTHONPATH=. python scripts/check_endpoints.py --issues    # file/close GitHub issues (CI)

Exit code is nonzero if any check failed (so the workflow goes red).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

from citypods.config import load_city_configs, load_site_config
from citypods.contracts import check_city, representative_cities

TITLE_PREFIX = "[endpoint]"
MARKER = "<!-- citypods:endpoint-contract -->"


def _gh(*args: str, check: bool = True) -> str:
    proc = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if check and proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def _open_issues() -> dict[str, dict]:
    out = _gh(
        "issue",
        "list",
        "--label",
        "endpoint-contract",
        "--state",
        "open",
        "--json",
        "number,title,body",
        "--limit",
        "500",
    )
    return {i["title"]: i for i in json.loads(out or "[]") if i["title"].startswith(TITLE_PREFIX)}


def _reconcile_issues(failures: list) -> None:
    _gh(
        "label",
        "create",
        "endpoint-contract",
        "--color",
        "B60205",
        "--description",
        "A provider endpoint contract broke",
        "--force",
        check=False,
    )
    wanted = {
        f"{TITLE_PREFIX} {r.provider}: {r.endpoint}": (
            f"{MARKER}\n\n**{r.provider}** endpoint `{r.endpoint}` failed on `{r.slug}`:\n\n"
            f"```\n{r.detail}\n```\n\n_Filed by check_endpoints.py; auto-closes when it passes._"
        )
        for r in failures
    }
    existing = _open_issues()
    for title, body in wanted.items():
        if title in existing:
            if existing[title].get("body", "").strip() != body.strip():
                _gh("issue", "edit", str(existing[title]["number"]), "--body", body)
        else:
            _gh("issue", "create", "--title", title, "--body", body, "--label", "endpoint-contract")
    for title, issue in existing.items():
        if title not in wanted:
            _gh(
                "issue",
                "close",
                str(issue["number"]),
                "--comment",
                "✅ Endpoint contract passing again.",
            )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--all", action="store_true", help="scan every city (default: 1 per provider)")
    ap.add_argument("--issues", action="store_true", help="reconcile findings to GitHub issues")
    ap.add_argument("--site-config", default="site_config.yml")
    ap.add_argument("--cities-dir", default="cities")
    args = ap.parse_args(argv)

    sc = load_site_config(args.site_config)
    cities = load_city_configs(args.cities_dir, sc.get("defaults", {}))
    targets = cities if args.all else representative_cities(cities)

    results = []
    for c in targets:
        results.extend(check_city(c.slug, c.provider, c.source))

    for r in results:
        mark = "PASS" if r.ok else "FAIL"
        print(f"  {mark}  {r.provider:10} {r.endpoint:12} {r.slug:28} {r.detail}")
    failures = [r for r in results if not r.ok]
    print(f"\n{len(results)} checks, {len(failures)} failing across {len(targets)} city(ies).")

    if args.issues:
        _reconcile_issues(failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
