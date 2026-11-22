#!/usr/bin/env python
"""Reset video slide detection backoff so the Slides run retries selected records.

Why this exists: the first runs of the Slides workflow failed on Granicus archive videos
due to HTTP 403 Forbidden responses before the Cloudflare worker proxy fallback was wired up.
Each failure recorded attempts in ``slides`` and marked the stage complete with failed output.

This script resets failed slide attempts on matching records so they are retried immediately
instead of being skipped as permanently failed.

Filters (optional; combine freely):
  * ``--provider granicus`` (repeatable) — only sources whose city provider matches.
  * ``--source <key>``     (repeatable) — only these exact source keys.
  * ``--uid <uid>``        (repeatable) — only these exact record UIDs.
  * ``--reason <reason>``  (repeatable) — only records with these failure reasons.
  * ``--include-completed``             — also reset completed records (ready / none-found).

Safe by default:
  * **dry-run unless ``--apply``** — prints exactly what it would reset and changes nothing;
  * pulls durable state first and pushes back only the affected ``sources/<key>/`` prefixes;
  * refuses to run if no records match.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from citypods.config import load_city_configs, load_site_config
from citypods.records import load_records, save_records, source_key
from citypods.state import resolve_state_dir
from citypods.statesync import pull_state, push_state
from citypods.storage import make_storage


def _is_completed(slides: dict) -> bool:
    return slides.get("status") in {"ready", "none-found"}


def _in_backoff(slides: dict) -> bool:
    return slides.get("status") == "failed" or int(slides.get("attempts") or 0) > 0


def _uid(rec: dict, fallback: str) -> str:
    return str(rec.get("uid") or fallback)


def _clean_filters(values: list[str], *, lower: bool = False) -> set[str] | None:
    cleaned = {v.strip().lower() if lower else v.strip() for v in values}
    cleaned.discard("")
    return cleaned or None


def reset_slides_backoff_records(
    records: dict,
    *,
    uids: set[str] | None = None,
    reasons: set[str] | None = None,
    include_completed: bool = False,
) -> int:
    """Clear slide failure state on matching records in ``records`` (mutates in place)."""
    reset = 0
    for key, rec in records.items():
        if not isinstance(rec, dict):
            continue
        slides = rec.get("slides") or {}
        if not include_completed and _is_completed(slides):
            continue
        if not include_completed and not _in_backoff(slides):
            continue
        if uids is not None and _uid(rec, str(key)) not in uids:
            continue
        if reasons is not None and str(slides.get("reason") or "") not in reasons:
            continue
        rec["slides"] = {}
        if isinstance(rec.get("stage_completion"), dict):
            rec["stage_completion"].pop("slides", None)
        reset += 1
    return reset


def select_sources(
    state_dir: Path,
    source_to_provider: dict[str, str],
    *,
    providers: set[str] | None,
    sources: set[str] | None,
) -> tuple[list[str], int]:
    """Return selected on-disk sources and the total available source count."""
    on_disk = sorted(p.parent.name for p in Path(state_dir).glob("sources/*/episodes.json"))
    selected = []
    for key in on_disk:
        if sources is not None and key not in sources:
            continue
        if providers is not None and source_to_provider.get(key) not in providers:
            continue
        selected.append(key)
    return selected, len(on_disk)


def reset_slides_backoff(
    state_dir: Path,
    source_keys: list[str],
    *,
    apply: bool = False,
    uids: set[str] | None = None,
    reasons: set[str] | None = None,
    include_completed: bool = False,
) -> dict:
    """Reset the slides backoff for each selected source; return summary.

    ``apply=False`` writes nothing.
    """
    reset_total = 0
    touched: set[str] = set()
    per_source: dict[str, int] = {}
    for key in source_keys:
        records = load_records(state_dir, key)
        if not records:
            continue
        n = reset_slides_backoff_records(
            records,
            uids=uids,
            reasons=reasons,
            include_completed=include_completed,
        )
        if n:
            per_source[key] = n
            reset_total += n
            touched.add(key)
            if apply:
                save_records(state_dir, key, records)
    return {"reset": reset_total, "touched_sources": touched, "per_source": per_source}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--provider", action="append", default=[], help="only this provider (repeatable)"
    )
    ap.add_argument(
        "--source", action="append", default=[], help="only this source key (repeatable)"
    )
    ap.add_argument("--uid", action="append", default=[], help="only this record UID (repeatable)")
    ap.add_argument(
        "--reason",
        action="append",
        default=[],
        help="only this failure reason (repeatable)",
    )
    ap.add_argument(
        "--include-completed",
        action="store_true",
        help="also reset completed (ready / none-found) records",
    )
    ap.add_argument("--apply", action="store_true", help="actually mutate state (default: dry-run)")
    ap.add_argument("--site-config", default="config/site_config.yml")
    ap.add_argument("--config-dir", default="config")
    ap.add_argument("--output-dir", default="docs")
    args = ap.parse_args(argv)

    site_config = load_site_config(args.site_config)
    cities = load_city_configs(args.config_dir, site_config.get("defaults", {}))
    source_to_provider = {source_key(c): c.provider for c in cities}

    storage = make_storage(site_config, "https://example.invalid", args.output_dir)
    state_dir = resolve_state_dir(site_config, args.output_dir)

    if args.apply and storage is None:
        print(
            "--apply requires a configured storage backend "
            "(would mutate local state with no durable push)"
        )
        return 1

    if storage is not None:
        pulled = pull_state(storage, state_dir)
        print(f"state: pulled {pulled} file(s) from durable storage")

    providers = _clean_filters(args.provider, lower=True)
    sources = _clean_filters(args.source)
    uids = _clean_filters(args.uid)
    reasons = _clean_filters(args.reason)
    selected, available_sources = select_sources(
        state_dir, source_to_provider, providers=providers, sources=sources
    )
    if not selected:
        print("no sources matched the given filters; nothing to scan")
        return 1

    summary = reset_slides_backoff(
        state_dir,
        selected,
        apply=args.apply,
        uids=uids,
        reasons=reasons,
        include_completed=args.include_completed,
    )
    if not summary["reset"]:
        print(f"scanned {len(selected)} source(s); no matching records found")
        return 1

    print(f"scanned {len(selected)} source(s); slides state to reset:")
    for key, n in sorted(summary["per_source"].items()):
        provider = source_to_provider.get(key, "?")
        print(f"  {key} ({provider}): {n} record(s)")

    if args.apply and storage is not None and summary["touched_sources"]:
        prefixes = [f"sources/{sk}/" for sk in sorted(summary["touched_sources"])]
        pushed = push_state(storage, state_dir, only_prefixes=prefixes)
        print(f"state: pushed {pushed} file(s) back (scoped to {len(prefixes)} source(s))")

    verb = "reset" if args.apply else "would reset (dry-run)"
    print(
        f"\n{summary['reset']} record(s) {verb}"
        + ("" if args.apply else "  — re-run with --apply to make changes")
    )
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
