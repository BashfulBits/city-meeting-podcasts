#!/usr/bin/env python3
"""Record remedy PR and issue feedback, `/remedy reopen`, and reconciliation in the P2 ledger.

Run by `remedy-feedback.yml` (webhook events and a daily reconciliation) and `remedy-commands.yml`
(the reopen command), always from `main`, never from a PR head, and with no model provider keys:
only storage access to append to the remedy ledger under its maintenance lease.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from citypods import remedy_feedback as fb
from citypods.config import load_site_config
from citypods.ops.maintenance_leases import acquire
from citypods.remedy_ledger import REMEDY_LEASE_KEY
from citypods.storage import make_storage


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _storage():
    site = load_site_config(Path("config/site_config.yml"))
    storage = make_storage(site, site.get("base_url", ""), Path(site.get("output_dir", "docs")))
    if storage is None or not getattr(storage, "cas_capable", False):
        raise RuntimeError("remedy feedback requires B2 storage and R2 CAS coordination")
    return storage


def _lease(storage, owner: str):
    return acquire(storage, owner=owner, key=REMEDY_LEASE_KEY)


def apply_event(
    event_name: str, payload: dict, permission: dict, repository: str, *, storage, lease_factory
):
    """A webhook signal: verified, checked against the actor's access, then recorded."""
    signal = fb.signal_from_event(event_name, payload, repository=repository)
    sender = (payload.get("sender") or {}).get("login")
    if (permission.get("user") or {}).get("login") != sender:
        raise fb.FeedbackError("permission does not belong to the sender")
    try:
        fb.require_repository_write(dict(permission))
    except fb.RepositoryPermissionError as exc:
        raise fb.FeedbackError(str(exc)) from exc
    with lease_factory() as lease:
        keys = fb.record_signal(storage, signal, lease=lease)
    return {"recorded": len(keys), "keys": keys, "decisions": len(signal.decision_ids)}


def apply_reopen(
    event: dict, permission: dict, repository: str, artifact: dict | None, *, storage, lease_factory
):
    """A maintainer's `/remedy reopen <decision_id>` on a remedy PR or issue."""
    comment = event.get("comment") or {}
    if artifact is None:
        artifact, kind = event.get("issue") or {}, "issue"
    else:
        kind = "pr"
    with lease_factory() as lease:
        reopened, key = fb.reopen_command(
            storage,
            comment=comment,
            permission=permission,
            repository=repository,
            artifact=artifact,
            kind=kind,
            lease=lease,
        )
    return {"recorded": 1, "key": key, "decision": reopened.decision_id}


def apply_reconcile(snapshot, repository: str, *, storage, lease_factory):
    """Recover feedback a webhook missed: each settled, verified remedy artifact in the listing."""
    if not snapshot.complete:
        return {"recorded": 0, "checked": 0, "reason": "github_snapshot_incomplete"}
    signals = [
        s
        for artifact in snapshot.artifacts
        if (s := fb.signal_from_snapshot(artifact, repository=repository)) is not None
    ]
    recorded = 0
    with lease_factory() as lease:
        for signal in signals:
            recorded += len(fb.record_signal(storage, signal, lease=lease))
    return {"recorded": recorded, "checked": len(signals)}


def _public(exc: Exception) -> str:
    text = str(exc)
    if isinstance(exc, fb.FeedbackError) and "\n" not in text:
        return text
    return "remedy feedback could not be recorded; see the workflow log"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("event", "reopen"):
        one = sub.add_parser(name)
        one.add_argument("--event", required=True, type=Path)
        one.add_argument("--permission", required=True, type=Path)
        one.add_argument("--repository", required=True)
        one.add_argument("--result", required=True, type=Path)
    sub.choices["event"].add_argument("--event-name", required=True)
    sub.choices["reopen"].add_argument("--artifact", type=Path)
    reconcile = sub.add_parser("reconcile")
    reconcile.add_argument("--repository", required=True)
    reconcile.add_argument("--result", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        storage = _storage()

        def lease_factory(owner=f"remedy-feedback-{args.command}"):
            return _lease(storage, owner)

        if args.command == "event":
            result = apply_event(
                args.event_name,
                _json(args.event),
                _json(args.permission),
                args.repository,
                storage=storage,
                lease_factory=lease_factory,
            )
        elif args.command == "reopen":
            artifact = _json(args.artifact) if args.artifact else None
            result = apply_reopen(
                _json(args.event),
                _json(args.permission),
                args.repository,
                artifact,
                storage=storage,
                lease_factory=lease_factory,
            )
        else:
            from scripts.remedy_p3_publish import _fetch_snapshot

            result = apply_reconcile(
                _fetch_snapshot(), args.repository, storage=storage, lease_factory=lease_factory
            )
        result["accepted"] = True
    except Exception as exc:  # reported to the workflow without exposing secrets
        result = {"accepted": False, "error": _public(exc)}
    args.result.parent.mkdir(parents=True, exist_ok=True)
    args.result.write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True))
    # An untrusted or irrelevant event is a normal outcome, not a failed run; only an
    # infrastructure failure (no storage, a lost lease) makes the workflow red.
    return 1 if not result["accepted"] and "could not be recorded" in result.get("error", "") else 0


if __name__ == "__main__":
    raise SystemExit(main())
