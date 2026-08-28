"""H16's authenticated human-review adapter.

The weekly proxy digest is evidence, not a decision.  This module turns each persisted empty-media
candidate into a durable, exactly-one-outcome review child.  It deliberately writes only the
``media_availability`` artifact block through the normal foreign-block-preserving record merge.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from citypods.availability import AVAILABLE, CONFIRMED_EMPTY, with_operator_override
from citypods.availability_digest import DIGEST_STATE_NAME, safe_stem
from citypods.records import ARTIFACT_BLOCKS, _availability_from_rec, load_records, save_records
from citypods.review_issues import render_decision_block, require_one_decision
from citypods.security import redact_subprocess_text

DECISIONS = ("Confirm empty", "Restore media")
_MARKER = re.compile(r"<!-- h16-candidate-b64: ([A-Za-z0-9_=-]+) -->")


def _encode(value: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(value, sort_keys=True).encode()).decode()


def _candidate_body(evidence: dict) -> str:
    payload = {
        key: evidence.get(key)
        for key in ("uid", "source_key", "state", "detector_version", "source_fingerprint")
    }
    title = " ".join(str(evidence.get("title") or payload["uid"]).split())
    watch = " ".join(
        str(redact_subprocess_text(evidence.get("canonical_source_url")) or "—").split()
    )
    return (
        f"<!-- h16-candidate-b64: {_encode(payload)} -->\n"
        f"# H16 availability review: {title}\n\n"
        f"Current detector state: `{evidence.get('state')}`.\n\n"
        f"Watch page: {watch}\n\n"
        "Listen to the matching evidence proxies from the parent batch artifact, then choose "
        "exactly one:\n\n"
        f"{render_decision_block(DECISIONS)}\n"
        "\nRationale (optional): \n"
    )


def review_key(value: dict) -> str:
    return ":".join(
        str(value.get(name) or "")
        for name in ("uid", "source_fingerprint", "detector_version", "state")
    )


def package_evidence(*, evidence: list[dict], out_dir: Path, state_dir: Path) -> dict:
    """Create a standard batch manifest, skipping candidates already durably resolved."""
    out_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / DIGEST_STATE_NAME
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        state = {}
    reviews = state.get("reviews") if isinstance(state.get("reviews"), dict) else {}
    children = []
    for row in evidence:
        key = review_key(row)
        if isinstance(reviews.get(key), dict) and reviews[key].get("status") == "resolved":
            continue
        filename = f"{safe_stem(str(row.get('uid') or ''))}.md"
        (out_dir / filename).write_text(_candidate_body(row), encoding="utf-8")
        children.append({"candidate_id": key, "body_file": filename})
    parent = (
        "# H16 empty-recording review batch\n\n"
        "Each child has a deterministic source-state key and one resolving decision. "
        "The evidence archive from this workflow run contains the matching proxies.\n"
    )
    (out_dir / "parent.md").write_text(parent, encoding="utf-8")
    manifest = {"version": 1, "children": children}
    (out_dir / "review-batch.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def mark_pending(*, out_dir: Path, state_dir: Path) -> int:
    """Record successfully published H16 children without treating publication as resolution."""
    manifest = json.loads((out_dir / "review-batch.json").read_text(encoding="utf-8"))
    state_path = state_dir / DIGEST_STATE_NAME
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {"schema_version": 2}
    reviews = state.setdefault("reviews", {})
    for child in manifest.get("children") or []:
        key = str(child.get("candidate_id") or "")
        existing = reviews.get(key)
        already_resolved = isinstance(existing, dict) and existing.get("status") == "resolved"
        if key and not already_resolved:
            reviews[key] = {"status": "pending", "published_at": datetime.now(UTC).isoformat()}
    state["schema_version"] = 2
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return len(manifest.get("children") or [])


def ingest(args: argparse.Namespace) -> int:
    from citypods.config import load_site_config
    from citypods.state import resolve_state_dir
    from citypods.statesync import pull_state, push_records_merged, push_state
    from citypods.storage import make_storage

    body = Path(args.issue_body_file).read_text(encoding="utf-8")
    marker = _MARKER.search(body)
    if not marker:
        raise ValueError("missing H16 candidate marker")
    candidate = json.loads(base64.urlsafe_b64decode(marker.group(1)).decode())
    decision = require_one_decision(body, DECISIONS)
    source_key = str(candidate.get("source_key") or "")
    uid = str(candidate.get("uid") or "")
    if not source_key or not uid:
        raise ValueError("H16 candidate marker has no source identity")
    site = load_site_config(args.site_config)
    output_dir = Path(args.output_dir)
    state_dir = resolve_state_dir(site, output_dir)
    storage = make_storage(site, site.get("base_url", ""), output_dir)
    pull_state(storage, state_dir)
    records = load_records(state_dir, source_key)
    raw = records.get(uid)
    if not isinstance(raw, dict):
        raise ValueError("H16 candidate is no longer present in durable records")
    av = raw.get("media_availability") if isinstance(raw.get("media_availability"), dict) else {}
    for key in ("state", "detector_version", "source_fingerprint"):
        if candidate.get(key) != av.get(key):
            raise ValueError(f"H16 candidate differs from durable media availability field {key}")
    prior = _availability_from_rec(raw)
    target = CONFIRMED_EMPTY if decision == "Confirm empty" else AVAILABLE
    rationale_match = re.search(r"(?mi)^Rationale \(optional\):\s*(.+)$", body)
    rationale = rationale_match.group(1).strip() if rationale_match else decision
    reason = f"GitHub review #{args.issue_number} by @{args.actor}: {decision}; {rationale}"
    raw["media_availability"] = dataclasses.asdict(with_operator_override(prior, target, reason))
    save_records(state_dir, source_key, records)
    push_records_merged(
        storage,
        state_dir,
        [source_key],
        protected_blocks=ARTIFACT_BLOCKS - {"media_availability"},
        lane="availability-review",
        raise_on_transient=True,
    )
    digest_path = state_dir / DIGEST_STATE_NAME
    try:
        digest = json.loads(digest_path.read_text())
    except (OSError, ValueError):
        digest = {"schema_version": 2}
    reviews = digest.setdefault("reviews", {})
    candidate_key = review_key(candidate)
    reviews[candidate_key] = {
        "status": "resolved",
        "decision": decision,
        "issue_number": args.issue_number,
        "issue_url": args.issue_url,
        "actor": args.actor,
        "rationale": rationale,
        "resolved_at": datetime.now(UTC).isoformat(),
    }
    digest["schema_version"] = 2
    digest_path.write_text(json.dumps(digest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    push_state(storage, state_dir, only_prefixes=[DIGEST_STATE_NAME])
    print(json.dumps({"stored": True, "decision": decision, "candidate_id": candidate_key}))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="citypods availability-review")
    sub = parser.add_subparsers(dest="command", required=True)
    ingest_parser = sub.add_parser("ingest")
    ingest_parser.add_argument("--site-config", default="config/site_config.yml")
    ingest_parser.add_argument("--output-dir", default="docs")
    ingest_parser.add_argument("--issue-number", required=True, type=int)
    ingest_parser.add_argument("--issue-body-file", required=True)
    ingest_parser.add_argument("--actor", required=True)
    ingest_parser.add_argument("--issue-url", default="")
    args = parser.parse_args(argv)
    return ingest(args)
