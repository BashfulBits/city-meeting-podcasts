"""Archive and clear stored judgments so the planner asks for them again (review/53 recovery).

``python -m citypods.judging.clear --task moment --mode stale-evidence`` is a dry run that counts
what would go. ``--apply`` archives the rows (``state/archive/<name>/<source>.jsonl``), removes
them from the episodes' ``judging`` blocks and pushes the changed records. ``--restore <name>`` puts
an archive back. Nothing else is touched: producer candidates, other tasks and the audit state stay.

Why this is needed. The judge stage treats a judgment as present when a row exists for the
(subject, question, judge model, prompt version, tier), whatever its evidence digest
(``runner.episode_needs_judging``), so rows asked of evidence that has since changed are never
revisited on their own. Removing them is the supported way to have them asked again; the planner
then keys the new question by the current evidence digest.

Two scopes:

``stale-evidence`` (default) removes a row only when the evidence the current builder produces for
that subject and tier has a different digest than the one stored on the row: the rows that were
really asked a different question. Rows whose evidence is unchanged stay valid and are not asked
again. A row whose evidence cannot be rebuilt (no transcript) is kept and counted. ``choose``
questions are skipped: their evidence is the option quotes, which no transcript window changes.

``before`` removes every row of the task judged before ``--before``, valid or not, and every
pending marker of the task.

Stop the judge workflow first. A run that started earlier would push its older copy of the same
records over this change.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from citypods.judging import ledger
from citypods.judging.tasks import EpisodeTexts, Subject, TaskSpec

SKIP_KINDS = frozenset({"choose"})  # evidence is the option quotes, not a transcript window


@dataclasses.dataclass
class Partition:
    """What to keep and what to remove for one subject."""

    removed_rows: list[dict] = dataclasses.field(default_factory=list)
    removed_pending: list[dict] = dataclasses.field(default_factory=list)
    kept_rows: int = 0
    unverifiable: int = 0


def _base_question(question_id: str) -> str:
    """``supported_s`` (a stability re-ask) and ``best_r`` (the reverse order) share a spec."""
    for suffix in ("_s", "_r"):
        if question_id.endswith(suffix):
            return question_id[: -len(suffix)]
    return question_id


def _stale(
    spec: TaskSpec,
    subject: Subject,
    texts: EpisodeTexts | None,
    row: Mapping[str, Any],
    cache: dict,
) -> bool | None:
    """Whether ``row`` was asked of evidence that differs from what is built now (None: cannot
    tell)."""
    tier = row.get("context_tier")
    if tier is None or texts is None:
        return None
    if tier not in cache:
        try:
            cache[tier] = spec.evidence(subject, tier, texts)
        except (KeyError, ValueError, TypeError):
            cache[tier] = None
    evidence = cache[tier]
    if evidence is None or not row.get("evidence_digest"):
        return None
    return evidence.digest != row.get("evidence_digest")


def partition_subject(
    spec: TaskSpec,
    subject: Subject,
    texts: EpisodeTexts | None,
    *,
    mode: str,
    before: str | None = None,
) -> Partition:
    """Split one subject's judgments and pending markers into kept and removed."""
    kinds = {q.id: q.kind for q in spec.questions}
    part = Partition()
    cache: dict = {}

    def removable(row: Mapping[str, Any], *, judged: bool) -> bool | None:
        if mode == "before":
            if not judged:
                return True  # a pending marker has no date; it is re-planned with the rest
            return str(row.get("judged_at") or "") < str(before)
        kind = kinds.get(_base_question(str(row.get("question_id"))))
        if kind in SKIP_KINDS or row.get("kind") in SKIP_KINDS:
            return False
        return _stale(spec, subject, texts, row, cache)

    for row in ledger.judgments(subject):
        verdict = removable(row, judged=True)
        if verdict is None:
            part.unverifiable += 1
            part.kept_rows += 1
        elif verdict:
            part.removed_rows.append(dict(row))
        else:
            part.kept_rows += 1
    for marker in ledger.pending(subject):
        if removable(marker, judged=False):
            part.removed_pending.append(dict(marker))
    return part


def _canon(row: Mapping[str, Any]) -> str:
    return json.dumps(row, sort_keys=True)


def apply_partition(subject: Subject, part: Partition) -> None:
    """Remove the partition's rows and markers from the subject's stored entry."""
    entry = ledger._entry(subject, create=False)
    if entry is None:
        return
    gone = {_canon(r) for r in part.removed_rows}
    entry["judgments"] = [
        r for r in entry.get("judgments") or [] if not (isinstance(r, dict) and _canon(r) in gone)
    ]
    gone_pending = {_canon(r) for r in part.removed_pending}
    kept = [
        r
        for r in entry.get("pending") or []
        if not (isinstance(r, dict) and _canon(r) in gone_pending)
    ]
    if kept:
        entry["pending"] = kept
    else:
        entry.pop("pending", None)


def _label(row: Mapping[str, Any]) -> str:
    return f"{row.get('question_id')}|{row.get('judge_role')}|{row.get('judge_model')}"


