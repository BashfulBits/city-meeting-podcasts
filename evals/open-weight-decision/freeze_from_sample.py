"""Freeze the open-weight decision evaluation set (review/60 Phase 2) from the labeling sample.

The sample, the labels and the rebuilt moment passages live outside the repo (they were drawn from
the durable state). This documents how the committed files were derived. Run once:

    python evals/open-weight-decision/freeze_from_sample.py SAMPLE LABELS CLAUDE_CHAPTERS \
        CLAUDE_MOMENTS REBUILT_MOMENTS

Inputs (`manifest.json`) and truth (`gold.json`) are kept apart; production verdicts go to
`production_reference.json` for agreement analysis only. Split: a stable hash of the episode
(`src`, `uid`), about 50/50 by episode, so no episode is on both sides.
"""

import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
SET_VERSION = 1


def split_of(src: str, uid: str) -> str:
    key = f"{src}:{uid}"
    return "dev" if int(hashlib.sha256(key.encode()).hexdigest(), 16) % 2 == 0 else "holdout"


def main(sample_p, labels_p, chapters_p, moments_p, rebuilt_p):
    sample = json.loads(Path(sample_p).read_text())
    labels = json.loads(Path(labels_p).read_text())
    claude_ch = json.loads(Path(chapters_p).read_text())
    claude_mo = json.loads(Path(moments_p).read_text())
    rebuilt = {r["id"]: r for r in json.loads(Path(rebuilt_p).read_text())}
    rebuilt_labels = {it["id"]: it["claude_label_on_rebuilt_passage"] for it in claude_mo["items"]}
    manifest = {
        "version": SET_VERSION,
        "frozen_on": "2026-11-18",
        "tags": [],
        "moments": [],
        "chapters": [],
    }
    gold = {"version": SET_VERSION, "tags": {}, "moments": {}, "chapters": {}}
    ref = {"version": SET_VERSION, "judge_verdicts": {}, "strata": {}, "suggested_tags": {}}
    for item in sample["lane_a"]:
        sid = item["id"]
        base = {
            "id": sid,
            "split": split_of(item["src"], item["uid"]),
            "src": item["src"],
            "uid": item["uid"],
            "episode_title": item["episode_title"],
        }
        ref["judge_verdicts"][sid] = item.get("verdicts")
        ref["strata"][sid] = item["stratum"]
        if item["task"] == "tag":
            manifest["tags"].append(
                base
                | {
                    "tag_id": item["tag_id"],
                    "tag_definition": item["tag_definition"],
                    "chapter_title": item["chapter_title"],
                    "evidence_t1": item["evidence_t1"],
                    "evidence_t2": item["evidence_t2"],
                }
            )
            gold["tags"][sid] = {"label": labels[sid]["label"], "source": "human"}
        else:
            if sid in rebuilt_labels:
                passage = rebuilt[sid]["passage"]
                evidence = (
                    f"Quote: {item['quote']}\nStated reason: {item['why']}\n"
                    f"Transcript around the quote: {passage}"
                )
                source = "rebuilt-quote-centered"
                gold["moments"][sid] = {"label": rebuilt_labels[sid], "source": "claude"}
            else:
                evidence = item["evidence_t1"]
                source = "production-t1"
                gold["moments"][sid] = {"label": labels[sid]["label"], "source": "human"}
            manifest["moments"].append(
                base
                | {
                    "quote": item["quote"],
                    "why": item["why"],
                    "evidence": evidence,
                    "evidence_source": source,
                }
            )
    by_chapter = {c["chapter_id"]: c for c in claude_ch["chapters"]}
    for ch in sample["lane_b"]:
        cid = ch["chapter_id"]
        shown = [{"id": t["id"], "definition": t["definition"]} for t in ch["shown"]]
        manifest["chapters"].append(
            {
                "chapter_id": cid,
                "split": split_of(ch["src"], ch["uid"]),
                "src": ch["src"],
                "uid": ch["uid"],
                "title": ch["title"],
                "episode_title": ch["episode_title"],
                "text": ch["text"],
                "truncated": ch["truncated"],
                "tags": shown,
            }
        )
        gold["chapters"][cid] = {"tags": by_chapter[cid]["claude_tags"], "source": "claude"}
        ref["suggested_tags"][cid] = ch["suggested"]
    for name, data in (("manifest", manifest), ("gold", gold), ("production_reference", ref)):
        (HERE / f"{name}.json").write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    print({k: len(v) for k, v in manifest.items() if isinstance(v, list)})


if __name__ == "__main__":
    main(*sys.argv[1:6])
