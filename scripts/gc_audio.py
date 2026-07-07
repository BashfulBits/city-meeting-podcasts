#!/usr/bin/env python
"""Garbage-collect orphaned content-addressed objects from storage.

Audio and transcripts are content-addressed (the key embeds a spec hash), so when an episode's
artifact is regenerated — chapters added, a bitrate bump, a new transcript version, or a duplicate
source view coalesced (GH#421) — the old object is left behind, no longer referenced by any record.
This sweep deletes those orphans.

The live set comes from ``records.referenced_audio_keys`` (despite the name, it returns both
audio AND transcript keys), so hosted transcripts are protected, not reaped.

Auto-apply tier (``--auto-confirm``, GH#496): a *scheduled* run may delete the provably-safe subset
without a human — orphans observed unreferenced across ``>= 2`` runs AND past the
``orphan_quarantine_days`` quarantine window (tracked in ``state/orphan-ledger.json``; a key that
reappears in the live set is dropped from the ledger, so a GH#421 flip-flop never matures). Every
delete (auto or manual ``--apply``) is recorded in the append-only reclaim log
(``state/reclaim-log.jsonl``) with a ``recover_by`` deadline, so the resurrection watchdog script
can raise a HIGH-priority issue if a reaped key is re-referenced while still restorable from B2.

Safe by default:
  * dry-run unless ``--apply`` (delete all reported) or ``--auto-confirm`` (matured subset only);
  * ``--pull-state`` first restores the durable bucket state so the live set is current — never
    reap against a stale/partial set of records;
  * only deletes objects older than ``--min-age-days`` (default 7) so an object written by a
    build that hasn't yet persisted its record isn't reaped out from under it;
  * refuses to run if no records are found (would look like everything is orphaned);
  * only ever reaps *managed artifacts* — content-addressed audio (``*.m4a``) and transcripts
    (``transcripts/…``). Non-artifact infrastructure (the durable ``state/`` snapshot, the
    ``models/`` ASR-weight mirror, ``clips/``, or any future prefix) is allow-listed out and
    never touched — see ``is_managed_artifact``.

With ``--out DIR`` it writes a machine-readable report for the scheduled workflow:
``orphans.tsv`` (every candidate key + size + city), ``summary.json``, ``issue-body.md`` (a
per-city table), and ``has_orphans`` (``true``/``false``).

By default it scans the whole bucket (``--prefix ""``); pass ``--prefix`` to scope it. The
allow-list (``is_managed_artifact``) keeps the unscoped sweep safe — only audio/transcript
objects are deletion candidates. When soundbite clips (``clips/…``) land they must be added to
the live set or given their own ephemeral GC policy before they can be reaped here.

Usage:
    PYTHONPATH=. python scripts/gc_audio.py [--apply] [--pull-state] [--min-age-days N] \
        [--prefix P] [--out DIR]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from citypods.config import load_city_configs, load_site_config
from citypods.ops import reclaim_log
from citypods.ops.reclaim import b2_retention_days, orphan_quarantine_days
from citypods.records import referenced_audio_keys, source_key
from citypods.state import resolve_state_dir
from citypods.statesync import STATE_PREFIX
from citypods.storage import make_storage

_UNCONFIGURED = "(unconfigured)"
_OTHER = "(other)"

# Object namespaces the GC *manages* and may delete from:
#   * AUDIO       — ``<provider>/<source_key>/<uid>-<spec>.m4a`` (and the legacy slug-keyed
#                   ``<slug>/<digest>.m4a`` predating the source-key rename) — always ``.m4a``;
#   * TRANSCRIPTS — ``transcripts/<source_key>/<uid>-<spec>.<fmt>``, incl. the ASR word JSON.
# Everything else in the bucket is infrastructure that must NEVER be reaped: the durable
# ``state/`` snapshot and the ``models/`` ASR-weight mirror (written by
# ``scripts/prepare_whisper.py``, depended on by the ASR workers — a stray ``--apply`` that
# deleted ``models/faster-whisper-large-v3-turbo/model.bin`` would break transcription).
# ``clips/`` (soundbites) is not produced yet and gets its own ephemeral GC policy when it lands.
#
# This is an *allow-list* of managed shapes, deliberately not a deny-list of known infra: a new
# infrastructure prefix added to the bucket later can then never be reaped by an old copy of this
# script — it simply isn't a recognized artifact, so it's skipped.
_TRANSCRIPT_PREFIX = "transcripts/"
_CLIPS_PREFIX = "clips/"


def is_managed_artifact(key: str) -> bool:
    """True iff ``key`` is an audio or transcript object the orphan GC is allowed to delete.

    Anything that is not a managed artifact (``state/``, ``models/``, ``clips/``, or any future
    infra prefix) returns ``False`` and is left untouched by the sweep — see the note above.
    """
    if key.startswith(_TRANSCRIPT_PREFIX):
        return True
    return key.endswith(".m4a") and not key.startswith(_CLIPS_PREFIX)


ORPHAN_LEDGER_NAME = "orphan-ledger.json"


def _github_run_url() -> str | None:
    run_id = os.environ.get("GITHUB_RUN_ID")
    repo = os.environ.get("GITHUB_REPOSITORY")
    return f"https://github.com/{repo}/actions/runs/{run_id}" if run_id and repo else None


def _artifact_backend(storage) -> str:
    """The backend that actually holds managed artifacts. Under ``RoutingStorage`` audio/transcripts
    live on the B2 primary (coordination prefixes route to R2, but those aren't managed artifacts),
    report the primary's name; a plain backend reports its own. Drives ``recover_by`` in the reclaim
    log — only B2 has a recoverable-delete window."""
    primary = getattr(storage, "primary", storage)
    return getattr(primary, "name", "local")


def _load_ledger(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_ledger(path: Path, ledger: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _update_orphan_ledger(
    ledger: dict[str, dict],
    current_orphan_keys: set[str],
    *,
    now: datetime,
    quarantine_days: float,
) -> set[str]:
    """Advance the double-confirm ledger and return the keys cleared to auto-delete.

    * Drop any tracked key that is **no longer** a candidate orphan (re-referenced, or already
      deleted): this reset is the flip-flop guard (GH#421) — an oscillating object never matures.
    * Bump ``run_count``/``last_seen`` for each still-orphan key (first sighting seeds first_seen).
    * A key is auto-deletable once observed across ``>= 2`` runs AND its ``first_seen`` is at least
      ``quarantine_days`` old — the pre-delete quarantine window (distinct from the object's age,
      which ``--min-age-days`` already guards, and from the post-delete B2 recovery window).
    """
    for key in list(ledger):
        if key not in current_orphan_keys:
            del ledger[key]
    auto_delete: set[str] = set()
    for key in current_orphan_keys:
        rec = ledger.get(key)
        if rec is None:
            rec = {"first_seen": now.isoformat(), "run_count": 0, "last_seen": None}
            ledger[key] = rec
        rec["run_count"] = int(rec.get("run_count", 0)) + 1
        rec["last_seen"] = now.isoformat()
        first_seen = _parse_iso(rec.get("first_seen")) or now
        mature = first_seen <= now - timedelta(days=quarantine_days)
        if rec["run_count"] >= 2 and mature:
            auto_delete.add(key)
    return auto_delete


def _parse_iso(raw) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@dataclass(frozen=True)
class Orphan:
    key: str
    bytes: int
    city: str
    last_modified: str | None


def source_to_city(cities: list) -> dict[str, str]:
    """Map each configured source_key to its government entity (``city:``) or slug.

    Per-board and combined feeds of one entity collapse to the same label, which is what makes the
    GC report read as "per city" rather than "per feed". A source_key with no configured city
    (e.g. a removed feed) is attributed to ``(unconfigured)`` at report time.
    """
    mapping: dict[str, str] = {}
    for city in cities:
        mapping[source_key(city)] = city.city_entity or city.slug
    return mapping


def attribute(key: str, mapping: dict[str, str]) -> str:
    """Resolve a storage key to a city label via its source_key segment."""
    segments = key.split("/")
    src: str | None = None
    if segments[0] == "transcripts" and len(segments) >= 3:
        src = segments[1]  # transcripts/<source_key>/<uid>-<spec>.<fmt>
    elif len(segments) >= 3 and segments[0] not in {"clips", STATE_PREFIX}:
        src = segments[1]  # <provider>/<source_key>/<uid>-<spec>.m4a
    if src is None:
        return _OTHER
    return mapping.get(src, _UNCONFIGURED)


def summarize(orphans: list[Orphan]) -> dict:
    by_city: dict[str, dict[str, int]] = defaultdict(lambda: {"files": 0, "bytes": 0})
    for o in orphans:
        by_city[o.city]["files"] += 1
        by_city[o.city]["bytes"] += o.bytes
    return {
        "by_city": {city: dict(v) for city, v in sorted(by_city.items())},
        "total_files": len(orphans),
        "total_bytes": sum(o.bytes for o in orphans),
    }


def human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def render_issue_body(summary: dict, *, applied: bool, auto_reaped: int = 0) -> str:
    verb = "reclaimed" if applied else "reclaimable"
    # In --auto-confirm mode (auto_reaped > 0) matured double-confirmed orphans WERE deleted this
    # run even though args.apply is False, so the body must not read as a pure dry-run — the
    # `summary` here already counts only the still-reclaimable remainder.
    if applied:
        preamble = "These objects were **deleted** by a manual `apply` run of the GC workflow."
    elif auto_reaped:
        preamble = (
            f"**{auto_reaped} object(s) were auto-reaped this run** (double-confirmed: orphaned "
            f"across ≥2 runs past the quarantine window). The {summary['total_files']} below "
            "remain reclaimable — they are not yet matured for auto-apply. No human action is "
            "required; matured orphans auto-reap and drop off this list on a later run."
        )
    else:
        preamble = (
            "These content-addressed audio/transcript objects are no longer referenced by any "
            "record (regenerated artifacts, retired recipes, or coalesced duplicate source views, "
            "GH#421). This run was a **dry-run** — nothing was deleted."
        )
    lines = [
        f"## Audio storage GC — {summary['total_files']} orphan object(s), "
        f"{human_bytes(summary['total_bytes'])} {verb}",
        "",
        preamble,
        "",
        "| City | Files | Size |",
        "|---|---:|---:|",
    ]
    for city, v in summary["by_city"].items():
        lines.append(f"| {city} | {v['files']} | {human_bytes(v['bytes'])} |")
    lines.append(
        f"| **Total** | **{summary['total_files']}** | **{human_bytes(summary['total_bytes'])}** |"
    )
    if not applied:
        lines += [
            "",
            "To actually delete these objects, run the **Audio orphan GC** workflow via "
            "*Run workflow* with `apply = true`.",
        ]
    return "\n".join(lines) + "\n"


def _write_report(
    out_dir: Path,
    orphans: list[Orphan],
    summary: dict,
    *,
    applied: bool,
    auto_reaped: int = 0,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = ["key\tbytes\tcity\tlast_modified"]
    rows += [f"{o.key}\t{o.bytes}\t{o.city}\t{o.last_modified or ''}" for o in orphans]
    (out_dir / "orphans.tsv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (out_dir / "issue-body.md").write_text(
        render_issue_body(summary, applied=applied, auto_reaped=auto_reaped), encoding="utf-8"
    )
    (out_dir / "has_orphans").write_text("true" if orphans else "false", encoding="utf-8")


def _non_negative_days(value: str) -> float:
    """A negative ``--min-age-days`` would push the cutoff into the future, making every object
    look old enough to reap — including freshly written ones. Reject it."""
    days = float(value)
    if days < 0:
        raise argparse.ArgumentTypeError("--min-age-days must be >= 0")
    return days


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="actually delete (default: dry-run)")
    ap.add_argument(
        "--pull-state",
        action="store_true",
        help="restore the durable bucket state first so the live set is current (no-op for a "
        "sync-less local backend)",
    )
    ap.add_argument(
        "--min-age-days",
        type=_non_negative_days,
        default=7.0,
        help="skip objects newer than this (must be >= 0)",
    )
    ap.add_argument("--prefix", default="", help="limit to keys under this storage prefix")
    ap.add_argument("--site-config", default="config/site_config.yml")
    ap.add_argument("--config-dir", default="config", help="config dir for per-city attribution")
    ap.add_argument("--output-dir", default="docs")
    ap.add_argument("--out", default=None, help="write the GC report (tsv/json/md) into this dir")
    ap.add_argument(
        "--auto-confirm",
        action="store_true",
        help="enable the double-confirmed auto-apply tier: on a scheduled run, delete only orphans "
        "observed across >=2 runs past the quarantine window (safe subset). Ignored with --apply, "
        "which deletes all reported orphans.",
    )
    ap.add_argument(
        "--orphan-quarantine-days",
        type=_non_negative_days,
        default=None,
        help="pre-delete quarantine window for --auto-confirm (default: orphan_quarantine_days)",
    )
    ap.add_argument(
        "--ledger",
        default=None,
        help="path to the double-confirm ledger (default: <state_dir>/orphan-ledger.json)",
    )
    ap.add_argument(
        "--push-state",
        action="store_true",
        help="push the reclaim log + orphan ledger back to the bucket so they persist across runs "
        "(scoped push — never clobbers sibling state)",
    )
    args = ap.parse_args(argv)

    site_config = load_site_config(args.site_config)
    defaults = site_config.get("defaults", {})
    storage = make_storage(site_config, "https://example.invalid", args.output_dir)
    if storage is None or not hasattr(storage, "iter_objects"):
        print("no storage backend with GC support configured; nothing to do")
        return 0

    state_dir = resolve_state_dir(site_config, args.output_dir)
    if args.pull_state:
        from citypods.statesync import pull_state

        pull_state(storage, state_dir)

    referenced = referenced_audio_keys(state_dir)
    if not referenced:
        print("no records found; refusing to GC (would look like everything is orphaned)")
        return 1

    try:
        cities = load_city_configs(args.config_dir, site_config.get("defaults", {}))
    except (FileNotFoundError, OSError):
        cities = []
    mapping = source_to_city(cities)

    now = datetime.now(UTC)
    cutoff = now - timedelta(days=args.min_age_days)

    # Pass 1 — candidate orphans: unreferenced, a managed artifact, and older than --min-age-days
    # (the object-age guard against a just-written object whose record hasn't synced yet).
    candidates: list[tuple[str, datetime | None, int]] = []
    kept_young = 0
    kept_unmanaged = 0
    for key, last_modified, size in storage.iter_objects(args.prefix):
        if key in referenced:
            continue  # live: referenced by a record
        if not is_managed_artifact(key):
            kept_unmanaged += 1
            continue  # state/, models/, clips/, or any other non-artifact infra — never reap
        if last_modified is not None and last_modified > cutoff:
            kept_young += 1
            continue
        candidates.append((key, last_modified, int(size or 0)))
    candidate_keys = {k for k, _, _ in candidates}

    # Double-confirm tier — only on a scheduled run (auto-confirm without a full --apply). Advance
    # ledger and take the matured subset; a full --apply still deletes everything reported.
    auto_mode = args.auto_confirm and not args.apply
    auto_delete_keys: set[str] = set()
    ledger_path = Path(args.ledger) if args.ledger else state_dir / ORPHAN_LEDGER_NAME
    if auto_mode:
        quarantine = args.orphan_quarantine_days
        if quarantine is None:
            quarantine = orphan_quarantine_days(defaults)
        ledger = _load_ledger(ledger_path)
        auto_delete_keys = _update_orphan_ledger(
            ledger, candidate_keys, now=now, quarantine_days=quarantine
        )
        _write_ledger(ledger_path, ledger)

    backend = _artifact_backend(storage)
    run_url = _github_run_url()
    retention = b2_retention_days(defaults)

    # Pass 2 — delete + report. Report the still-reclaimable set: for a full --apply everything is
    # deleted (rendered "reclaimed"); in auto/dry-run mode the not-yet-matured remainder stays on
    # rolling issue while matured orphans auto-reap and drop off it.
    #
    # The reclaim-log entry for each key is committed BEFORE that key's storage.delete(), one key
    # at a time — not batched after the whole loop. If the process is killed mid-loop (SIGTERM, OOM,
    # the Actions job timeout), every key already deleted has a durable recover_by record, so the
    # resurrection watchdog can still catch a false-positive reap of any of them. The failure this
    # trades into — a crash between log-write and delete leaves a log entry for an object that was
    # never actually removed — is strictly safer: at worst it later raises a harmless resurrection
    # alert for a still-live object, versus the batched approach's silent miss (a real deletion
    # with no record at all). Correctness over throughput: this is a weekly, bounded-size run, so
    # the many small append+prune cycles are an acceptable cost.
    orphans: list[Orphan] = []
    deleted_count = 0
    for key, last_modified, size in candidates:
        delete_now = args.apply or (key in auto_delete_keys)
        if delete_now:
            reclaim_log.append_deletions(
                state_dir,
                [
                    reclaim_log.make_entry(
                        key,
                        backend=backend,
                        reason="orphan-manual" if args.apply else "orphan-auto",
                        run_url=run_url,
                        retention_days=retention,
                        now=now,
                    )
                ],
                retention_days=retention,
                now=now,
            )
            storage.delete(key)
            deleted_count += 1
            print(f"DELETED  {key}")
        else:
            print(f"ORPHAN   {key}")
        if args.apply or key not in auto_delete_keys:
            orphans.append(
                Orphan(
                    key=key,
                    bytes=size,
                    city=attribute(key, mapping),
                    last_modified=last_modified.isoformat() if last_modified is not None else None,
                )
            )

    if args.push_state and (auto_mode or deleted_count):
        from citypods.statesync import push_state

        push_state(
            storage,
            state_dir,
            only_prefixes=[reclaim_log.RECLAIM_LOG_NAME, ORPHAN_LEDGER_NAME],
        )

    summary = summarize(orphans)
    auto_reaped = len(auto_delete_keys) if auto_mode else 0
    if args.out:
        _write_report(Path(args.out), orphans, summary, applied=args.apply, auto_reaped=auto_reaped)

    action = "deleted" if args.apply else "orphaned (dry-run)"
    auto_note = f"; {len(auto_delete_keys)} auto-reaped (double-confirmed)" if auto_mode else ""
    print(
        f"\n{len(referenced)} referenced; {summary['total_files']} {action} "
        f"({human_bytes(summary['total_bytes'])}){auto_note}; "
        f"{kept_young} skipped (younger than {args.min_age_days}d); "
        f"{kept_unmanaged} non-artifact objects protected (state/, models/, clips/, …)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
