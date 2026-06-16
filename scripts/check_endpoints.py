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
from citypods.contracts import CheckResult, check_city, representative_cities

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


def _failure_explanation(result: CheckResult) -> str:
    if result.endpoint == "list":
        return (
            "The provider listing step failed before usable episodes were available. This usually "
            "means the upstream feed/API/page shape changed, the configured source URL is wrong, "
            "or the provider blocked the list request."
        )
    if result.endpoint == "media":
        return (
            "The provider returned episodes, but the code could not resolve the newest episode to "
            "a concrete HTTP media URL for ffmpeg. This points at provider URL-signing, "
            "player-page, or media-resolution logic."
        )
    if result.endpoint == "media-fetch":
        return (
            "The provider returned episodes and media resolution produced a URL, but the "
            "production ffmpeg download path could not copy even a short audio sample. This is "
            "usually a CDN access problem such as HTTP 403/429, an expired/unsigned URL, "
            "User-Agent blocking, or a stalled media response."
        )
    if result.endpoint == "chapters":
        return (
            "The episode list and media URL resolved, but the provider-specific "
            "chapters/transcript endpoint raised while fetching or parsing markers."
        )
    if result.endpoint == "view_counts":
        return (
            "The provider-specific view-count probe failed. For Granicus this is the RSS view "
            "count used to detect the 100-item cap."
        )
    if result.endpoint == "deeplink":
        return (
            "The provider returned an episode, but the generated human watch-page/deeplink no "
            "longer looked reachable or did not match the provider's current player route."
        )
    return "The provider contract check failed for this endpoint."


def _issue_body(result: CheckResult) -> str:
    return (
        f"{MARKER}\n\n"
        f"**Provider:** `{result.provider}`\n"
        f"**Endpoint:** `{result.endpoint}`\n"
        f"**Representative city/feed:** `{result.slug}`\n\n"
        f"**What this means**\n\n{_failure_explanation(result)}\n\n"
        f"**Observed detail**\n\n```\n{result.detail}\n```\n\n"
        "_Filed by check_endpoints.py; auto-closes when it passes._"
    )


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
        f"{TITLE_PREFIX} {r.provider}: {r.endpoint}": _issue_body(r)
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
    ap.add_argument("--site-config", default="config/site_config.yml")
    ap.add_argument("--config-dir", default="config")
    args = ap.parse_args(argv)

    sc = load_site_config(args.site_config)
    cities = load_city_configs(args.config_dir, sc.get("defaults", {}))
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
