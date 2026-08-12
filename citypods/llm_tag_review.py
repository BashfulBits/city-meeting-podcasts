"""Package and ingest the weekly R5 unified tag calibration review.

This feature adapter discovers R5 candidates from episode records and supplies the GitHub
workflow's files; :mod:`citypods.llm_evaluation` owns the calibration matrix and issue-evidence
contract.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from citypods.config import load_city_configs, load_site_config
from citypods.llm_evaluation import (
    candidate_id,
    config_from_mapping,
    ingest_review_body,
    load_state,
    prelabeler_review_candidate,
    refresh_matrix,
    render_digest,
    render_review_body,
    save_state,
    select_review_candidates,
)
from citypods.records import load_records, record_to_episode, source_key
from citypods.state import resolve_state_dir
from citypods.statesync import pull_state, push_state
from citypods.storage import make_storage
from citypods.tags import load_taxonomy


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _paths(args: argparse.Namespace, *, pull_only_paths=None):
    site_config = load_site_config(args.site_config)
    output_dir = Path(args.output_dir)
    state_dir = resolve_state_dir(site_config, output_dir)
    storage = make_storage(site_config, site_config.get("base_url", ""), output_dir)
    _log("ingest: pulling state from storage…")
    restored = pull_state(storage, state_dir, only_paths=pull_only_paths, log=_log)
    _log(f"ingest: pull_state done ({restored} file(s) restored)")
    raw = (site_config.get("tagging") or {}).get("evaluation") or {}
    return site_config, output_dir, state_dir, storage, config_from_mapping(raw)


def collect_candidates(state_dir: Path, config_dir: str, site_config: dict) -> list[dict]:
    candidates: dict[str, dict] = {}
    cities = load_city_configs(config_dir, site_config.get("defaults", {}))
    for city in cities:
        for record in load_records(state_dir, source_key(city)).values():
            episode = record_to_episode(record)
            for raw in episode.llm_tag_candidates:
                if not isinstance(raw, dict):
                    continue
                value = dict(raw)
                # Superseded chapter/recipe rows remain in the canonical ledger for audit, but
                # they are not active weekly-review subjects. Reviewing them would reintroduce
                # legacy episode-level candidates into the chapter-only calibration path.
                if value.get("candidate_state") == "historical":
                    continue
                value.setdefault("source_key", source_key(city))
                value.setdefault("city_slug", city.slug)
                value.setdefault("episode_uid", episode.uid)
                value.setdefault("episode_title", episode.title)
                value.setdefault("source_kind", "llm")
                value.setdefault("assessment_kind", "tagger-admission")
                value.setdefault("scope", "chapter" if value.get("chapter_id") else "episode")
                # Records written before the unified ledger did not carry candidate_id.  Give
                # them their stable compatibility identity here so they remain reviewable instead
                # of silently disappearing from every future weekly packet.
                value["candidate_id"] = str(value.get("candidate_id") or candidate_id(value))
                candidate_key = value["candidate_id"]
                candidates[candidate_key] = value
                if value.get("prelabeler_decision") in {
                    "likely_correct",
                    "needs_human_review",
                    "likely_incorrect",
                }:
                    audit = prelabeler_review_candidate(value)
                    audit.setdefault("source_key", source_key(city))
                    audit.setdefault("city_slug", city.slug)
                    candidates[str(audit["candidate_id"])] = audit
    return [candidates[key] for key in sorted(candidates)]


def collect_rule_audits(state_dir: Path, config_dir: str, site_config: dict) -> list[dict]:
    """Collect deterministic include/exclude observations without making them review subjects."""
    audits: list[dict] = []
    seen: set[tuple[str, str, str, str, str, str, str]] = set()
    cities = load_city_configs(config_dir, site_config.get("defaults", {}))
    for city in cities:
        for record in load_records(state_dir, source_key(city)).values():
            episode = record_to_episode(record)
            for attempt in episode.tags_llm_call_attempts:
                if not isinstance(attempt, dict) or attempt.get("purpose") != "topic-tags:rules":
                    continue
                observations = attempt.get("rule_audit")
                if not isinstance(observations, list):
                    continue
                for observation in observations:
                    if not isinstance(observation, dict):
                        continue
                    scope = str(attempt.get("scope") or "episode")
                    chapter_id = str(attempt.get("chapter_id") or "")
                    identity = (
                        str(episode.uid or ""),
                        chapter_id,
                        scope,
                        str(observation.get("tag_id") or ""),
                        str(observation.get("kind") or ""),
                        str(observation.get("pattern") or ""),
                        str(observation.get("where") or ""),
                    )
                    if identity in seen:
                        continue
                    seen.add(identity)
                    audits.append(
                        {
                            **observation,
                            "source_key": source_key(city),
                            "city_slug": city.slug,
                            "episode_uid": episode.uid,
                            "episode_title": episode.title,
                            "scope": scope,
                            "chapter_id": attempt.get("chapter_id"),
                            "attempt_id": attempt.get("attempt_id"),
                        }
                    )
    return audits


def package(args: argparse.Namespace) -> int:
    # Full pull: package needs all source episode records to collect candidates.
    site_config, output_dir, state_dir, storage, config = _paths(args)
    state_path = state_dir / config.state_path
    state = load_state(state_path)
    refresh_matrix(state, config=config)
    candidates = collect_candidates(state_dir, args.config_dir, site_config)
    rule_audits = collect_rule_audits(state_dir, args.config_dir, site_config)
    selected = select_review_candidates(candidates, state=state, config=config)
    taxonomy = load_taxonomy(
        (site_config.get("tagging") or {}).get("taxonomy_path", "config/taxonomy.yml")
    )
    if args.limit is not None:
        selected = selected[: max(0, args.limit)]
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "parent.md").write_text(
        render_digest(
            candidates,
            selected=selected,
            config=config,
            state=state,
            taxonomy=taxonomy,
            rule_audits=rule_audits,
        ),
        encoding="utf-8",
    )
    children = []
    for candidate in selected:
        candidate_id = str(candidate["candidate_id"])
        body_file = out_dir / f"{candidate_id}.md"
        body_file.write_text(
            render_review_body(candidate, config=config, state=state), encoding="utf-8"
        )
        children.append({"candidate_id": candidate_id, "body_file": str(body_file)})
    (out_dir / "review-batch.json").write_text(
        json.dumps({"version": 1, "children": children}, indent=2) + "\n", encoding="utf-8"
    )
    save_state(state_path, state)
    push_state(storage, state_dir, only_paths=[config.state_path], log=_log)
    print(f"packaged {len(children)} unified R5 tag review issue(s)")
    return 0


def ingest(args: argparse.Namespace) -> int:
    _log(f"ingest: starting issue #{args.issue_number}")
    # Resolve config first so we can scope the pull to just the evaluation state file.
    site_config = load_site_config(args.site_config)
    raw = (site_config.get("tagging") or {}).get("evaluation") or {}
    config = config_from_mapping(raw)
    site_config, output_dir, state_dir, storage, config = _paths(
        args, pull_only_paths=[config.state_path]
    )
    state_path = state_dir / config.state_path
    _log("ingest: loading evaluation state")
    state = load_state(state_path)
    body = Path(args.issue_body_file).read_text(encoding="utf-8")
    _log("ingest: parsing and recording review decision")
    try:
        review = ingest_review_body(
            state,
            body,
            config=config,
            actor=args.actor,
            issue_number=args.issue_number,
            issue_url=args.issue_url,
        )
    except ValueError as exc:
        if "no LLM review decision checked" in str(exc):
            _log("ingest: no decision checked; skipping")
            print(json.dumps({"stored": False, "reason": "no_decision_checked"}, indent=2))
            return 0
        raise
    _log(f"ingest: decision recorded — {review['decision']} for {review['candidate_id']}")
    save_state(state_path, state)
    _log("ingest: pushing evaluation state")
    push_state(storage, state_dir, only_paths=[config.state_path], log=_log)
    _log("ingest: push complete")
    print(
        json.dumps(
            {
                "stored": True,
                "candidate_id": review["candidate_id"],
                "decision": review["decision"],
                "comment": (
                    f"Stored unified R5 calibration decision `{review['decision']}` for "
                    f"`{review['candidate_id']}`."
                ),
            },
            indent=2,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    package_parser = sub.add_parser("package", help="package the weekly GitHub review issues")
    package_parser.add_argument("--site-config", default="config/site_config.yml")
    package_parser.add_argument("--config-dir", default="config")
    package_parser.add_argument("--output-dir", default="docs")
    package_parser.add_argument("--out-dir", required=True)
    package_parser.add_argument("--limit", type=int, default=None, help="optional review-child cap")
    ingest_parser = sub.add_parser("ingest", help="ingest a checked review issue")
    ingest_parser.add_argument("--site-config", default="config/site_config.yml")
    ingest_parser.add_argument("--output-dir", default="docs")
    ingest_parser.add_argument("--issue-number", required=True, type=int)
    ingest_parser.add_argument("--issue-body-file", required=True)
    ingest_parser.add_argument("--actor", default="")
    ingest_parser.add_argument("--issue-url", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "package":
        return package(args)
    return ingest(args)


if __name__ == "__main__":
    raise SystemExit(main())
