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
per-city table), and ``has_orphans`` (``true``/``false``). ``--reconcile-issue`` additionally
open/updates/closes the single rolling "Audio storage GC" issue via ``gh`` to match this run's
outcome (Python-owned reconcile, mirroring ``audit_feeds.py``'s pattern — see
``reconcile_gc_issue``) — no workflow-YAML conditionals needed.

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
import subprocess
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


def render_issue_body(
    summary: dict,
    *,
    applied: bool,
    auto_mode: bool = False,
    auto_reaped: int = 0,
    quarantine_days: float | None = None,
) -> str:
    verb = "reclaimed" if applied else "reclaimable"
    # In --auto-confirm mode matured double-confirmed orphans WERE deleted this run even though
    # args.apply is False, so the body must not read as a pure dry-run — branch on auto_mode (not
    # auto_reaped) so the burndown explanation still shows up on a run that matured zero this cycle
    # (still tracking toward the quarantine window, not idle).
    if applied:
        preamble = "These objects were **deleted** by a manual `apply` run of the GC workflow."
    elif auto_mode:
        quarantine_note = (
            f"**≥{quarantine_days:g} days** since first observed orphaned"
            if quarantine_days is not None
            else "the configured quarantine window"
        )
        reaped_note = (
            f"**{auto_reaped} object(s) were auto-reaped this run.**"
            if auto_reaped
            else (
                "**No objects matured for auto-reap this run** — everything below is still "
                "within the quarantine window."
            )
        )
        preamble = (
            f"{reaped_note} The double-confirmed auto-reap policy only deletes an orphan once it "
            f"has been observed unreferenced across **≥2 scheduled runs** and {quarantine_note} "
            "— a safety window against a false-positive orphan (e.g. a GH#421-style identity "
            "flip-flop). An object that becomes referenced again before then drops off this list "
            "entirely; no action needed. **This ticket auto-closes on its own** once nothing "
            "remains outstanding — no human step is required unless you want to reclaim everything "
            "immediately (see below)."
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
    auto_mode: bool = False,
    auto_reaped: int = 0,
    quarantine_days: float | None = None,
) -> str:
    """Write the tsv/json/md report files and return the rendered issue body (so a caller wiring
    up ``reconcile_gc_issue`` doesn't need to recompute it or re-read it off disk)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = ["key\tbytes\tcity\tlast_modified"]
    rows += [f"{o.key}\t{o.bytes}\t{o.city}\t{o.last_modified or ''}" for o in orphans]
    (out_dir / "orphans.tsv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    body = render_issue_body(
        summary,
        applied=applied,
        auto_mode=auto_mode,
        auto_reaped=auto_reaped,
        quarantine_days=quarantine_days,
    )
    (out_dir / "issue-body.md").write_text(body, encoding="utf-8")
    (out_dir / "has_orphans").write_text("true" if orphans else "false", encoding="utf-8")
    return body


# ---------------------------------------------------------------------------
# Rolling GC issue reconcile — Python-owned, mirroring scripts/audit_feeds.py's `reconcile()` /
# `_gh()` pattern rather than encoding this state machine as workflow-YAML `if:` conditions.
# Three terminal states from one (has_orphans, applied) pair, and no per-condition step can be unit
# tested without reimplementing a GitHub Actions expression evaluator — audit_feeds.py already
# solved the harder version of this problem (N issues, per-slug dedup, label diffing) this way, so
# the simpler one-rolling-issue case here follows the same, already-proven shape.
# ---------------------------------------------------------------------------

GC_ISSUE_TITLE = "Audio storage GC — reclaimable orphans"


def _gh(*args: str, check: bool = True) -> str:
    proc = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if check and proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def _find_open_gc_issue() -> str | None:
    """The rolling GC issue's number, or ``None`` — dedup by exact title match (there is only ever
    one of these, unlike feed-health's many per-check/per-slug issues, so a title search is enough
    and avoids the embedded-marker parsing that model needs)."""
    out = _gh(
        "issue",
        "list",
        "--state",
        "open",
        "--search",
        f"in:title {GC_ISSUE_TITLE}",
        "--json",
        "number,title",
    )
    for issue in json.loads(out or "[]"):
        if issue.get("title") == GC_ISSUE_TITLE:
            return str(issue["number"])
    return None


def reconcile_gc_issue(
    body: str,
    *,
    has_orphans: bool,
    applied: bool,
    run_url: str | None = None,
    dry_run: bool = False,
) -> str:
    """Open/update/close the single rolling GC issue to match this run's outcome. Returns one of
    ``"created"``, ``"updated"``, ``"closed-apply"``, ``"closed-auto"``, ``"noop"`` (for logging and
    tests — never parsed by a caller). The three branches are exactly the three (has_orphans,
    applied) terminal states gc_audio.py's GC step can produce:

    * still-reclaimable remainder (``has_orphans and not applied``) → open or refresh the ticket;
    * a full manual ``--apply`` reaped everything reported → close, regardless of has_orphans;
    * nothing remains outstanding on a scheduled run (``not has_orphans and not applied``) → close
      — the case that used to fall through neither of the above and leave the ticket open forever.
    """
    existing = _find_open_gc_issue()

    if has_orphans and not applied:
        full_body = body + (
            f"\n\n> Full object list (`orphans.tsv`): {run_url}\n" if run_url else ""
        )
        if dry_run:
            return "updated" if existing else "created"
        if existing:
            _gh("issue", "edit", existing, "--body", full_body)
            return "updated"
        url = _gh("issue", "create", "--title", GC_ISSUE_TITLE, "--body", full_body)
        number = url.strip().rsplit("/", 1)[-1]
        _gh(
            "issue",
            "edit",
            number,
            "--add-label",
            "area:ops",
            "--add-label",
            "type:operations",
            check=False,
        )
        return "created"

    if not existing:
        return "noop"
    if applied:
        comment = f"Reaped by apply run: {run_url}" if run_url else "Reaped by apply run."
        action = "closed-apply"
    else:
        comment = (
            f"Backlog cleared (double-confirmed auto-reap and/or objects re-referenced): {run_url}"
            if run_url
            else "Backlog cleared (double-confirmed auto-reap and/or objects re-referenced)."
        )
        action = "closed-auto"
    if dry_run:
        return action
    _gh("issue", "comment", existing, "--body", comment)
    _gh("issue", "close", existing)
    return action


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
    ap.add_argument(
        "--reconcile-issue",
        action="store_true",
        help="open/update/close the single rolling GC issue via `gh` to match this run's outcome "
        "(requires GH_TOKEN in the environment; off by default so local/test runs never hit the "
        "GitHub API unless asked)",
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
        # A missing/non-comparable last_modified (e.g. a backend that omits it on some listing
        # path) must be treated conservatively — kept, not silently eligible for delete
        # (CR2-SC-18): the age guard exists specifically to protect a just-written object.
        if last_modified is None or last_modified > cutoff:
            kept_young += 1
            continue
        candidates.append((key, last_modified, int(size or 0)))
    candidate_keys = {k for k, _, _ in candidates}

    # Double-confirm tier — only on a scheduled run (auto-confirm without a full --apply). Advance
    # ledger and take the matured subset; a full --apply still deletes everything reported. The
    # quarantine value is resolved unconditionally (not just under auto_mode) so the issue body can
    # always explain the burndown timeline in terms of the actual configured window.
    auto_mode = args.auto_confirm and not args.apply
    quarantine = args.orphan_quarantine_days
    if quarantine is None:
        quarantine = orphan_quarantine_days(defaults)
    auto_delete_keys: set[str] = set()
    ledger_path = Path(args.ledger) if args.ledger else state_dir / ORPHAN_LEDGER_NAME
    if auto_mode:
        ledger = _load_ledger(ledger_path)
        auto_delete_keys = _update_orphan_ledger(
            ledger, candidate_keys, now=now, quarantine_days=quarantine
        )
        _write_ledger(ledger_path, ledger)

    backend = _artifact_backend(storage)
    run_url = _github_run_url()
    retention = b2_retention_days(defaults)

    from citypods.statesync import push_state

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
    #
    # But the local write only survives on a disk that lives long enough to be pushed. On a GitHub
    # Actions runner the workspace is ephemeral and torn down the instant the job ends — cleanly, or
    # via the same SIGTERM/OOM/timeout/cancellation that motivated the per-key log-write. A single
    # end-of-loop push_state() is therefore not enough: a kill after (say) 50 of 100 deletes never
    # reaches it, the VM is destroyed, and all 50 durable-on-paper log entries vanish; the next
    # run's --pull-state restores a stale bucket copy without them and the watchdog can never see
    # the reap. So the reclaim log is pushed to the durable bucket per-key, at the same granularity
    # as the local write, under the same correctness-over-throughput reasoning — one B2 PUT
    # (whole-file put_file overwrite) per deletion, a small, bounded, acceptable cost for a weekly
    # job.
    #
    # And that per-key push must happen BEFORE this key's storage.delete(), not after: the same
    # ephemeral-runner argument that forces a push at all also dictates its ordering. If we deleted
    # first and a kill landed in the gap before the push, the object would be gone from the bucket
    # while its only proof-of-deletion sat unpushed on the dying VM's disk — the exact silent miss
    # this design prevents. Pushing first inverts the failure into the harmless direction: a crash
    # after push, before delete, leaves a durable log entry for a still-live object — at worst a
    # phantom resurrection alert that self-clears next run. A harmless phantom log entry beats a
    # silent unlogged deletion. push_state() is a no-op unless args.push_state was requested, so
    # tests/local/offline runs are never forced into a push.
    #
    # The orphan LEDGER is deliberately NOT pushed per-key here. Unlike the reclaim log it is not
    # the resurrection watchdog's safety record: it only decides which orphans have matured for
    # auto-delete on a FUTURE run. Losing a mid-run ledger update just makes the next run re-observe
    # some keys as if this run hadn't advanced them — an extra observation cycle (a bounded delay)
    # before auto-reap, never a silent data-loss/missed-recovery. It is already persisted locally
    # once before this loop, and the end-of-run push below covers it; that lower-stakes
    # end-of-run-only durability is an acceptable tradeoff (mirroring why 106c14e left
    # run_history.jsonl on end-of-run batching).
    orphans: list[Orphan] = []
    deleted_count = 0
    actually_deleted: set[str] = set()
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
            if args.push_state:
                # Push BEFORE storage.delete(), never after — see the ordering rationale in the
                # block above ("that per-key push must happen BEFORE this key's storage.delete()").
                push_state(
                    storage,
                    state_dir,
                    only_prefixes=[reclaim_log.RECLAIM_LOG_NAME],
                )
            # A single delete failure must not abort the sweep and skip _write_report for every
            # other candidate (CR2-SC-19/MR-SC-02) — the reclaim-log entry (and its push, above)
            # already accounts for this exact "logged but not actually deleted" case as a
            # harmless phantom resurrection alert, so recording-and-continuing here is safe.
            try:
                storage.delete(key)
            except Exception as exc:  # noqa: BLE001 - one object's failure must not sink the run
                print(f"DELETE FAILED  {key}: {exc}")
            else:
                deleted_count += 1
                actually_deleted.add(key)
                print(f"DELETED  {key}")
        else:
            print(f"ORPHAN   {key}")
        # A failed auto-delete must still count as a live orphan (report it) and must not be
        # counted as reaped below — `key in auto_delete_keys` alone can't distinguish "selected
        # for auto-delete" from "actually deleted."
        if args.apply or key not in actually_deleted:
            orphans.append(
                Orphan(
                    key=key,
                    bytes=size,
                    city=attribute(key, mapping),
                    last_modified=last_modified.isoformat() if last_modified is not None else None,
                )
            )

    if args.push_state and (auto_mode or deleted_count):
        # Final push covers the orphan ledger (end-of-run-only durability, see above) and re-asserts
        # the reclaim log — a cheap consistency backstop on a clean finish.
        push_state(
            storage,
            state_dir,
            only_prefixes=[reclaim_log.RECLAIM_LOG_NAME, ORPHAN_LEDGER_NAME],
        )

    summary = summarize(orphans)
    auto_reaped = len(actually_deleted) if auto_mode else 0
    if args.out:
        body = _write_report(
            Path(args.out),
            orphans,
            summary,
            applied=args.apply,
            auto_mode=auto_mode,
            auto_reaped=auto_reaped,
            quarantine_days=quarantine,
        )
        if args.reconcile_issue:
            gc_action = reconcile_gc_issue(
                body,
                has_orphans=bool(orphans),
                applied=args.apply,
                run_url=run_url,
            )
            print(f"gc-issue: {gc_action}")

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
