"""Request building and scoring for the open-weight decision evaluation (review/60 Phase 3).

The frozen set lives in ``evals/open-weight-decision/``. A model sees only ``manifest.json``: the
request builders here read nothing else, so a model can never be shown a label, a stratum, a
production verdict or a suggestion. Truth (``gold.json``) and the production reference are read
only by the scoring half of this module, which runs on the maintainer's machine.

Every runner (OpenRouter, the cloud Karpo/GLiDE worker, a local screen) produces one *answers*
document keyed by request key; ``score`` turns answers plus gold into the metrics in review/60.
"""

from __future__ import annotations

import json
import math
import random
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

SET_DIR = Path(__file__).resolve().parents[2] / "evals" / "open-weight-decision"

# Frozen copies of the production rubrics (citypods/judging/tag_task.py and moment_task.py). A test
# pins them to production, so a rubric change there fails loudly instead of silently moving the
# measurement.
TAG_RUBRIC = (
    "Decide whether the meeting segment supports the proposed topic tag. The tag is correct when "
    "the segment involves a specific project, contract, program or policy on the topic, even when "
    "it is approved routinely (for example on a consent agenda). It is not correct for a generic "
    "mention, a passing reference in a list or summary, a read-back of past items, or a "
    "general-purpose services contract not tied to a specific project or policy on the topic."
)
MOMENT_RUBRIC = (
    "Does the quote, as spoken in this transcript, actually say what the stated reason claims? "
    "Answer from the transcript only."
)

LANES = ("tags", "moments", "chapters")
SPLITS = ("dev", "holdout")
# scoring only: both splits pooled (after the holdout run, for power; nothing was tuned)
SCORE_SPLITS = (*SPLITS, "all")

# Production tag mix among subjects with at least three binary `supported` verdicts (review/60 §8,
# state inventory 2026-11-17): 29% unanimous yes, 34% unanimous no, 37% split of 67,804.
PRODUCTION_STRATUM_MIX = {"yes": 19807 / 67804, "no": 23133 / 67804, "split": 24864 / 67804}

THRESHOLD = 0.5


# ------------------------------------------------------------------ loading


def load_manifest(path: Path | None = None) -> dict[str, Any]:
    return json.loads((path or SET_DIR / "manifest.json").read_text())


def load_gold(path: Path | None = None) -> dict[str, Any]:
    return json.loads((path or SET_DIR / "gold.json").read_text())


def load_reference(path: Path | None = None) -> dict[str, Any]:
    return json.loads((path or SET_DIR / "production_reference.json").read_text())


# ------------------------------------------------------------------ requests

# A request is Karpo's and GLiDE's native shape: one shared ``state`` and named yes/no questions.
Request = dict[str, Any]


def _noul(instructions: str) -> dict[str, Any]:
    return {"type": "noul", "instructions": instructions}


def chapter_state(chapter: Mapping[str, Any]) -> str:
    return f"Chapter: {chapter['title']}\nTranscript:\n{chapter['text']}"


def chapter_question(tag: Mapping[str, Any]) -> str:
    return f"{TAG_RUBRIC}\n\nProposed tag: {tag['definition']}"


def build_requests(
    manifest: Mapping[str, Any], split: str, *, chapter_mode: str = "grouped"
) -> dict[str, Request]:
    """Every request for one split, keyed ``tags/<id>``, ``moments/<id>`` or ``chapters/...``.

    ``chapter_mode`` is ``grouped`` (one request per chapter holding all its shown tags, key
    ``chapters/<chapter>``) or ``single`` (one request per tag, key ``chapters/<chapter>/<tag>``).
    The evidence is in ``state`` and the rubric in the question, as the production batch rows do.
    """
    if split not in SPLITS:
        raise ValueError(f"unknown split {split!r}")
    if chapter_mode not in ("grouped", "single"):
        raise ValueError(f"unknown chapter mode {chapter_mode!r}")
    out: dict[str, Request] = {}
    for item in manifest["tags"]:
        if item["split"] == split:
            out[f"tags/{item['id']}"] = {
                "state": item["evidence_t1"],
                "questions": {"supported": _noul(TAG_RUBRIC)},
            }
    for item in manifest["moments"]:
        if item["split"] == split:
            out[f"moments/{item['id']}"] = {
                "state": item["evidence"],
                "questions": {"supported": _noul(MOMENT_RUBRIC)},
            }
    for chapter in manifest["chapters"]:
        if chapter["split"] != split:
            continue
        state = chapter_state(chapter)
        questions = {t["id"]: _noul(chapter_question(t)) for t in chapter["tags"]}
        if chapter_mode == "grouped":
            out[f"chapters/{chapter['chapter_id']}"] = {"state": state, "questions": questions}
        else:
            for tag_id, question in questions.items():
                out[f"chapters/{chapter['chapter_id']}/{tag_id}"] = {
                    "state": state,
                    "questions": {tag_id: question},
                }
    return out


