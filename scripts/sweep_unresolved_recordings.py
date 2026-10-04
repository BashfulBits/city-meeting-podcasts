#!/usr/bin/env python
"""Report unresolved recordings from local cache only; never dispatch a remedy.

Run with PYTHONPATH=. from the repository root. Configured sources absent from the local
cache are outside this snapshot; this command does not establish live archive completeness.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from citypods.bodies import record_matches_body, source_body_filter, source_body_inclusions
from citypods.config import load_city_configs
from citypods.records import source_key

_REQUIRED_STRINGS = {
    "case_id",
    "city",
    "source_key",
    "uid",
    "provider_guid",
    "question",
    "missing_evidence",
    "next_action",
    "last_researched",
    "status",
}


def load_cases(path: Path) -> list[dict]:
    """Validate a durable register without changing it."""
    envelope = json.loads(path.read_text())
    if not isinstance(envelope, dict) or type(envelope.get("schema_version")) is not int:
        raise ValueError("case register requires integer schema_version 1")
    if envelope["schema_version"] != 1 or not isinstance(envelope.get("cases"), list):
        raise ValueError("case register requires schema_version 1 and cases list")
    ids, identities = set(), set()
    for case in envelope["cases"]:
        if not isinstance(case, dict) or any(
            not isinstance(case.get(key), str) or not case[key].strip() for key in _REQUIRED_STRINGS
        ):
            raise ValueError("case requires nonempty string identity and evidence fields")
        assignments = case.get("current_feed_assignments")
        if (
            not isinstance(assignments, list)
            or any(not isinstance(slug, str) or not slug.strip() for slug in assignments)
            or len(assignments) != len(set(assignments))
        ):
            raise ValueError("current_feed_assignments requires unique nonempty strings")
        researched = date.fromisoformat(case["last_researched"])
        if researched.isoformat() != case["last_researched"]:
            raise ValueError("last_researched requires ISO YYYY-MM-DD")
        disposition = case.get("disposition")
        if "disposition" not in case or case["status"] not in {"open", "resolved"}:
            raise ValueError("case requires open/resolved status and disposition")
        if case["status"] == "open" and disposition is not None:
            raise ValueError("open case disposition must be null")
        if case["status"] == "resolved" and (
            not isinstance(disposition, str) or not disposition.strip()
        ):
            raise ValueError("resolved case requires evidenced disposition")
        identity = (case["source_key"], case["uid"])
        if case["case_id"] in ids or identity in identities:
            raise ValueError("duplicate case ID or source/UID identity")
        ids.add(case["case_id"])
        identities.add(identity)
    return sorted(envelope["cases"], key=lambda c: (c["source_key"], c["uid"], c["case_id"]))


def build_report(state_root: Path, config_root: Path, case_register: Path) -> dict:
    """Compare all cached raw recordings with actual selectors and persistent obligations."""
    cases = load_cases(case_register)
    groups = {}
    for city in load_city_configs(config_root, {}):
        groups.setdefault(source_key(city), []).append(city)
    by_identity = {(c["source_key"], c["uid"]): c for c in cases}
    rows = {}
    for path in sorted((state_root / "sources").glob("*/episodes.json")):
        key = path.parent.name
        if key not in groups:
            raise ValueError(f"cached source {key} has no configured city binding")
        feeds = sorted(groups[key], key=lambda c: c.slug)
        city_ids = {c.city_entity for c in feeds}
        if len(city_ids) != 1:
            raise ValueError(f"source {key} has ambiguous city binding")
        payload = json.loads(path.read_text())
        records = payload.get("episodes") if isinstance(payload, dict) else None
        if not isinstance(records, dict):
            raise ValueError(f"{path}: requires episodes mapping")
        for uid, record in sorted(records.items()):
            if not isinstance(record, dict) or record.get("uid") != uid:
                raise ValueError(f"{path}: invalid record identity {uid}")
            assignments = [
                c.slug
                for c in feeds
                if record_matches_body(
                    record, source_body_filter(c.source), source_body_inclusions(c.source)
                )
            ]
            case = by_identity.get((key, uid))
            if case and (
                case["city"] not in city_ids
                or case["provider_guid"] != str(record.get("provider_guid") or "")
            ):
                raise ValueError(f"case {case['case_id']} does not match cached city/GUID")
            row = {
                "city": next(iter(city_ids)),
                "source_key": key,
                "uid": uid,
                "provider_guid": str(record.get("provider_guid") or ""),
                "body": record.get("body"),
                "title": record.get("title"),
                "date": record.get("published"),
                "current_feed_assignments": assignments,
            }
            if case and case["status"] == "resolved":
                row["disposition"] = case["disposition"]
            rows[(key, uid)] = row
    report = {
        "schema_version": 1,
        "uncovered_recordings": [r for r in rows.values() if not r["current_feed_assignments"]],
        "open_cases": [],
        "assignment_changes": [],
        "missing_registered_records": [],
    }
    for case in cases:
        row = rows.get((case["source_key"], case["uid"]))
        if row is None:
            report["missing_registered_records"].append(case)
            continue
        if case["status"] == "open":
            report["open_cases"].append({**case, **row})
        if sorted(case["current_feed_assignments"]) != row["current_feed_assignments"]:
            report["assignment_changes"].append(
                {
                    **row,
                    "case_id": case["case_id"],
                    "previous_feed_assignments": sorted(case["current_feed_assignments"]),
                }
            )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("state-root", "config-root", "case-register", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if (
        output == args.case_register.resolve()
        or output.is_relative_to(args.state_root.resolve())
        or output.is_relative_to(args.config_root.resolve())
    ):
        parser.error("output must not overwrite the register, cache or config")
    report = build_report(args.state_root, args.config_root, args.case_register)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
