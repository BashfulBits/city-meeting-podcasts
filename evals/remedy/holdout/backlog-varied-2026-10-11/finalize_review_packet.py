import hashlib
import json
import zipfile
from pathlib import Path

root = Path(__file__).resolve().parents[4]
out = root / "evals/remedy/holdout/backlog-varied-2026-10-11/packet-v2"
rows = json.load(open(out / "review-decisions.json"))["cases"]
inputs = json.load(open(out / "model-inputs-draft.json"))["cases"]
index = {r["case_id"]: r for r in rows}
feed_names = {}
for c in inputs:
    for f in c["existing_feeds"]:
        feed_names[f["slug"]] = f.get("title") or f["slug"]
questions = []
for r in rows:
    targets = r["publication_targets_draft"]
    city = r["city"]
    for target in targets:
        questions.append(
            {
                "id": r["case_id"] + "-member-" + target,
                "recording_case": r["case_id"],
                "question": (
                    "Under the supplied approved publication policy, this recording belongs in "
                )
                + target
                + ".",
                "expected_supported_draft": True,
                "target": target,
                "metric": "publication-membership",
                "approval": "pending",
            }
        )
    wrong = city + "-city-council" if city not in ["arlington-tx"] else "arlington-tx-council"
    if wrong in targets:
        wrong = {
            "addison-tx": "addison-tx-bond-committees",
            "denton-tx": "denton-tx-tirz-2-board",
            "fort-worth-tx": "fort-worth-tx-crime-control-and-prevention-district",
        }[city]
    metric = (
        "unsupported-claim-rejection"
        if r["outcome_draft"] == "insufficient-owner-proof"
        else "publication-membership"
    )
    questions.append(
        {
            "id": r["case_id"] + "-wrong-target",
            "recording_case": r["case_id"],
            "question": (
                "The supplied evidence and approved project policy are sufficient"
                " to place this recording in "
            )
            + wrong
            + ".",
            "expected_supported_draft": False,
            "target": wrong,
            "metric": metric,
            "approval": "pending",
        }
    )
with (out / "questions-for-approval.json").open("x") as f:
    json.dump(
        {
            "recordings": 60,
            "questions": questions,
            "warning": "Not approved gold; membership is not evidence of joint convening.",
        },
        f,
        indent=2,
    )
lines = [
    "# Entire 60-recording evidence and expected-answer packet",
    "",
    (
        "Status: evidence collected; expected test answers awaiting maint"
        "ainer approval. No model calls."
    ),
    "",
    "## What is being measured",
    "",
    "Forty recordings have proposed existing-feed memberships. Fifteen have previously approved",
    (
        "archive/event/untracked dispositions. Five Fort Worth program re"
        "cords lack proof of a municipal"
    ),
    "institutional owner: their test asks whether evidence is sufficient for Council assignment.",
    "Rejecting an unsupported claim does not prove that the recording has no government content.",
    "",
    "These are historical challenge cases, not 60 newly discovered live problems. Most should now",
    "be handled by deterministic rules or saved decisions before a production model call.",
    "",
    "The proposed set contains "
    + str(len(questions))
    + " positive/negative membership questions across 60 unique source/UID",
    "pairs. Record-level success requires all its scored memberships and negative checks to pass.",
    (
        "Do not mistake question variants for independent meetings. Repor"
        "t body identity, policy compliance,"
    ),
    (
        "multi-feed coverage, supported abstention and transport failure "
        "separately. Known exclusions require"
    ),
    (
        "the applicable public policy in the prompt; omitting it would un"
        "fairly penalize a reasonable proposal."
    ),
    "",
    (
        "Three recordings overlap the previous Denton comparison. The two"
        " Fort Worth Gas Task Force UIDs"
    ),
    (
        "reference the same underlying event/media asset and must be grou"
        "ped in event-level statistics."
    ),
    (
        "No independent admission-holdout claim is made: source/family ov"
        "erlap with earlier work remains."
    ),
    "",
    "## Proposed answers by recording",
    "",
    (
        "The links below point to the full retained provider/agenda evide"
        "nce in the packet. No source record,"
    ),
    (
        "date, UID, media reference, production feed or prior disposition"
        " was changed. All 60 have source media."
    ),
    "",
    "| City | Recording | Proposed publication answer | Evidence / limit |",
    "|---|---|---|---|",
]
for r, c in zip(rows, inputs, strict=True):
    targets = "; ".join(feed_names[t] for t in r["publication_targets_draft"])
    if not targets:
        targets = {
            "archive-only": "Archive only, per prior approval",
            "event-exclusion": "No podcast feed; ceremony/dedication",
            "archive-series": "No podcast feed; approved candidate-forum archive series",
            "real-body-untracked": "Real committee, currently untracked; no Council assignment",
            "insufficient-owner-proof": (
                "Reject unsupported Council assignment; owner remains unknown"
            ),
        }[r["outcome_draft"]]
    uid = r["uid"]
    evidence = "[Provider record](documents/" + uid + "-provider-readable.txt)"
    if any(e["id"] == "retained-agenda" for e in c["evidence"]):
        evidence += "; [agenda](documents/" + uid + "-agenda_text_artifact.txt)"
    if r["outcome_draft"] == "insufficient-owner-proof":
        evidence += "; title/player only, no convening proof"
    if uid in ["0d5832dbd08e6dd8", "27254aa1a700569f"]:
        evidence += (
            "; earlier manual official-opening proof in committed audit; no new frame capture"
        )
    if uid == "16300c982b358fe9":
        evidence += (
            "; official dated listing/native index; missing exact minutes; wi"
            "der publication previously approved"
        )
    if uid == "63b22f80ce9500ff":
        evidence += "; compilation plus June 2007 Council context; no invented part dates"
    lines.append(
        "| "
        + r["city"]
        + " | "
        + r["title"].replace("|", "/")
        + " (`"
        + uid
        + "`) | "
        + targets
        + " | "
        + evidence
        + " |"
    )