def fold_for_openrouter(request: Request) -> Request:
    """Production's shape for the Decider route: evidence inside each question, empty state.

    A chapter request (several questions sharing the chapter) keeps the chapter in ``state``,
    since the endpoint takes a shared state for exactly that.
    """
    questions = request["questions"]
    if len(questions) > 1:
        return request
    ((qid, question),) = questions.items()
    return {
        "state": {},
        "questions": {qid: _noul(f"{question['instructions']}\n\n{request['state']}")},
    }


def answer_keys(request_key: str, request: Request) -> list[str]:
    """The per-question answer keys a request produces: ``request`` + question id for chapters."""
    if request_key.startswith("chapters/") and request_key.count("/") == 1:
        return [f"{request_key}/{q}" for q in request["questions"]]
    return [request_key]


# ------------------------------------------------------------------ metrics


def _bin_ece(pairs: Sequence[tuple[float, bool]], bins: int = 10) -> float:
    if not pairs:
        return float("nan")
    total = 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        members = [(p, y) for p, y in pairs if lo <= p < hi or (b == bins - 1 and p == 1.0)]
        if members:
            conf = sum(p for p, _ in members) / len(members)
            freq = sum(y for _, y in members) / len(members)
            total += len(members) / len(pairs) * abs(conf - freq)
    return total


def brier(pairs: Sequence[tuple[float, bool]]) -> float:
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs) if pairs else float("nan")


def ece(pairs: Sequence[tuple[float, bool]]) -> float:
    return _bin_ece(pairs)


def _percentile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return float("nan")
    pos = q * (len(sorted_values) - 1)
    lo, hi = math.floor(pos), math.ceil(pos)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def bootstrap_ci(
    n: int,
    statistic: Callable[[list[int]], float],
    *,
    clusters: Sequence[Any] | None = None,
    strata: Sequence[Any] | None = None,
    reps: int = 2000,
    seed: int = 20261118,
) -> tuple[float, float]:
    """Percentile 95% interval of ``statistic(indices)`` over resampled item indices.

    ``clusters`` resamples whole groups (chapters, so the tags of one chapter move together);
    ``strata`` resamples inside each stratum, keeping the sample mix. The same seed gives the same
    resamples to every model, which makes a difference of two statistics a *paired* bootstrap.
    """
    rng = random.Random(seed)
    if clusters is None:
        groups = {None: list(range(n))}
        units = [[i] for i in range(n)]
    else:
        by: dict[Any, list[int]] = {}
        for i, c in enumerate(clusters):
            by.setdefault(c, []).append(i)
        units = list(by.values())
        groups = {None: list(range(len(units)))}
    if strata is not None:
        # resample units inside the stratum of their first member
        stratum_units: dict[Any, list[int]] = {}
        for u, members in enumerate(units):
            stratum_units.setdefault(strata[members[0]], []).append(u)
        groups = stratum_units
    values = []
    for _ in range(reps):
        picked: list[int] = []
        for members in groups.values():
            for _ in members:
                picked.extend(units[members[rng.randrange(len(members))]])
        values.append(statistic(picked))
    values.sort()
    return _percentile(values, 0.025), _percentile(values, 0.975)


# ------------------------------------------------------------------ scoring


def _prob(answer: Mapping[str, Any] | None) -> float | None:
    if not answer or answer.get("status", "ok") != "ok":
        return None
    p = answer.get("p")
    if isinstance(p, bool) or not isinstance(p, (int, float)):
        return None
    try:
        value = float(p)  # a huge integer raises OverflowError here, and is simply invalid
    except OverflowError:
        return None
    return value if math.isfinite(value) and 0.0 <= value <= 1.0 else None