def summarize(parts: Iterable[tuple[Subject, Partition]]) -> dict[str, Any]:
    """Counts by (question, role, model) of what is removed, and what was kept."""
    removed: Counter = Counter()
    pending: Counter = Counter()
    kept = unverifiable = 0
    for _subject, part in parts:
        kept += part.kept_rows
        unverifiable += part.unverifiable
        removed.update(_label(row) for row in part.removed_rows)
        pending.update(_label(marker) for marker in part.removed_pending)
    return {
        "removed_rows": sum(removed.values()),
        "removed_pending": sum(pending.values()),
        "kept_rows": kept,
        "kept_unverifiable": unverifiable,
        "rows_by_question_role_model": dict(sorted(removed.items())),
        "pending_by_question_role_model": dict(sorted(pending.items())),
    }


def write_archive(path: Path, entries: Sequence[Mapping[str, Any]]) -> None:
    """One JSON object per line. The state sync uploads only ``.json`` and ``.jsonl`` files
    (``statesync._SUFFIXES``), so an archive in any other format would silently stay local."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")


def read_archive(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def verify_uploaded(
    read_remote: Callable[[str], bytes | None], state_dir: Path, rels: Sequence[str]
):
    """Names of archive files whose uploaded copy is missing or differs from the local file."""
    bad = []
    for rel in rels:
        remote = read_remote(f"state/{rel}")
        if remote is None or remote != (state_dir / rel).read_bytes():
            bad.append(rel)
    return bad


def restore_entries(
    entries: Sequence[Mapping[str, Any]], subject_for: Callable[[str], Subject | None]
) -> int:
    """Put archived rows back into their subjects; rows already present (by key) are skipped."""
    restored = 0
    for entry in entries:
        subject = subject_for(entry["subject_id"])
        if subject is None:
            continue
        if entry["kind"] == "judgment":
            if ledger.append_judgment(subject, entry["row"]):
                restored += 1
        elif entry["kind"] == "pending":
            ledger.add_pending(subject, entry["row"])
            restored += 1
    return restored


# Production wiring.


def running_judge_runs() -> list[str]:
    """Ids of judge.yml runs that have not completed."""
    import subprocess

    out = subprocess.run(  # noqa: S603 - fixed argument list, no shell
        [
            "gh",
            "run",
            "list",
            "--workflow",
            "judge.yml",
            "--json",
            "databaseId,status",
            "--jq",
            '.[] | select(.status != "completed") | .databaseId',
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        # An unknown state is not "stopped": a run that is active would push its older copy.
        raise SystemExit(f"could not list judge.yml runs (gh exit {out.returncode}): {out.stderr}")
    return out.stdout.split()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", required=True)
    parser.add_argument("--mode", choices=["stale-evidence", "before"], default="stale-evidence")
    parser.add_argument("--before", help="with --mode before: remove rows judged before this date")
    parser.add_argument("--archive-name", help="archive directory name (required with --apply)")
    parser.add_argument("--apply", action="store_true", help="archive, remove and push")
    parser.add_argument("--restore", metavar="NAME", help="put the named archive back")
    parser.add_argument("--site-config", default="config/site_config.yml")
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--output-dir", default="public")
    parser.add_argument("--no-pull", action="store_true")
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--skip-run-check", action="store_true")
    args = parser.parse_args(argv)
    if args.mode == "before" and not args.before:
        parser.error("--mode before needs --before DATE")
    if args.apply and not args.archive_name:
        parser.error("--apply needs --archive-name")

    from concurrent.futures import ThreadPoolExecutor

    from citypods.config import load_city_configs, load_site_config
    from citypods.judging.tasks import task
    from citypods.judging.texts import build_episode_texts, tag_definitions
    from citypods.records import (
        load_records,
        protected_blocks_for_lane,
        record_to_episode,
        save_records,
        source_key,
    )
    from citypods.stages import _read_storage_bytes
    from citypods.state import resolve_state_dir
    from citypods.statesync import pull_state, push_records_merged, push_state
    from citypods.storage import make_storage

    def log(message: str) -> None:
        print(message, file=sys.stderr, flush=True)

    site = load_site_config(args.site_config)
    spec = task(args.task)
    state_dir = resolve_state_dir(site, Path(args.output_dir))
    storage = make_storage(site, site.get("base_url", ""), Path(args.output_dir))
    cities = load_city_configs(args.config_dir, site.get("defaults", {}))
    sources = sorted({source_key(c) for c in cities})
    record_paths = [f"sources/{key}/episodes.json" for key in sources]
    archive_dir = Path("archive") / (args.restore or args.archive_name or "dry-run")
    if (args.apply or args.restore) and not args.skip_run_check:
        running = running_judge_runs()
        if running:
            raise SystemExit(
                f"judge.yml has runs in progress ({', '.join(running)}); disable the workflow and "
                "wait for them to finish first (--skip-run-check to override)"
            )
    if args.apply:
        # An archive name is used once: writing it again would replace what an earlier clear put
        # there, and --restore could then recover only the later rows. Check the bucket as well as
        # the local copy.
        pull_state(
            storage,
            state_dir,
            only_prefixes=[f"{archive_dir.as_posix()}/"],
            apply_tombstones=False,
            log=log,
        )
        existing = state_dir / archive_dir
        if existing.exists() and any(existing.iterdir()):
            raise SystemExit(f"archive {args.archive_name!r} already exists; choose a new name")
    if not args.no_pull:
        extra = [str(archive_dir / f"{key}.jsonl") for key in sources] if args.restore else []
        pull_state(
            storage, state_dir, only_paths=record_paths + extra, apply_tombstones=False, log=log
        )

    def load(key: str) -> tuple[dict, list[tuple[str, dict, Any]]]:
        records = load_records(state_dir, key)
        episodes = []
        for uid, rec in records.items():
            ep = record_to_episode(rec)
            ep.judging = rec.setdefault("judging", {})  # subjects read and write the stored block
            if spec.subjects(ep):
                episodes.append((uid, rec, ep))
        return records, episodes

    if args.restore:
        changed: list[str] = []
        total = 0
        for key in sources:
            archive = state_dir / archive_dir / f"{key}.jsonl"
            if not archive.exists():
                continue
            records, episodes = load(key)
            subjects = {s.subject_id: s for _uid, _rec, ep in episodes for s in spec.subjects(ep)}
            total += restore_entries(read_archive(archive), subjects.get)
            save_records(state_dir, key, records)
            changed.append(f"sources/{key}/episodes.json")
        pushed = push_records_merged(
            storage,
            state_dir,
            [path.split("/")[1] for path in changed],
            protected_blocks=protected_blocks_for_lane("judge"),
            lane="judge",
            log=log,
        )
        if pushed != len(changed):
            raise SystemExit(f"pushed {pushed} of {len(changed)} sources; re-run --restore")
        print(json.dumps({"restored": total, "sources": len(changed)}))
        return 0

    definitions = tag_definitions()

    def texts_for(ep: Any) -> EpisodeTexts | None:
        if args.mode == "before" or not getattr(ep, "transcript_key", None):
            return None
        try:
            raw = _read_storage_bytes(storage, ep.transcript_key)
            return build_episode_texts(ep, raw, definitions)
        except Exception as exc:  # noqa: BLE001 - an unreadable transcript keeps its rows
            log(f"transcript {ep.transcript_key}: {type(exc).__name__}: {exc}")
            return None

    all_parts: list[tuple[Subject, Partition]] = []
    changed_sources: list[str] = []
    for key in sources:
        records, episodes = load(key)
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            texts = list(pool.map(lambda item: texts_for(item[2]), episodes))
        archived: list[dict] = []
        for (uid, _rec, ep), episode_texts in zip(episodes, texts, strict=True):
            for subject in spec.subjects(ep):
                part = partition_subject(
                    spec, subject, episode_texts, mode=args.mode, before=args.before
                )
                all_parts.append((subject, part))
                for kind, rows in (
                    ("judgment", part.removed_rows),
                    ("pending", part.removed_pending),
                ):
                    archived += [
                        {"uid": uid, "subject_id": subject.subject_id, "kind": kind, "row": row}
                        for row in rows
                    ]
                if args.apply:
                    apply_partition(subject, part)
        if args.apply and archived:
            write_archive(state_dir / archive_dir / f"{key}.jsonl", archived)
            save_records(state_dir, key, records)
            changed_sources.append(key)
        log(f"{key}: {len(episodes)} episodes, {len(archived)} entries to archive")

    summary = {
        "task": spec.name,
        "mode": args.mode,
        "before": args.before,
        "applied": bool(args.apply),
        **summarize(all_parts),
    }
    if args.apply:
        # The archive goes up first: rows must be recoverable before they leave the records.
        paths = [str(archive_dir / f"{k}.jsonl") for k in changed_sources]
        summary_path = state_dir / archive_dir / "summary.json"
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps(summary, indent=2) + "\n")
        uploads = [*paths, str(archive_dir / "summary.json")]
        push_state(storage, state_dir, only_paths=uploads, log=log)
        # Never remove a row until its archive is readable back from storage.
        missing = verify_uploaded(lambda key: _read_storage_bytes(storage, key), state_dir, uploads)
        if missing:
            raise SystemExit(f"archive not in storage, records not changed remotely: {missing}")
        # Records go through the foreign-block-preserving push: other lanes may have written their
        # own blocks to these files since this run pulled them.
        pushed = push_records_merged(
            storage,
            state_dir,
            changed_sources,
            protected_blocks=protected_blocks_for_lane("judge"),
            lane="judge",
            log=log,
        )
        if pushed != len(changed_sources):
            raise SystemExit(
                f"archived, but pushed the records of only {pushed} of {len(changed_sources)} "
                "sources; --restore the archive, then re-run with a new --archive-name"
            )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