lines.extend(
    [
        "",
        "## Evidence retrieval and provenance",
        "",
        (
            "Source records came from five append-only cached source snapshot"
            "s, each hashed. 121 linked"
        ),
        (
            "agenda/document requests succeeded, plus 65 of 67 supplemental r"
            "equests. HTTP 200 alone is not"
        ),
        (
            "a valid agenda: generic schedules and player shells remain label"
            "led as such. 38 recordings have"
        ),
        (
            "retained agendas; three Dallas cases additionally have independe"
            "nt official minutes. Native chapter"
        ),
        (
            "headings and dated provider indices supply recording binding whe"
            "re available. Provider captions"
        ),
        "are potentially automatic and never silently become official transcripts.",
        "",
        (
            "The current Addison bond-program page and FY2007 budget returned"
            " 403. The retained committee"
        ),
        (
            "agendas, recording headings and retrieved June 2007 Council pack"
            "et provide alternative evidence."
        ),
        "Original unavailable responses are recorded; no unseen page contents are asserted.",
        "",
        "Raw bytes are sealed in raw-evidence.zip with per-file hashes in the retrieval manifests.",
        (
            "Readable provider text, agenda text, original source records and"
            " separate proposed answers are kept"
        ),
        "outside the archive. Archive entries are inert evidence, not scripts to execute.",
        "",
        "## Approval and execution",
        "",
        (
            "Approve expected test answers after reviewing this table, especi"
            "ally the five evidence-insufficient"
        ),
        (
            "cases. This approval is for the evaluation, not new production d"
            "ispositions or model activation."
        ),
        (
            "Known archive/suppression policies must be presented explicitly "
            "and scored as compliance controls."
        ),
        "",
        (
            "After approval, validate/freeze the existing evaluation schema a"
            "nd pilot a small balanced set before"
        ),
        (
            "the full run. Use the nine already compared model candidates: Ge"
            "mini 3.5 Flash-Lite, Gemini 3.7"
        ),
        (
            "Flash, Gemini 3.8 Flash, NVIDIA Kimi K3, NVIDIA full GLM 5.3, Or"
            "caRouter GLM 5.3 Flash, and"
        ),
        "BeatAPI Astra, Sol and DeepSeek V4.1 Flash. No newly suggested GLM 4.7 route is included.",
        "",
        (
            "Run only within observed shared capacity, with production headro"
            "om and immutable checkpoints."
        ),
        (
            "Gemini high routes have low daily limits; multiple keys are not "
            "assumed to multiply project quota."
        ),
        (
            "BeatAPI calls share capacity with JEV. Larger batches will span "
            "resets rather than raise limits."
        ),
        (
            "Provider identity/maximum-effort holds remain explicit; diagnost"
            "ic accuracy is not admission."
        ),
        (
            "A timed-out NVIDIA DeepSeek route is not relaunched into a full "
            "batch merely to accumulate timeouts."
        ),
    ]
)
with (out / "REVIEW.md").open("x") as f:
    f.write("\n".join(lines) + "\n")
# Raw HTML is repeated large boilerplate; archive once while retaining exact retrieval byte hashes.
raws = []
for name in ["document-retrieval.json", "supplement-retrieval.json"]:
    for d in json.load(open(out / name))["documents"]:
        if d.get("raw_file"):
            raws.append(root / d["raw_file"])
with zipfile.ZipFile(out / "raw-evidence.zip", "x", compression=zipfile.ZIP_DEFLATED) as z:
    for p in sorted(set(raws)):
        z.write(p, p.relative_to(out))
with (out / "raw-evidence-sha256.json").open("x") as f:
    json.dump(
        {
            "zip_sha256": hashlib.sha256((out / "raw-evidence.zip").read_bytes()).hexdigest(),
            "entries": {
                str(p.relative_to(out)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(set(raws))
            },
        },
        f,
        indent=2,
    )
print("questions", len(questions), "raw archive bytes", (out / "raw-evidence.zip").stat().st_size)
