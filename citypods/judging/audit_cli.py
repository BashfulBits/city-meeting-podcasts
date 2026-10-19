"""Command line for the weekly judge audit: package, ingest an answer, promote to gold.

``package`` settles old batches, picks this week's claims and writes the shared review manifest
that ``scripts/publish_review_batch.py`` publishes. ``ingest`` is what the shared resolution
workflow runs when a person checks a box. ``promote`` turns a clear-cut finalized answer into a
probe.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from citypods.judging import audit
from citypods.judging.probes import (
    DEFAULT_ROOT,
    GATING_ITEMS,
    gating,
    load_probes,
    parse_probes,
)
from citypods.judging.tasks import make_evidence, task


def _tasks(site: dict[str, Any]):
    configured = (site.get("judging") or {}).get("tasks") or {}
    return [task(name) for name in sorted(configured) if isinstance(configured[name], dict)]


def probes_clean(specs, accuracy: dict[str, float]) -> bool:
    """Whether every task has a gating probe set and every judge answers it near-perfectly."""
    if not accuracy or not specs:
        return False
    return all(gating(load_probes(spec)) for spec in specs) and min(accuracy.values()) >= 0.95


def _settings(args: argparse.Namespace):
    from citypods.config import load_site_config
    from citypods.state import resolve_state_dir
    from citypods.storage import make_storage

    site = load_site_config(args.site_config)
    output_dir = Path(args.output_dir)
    state_dir = resolve_state_dir(site, output_dir)
    storage = make_storage(site, site.get("base_url", ""), output_dir)
    return site, state_dir, storage


def _state_paths() -> list[Path]:
    return [
        Path(audit.STATE_DIR) / "items.json",
        Path(audit.STATE_DIR) / "answers.json",
    ]


def _episodes(site: dict[str, Any], state_dir: Path, config_dir: str):
    from citypods.config import load_city_configs
    from citypods.records import load_records, record_to_episode, source_key

    for city in load_city_configs(config_dir, site.get("defaults", {})):
        for raw in load_records(state_dir, source_key(city)).values():
            yield city, record_to_episode(raw)


def package(args: argparse.Namespace) -> int:
    from citypods.judging.runner import _largest_tier_evidence
    from citypods.judging.texts import build_episode_texts, tag_definitions
    from citypods.site import meeting_page_url
    from citypods.stages import _read_storage_bytes
    from citypods.statesync import pull_state, push_state

    site, state_dir, storage = _settings(args)
    pull_state(storage, state_dir)
    now = datetime.now(UTC)
    week = audit.batch_id(now)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    state = audit.AuditState.load(state_dir)

    settlements = audit.settle(state, now)
    listed = list(_episodes(site, state_dir, args.config_dir))
    pairs = [(city.slug, ep) for city, ep in listed]
    specs = _tasks(site)
    tickets = _tickets(settlements, audit.disagreements((ep for _c, ep in pairs), specs, now))
    manifest: dict[str, Any] = {"version": 1, "children": [], "tickets": tickets}
    children: list[dict[str, str]] = []
    if week in state.batches:
        manifest["reasons"] = [f"batch {week} was already packaged"]
    else:
        noisy = audit.judge_noise(ep for _c, ep in pairs)
        loudest = max(noisy, key=lambda m: noisy[m]) if noisy and max(noisy.values()) > 0 else None
        candidates = audit.gather(
            pairs,
            specs,
            audited=state.items,
            noisy=loudest,
            newest=audit.newest_judge((ep for _c, ep in pairs), now),
        )
        limit = audit.weekly_item_count(
            probes_clean=_clean_from_episodes(specs, (ep for _c, ep in pairs)),
            shrink=state.shrink,
        )
        definitions = tag_definitions()
        cities = {city.slug: city for city, _ep in listed}
        base_url = site.get("base_url", "")
        picked = audit.select(candidates, limit=limit, week=week)
        # A selected item whose evidence cannot be built is replaced by the next best.
        spare = [c for c in sorted(candidates, key=lambda c: -c.score) if c not in picked]
        built: list[tuple[audit.Candidate, str, str, str, str | None]] = []
        for candidate in [*picked, *spare]:
            if len(built) >= len(picked):
                break
            key = candidate.episode.transcript_key
            raw = _read_storage_bytes(storage, key) if key else None
            texts = build_episode_texts(candidate.episode, raw, definitions)
            found = _largest_tier_evidence(candidate.spec, candidate.subject, texts)
            if found is None:
                continue
            tier, evidence = found
            audio = None
            if candidate.task == "moment":
                url = meeting_page_url(cities[candidate.city], candidate.episode, base_url)
                start = int(float(candidate.subject.payload.get("start") or 0))
                audio = f"{url}#t={start}" if url else None
            built.append((candidate, tier, evidence.text, evidence.digest, audio))
        per_task: dict[str, int] = {}
        for candidate, tier, text, digest, audio in built:
            name = f"{candidate.item_id}.md"
            (out_dir / name).write_text(
                audit.render_child(candidate, text, audio_url=audio), encoding="utf-8"
            )
            children.append(
                {
                    "candidate_id": candidate.item_id,
                    "title": f"Judge audit {week}: {candidate.task} {candidate.item_id[:8]}",
                    "body_file": name,
                }
            )
            per_task[candidate.task] = per_task.get(candidate.task, 0) + 1
            state.items[candidate.item_id] = {
                "task": candidate.task,
                "city": candidate.city,
                "episode_uid": candidate.subject.episode_uid,
                "subject_id": candidate.subject.subject_id,
                "question_id": candidate.question.id,
                "tier": tier,
                "evidence_digest": digest,
                "evidence": text,
                "item_type": audit.item_type(candidate),
                "batch": week,
                "reasons": list(candidate.reasons),
                "score": round(candidate.score, 3),
            }
        if children:
            state.batches[week] = {
                "created": now.isoformat(timespec="seconds"),
                "status": "open",
                "item_ids": [c["candidate_id"] for c in children],
            }
            (out_dir / "parent.md").write_text(
                audit.render_parent(week, len(children), per_task), encoding="utf-8"
            )
        manifest["children"] = children
        if not children:
            manifest["reasons"] = ["no claim has both an anchor and a sibling reading yet"]
    paths = state.save(state_dir)
    # Only items.json: ingest owns answers.json, and pushing a stale copy would lose an answer.
    push_state(storage, state_dir, only_paths=[paths[0].relative_to(state_dir)])
    (out_dir / "review-batch.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(
        f"packaged {len(children)} judge-audit claim(s); "
        f"settled {len(settlements)} task batch(es); {len(tickets)} ticket(s)"
    )
    return 0


def _clean_from_episodes(specs, episodes) -> bool:
    """Whether the probe set is large enough to gate and every judge answers it near-perfectly."""
    from citypods.judging.report import summarize

    gold = {p.id: p.verdict for spec in specs for p in load_probes(spec)}
    records = ({"published": "", "judging": getattr(ep, "judging", None) or {}} for ep in episodes)
    results = summarize(records, probes=gold)["probes"]
    return probes_clean(
        specs, {m: v["accuracy"] for m, v in results.items() if v["accuracy"] is not None}
    )


def _tickets(settlements: list[audit.Settlement], disagree: dict[str, int]) -> list[dict[str, str]]:
    """Tickets: a discarded task batch, and repeated audit-versus-adjudicator disagreement."""
    tickets: list[dict[str, str]] = []
    for item in settlements:
        if not item.discarded:
            continue
        types = ", ".join(f"{kind or 'unknown'} ({n})" for kind, n in item.failing_types)
        tickets.append(
            {
                "title": f"Judge audit {item.batch}: the {item.task} audit format failed",
                "body": (
                    f"{item.uncertain} of {item.answered} {item.task} answers in batch "
                    f"{item.batch} were Uncertain (limit {int(audit.UNCERTAIN_LIMIT * 100)}%). "
                    "The format is judged broken for this task: that batch's labels are "
                    "discarded and the next batch shrinks. Item types that failed: "
                    f"{types or 'none recorded'}. No judge is changed by this ticket."
                ),
            }
        )
    for name, count in sorted(disagree.items()):
        if count >= audit.DISAGREEMENT_TICKET_MIN:
            tickets.append(
                {
                    "title": f"Judge audit: audit and adjudicator disagree on {name}",
                    "body": (
                        f"{count} audited {name} items in the last {audit.DISAGREEMENT_WINDOW_DAYS}"
                        " days were answered differently by a person and by the adjudicator. A "
                        "person's answer is one vote and flips nothing; this asks for a look at "
                        "the adjudicator's reliability on this task."
                    ),
                }
            )
    return tickets


def ingest(args: argparse.Namespace) -> int:
    from citypods.review_issues import decode_envelope, require_one_decision
    from citypods.statesync import pull_state, push_state

    site, state_dir, storage = _settings(args)
    pull_state(storage, state_dir, only_paths=_state_paths())
    body = Path(args.issue_body_file).read_text(encoding="utf-8")
    ident = str(decode_envelope(body).get("candidate_id") or "")
    choice = require_one_decision(body, audit.CHOICES)
    state = audit.AuditState.load(state_dir)
    if ident not in state.items:
        raise ValueError("audit candidate is no longer present in durable records")
    result = audit.record_answer(
        state,
        ident,
        choice,
        actor=args.actor,
        issue=args.issue_number,
        now=datetime.now(UTC),
    )
    if result["stored"]:
        paths = state.save(state_dir)
        push_state(storage, state_dir, only_paths=[paths[1].relative_to(state_dir)])
    print(json.dumps(result, sort_keys=True))
    return 0


def promote_entry(state: audit.AuditState, ident: str, now: datetime) -> tuple[str, dict[str, Any]]:
    """The gold entry for a clear-cut finalized answer (Supported or Not supported)."""
    item = state.items.get(ident)
    answer = state.answers.get(ident)
    if item is None or answer is None:
        raise ValueError("audit item has no recorded answer")
    batch = state.batches.get(item["batch"], {})
    if batch.get("status") != "finalized" or (batch.get("tasks") or {}).get(item["task"]) not in (
        None,
        "finalized",
    ):
        raise ValueError("audit item's batch is not finalized, or its labels were discarded")
    if answer["verdict"] == "uncertain":
        raise ValueError("an Uncertain answer is never a label")
    entry = {
        "id": f"{item['task']}-{ident[:10]}",
        "question_id": item["question_id"],
        "tier": item["tier"],
        "evidence": item["evidence"],
        "verdict": answer["verdict"] == "supported",
        "source": f"audit:{ident}",
        "added": now.date().isoformat(),
    }
    if make_evidence(item["tier"], item["evidence"]).digest != item["evidence_digest"]:
        raise ValueError("audit item's evidence differs from the digest it was asked with")
    return item["task"], entry


def promote(args: argparse.Namespace) -> int:
    from citypods.statesync import pull_state

    _site, state_dir, storage = _settings(args)
    pull_state(storage, state_dir, only_paths=_state_paths())
    state = audit.AuditState.load(state_dir)
    name, entry = promote_entry(state, args.item_id, datetime.now(UTC))
    root = Path(args.repo_root) / "evals" / "judge" if args.repo_root else DEFAULT_ROOT
    path = root / name / "gold.json"
    rows = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    if any(row.get("source") == entry["source"] for row in rows):
        print(json.dumps({"promoted": False, "reason": "already_promoted", "id": entry["id"]}))
        return 0
    rows.append(entry)
    parse_probes(task(name), rows)  # the whole list must stay valid
    rows.sort(key=lambda row: row["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {"promoted": True, "id": entry["id"], "task": name, "gating_at": GATING_ITEMS},
            sort_keys=True,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="citypods judge-audit")
    sub = parser.add_subparsers(dest="command", required=True)
    common: list[tuple[str, dict[str, Any]]] = [
        ("--site-config", {"default": "config/site_config.yml"}),
        ("--output-dir", {"default": "docs"}),
    ]
    package_parser = sub.add_parser("package", help="settle old batches and package this week")
    ingest_parser = sub.add_parser("ingest", help="ingest one answered audit issue")
    promote_parser = sub.add_parser("promote", help="promote a finalized answer to gold.json")
    for parser_ in (package_parser, ingest_parser, promote_parser):
        for flag, options in common:
            parser_.add_argument(flag, **options)
    package_parser.add_argument("--config-dir", default="config")
    package_parser.add_argument("--out-dir", required=True)
    ingest_parser.add_argument("--issue-number", required=True, type=int)
    ingest_parser.add_argument("--issue-body-file", required=True)
    ingest_parser.add_argument("--actor", required=True)
    promote_parser.add_argument("--item-id", required=True)
    promote_parser.add_argument("--repo-root", default=".")
    args = parser.parse_args(argv)
    return {"package": package, "ingest": ingest, "promote": promote}[args.command](args)


__all__ = ["main"]