def gold_rows(
    lane: str,
    manifest: Mapping[str, Any],
    gold: Mapping[str, Any],
    reference: Mapping[str, Any],
    split: str,
) -> list[dict[str, Any]]:
    """Scorable rows for one lane: key, truth (bool), stratum weight group and cluster."""
    rows: list[dict[str, Any]] = []
    if lane in ("tags", "moments"):
        for item in manifest[lane]:
            if split != "all" and item["split"] != split:
                continue
            label = gold[lane][item["id"]]["label"]
            rows.append(
                {
                    "key": f"{lane}/{item['id']}",
                    "truth": label == "supported",
                    "uncertain": label == "uncertain",
                    "stratum": reference["strata"].get(item["id"]) if lane == "tags" else None,
                    "cluster": f"{item['src']}:{item['uid']}",
                    "gold_source": gold[lane][item["id"]]["source"],
                }
            )
    elif lane == "chapters":
        for chapter in manifest["chapters"]:
            if split != "all" and chapter["split"] != split:
                continue
            chosen = set(gold["chapters"][chapter["chapter_id"]]["tags"])
            for tag in chapter["tags"]:
                rows.append(
                    {
                        "key": f"chapters/{chapter['chapter_id']}/{tag['id']}",
                        "truth": tag["id"] in chosen,
                        "uncertain": False,
                        "stratum": None,
                        "cluster": chapter["chapter_id"],
                        "gold_source": "claude",
                    }
                )
    else:
        raise ValueError(f"unknown lane {lane!r}")
    return rows


def _weights(rows: Sequence[Mapping[str, Any]]) -> list[float]:
    """Tag rows weighted to the production stratum mix; everything else weight 1."""
    if not rows or rows[0]["stratum"] is None:
        return [1.0] * len(rows)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["stratum"]] = counts.get(r["stratum"], 0) + 1
    return [PRODUCTION_STRATUM_MIX[r["stratum"]] / counts[r["stratum"]] for r in rows]


def _weighted_mean(values: Sequence[float], weights: Sequence[float], idx: Iterable[int]) -> float:
    num = den = 0.0
    for i in idx:
        num += values[i] * weights[i]
        den += weights[i]
    return num / den if den else float("nan")


def _group_mean(values: Sequence[float], strata: Sequence[Any] | None, idx: Iterable[int]) -> float:
    """Mean of ``values`` over ``idx``; with strata, each stratum's mean weighted to the production
    mix (renormalized over the strata present), so a bootstrap draw that repeats a small episode
    cannot shift a stratum's weight. The same as the fixed per-item weights on the full sample."""
    if strata is None:
        picked = list(idx)
        return sum(values[i] for i in picked) / len(picked) if picked else float("nan")
    sums: dict[Any, list[float]] = {}
    for i in idx:
        sums.setdefault(strata[i], []).append(values[i])
    den = sum(PRODUCTION_STRATUM_MIX[s] for s in sums)
    if not den:
        return float("nan")
    return sum(PRODUCTION_STRATUM_MIX[s] * sum(v) / len(v) for s, v in sums.items()) / den


