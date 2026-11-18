import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "evals" / "open-weight-decision"
LABELS = {"supported", "not_supported", "uncertain"}


def load(name):
    return json.loads((ROOT / f"{name}.json").read_text())


def test_manifest_and_gold_agree():
    manifest, gold = load("manifest"), load("gold")
    for lane in ("tags", "moments"):
        ids = [item["id"] for item in manifest[lane]]
        assert len(ids) == len(set(ids))
        assert set(ids) == set(gold[lane])
        assert all(gold[lane][i]["label"] in LABELS for i in ids)
        assert all(gold[lane][i]["source"] in {"human", "claude"} for i in ids)
    chapter_ids = [c["chapter_id"] for c in manifest["chapters"]]
    assert len(chapter_ids) == len(set(chapter_ids)) == len(gold["chapters"])
    for chapter in manifest["chapters"]:
        shown = {t["id"] for t in chapter["tags"]}
        assert set(gold["chapters"][chapter["chapter_id"]]["tags"]) <= shown


def test_manifest_carries_no_truth_or_production_hints():
    text = (ROOT / "manifest.json").read_text()
    for banned in ('"verdicts"', '"suggested"', '"kind"', '"blind"', '"label"', '"stratum"'):
        assert banned not in text


def test_split_is_balanced_and_stable():
    manifest = load("manifest")
    for lane in ("tags", "moments", "chapters"):
        splits = [item["split"] for item in manifest[lane]]
        assert set(splits) == {"dev", "holdout"}
        # split by episode, so item counts are only roughly balanced
        assert 0.25 < splits.count("dev") / len(splits) < 0.75
    rebuilt = [m for m in manifest["moments"] if m["evidence_source"] == "rebuilt-quote-centered"]
    assert len(rebuilt) == 34
    for moment in rebuilt:
        words = moment["quote"].lower().split()
        evidence = moment["evidence"].lower().split("transcript around the quote:", 1)[1]
        assert any(" ".join(words[i : i + 5]) in evidence for i in range(len(words) - 4))


def test_no_episode_straddles_the_split():
    manifest = load("manifest")
    side = {}
    for lane in ("tags", "moments", "chapters"):
        for item in manifest[lane]:
            assert side.setdefault((item["src"], item["uid"]), item["split"]) == item["split"]
