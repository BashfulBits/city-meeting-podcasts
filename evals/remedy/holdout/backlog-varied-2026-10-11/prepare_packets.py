import json
from pathlib import Path

import yaml

root = Path(__file__).resolve().parents[4]
out = root / "evals/remedy/holdout/backlog-varied-2026-10-11"
packets = json.loads((out / "evidence-packets.json").read_text())["cases"]
review = {c["id"]: c for c in json.loads((out / "candidate-review.json").read_text())["cases"]}
feeds = {}
for path in sorted((root / "config/feeds").glob("*.yml")):
    d = yaml.safe_load(path.read_text())
    if isinstance(d, dict):
        city = d.get("city")
        feeds.setdefault(city, []).append(
            {
                "slug": d.get("slug", path.stem),
                "title": d.get("podcast_title"),
                "description": d.get("podcast_description"),
            }
        )
# Exact previous recording identity includes provider GUID: retained UID variants cannot hide
# overlap.
seen = []
for path in [
    root / "evals/remedy/manifest.json",
    root / "evals/remedy/holdout/p1-denton-2026-10-10/manifest.json",
]:
    d = json.loads(path.read_text())
    for c in d["cases"]:
        for r in c["recordings"]:
            seen.append(
                (c["source_key"], r.get("uid"), r["provider_guid"], str(path.relative_to(root)))
            )
model = []
drafts = []
for p in packets:
    c = review[p["id"]]
    doc = p["official_document"]
    text = doc.get("excerpt", "") if doc else ""
    owners = []
    rationale = (
        "Evidence needs independent content review; no owner inferred from "
        "a title or current assignment."
    )
    if text:
        title = c["title"].lower()
        if c["city"] == "arlington-tx":
            owners = [
                "arlington-tx-environmental-task-force"
                if "environmental" in title
                else "arlington-tx-community-and-neighborhood-development-committee"
            ]
        elif c["city"] == "dallas-tx":
            owners = ["dallas-tx-redistricting-commission"]
        elif c["city"] == "denton-tx":
            if "joint" in title:
                owners = ["denton-tx-city-council", "denton-tx-library-board"]
            elif "health" in title:
                owners = ["denton-tx-health-building-standards-commission"]
            elif "library" in title:
                owners = ["denton-tx-library-board"]
            elif "bond" in title:
                owners = ["denton-tx-special-citizens-bond-advisory-committee"]
            elif "no. 2" in title:
                owners = ["denton-tx-tirz-2-board"]
            elif "zone no. 1" in title:
                owners = ["denton-tx-tirz-1-board"]
        elif c["city"] == "fort-worth-tx":
            owners = ["fort-worth-tx-city-council", "fort-worth-tx-emsisd-board-joint-meetings"]
        rationale = (
            "Draft based on recovered official agenda naming the convening "
            "body; verify date and configured feed identity before approval."
        )
    # Resolve guessed display aliases against actual inventory; never silently invent a slug.
    inventory = feeds.get(c["city"], [])
    slugs = {f["slug"] for f in inventory}
    unresolved = [o for o in owners if o not in slugs]
    overlaps = sorted(
        {
            path
            for source, uid, guid, path in seen
            if source == c["source_key"] and (uid == c["uid"] or guid == c["provider_guid"])
        }
    )
    alternatives = [f["slug"] for f in inventory if f["slug"] not in owners][:3]
    drafts.append(
        {
            "id": c["id"],
            "expected_feeds_draft": owners,
            "unresolved_feed_slugs": unresolved,
            "rationale": rationale,
            "truth_status": "draft-not-approved",
            "overlap": overlaps,
            "slice": "previously-tested-challenge"
            if overlaps
            else "new-challenge-pending-split-audit",
            "questions": [{"kind": "positive-membership", "feed": o} for o in owners]
            + [{"kind": "plausible-negative-membership", "feed": o} for o in alternatives],
            "needs": ["Confirm official body/date/source binding and expected answer"]
            if text
            else [
                (
                    "Locate official content evidence, or design an explicit "
                    "insufficient-evidence question"
                )
            ],
        }
    )
    model.append(
        {
            "id": p["id"],
            "city": p["city"],
            "source_key": p["source_key"],
            "recording": {
                "uid": p["uid"],
                "provider_guid": p["provider_guid"],
                "title": p["title"],
                "body": c.get("body"),
            },
            "candidate_feeds": inventory,
            "evidence": [{"url": doc["url"], "span": text[:4000], "document_sha256": doc["sha256"]}]
            if text
            else [],
            "policy": {
                "identity": (
                    "Officially convened bodies determine identity; venue and shared "
                    "topics do not. Joint membership requires participant proof. "
                    "Ordinary public comment does not by itself create Public Info "
                    "membership. Missing proof permits abstention."
                )
            },
            "task": (
                "Propose supported existing-feed memberships or explain "
                "insufficient evidence; do not invent a new identity."
            ),
        }
    )
with (out / "evidence-model-inputs.json").open("x") as f:
    json.dump({"kind": "draft-inputs-not-frozen-manifest", "cases": model}, f, indent=2)
with (out / "expected-decisions-draft.json").open("x") as f:
    json.dump({"kind": "review-draft-not-approved-gold", "cases": drafts}, f, indent=2)
size = [len(json.dumps(p, ensure_ascii=False)) for p in model]
summary = {
    "unique_recordings": 60,
    "official_documents_retrieved": sum(bool(p["official_document"]) for p in packets),
    "draft_feed_sets_from_agendas": sum(bool(d["expected_feeds_draft"]) for d in drafts),
    "evidence_pending": sum(not bool(p["official_document"]) for p in packets),
    "prior_recording_overlap": sum(bool(d["overlap"]) for d in drafts),
    "unresolved_feed_slugs": {
        d["id"]: d["unresolved_feed_slugs"] for d in drafts if d["unresolved_feed_slugs"]
    },
    "model_packet_characters": {
        "minimum": min(size),
        "maximum": max(size),
        "median": sorted(size)[len(size) // 2],
    },
    "measurement_caveat": (
        "Characters, not tokenizer counts. Includes first 4000 agenda "
        "characters, candidate display names/descriptions; full documents "
        "retained separately. No per-UID selector pins in model input."
    ),
}
with (out / "preparation-status.json").open("x") as f:
    json.dump(summary, f, indent=2)
print(json.dumps(summary, indent=2))
