"""Build a blinded, all-boundaries review cohort from existing locator runs."""

from __future__ import annotations

import hashlib
import json
import statistics
import urllib.request
from collections import defaultdict
from pathlib import Path

REPO = Path("/Users/Eric/.codex/worktrees/9ac6/city-meeting-podcasts")
OUT = Path(__file__).resolve().parent
DATA = REPO / "evals/chapter-locator"
TELEMETRY = Path("/private/tmp/chapter-locator-telemetry")
CACHE = TELEMETRY / "prompt-manual-review-v1/word-transcripts"
SEED = "blinded-all-boundaries-20260928-v1"


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def stable_id(uid: str, index: int) -> str:
    digest = hashlib.sha256(f"{SEED}|{uid}|{index}".encode()).hexdigest()
    return "B-" + digest[:8].upper()


def old_id(uid: str, index: int) -> str:
    digest = hashlib.sha256(f"blind-prompt-review-20260928-v1|{uid}|{index}".encode())
    return "P-" + digest.hexdigest()[:7].upper()


def fmt_clock(seconds: float) -> str:
    hours = int(seconds // 3600)
    minutes = int(seconds % 3600 // 60)
    remainder = seconds % 60
    prefix = f"{hours:02d}:" if hours else ""
    return f"{prefix}{minutes:02d}:{remainder:04.1f}"


def result_rows() -> dict[tuple[str, str, str], dict]:
    chosen: dict[tuple[str, str, str], tuple[int, dict]] = {}
    for path in TELEMETRY.rglob("*.json"):
        try:
            payload = load(path)
        except (OSError, json.JSONDecodeError):
            continue
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, (list, dict)):
            continue
        metadata = {}
        metadata_path = path.parent / "run-metadata.json"
        if metadata_path.exists():
            try:
                metadata = load(metadata_path)
            except (OSError, json.JSONDecodeError):
                pass
        rows = results if isinstance(results, list) else results.values()
        for source_row in rows:
            if not isinstance(source_row, dict):
                continue
            row = dict(source_row)
            row.setdefault("uid", row.get("packet_uid"))
            row.setdefault("model", payload.get("model") or metadata.get("model"))
            row.setdefault(
                "prompt_variant",
                row.get("mode_name") or row.get("structured_mode") or "unspecified",
            )
            if not row.get("output"):
                final = row.get("final") or {}
                anchors = final.get("anchors") or row.get("anchors") or []
                validation = final.get("locator_valid", row.get("validation") == "valid")
                if anchors and validation:
                    row["output"] = {"route": "full", "anchors": anchors}
                else:
                    row["output"] = None
                    row["error"] = row.get("error") or final.get("locator_error") or "invalid"
            if not row.get("uid") or not row.get("model"):
                continue
            key = (row["uid"], row["model"], row.get("prompt_variant", "unspecified"))
            valid = bool(
                row.get("output") and not row.get("error") and row["output"].get("route") == "full"
            )
            # Prefer valid results, then the latest saved copy of an identical arm.
            rank = (int(valid) * 10**15) + int(path.stat().st_mtime * 1000)
            previous = chosen.get(key)
            if previous is None or rank > previous[0]:
                chosen[key] = (rank, row)
    return {key: row for key, (_, row) in chosen.items()}


def agenda_map(crosswalk_row: dict, provider_count: int) -> dict[int, list[int]]:
    result: dict[int, list[int]] = defaultdict(list)
    for item in crosswalk_row.get("generated_items", []):
        status = item.get("status")
        if status not in {"mapped", "conflicted"}:
            continue
        index = item.get("best_provider_chapter_index")
        agenda_index = item.get("generated_item_index")
        if isinstance(index, int) and 0 <= index < provider_count:
            if isinstance(agenda_index, int):
                result[index].append(agenda_index)
    return {key: sorted(set(value)) for key, value in result.items()}


def word_segments(uid: str, transcript: dict) -> list[dict]:
    url = transcript.get("words_url")
    if not url:
        return []
    cache = CACHE / f"{uid}.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    if cache.exists():
        try:
            return load(cache).get("segments", [])
        except (OSError, json.JSONDecodeError):
            cache.unlink(missing_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            payload = json.load(response)
    except Exception as exc:  # Keep the case; the UI can still save a numeric time.
        print(f"Transcript unavailable for {uid}: {exc}")
        return []
    cache.write_text(json.dumps(payload, ensure_ascii=False))
    return payload.get("segments", [])


def transcript_excerpt(segments: list[dict], centers: list[float]) -> list[dict]:
    windows = [(max(0, center - 90), center + 90) for center in centers]
    excerpt = []
    for segment in segments:
        try:
            start = float(segment["start"])
            end = float(segment.get("end", start))
        except (KeyError, TypeError, ValueError):
            continue
        if not any(end >= low and start <= high for low, high in windows):
            continue
        words = []
        for word in segment.get("words", []):
            try:
                words.append(
                    {
                        "text": word["w"],
                        "start_seconds": round(float(word["s"]), 3),
                        "end_seconds": round(float(word.get("e", word["s"])), 3),
                    }
                )
            except (KeyError, TypeError, ValueError):
                continue
        excerpt.append(
            {
                "start_seconds": round(start, 3),
                "end_seconds": round(end, 3),
                "text": segment.get("text", ""),
                "words": words,
            }
        )
    return excerpt


def main() -> None:
    splits = {}
    for split in ("dataset-v3", "holdout-v1"):
        folder = DATA / split
        manifest = load(folder / "manifest.json")
        gold = load(folder / "gold.json")
        crosswalk = load(folder / "crosswalk.json")
        splits[split] = {
            "manifest": {row["uid"]: row for row in manifest["episodes"]},
            "gold": {row["uid"]: row for row in gold["episodes"]},
            "crosswalk": {row["uid"]: row for row in crosswalk["episodes"]},
        }

    human_dev = load(DATA / "dataset-v3/human-crosswalk-v1.json")
    manual_dev = {row["uid"]: row for row in human_dev["episodes"]}
    holdout_audit = load(DATA / "holdout-v1/human-crosswalk-audit.json")
    manual_holdout = {
        (row["uid"], row["provider_chapter_index"]): row for row in holdout_audit["cases"]
    }
    results = result_rows()
    old_raw = load(
        TELEMETRY / "locator-coverage-prompt-test-20260927T171142Z/partial-results.json"
    )["results"]
    reviewed_dev_uids = {row["uid"] for row in old_raw if row.get("prompt_variant") == "baseline"}
    v41_uids = {
        uid
        for uid, model, _ in results
        if model == "deepseek/deepseek-v4.1-flash" and uid in splits["dataset-v3"]["manifest"]
    }
    # Preserve the eight already reviewed development packets and add the disjoint V4.1
    # development cohort so the median suggestions include that long-context candidate.
    dev_uids = reviewed_dev_uids | v41_uids
    wanted = {("dataset-v3", uid) for uid in dev_uids} | {
        ("holdout-v1", uid) for uid in splits["holdout-v1"]["manifest"]
    }

    cases = []
    hidden = {"models": {}, "cases": {}, "sources": []}
    meeting_number = 0
    for split, uid in sorted(wanted, key=lambda item: (item[0], item[1])):
        bundle = splits[split]
        meeting = bundle["manifest"].get(uid)
        gold_row = bundle["gold"].get(uid)
        if not meeting or not gold_row:
            continue
        chapters = gold_row.get("chapters", [])
        agenda_model, agenda = next(
            iter((meeting.get("generated_agenda") or {}).items()), (None, {})
        )
        items = agenda.get("items", [])
        if not items:
            continue
        meeting_number += 1
        crosswalk_row = bundle["crosswalk"].get(uid, {})
        auto_map = agenda_map(crosswalk_row, len(chapters))
        if split == "dataset-v3":
            if uid in manual_dev:
                manual_rows = {
                    row["provider_chapter_index"]: row
                    for row in manual_dev[uid].get("provider_chapters", [])
                }
                target_map = {
                    idx: row.get("candidate_indices", [])
                    for idx, row in manual_rows.items()
                    if row.get("label") in {"matched_candidate", "consent_composite"}
                }
            else:
                manual_rows = {}
                target_map = auto_map
        else:
            manual_rows = {}
            target_map = auto_map

        segments = word_segments(uid, meeting.get("transcript", {}))
        episode_results = {
            (model, prompt): result
            for (result_uid, model, prompt), result in results.items()
            if result_uid == uid
        }
        model_predictions: dict[str, dict[str, list[float]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for (model, prompt), result in episode_results.items():
            output = result.get("output") or {}
            if output.get("route") != "full" or result.get("error"):
                continue
            for anchor in output.get("anchors", []):
                try:
                    item_index = int(anchor["agenda_item_index"])
                    start = float(anchor["start"])
                except (KeyError, TypeError, ValueError):
                    continue
                model_predictions[model][prompt].append((item_index, start))

        for chapter_index, chapter in enumerate(chapters):
            case_id = stable_id(uid, chapter_index)
            prior_id = old_id(uid, chapter_index)
            candidates = target_map.get(chapter_index, [])
            audit = manual_holdout.get((uid, chapter_index))
            if audit:
                if audit.get("status") == "matched_candidate":
                    candidates = [audit["agenda_item_index"]]
                elif audit.get("status") == "ambiguous":
                    candidates = audit.get("agenda_item_indices", [])
                else:
                    candidates = []
            candidates = sorted({i for i in candidates if 0 <= i < len(items)})
            chapter_time = float(chapter.get("start", 0))
            model_medians = []
            case_arms = []
            for (model, prompt), result in sorted(episode_results.items()):
                output = result.get("output") or {}
                valid = bool(output.get("route") == "full" and not result.get("error"))
                anchors = []
                for anchor in output.get("anchors", []) if valid else []:
                    try:
                        item_index = int(anchor["agenda_item_index"])
                        start = float(anchor["start"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    anchors.append(
                        {
                            "agenda_item_index": item_index,
                            "start_seconds": round(start, 3),
                            "unit_id": anchor.get("unit_id"),
                            "transition_quote": anchor.get("transition_quote"),
                        }
                    )
                target_anchors = [
                    anchor for anchor in anchors if anchor["agenda_item_index"] in candidates
                ]
                case_arms.append(
                    {
                        "model": model,
                        "provider": result.get("provider"),
                        "route_id": result.get("route_id"),
                        "prompt": prompt,
                        "valid": valid,
                        "elapsed_seconds": result.get("elapsed_seconds"),
                        "error": result.get("error"),
                        "anchors": anchors,
                        "target_anchor_count": len(target_anchors),
                    }
                )
            for model, prompts in sorted(model_predictions.items()):
                prompt_medians = []
                for prompt, pairs in sorted(prompts.items()):
                    times = [time for item_index, time in pairs if item_index in candidates]
                    if not times:
                        continue
                    arm_median = statistics.median(times)
                    prompt_medians.append(arm_median)
                    case_arms.append(
                        {
                            "model": model,
                            "prompt": prompt,
                            "predicted_start_seconds": round(arm_median, 3),
                            "candidate_anchor_count": len(times),
                        }
                    )
                if prompt_medians:
                    model_median = statistics.median(prompt_medians)
                    model_medians.append(model_median)
            suggestion = statistics.median(model_medians) if model_medians else None
            centers = [chapter_time]
            if suggestion is not None and abs(suggestion - chapter_time) > 60:
                centers.append(suggestion)
            source_status = "human-reviewed" if uid in manual_dev else "automated"
            if audit:
                source_status = "human-reviewed"
            top = chapter.get("title", "")
            neighboring = []
            for index in range(max(0, chapter_index - 2), min(len(chapters), chapter_index + 3)):
                other = chapters[index]
                neighboring.append(
                    {
                        "index": index,
                        "title": other.get("title", ""),
                        "start_seconds": round(float(other.get("start", 0)), 2),
                        "start_clock": fmt_clock(float(other.get("start", 0))),
                    }
                )
            agenda_items = [
                {
                    "index": index,
                    "title": item.get("title", ""),
                    "display_ref": item.get("display_ref"),
                }
                for index, item in enumerate(items)
            ]
            default_composite = prior_id == "P-1BB8ED7"
            cases.append(
                {
                    "case_id": case_id,
                    "legacy_case_id": prior_id,
                    "split": split,
                    "meeting_number": meeting_number,
                    "meeting_case_count": len(chapters),
                    "body": meeting.get("body", ""),
                    "provider": meeting.get("provider", ""),
                    "published": meeting.get("published", ""),
                    "agenda_url": (meeting.get("agenda") or {}).get("url"),
                    "duration_bucket": meeting.get("duration_bucket"),
                    "duration_seconds": meeting.get("duration_seconds"),
                    "chapter_index": chapter_index,
                    "chapter_title": top,
                    "chapter_start_seconds": round(chapter_time, 3),
                    "chapter_start_clock": fmt_clock(chapter_time),
                    "suggested_start_seconds": (
                        round(suggestion, 3) if suggestion is not None else None
                    ),
                    "suggestion_source": "median-of-models" if suggestion is not None else None,
                    "crosswalk_status": source_status,
                    "target_agenda_indices": candidates,
                    "target_agenda_items": [agenda_items[i] for i in candidates],
                    "neighbor_chapters": neighboring,
                    "agenda_items": agenda_items,
                    "transcript_excerpt": transcript_excerpt(segments, centers),
                    "is_composite_default": default_composite,
                    "source_episode_key": uid,
                }
            )
            hidden["cases"][case_id] = {
                "episode_uid": uid,
                "chapter_index": chapter_index,
                "automatic_agenda_candidates": candidates,
                "model_arms": case_arms,
            }

    cases.sort(key=lambda row: (row["meeting_number"], row["chapter_index"]))
    for details in hidden["cases"].values():
        for arm in details["model_arms"]:
            hidden["models"].setdefault(arm["model"], {})
    packet = {
        "title": "Blinded all-boundaries chapter locator review",
        "version": 3,
        "purpose": (
            "Manual boundary timing and optional bundled-agenda review across all selected "
            "chapters."
        ),
        "meeting_count": meeting_number,
        "case_count": len(cases),
        "cases": cases,
        "instructions": {
            "scoring": (
                "Confirm or correct one transition start for every published chapter boundary."
            ),
            "suggestions": (
                "The initial time is the median of available model responses, with each model "
                "weighted once."
            ),
            "bundles": (
                "Use the composite checkbox when a published chapter combines multiple agenda "
                "items; select each included item."
            ),
        },
    }
    hidden["sources"] = [
        "evals/chapter-locator/dataset-v3/human-crosswalk-v1.json",
        "evals/chapter-locator/holdout-v1/crosswalk.json",
        "private raw chapter-locator results under /private/tmp/chapter-locator-telemetry",
    ]
    old_packet_path = OUT / "packet.json"
    old_scores_path = OUT / "scores.json"
    old_answer_path = OUT / "answer-key.json"
    if old_packet_path.exists() and not (OUT / "packet-v1-preserved.json").exists():
        (OUT / "packet-v1-preserved.json").write_bytes(old_packet_path.read_bytes())
    if old_scores_path.exists() and not (OUT / "scores-v1-preserved.json").exists():
        (OUT / "scores-v1-preserved.json").write_bytes(old_scores_path.read_bytes())
    if old_answer_path.exists() and not (OUT / "answer-key-v1-preserved.json").exists():
        (OUT / "answer-key-v1-preserved.json").write_bytes(old_answer_path.read_bytes())
    prior_scores = load(old_scores_path) if old_scores_path.exists() else {}
    migrated = {}
    for case in cases:
        entry = prior_scores.get(case["case_id"]) or prior_scores.get(case["legacy_case_id"])
        if entry:
            migrated[case["case_id"]] = entry
    (OUT / "packet.json").write_text(json.dumps(packet, ensure_ascii=False))
    (OUT / "answer-key.json").write_text(json.dumps(hidden, ensure_ascii=False))
    (OUT / "scores.json").write_text(json.dumps(migrated, indent=2, ensure_ascii=False) + "\n")
    suggested = sum(row["suggested_start_seconds"] is not None for row in cases)
    print(
        f"Prepared {meeting_number} meetings, {len(cases)} boundaries, "
        f"{suggested} with median suggestions, {len(migrated)} existing scores preserved."
    )


if __name__ == "__main__":
    main()
