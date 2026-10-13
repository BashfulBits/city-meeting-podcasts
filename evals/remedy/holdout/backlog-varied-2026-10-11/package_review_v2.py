"""Build a review-only answer packet; current selector replay never manufactures gold."""

import json
from collections import Counter
from pathlib import Path

import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[4]
OUT = Path(__file__).resolve().parent / "packet-v2"


def plaintext(path):
    text = path.read_text(errors="replace")
    if "<html" in text.lower() or "<!doctype" in text.lower():
        soup = BeautifulSoup(text, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()
        return " ".join(soup.stripped_strings)
    return text


def proposed(record):
    title = record["title"].lower()
    city = record["city"]
    if city == "addison-tx":
        if "bond advisory" in title:
            return ["addison-tx-bond-committees"], "Bond Advisory Committee", "assignment"
        if "citizen advisory" in title:
            return (
                ["addison-tx-town-meetings"],
                "Historical citizen advisory compilation",
                "assignment",
            )
        if "joint" in title:
            targets = ["addison-tx-city-council", "addison-tx-planning-and-zoning-commission"]
            if "cpac" in title:
                targets.append("addison-tx-comprehensive-plan-advisory-committee")
            return targets, "Joint convening of the explicitly named bodies", "assignment"
        if "combined" in title:
            return ["addison-tx-city-council"], "City Council", "assignment"
        if "tirz" in title:
            return ["addison-tx-tif"], "TIRZ No. 1 Board", "assignment"
        if "work session" in title:
            return ["addison-tx-planning-and-zoning-commission"], "P&Z Commission", "assignment"
        if "luncheon" in title:
            return (
                [],
                "Luncheon; possible Council attendance is not Council convening",
                "archive-only",
            )
        return (
            [],
            "Ceremony/dedication; official title and approved event policy",
            "event-exclusion",
        )
    if city == "arlington-tx":
        target = (
            "arlington-tx-environmental-task-force"
            if "environmental" in title
            else "arlington-tx-community-and-neighborhood-development-committee"
        )
        return [target], record["body"], "assignment"
    if city == "dallas-tx":
        if "redistrict" in title:
            return ["dallas-tx-redistricting-commission"], "Redistricting Commission", "assignment"
        if "streets" in title:
            targets = [
                "dallas-tx-2024-bond-streets-and-transportation-subcommittee",
                "dallas-tx-bond-program-meetings",
            ]
            if record["provider_guid"] == "270975":
                targets.append("dallas-tx-transportation-and-infrastructure")
            return (
                targets,
                "Bond Streets subcommittee; wider feeds are publication policy",
                "assignment",
            )
        return ["dallas-tx-2024-community-bond-task-force"], "2024 CBTF", "assignment"
    if city == "denton-tx":
        if "women voters" in title:
            return [], "Candidate forum; not a Council meeting", "archive-series"
        if "animal shelter" in title:
            return (
                [],
                "Real Animal Shelter Advisory Committee; no existing local feed",
                "real-body-untracked",
            )
        if "joint" in title:
            return (
                ["denton-tx-city-council", "denton-tx-library-board"],
                "Council/Library joint",
                "assignment",
            )
        if "health" in title:
            return (
                ["denton-tx-health-building-standards-commission"],
                "HBS Commission",
                "assignment",
            )
        if "library" in title:
            return ["denton-tx-library-board"], "Library Board", "assignment"
        if "bond" in title:
            return (
                ["denton-tx-special-citizens-bond-advisory-committee"],
                "2019 bond committee",
                "assignment",
            )
        zone = "2" if "no. 2" in title else "1"
        return [f"denton-tx-tirz-{zone}-board"], f"TIRZ No. {zone} Board", "assignment"
    if city == "fort-worth-tx":
        if "gas drilling task force" in title:
            return (
                ["fort-worth-tx-city-council", "fort-worth-tx-gas-drilling-task-force"],
                "Council/Gas Task Force joint",
                "assignment",
            )
        if "eagle mountain" in title:
            return (
                ["fort-worth-tx-city-council", "fort-worth-tx-emsisd-board-joint-meetings"],
                "Council/EMSISD joint",
                "assignment",
            )
        return (
            [],
            "Institutional owner not established by supplied program/player evidence",
            "insufficient-owner-proof",
        )
    raise ValueError(record["case_id"])


def main():
    rows = json.loads((OUT / "source-records.json").read_text())["records"]
    docs = json.loads((OUT / "document-retrieval.json").read_text())["documents"]
    supplements = json.loads((OUT / "supplement-retrieval.json").read_text())["documents"]
    catalog = {}
    for path in sorted((ROOT / "config/feeds").glob("*.yml")):
        data = yaml.safe_load(path.read_text())
        catalog.setdefault(data["city"], []).append(
            {
                "slug": data["slug"],
                "title": data.get("podcast_title"),
                "description": data.get("podcast_description"),
            }
        )
    judgments = []
    inputs = []
    policy = {
        "identity": (
            "Use convened body, not venue, topic or common members; numbered boards differ."
        ),
        "joint": (
            "Same recording may serve every proven participant; separate subs"
            "cription aggregation from identity."
        ),
        "publication": (
            "Ordinary public comment does not add Public Info membership; ceremonies are excluded."
        ),
        "untracked": (
            "An unassigned real body is not automatically a non-meeting. Miss"
            "ing proof permits abstention."
        ),
    }
    for record in rows:
        uid = record["uid"]
        evidence = []
        retained = next((d for d in docs if d["id"] == uid + "-agenda_text_artifact"), None)
        if retained and retained["status"] != "unavailable":
            text = plaintext(ROOT / retained["text_file"])
            if len(text.strip()) > 400:
                evidence.append(
                    {
                        "id": "retained-agenda",
                        "url": retained["original_url"],
                        "span": text,
                        "sha256": retained["sha256"],
                    }
                )
        player = next(d for d in supplements if d["id"] == uid + "-provider-player")
        if player["status"] != "unavailable":
            text = plaintext(ROOT / player["text_file"])
            with (OUT / "documents" / (uid + "-provider-readable.txt")).open("x") as handle:
                handle.write(text)
            evidence.append(
                {
                    "id": "provider-player",
                    "url": player["original_url"],
                    "span": text[:4000],
                    "sha256": player["sha256"],
                    "caution": "Provider captions may be automatic; not official minutes.",
                }
            )
        if record.get("source_chapters"):
            evidence.append(
                {
                    "id": "native-source-chapters",
                    "span": record["source_chapters"],
                    "url": (record.get("links") or {}).get("canonical_video"),
                }
            )
        extra = []
        if uid == "63b22f80ce9500ff":
            extra = ["citizen-council-june2007"]
        if record["provider_guid"] == "269608":
            extra = ["dallas-cbtf-aug15"]
        if record["provider_guid"] == "259821":
            extra = ["dallas-streets-june13"]
        if record["provider_guid"] == "233032":
            extra = ["dallas-streets-may25"]
        if record["provider_guid"] == "270975":
            extra = ["dallas-streets-archive"]
        for identifier in extra:
            d = next(d for d in supplements if d["id"] == identifier)
            if d["status"] != "unavailable":
                text = plaintext(ROOT / d["text_file"])
                if identifier == "citizen-council-june2007":
                    offset = text.find("Process for Receiving the Citizen Advisory")
                    text = text[max(0, offset - 200) : offset + 3500]
                evidence.append(
                    {
                        "id": identifier,
                        "url": d["original_url"],
                        "span": text[:8000],
                        "sha256": d["sha256"],
                        "binding": (
                            "Use native source date/title and policy; contextual evidence alo"
                            "ne is not a UID binding."
                        ),
                    }
                )
        targets, identity, outcome = proposed(record)
        known_slugs = {f["slug"] for f in catalog[record["city"]]}
        assert all(target in known_slugs for target in targets)
        judgments.append(
            {
                "case_id": record["case_id"],
                "uid": uid,
                "city": record["city"],
                "title": record["title"],
                "body_identity_draft": identity,
                "publication_targets_draft": targets,
                "outcome_draft": outcome,
                "evidence_ids": [e["id"] for e in evidence],
                "truth_status": "awaiting-test-answer-approval",
                "limits": (
                    "Global All Meetings aggregates excluded from this body-identity "
                    "test. No new feed or production decision."
                ),
            }
        )
        inputs.append(
            {
                "id": record["case_id"],
                "city": record["city"],
                "source_key": record["source_key"],
                "recording": record,
                "existing_feeds": catalog[record["city"]],
                "family_policy": policy,
                "evidence": evidence,
            }
        )
    for name, data in [
        ("review-decisions.json", {"cases": judgments}),
        ("model-inputs-draft.json", {"cases": inputs}),
    ]:
        with (OUT / name).open("x") as handle:
            json.dump(data, handle, indent=2)
    summary = {
        "recordings": len(rows),
        "with_source_media": sum(bool(r["sources"]) for r in rows),
        "outcomes": dict(Counter(j["outcome_draft"] for j in judgments)),
        "multiple_body_feeds": sum(len(j["publication_targets_draft"]) > 1 for j in judgments),
        "retained_agendas": sum(
            any(e["id"] == "retained-agenda" for e in c["evidence"]) for c in inputs
        ),
        "approved_test_answers": 0,
        "provider_calls": 0,
    }
    with (OUT / "summary.json").open("x") as handle:
        json.dump(summary, handle, indent=2)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
