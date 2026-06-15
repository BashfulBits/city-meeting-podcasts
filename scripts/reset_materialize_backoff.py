#!/usr/bin/env python
"""Reset the #120 materialization backoff on un-hosted records so the next Audio run retries them.

Why this exists (and why ``clear_run_materializations.py`` can't do it): the first sharded ``Audio``
runs failed to host audio for reasons since fixed — Granicus User-Agent 403s (#293/#297) and Swagit
truncation (#274/#39). Each failure incremented the record's ``materialize_attempts`` (the #120
exponential backoff), so those episodes are now SKIPPED for up to 30 days even though the bug is
fixed. Granicus in particular has *never* successfully hosted, so its records have no
``audio encode done`` line for ``clear_run_materializations.py`` to match — that tool only undoes
*hosted* materializations, leaving the errored-into-backoff records stuck.

What it does, per record that is NOT hosted (no audio url/key) but IS in backoff (attempts > 0):
  * clears the backoff fields (``attempts`` → 0, ``last_attempt`` → None, ``error`` → None) so the
    next ``audio.yml`` re-attempts the encode immediately instead of waiting out the backoff window.

It never touches a hosted record (its audio is fine), the transcript block, or the durable
``state/`` snapshot — only the audio backoff bookkeeping of episodes that are stuck-and-unhosted.

Filters (optional; combine freely):
  * ``--provider granicus`` (repeatable) — only sources whose city provider matches.
  * ``--source <key>``     (repeatable) — only these exact source keys.
  With neither, every un-hosted in-backoff record across the catalog is reset (drains the whole
  bug-induced backlog at once).

Safe by default:
  * **dry-run unless ``--apply``** — prints exactly what it would reset and changes nothing;
  * pulls the durable state first and pushes back only the affected ``sources/<key>/`` prefixes
    (never reconciles, so it can't sweep a sibling shard's records);
  * refuses to run if no un-hosted in-backoff records match (so a no-op can't look like success).

Usage:
    # Dry-run: what backoff would be reset for Granicus (no changes):
    PYTHONPATH=. python scripts/reset_materialize_backoff.py --provider granicus

    # Actually reset it (needs B2_* / storage creds):
    PYTHONPATH=. python scripts/reset_materialize_backoff.py --provider granicus --apply

    # Drain every bug-induced backoff across all providers:
    PYTHONPATH=. python scripts/reset_materialize_backoff.py --apply
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from citypods.config import load_city_configs, load_site_config
from citypods.records import load_records, save_records, source_key
from citypods.state import resolve_state_dir
from citypods.statesync import pull_state, push_state
from citypods.storage import make_storage

# Backoff fields cleared to force a clean re-attempt (mirrors the audio block in episode_to_record).
_RESET_BACKOFF = {"attempts": 0, "last_attempt": None, "error": None}


def _is_hosted(audio: dict) -> bool:
    return bool(audio.get("url") or audio.get("key"))


def _in_backoff(audio: dict) -> bool:
    """A record that recorded a failed materialization attempt (#120). ``last_attempt`` may be unset
    on legacy records, so ``attempts > 0`` alone is the signal that a retry is being held back."""
    return int(audio.get("attempts") or 0) > 0


def reset_backoff(records: dict) -> int:
    """Clear the backoff on every un-hosted, in-backoff record in ``records`` (mutates in place).

    Pure (no I/O) so it is trivially unit-testable. Returns the number of records reset."""
    reset = 0
    for rec in records.values():
        if not isinstance(rec, dict):
            continue
        audio = rec.get("audio") or {}
        if _is_hosted(audio) or not _in_backoff(audio):
            continue
        rec["audio"] = {**audio, **_RESET_BACKOFF}
        reset += 1
    return reset


def select_sources(
    state_dir: Path,
    source_to_provider: dict[str, str],
    *,
    providers: set[str] | None,
    sources: set[str] | None,
) -> list[str]:
    """The on-disk source keys to scan, after applying the ``--provider`` / ``--source`` filters."""
    on_disk = sorted(p.parent.name for p in Path(state_dir).glob("sources/*/episodes.json"))
    selected = []
    for key in on_disk:
        if sources is not None and key not in sources:
            continue
        if providers is not None and source_to_provider.get(key) not in providers:
            continue
        selected.append(key)
    return selected


def reset_materialize_backoff(
    state_dir: Path,
    source_keys: list[str],
    *,
    apply: bool = False,
) -> dict:
    """Reset the backoff for each selected source; return a summary. ``apply=False`` writes none."""
    reset_total = 0
    touched: set[str] = set()
    per_source: dict[str, int] = {}
    for key in source_keys:
        records = load_records(state_dir, key)
        if not records:
            continue
        n = reset_backoff(records)
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

    # Pull regardless of --apply: read-only (downloads the durable records locally) and a dry-run
    # must see the real records to report what it would reset.
    if storage is not None:
        pulled = pull_state(storage, state_dir)
        print(f"state: pulled {pulled} file(s) from durable storage")

    providers = {p.lower() for p in args.provider} or None
    sources = set(args.source) or None
    selected = select_sources(state_dir, source_to_provider, providers=providers, sources=sources)
    if not selected:
        print("no sources matched the given filters; nothing to scan")
        return 1

    summary = reset_materialize_backoff(state_dir, selected, apply=args.apply)
    if not summary["reset"]:
        print(
            f"scanned {len(selected)} source(s); no un-hosted in-backoff records found "
            "(nothing to reset)"
        )
        return 1

    print(f"scanned {len(selected)} source(s); backoff to reset on un-hosted records:")
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
    sys.exit(main())
