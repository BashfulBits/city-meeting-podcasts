"""Build a diverse backlog preparation set; never manufacture approved truth."""

import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
OUT = Path(__file__).resolve().parent
REGISTER = ROOT / "review/evidence/unresolved-recording-cases.json"
INVENTORY = ROOT / "review/evidence/historical-selector-replay-2026-10-06.csv"


def bucket(title):
    text = title.lower()
    for label, pattern in [
        ("joint-or-consecutive", r"joint|combined|ccpd|school district"),
        ("bond-program", r"bond|task force|subcommittee"),
        ("numbered-district", r"tirz|reinvestment|tax increment"),
        ("community-information", r"public input|town hall|luncheon|briefing|community meeting"),
        ("non-meeting-or-archive", r"destination|vitruvian|addison way|ceremony|staff|applicant"),
        ("planning-versus-council", r"planning|p&z|p.z.|work session"),
    ]:
        if re.search(pattern, text):
            return label
    return "distinct-or-sparse-body"


rows = list(csv.DictReader(INVENTORY.open()))
lookup = {(r["source_key"], r["uid"]): r for r in rows}
fixtures = {}


def retain_records(data, fixture):
    if isinstance(data, dict):
        if data.get("uid") and data.get("title"):
            fixtures[data["uid"]] = (data, fixture)
        for value in data.values():
            retain_records(value, fixture)
    elif isinstance(data, list):
        for value in data:
            retain_records(value, fixture)


for path in sorted((ROOT / "tests/fixtures").glob("*.json")):
    try:
        retain_records(json.loads(path.read_text()), str(path.relative_to(ROOT)))
    except (ValueError, OSError):
        continue
register = json.loads(REGISTER.read_text())["cases"]
city_by_source = {c["source_key"]: c["city"] for c in register}

candidates = []
for case in json.loads(REGISTER.read_text())["cases"]:
    row = lookup.get((case["source_key"], case["uid"]), {})
    record, fixture = fixtures.get(case["uid"], ({}, None))
    title = record.get("title") or row.get("record_title") or row.get("body")
    if not title:
        continue
    candidates.append(
        {
            "id": case["case_id"],
            "city": case["city"],
            "source_key": case["source_key"],
            "uid": case["uid"],
            "provider_guid": case["provider_guid"],
            "title": title,
            "body": record.get("body") or row.get("record_body"),
            "category": bucket(title + " " + case["question"]),
            "recording_fixture": fixture,
            "historical_question": case["question"],
            "review_only": {
                "register_status": case["status"],
                "recorded_disposition": case.get("disposition"),
                "recorded_assignments": case["current_feed_assignments"],
                "missing_evidence": case["missing_evidence"],
            },
            "truth_status": "requires-independent-evidence-review",
        }
    )
# Balance cities and problem categories. Select unique real recording identities, not variants.
groups = defaultdict(list)
for case in candidates:
    groups[(case["city"], case["category"])].append(case)
selected = []
for group in sorted(groups):
    selected.extend(sorted(groups[group], key=lambda c: c["id"])[:4])
# Keep a separate sparse/untracked slice from the original unmatched inventory.
path = ROOT / "review/evidence/p0-final-unmatched-records-2026-10-07.csv"
seen = {(c["source_key"], c["uid"]) for c in selected}
for row in csv.DictReader(path.open()):
    key = (row["source_key"], row["uid"])
    if key in seen or len(selected) >= 60:
        continue
    selected.append(
        {
            "id": "untracked-" + row["source_key"] + "-" + row["uid"],
            "city": city_by_source.get(row["source_key"], "source-city-needs-verification"),
            "source_key": row["source_key"],
            "uid": row["uid"],
            "provider_guid": row["provider_guid"],
            "title": row["title"],
            "body": row["body"],
            "category": "sparse-or-untracked",
            "truth_status": "requires-independent-evidence-review",
            "review_only": {"historical_disposition": row["plain_language_disposition"]},
        }
    )
    seen.add(key)
inputs = [REGISTER, INVENTORY, path]
provenance = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs}
for name, data in [
    (
        "candidate-review.json",
        {
            "kind": "preparation-not-admission-holdout",
            "input_hashes": provenance,
            "cases": selected,
        },
    ),
    (
        "model-input-drafts.json",
        {
            "cases": [
                {
                    k: v
                    for k, v in c.items()
                    if k not in {"review_only", "truth_status", "historical_question"}
                }
                for c in selected
            ]
        },
    ),
    (
        "summary.json",
        {
            "unique_recordings": len(selected),
            "cities": dict(Counter(c["city"] for c in selected)),
            "categories": dict(Counter(c["category"] for c in selected)),
            "approved_gold_count": 0,
        },
    ),
]:
    with (OUT / name).open("x") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
print(json.dumps(json.loads((OUT / "summary.json").read_text()), indent=2))