def score_lane(
    lane: str,
    rows: Sequence[Mapping[str, Any]],
    answers: Mapping[str, Mapping[str, Any]],
    reference_answers: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    reps: int = 2000,
) -> dict[str, Any]:
    """Metrics for one model on one lane's scorable rows (``uncertain`` rows are excluded)."""
    scored = [r for r in rows if not r["uncertain"]]
    probs = [_prob(answers.get(r["key"])) for r in scored]
    truth = [bool(r["truth"]) for r in scored]
    weights = _weights(scored)
    valid = [p is not None for p in probs]
    # strict accuracy: an invalid answer is a miss, so models are compared on the same items
    correct = [
        1.0 if p is not None and (p >= THRESHOLD) == t else 0.0
        for p, t in zip(probs, truth, strict=True)
    ]
    pairs = [(p, t) for p, t in zip(probs, truth, strict=True) if p is not None]
    n = len(scored)
    all_idx = list(range(n))
    clusters = [r["cluster"] for r in scored]
    strata = (
        [r["stratum"] for r in scored]
        if lane == "tags" and all(r["stratum"] for r in scored)
        else None
    )
    out: dict[str, Any] = {
        "lane": lane,
        "items": len(rows),
        "excluded_uncertain": len(rows) - n,
        "uncertain_share": round((len(rows) - n) / len(rows), 4) if rows else None,
        "scored": n,
        "valid": sum(valid),
        "invalid_rate": round(1 - sum(valid) / n, 4) if n else None,
        "accuracy_strict": _group_mean(correct, strata, all_idx) if n else None,
    }
    vidx = [i for i in all_idx if valid[i]]
    out["accuracy_valid"] = _group_mean(correct, strata, vidx) if vidx else None
    if n:
        out["accuracy_strict_ci"] = bootstrap_ci(
            n,
            lambda ix: _group_mean(correct, strata, ix),
            clusters=clusters,
            strata=strata,
            reps=reps,
        )
    # the score of answering the commoner class every time, on the same weighting: any model that
    # does not clearly beat it has learned nothing (chapter lane: about 10% of tags chosen)
    if n:
        major = max(
            (True, False),
            key=lambda c: sum(w for t, w in zip(truth, weights, strict=True) if t == c),
        )
        out["accuracy_majority_class"] = _weighted_mean(
            [1.0 if t == major else 0.0 for t in truth], weights, all_idx
        )
    out["ece"] = ece(pairs)
    out["brier"] = brier(pairs)
    pos = [t for _, t in pairs]
    out["positive_rate_gold"] = sum(pos) / len(pos) if pos else None
    if lane == "chapters":
        # over every scored tag: a chosen tag with no valid answer is a miss, not an omission
        predicted = [p is not None and p >= THRESHOLD for p in probs]
        tp = sum(1 for yes, t in zip(predicted, truth, strict=True) if yes and t)
        fp = sum(1 for yes, t in zip(predicted, truth, strict=True) if yes and not t)
        fn = sum(1 for yes, t in zip(predicted, truth, strict=True) if not yes and t)
        out["precision"] = tp / (tp + fp) if tp + fp else None
        out["recall"] = tp / (tp + fn) if tp + fn else None
        out["f1"] = 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None
    if lane == "moments":
        # nearly every moment is supported, so the useful number is how many correct quotes a
        # model wrongly rejects
        sup = [(p, t) for p, t in pairs if t]
        out["wrongly_rejected"] = sum(1 for p, _ in sup if p < THRESHOLD)
        out["supported_valid"] = len(sup)
    if reference_answers is not None:
        ref_probs = [_prob(reference_answers.get(r["key"])) for r in scored]
        both = [i for i in all_idx if probs[i] is not None and ref_probs[i] is not None]
        out["agreement_with_reference"] = (
            sum((probs[i] >= THRESHOLD) == (ref_probs[i] >= THRESHOLD) for i in both) / len(both)
            if both
            else None
        )
        out["agreement_items"] = len(both)
        ref_correct = [
            1.0 if rp is not None and (rp >= THRESHOLD) == t else 0.0
            for rp, t in zip(ref_probs, truth, strict=True)
        ]
        diff = [c - rc for c, rc in zip(correct, ref_correct, strict=True)]
        out["accuracy_minus_reference"] = _group_mean(diff, strata, all_idx)
        out["accuracy_minus_reference_ci"] = bootstrap_ci(
            n,
            lambda ix: _group_mean(diff, strata, ix),
            clusters=clusters,
            strata=strata,
            reps=reps,
        )
    return out


def tokens_per_tag(answers: Mapping[str, Mapping[str, Any]], tags_total: int) -> float | None:
    """Lane B input tokens per shown tag (sum over every chapter request / tags asked)."""
    seen: dict[str, int] = {}
    for key, ans in answers.items():
        if not key.startswith("chapters/") or "tokens" not in ans:
            continue
        # grouped answers all carry the request's tokens; count each request once
        request = ans.get("request", key)
        seen[request] = ans["tokens"]
    if not seen or not tags_total:
        return None
    return sum(seen.values()) / tags_total
