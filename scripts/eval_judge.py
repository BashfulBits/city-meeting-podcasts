"""Judge pilots (review/49): how to feed judges, and which question types work.

The judge stack (JEV as the anchor judge, a sibling LLM, an adjudicator) admits producer outputs
(tags, pull quotes, later anything with a valid/invalid or pick-the-best answer). This lane answers
the design questions that cannot be settled by reading: does putting evidence in each question work
as well as putting it in the shared state, do `choose`/`grade`/`validate` questions behave, and how
much context does a judge need (speed versus accuracy). It follows the `evals/chapter-agenda`
layout:
frozen inputs in `evals/judge/manifest.json`, truth in `evals/judge/gold.json`, dated results in
`evals/judge/results/`.

    citypods-env python scripts/eval_judge.py run bundling --judge jev
    citypods-env python scripts/eval_judge.py run question-types --judge jev
    citypods-env python scripts/eval_judge.py run question-types --judge qwen
    citypods-env python scripts/eval_judge.py run context-ladder --judge jev
    citypods-env python scripts/eval_judge.py run adjudicator --judge qwen
    python scripts/eval_judge.py run context-ladder --judge jev --dry-run   # plan only, no calls
    python scripts/eval_judge.py report evals/judge/results/*.json
    citypods-env python scripts/eval_judge.py freeze --state-dir <pulled state>   # reviewed change

Experiments:
  bundling        evidence inside each question (tiny state) versus all evidence in the state, two
                  shuffled orders, synthetic items with known truth (JEV).
  question-types  real pull quotes from real meetings: `validate` (noul), `grade` (score) and
                  `choose` (choice, best of four, two option orders) on JEV; `choose` on Qwen.
                  No ground truth exists, so the metrics are consistency (order stability, agreement
                  between question kinds and between judges).
  context-ladder  real rule-matched tag candidates judged at three context tiers (matched span,
                  +-45 s window, whole chapter) plus planted wrong-tag controls with known
                  truth (JEV).
  adjudicator     the bundling items through Qwen in small packs (its 6k-token input limit).

Limits are enforced in code so a run cannot repeat the failures found while building this lane:
JEV accepts about 64k total input tokens and about 32k for the state plus the single largest
question, allows one successful call per minute on the free tier, and answers an oversized request
with a misleading `503 processing_failed, retryable: true`; Groq's Qwen route caps a request near
7k input tokens and 1,000 output tokens a minute. Keys are stripped of whitespace (a trailing
carriage return once made a valid key read as invalid).

A run with more than 10% unanswered calls is reported inconclusive, not as a result.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import random
import re
import statistics
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
EVAL_DIR = REPO_ROOT / "evals" / "judge"
MANIFEST_PATH = EVAL_DIR / "manifest.json"
GOLD_PATH = EVAL_DIR / "gold.json"
RESULTS_DIR = EVAL_DIR / "results"
TAXONOMY_PATH = REPO_ROOT / "config" / "taxonomy.yml"

SET_VERSION = 1
PROMPT_VERSION = (
    "2"  # 2: tag question encodes the maintainer rubric (specific project or policy counts)
)

JEV_URL = "https://api.beatapi.io/v1/systemone"
JEV_MODEL = "jev-1.13-free"
JEV_MIN_INTERVAL_SECONDS = 66.0
JEV_TOTAL_CEILING_TOKENS = 58_000
JEV_STATE_PLUS_LARGEST_CEILING_TOKENS = 28_000
JEV_OVERSIZE_SUSPECT_FRACTION = 0.85
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "qwen/qwen3.8-27b"
GROQ_INPUT_CEILING_TOKENS = 6_000
GROQ_OUTPUT_TOKENS_PER_MINUTE = 900
GROQ_EXPECTED_COMPLETION_TOKENS = 700
GROQ_PACK_SIZE = 5
INCONCLUSIVE_UNANSWERED = 0.10
TOKENS_PER_WORD = 1.35
BACKFILL_CANDIDATES = 127_719  # rule candidates in storage on 2026-09-30 (review/49)

CONTEXT_TIERS = ("T0", "T1", "T2")
T0_WORD_CAP = 120
T1_WORD_CAP = 400
T2_WORD_CAP = 1_800


class JudgeError(Exception):
    """A judge call that produced no usable answer; ``kind`` is a stable result-file token."""

    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail


# --------------------------------------------------------------------------- sizing and packing


def est_tokens(text: str) -> int:
    """Deliberately conservative token estimate (real prose runs about 1.3 tokens per word)."""
    return int(max(len(text.split()) * TOKENS_PER_WORD, len(text) / 4)) + 1


def jev_size(body: Mapping[str, Any]) -> dict[str, int]:
    state = est_tokens(json.dumps(body.get("state", {})))
    per_question = [
        est_tokens(str(q.get("instructions", "")) + json.dumps(q.get("criteria", {})))
        for q in (body.get("questions") or {}).values()
    ]
    largest = max(per_question, default=0)
    return {
        "state": state,
        "questions": sum(per_question),
        "largest_question": largest,
        "total": state + sum(per_question),
        "state_plus_largest": state + largest,
    }


def check_jev_size(body: Mapping[str, Any]) -> dict[str, int]:
    size = jev_size(body)
    if size["total"] > JEV_TOTAL_CEILING_TOKENS:
        raise JudgeError("oversize", f"total {size['total']} > {JEV_TOTAL_CEILING_TOKENS}")
    if size["state_plus_largest"] > JEV_STATE_PLUS_LARGEST_CEILING_TOKENS:
        raise JudgeError(
            "oversize",
            f"state+largest {size['state_plus_largest']} > {JEV_STATE_PLUS_LARGEST_CEILING_TOKENS}",
        )
    return size


def pack_by_tokens(
    items: Sequence[Any], tokens_of: Callable[[Any], int], ceiling: int
) -> list[list[Any]]:
    """Greedy, order-preserving packing. An item larger than the ceiling travels alone (and then
    fails the client's size guard, which records it as unanswered rather than dropping it)."""
    batches: list[list[Any]] = []
    current: list[Any] = []
    used = 0
    for item in items:
        cost = tokens_of(item)
        if current and used + cost > ceiling:
            batches.append(current)
            current, used = [], 0
        current.append(item)
        used += cost
    if current:
        batches.append(current)
    return batches


# --------------------------------------------------------------------------- clients


def _json_or_text(response: Any) -> Any:
    try:
        return response.json()
    except Exception:
        return {"raw": str(getattr(response, "text", ""))[:300]}


class JevClient:
    """One BeatAPI question-answering call at a time, paced to the free tier's one per minute."""

    def __init__(self, api_key: str, *, post=None, sleep=time.sleep, clock=time.monotonic):
        key = (api_key or "").strip()
        if not key:
            raise JudgeError("auth", "BEATAPI_API_KEY is not set")
        self._key = key
        self._post = post
        self._sleep = sleep
        self._clock = clock
        self._last: float | None = None
        self.calls = 0

    def _http_post(self, body: Mapping[str, Any]):
        post = self._post
        if post is None:
            import requests

            post = requests.post
        return post(
            JEV_URL,
            headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"},
            json=body,
            timeout=300,
        )

    def _pace(self) -> None:
        if self._last is None:
            return
        remaining = JEV_MIN_INTERVAL_SECONDS - (self._clock() - self._last)
        if remaining > 0:
            self._sleep(remaining)

    def judge(self, body: Mapping[str, Any]) -> dict[str, Any]:
        size = check_jev_size(body)
        for attempt in range(2):
            self._pace()
            response = self._http_post(body)
            self._last = self._clock()
            self.calls += 1
            payload = _json_or_text(response)
            status = response.status_code
            if status == 200:
                answers = payload.get("answers") if isinstance(payload, dict) else None
                if not isinstance(answers, dict):
                    raise JudgeError("invalid_response", "no answers object")
                return {"answers": answers, "usage": payload.get("usage") or {}, "size": size}
            error = (payload.get("error") or {}) if isinstance(payload, dict) else {}
            if status == 401:
                raise JudgeError("auth", str(error.get("message", "")))
            if status == 400:
                raise JudgeError("bad_request", str(error.get("message", "")))
            if status == 429 and attempt == 0:
                self._sleep(float(response.headers.get("Retry-After", JEV_MIN_INTERVAL_SECONDS)))
                continue
            if status == 503 and error.get("code") == "processing_failed":
                # Oversized input is answered with this same retryable-looking 503. Near a ceiling
                # a retry can never succeed, so do not resend.
                near = (
                    size["total"] >= JEV_OVERSIZE_SUSPECT_FRACTION * JEV_TOTAL_CEILING_TOKENS
                    or size["state_plus_largest"]
                    >= JEV_OVERSIZE_SUSPECT_FRACTION * JEV_STATE_PLUS_LARGEST_CEILING_TOKENS
                )
                if near:
                    raise JudgeError("oversize_suspected", str(size))
                if attempt == 0:
                    continue
            raise JudgeError("upstream", f"HTTP {status} {str(error.get('code', ''))}")
        raise JudgeError("upstream", "retries exhausted")


class GroqClient:
    """Qwen via Groq: JSON-schema answers, a small input ceiling, an output-tokens/minute cap."""

    def __init__(self, api_key: str, *, post=None, sleep=time.sleep, clock=time.monotonic):
        key = (api_key or "").strip()
        if not key:
            raise JudgeError("auth", "GROQ_API_KEY is not set")
        self._key = key
        self._post = post
        self._sleep = sleep
        self._clock = clock
        self._window: list[tuple[float, int]] = []
        self.calls = 0

    def _guard_output_budget(self) -> None:
        now = self._clock()
        self._window = [(t, n) for t, n in self._window if now - t < 60.0]
        used = sum(n for _, n in self._window)
        if used + GROQ_EXPECTED_COMPLETION_TOKENS > GROQ_OUTPUT_TOKENS_PER_MINUTE and self._window:
            self._sleep(max(0.0, 60.0 - (now - self._window[0][0])) + 1.0)
            self._window = []

    def answer(self, prompt: str, schema: Mapping[str, Any], *, max_tokens: int = 3_000):
        tokens = est_tokens(prompt)
        if tokens > GROQ_INPUT_CEILING_TOKENS:
            raise JudgeError("oversize", f"{tokens} > {GROQ_INPUT_CEILING_TOKENS}")
        body = {
            "model": GROQ_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "response_format": schema,
            "reasoning_effort": "high",
            "reasoning_format": "parsed",
        }
        post = self._post
        if post is None:
            import requests

            post = requests.post
        for attempt in range(2):
            self._guard_output_budget()
            response = post(
                GROQ_URL,
                headers={
                    "Authorization": f"Bearer {self._key}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=300,
            )
            self.calls += 1
            payload = _json_or_text(response)
            status = response.status_code
            if status == 200:
                usage = payload.get("usage") or {}
                self._window.append((self._clock(), int(usage.get("completion_tokens") or 0)))
                try:
                    content = json.loads(payload["choices"][0]["message"]["content"])
                except Exception as exc:
                    raise JudgeError("invalid_response", str(exc)) from exc
                return {"content": content, "usage": usage}
            if status == 401:
                raise JudgeError("auth", "Groq rejected the key")
            if status in (413, 400):
                raise JudgeError("oversize" if status == 413 else "bad_request", str(payload)[:200])
            if status == 429 and attempt == 0:
                self._sleep(65.0)
                continue
            raise JudgeError("upstream", f"HTTP {status}")
        raise JudgeError("upstream", "retries exhausted")


# --------------------------------------------------------------------------- question builders


def q_noul(instructions: str) -> dict[str, Any]:
    return {"type": "noul", "instructions": instructions, "criteria": {}}


def q_score(instructions: str, levels: Sequence[str]) -> dict[str, Any]:
    return {"type": "score", "instructions": instructions, "criteria": list(levels)}


def q_choice(instructions: str, options: Mapping[str, str]) -> dict[str, Any]:
    return {"type": "choice", "instructions": instructions, "criteria": dict(options)}


GRADE_LEVELS = (
    "1 unusable or procedural",
    "2 weak",
    "3 acceptable",
    "4 good",
    "5 excellent standalone quote",
)
PUBLISH_CRITERION = (
    "Would a resident who sees only this clip find it informative and worth sharing, and is it "
    "safe to publish (no procedural filler, no mocking, no private personal details)?"
)
BUNDLING_TASK = "Judge whether each claim is supported by the evidence given in its own question."
STATE_TASK = "Judge whether each claim is supported by the evidence in state.evidence."
QUOTE_TASK = "Judge candidate pull quotes from public city council meetings."
TAG_TASK = (
    "Validate topic tags for public city meeting agenda items using the excerpt given in each "
    "question."
)


def claim_text(item: Mapping[str, Any]) -> str:
    return f"The council approved {item['subject']}."


def bundling_body(items: Sequence[Mapping[str, Any]], layout: str) -> dict[str, Any]:
    """``question``: each question carries its own evidence; ``state``: evidence in the state."""
    if layout == "question":
        questions = {
            it["id"]: q_noul(
                f"Evidence: {it['text']}\n\nClaim: {claim_text(it)}\n"
                "Using ONLY the evidence above, "
                "how probable is it that the claim is supported?"
            )
            for it in items
        }
        return {"model": JEV_MODEL, "state": {"task": BUNDLING_TASK}, "questions": questions}
    if layout == "state":
        return {
            "model": JEV_MODEL,
            "state": {"task": STATE_TASK, "evidence": {it["id"]: it["text"] for it in items}},
            "questions": {
                it["id"]: q_noul(
                    f"Using ONLY state.evidence['{it['id']}'], how probable is it that this "
                    "claim is "
                    f"supported: {claim_text(it)}"
                )
                for it in items
            },
        }
    raise ValueError(f"unknown layout {layout!r}")


def question_types_body(meetings: Sequence[Mapping[str, Any]], order_seed: int):
    """One JEV call covering every quote with `validate`, `grade`, and one `choose` per meeting."""
    rng = random.Random(order_seed)
    questions: dict[str, Any] = {}
    choice_maps: dict[str, dict[str, str]] = {}
    for meeting in meetings:
        for quote in meeting["quotes"]:
            evidence = (
                f'Pull quote from a city council meeting: "{quote["quote"]}" '
                f"(producer's reason: {quote['why']}). Clip length {quote['dur']} s."
            )
            questions[f"pub_{quote['id']}"] = q_noul(
                evidence + "\n\n" + PUBLISH_CRITERION + " Probability the answer is yes."
            )
            questions[f"score_{quote['id']}"] = q_score(
                evidence + "\n\nRate the usefulness and publication-readiness of this clip.",
                GRADE_LEVELS,
            )
        options = list(meeting["quotes"])
        rng.shuffle(options)
        names = "ABCD"[: len(options)]
        questions[f"best_{meeting['uid']}"] = q_choice(
            "Which of these clips from the same meeting is the best standalone pull quote for a "
            "resident audience (informative, self-contained, safe)?",
            {names[i]: f'"{o["quote"]}" (why: {o["why"]})' for i, o in enumerate(options)},
        )
        choice_maps[f"best_{meeting['uid']}"] = {names[i]: o["id"] for i, o in enumerate(options)}
    body = {"model": JEV_MODEL, "state": {"task": QUOTE_TASK}, "questions": questions}
    return body, choice_maps


def tag_question(item: Mapping[str, Any], tier: str) -> str:
    return (
        f"Topic tag: {item['label']}. Definition: {item['desc']}\n"
        f"Meeting agenda item (chapter) title: {item['chapter']}\n"
        f"Transcript excerpt ({tier}): {item[tier]}\n\n"
        "Is this the right tag for this agenda item? Count it when the discussion or action "
        "involves a specific project, contract, program or policy on the topic, even if it is "
        "approved routinely (for example on a consent agenda). Do not count a generic mention, a "
        "passing reference in a list or summary, a read-back of past items, or a general-purpose "
        "services contract that is not tied to a specific project or policy on the topic. "
        "Probability the tag is correct."
    )


def context_body(items: Sequence[Mapping[str, Any]], tier: str) -> dict[str, Any]:
    return {
        "model": JEV_MODEL,
        "state": {"task": TAG_TASK},
        "questions": {it["id"]: q_noul(tag_question(it, tier)) for it in items},
    }


CHOOSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "choose",
        "strict": False,
        "schema": {
            "type": "object",
            "properties": {"best": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["best", "reason"],
            "additionalProperties": False,
        },
    },
}
ADJUDICATE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "adjudicate",
        "strict": False,
        "schema": {
            "type": "object",
            "properties": {
                "verdicts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "supported": {"type": "boolean"},
                            "quote": {"type": "string"},
                        },
                        "required": ["id", "supported", "quote"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["verdicts"],
            "additionalProperties": False,
        },
    },
}


def choose_prompt(meeting: Mapping[str, Any], rng: random.Random):
    options = list(meeting["quotes"])
    rng.shuffle(options)
    names = "ABCD"[: len(options)]
    text = (
        "Which of these clips from the same city council meeting is the best standalone pull quote "
        "for a resident audience (informative, self-contained, safe to publish)? Answer with the "
        "letter.\n"
        + "\n".join(f'{names[i]}: "{o["quote"]}" (why: {o["why"]})' for i, o in enumerate(options))
    )
    return text, {names[i]: o["id"] for i, o in enumerate(options)}


def adjudicate_prompt(pack: Sequence[Mapping[str, Any]]) -> str:
    parts = [
        "Decide for each claim whether its evidence supports it. Use ONLY that item's evidence. "
        "Quote the decisive phrase.\n"
    ]
    for it in pack:
        parts.append(f"ID {it['id']}\nEvidence: {it['text']}\nClaim: {claim_text(it)}\n")
    return "\n".join(parts)


# --------------------------------------------------------------------------- metrics


def auc(positives: Sequence[float], negatives: Sequence[float]) -> float | None:
    if not positives or not negatives:
        return None
    wins = sum((p > n) + 0.5 * (p == n) for p in positives for n in negatives)
    return wins / (len(positives) * len(negatives))


def accuracy_at(answers: Mapping[str, float], truth: Mapping[str, bool], threshold=0.5) -> float:
    ids = [k for k in answers if k in truth]
    return sum((answers[k] >= threshold) == truth[k] for k in ids) / len(ids) if ids else 0.0


def verdict_flips(a: Mapping[str, float], b: Mapping[str, float], threshold=0.5) -> int:
    return sum((a[k] >= threshold) != (b[k] >= threshold) for k in a if k in b)


def mean_abs_diff(a: Mapping[str, float], b: Mapping[str, float]) -> float:
    shared = [k for k in a if k in b]
    return sum(abs(a[k] - b[k]) for k in shared) / len(shared) if shared else 0.0


def separation_metrics(
    answers: Mapping[str, float], truth: Mapping[str, bool]
) -> dict[str, float | None]:
    positives = [v for k, v in answers.items() if truth.get(k) is True]
    negatives = [v for k, v in answers.items() if truth.get(k) is False]
    return {
        "accuracy_at_0.5": round(accuracy_at(answers, truth), 4),
        "auc": None if auc(positives, negatives) is None else round(auc(positives, negatives), 4),
        "mean_true": round(statistics.mean(positives), 3) if positives else None,
        "mean_false": round(statistics.mean(negatives), 3) if negatives else None,
        "max_false": round(max(negatives), 3) if negatives else None,
        "min_true": round(min(positives), 3) if positives else None,
    }


# --------------------------------------------------------------------------- data access


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_inputs() -> tuple[dict[str, Any], dict[str, Any]]:
    if not MANIFEST_PATH.exists() or not GOLD_PATH.exists():
        raise SystemExit(f"{MANIFEST_PATH} or {GOLD_PATH} missing; run `freeze` first")
    manifest, gold = load_json(MANIFEST_PATH), load_json(GOLD_PATH)
    if manifest.get("version") != SET_VERSION:
        raise SystemExit(f"manifest version {manifest.get('version')} != script {SET_VERSION}")
    return manifest, gold


def git_sha() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except Exception:
        return None


def finish(
    experiment: str, judge: str, payload: dict[str, Any], *, answered: int, asked: int, errors
) -> dict[str, Any]:
    unanswered = (asked - answered) / asked if asked else 0.0
    result = {
        "experiment": experiment,
        "judge": judge,
        "generated_at": datetime.now(UTC).isoformat(),
        "set_version": SET_VERSION,
        "prompt_version": PROMPT_VERSION,
        "git_sha": git_sha(),
        "calls_asked": asked,
        "calls_answered": answered,
        "unanswered_fraction": round(unanswered, 4),
        "inconclusive": unanswered > INCONCLUSIVE_UNANSWERED,
        "errors": list(errors),
        **payload,
    }
    return result


# --------------------------------------------------------------------------- experiments


def run_bundling(manifest, gold, jev: JevClient | None, *, dry_run=False) -> dict[str, Any]:
    items = manifest["bundling"]["items"]
    truth = gold["bundling"]
    order1 = list(items)
    order2 = list(items)
    random.Random(manifest["bundling"]["order_seed"]).shuffle(order2)
    plan = [
        (layout, name, order)
        for layout in ("question", "state")
        for name, order in (("order1", order1), ("order2", order2))
    ]
    sizes = {
        f"{layout}-{name}": jev_size(bundling_body(order, layout)) for layout, name, order in plan
    }
    if dry_run:
        return {"plan": sizes}
    answers: dict[str, dict[str, float]] = {}
    usage: dict[str, Any] = {}
    errors: list[str] = []
    for layout, name, order in plan:
        key = f"{layout}-{name}"
        try:
            out = jev.judge(bundling_body(order, layout))
        except JudgeError as exc:
            errors.append(f"{key}: {exc}")
            continue
        answers[key] = {k: float(v["noul"]) for k, v in out["answers"].items()}
        usage[key] = out["usage"]
    metrics: dict[str, Any] = {}
    for key, ans in answers.items():
        metrics[key] = separation_metrics(ans, truth)
    for a, b in (
        ("question-order1", "question-order2"),
        ("state-order1", "state-order2"),
        ("question-order1", "state-order1"),
    ):
        if a in answers and b in answers:
            metrics[f"{a}_vs_{b}"] = {
                "mean_abs_dp": round(mean_abs_diff(answers[a], answers[b]), 4),
                "max_abs_dp": round(max(abs(answers[a][k] - answers[b][k]) for k in answers[a]), 4),
                "verdict_flips": verdict_flips(answers[a], answers[b]),
            }
    return finish(
        "bundling",
        "jev",
        {"sizes": sizes, "usage": usage, "metrics": metrics, "answers": answers},
        answered=len(answers),
        asked=len(plan),
        errors=errors,
    )


def run_question_types(manifest, gold, judge: str, jev, qwen, *, dry_run=False) -> dict[str, Any]:
    meetings = manifest["question_types"]["meetings"]
    if dry_run:
        body, _ = question_types_body(meetings, 1)
        return {"plan": {"jev_calls": 2, "jev_size": jev_size(body), "qwen_calls": len(meetings)}}
    errors: list[str] = []
    if judge == "jev":
        runs: list[dict[str, Any]] = []
        for seed in (1, 2):
            body, maps = question_types_body(meetings, seed)
            try:
                out = jev.judge(body)
            except JudgeError as exc:
                errors.append(f"order{seed}: {exc}")
                continue
            runs.append({"answers": out["answers"], "maps": maps, "usage": out["usage"]})
        metrics = question_type_metrics(meetings, runs) if runs else {}
        return finish(
            "question-types",
            "jev",
            {"metrics": metrics, "runs": runs},
            answered=len(runs),
            asked=2,
            errors=errors,
        )
    winners: dict[str, str] = {}
    usage: dict[str, Any] = {}
    for n, meeting in enumerate(meetings):
        prompt, mapping = choose_prompt(meeting, random.Random(5 + n))
        try:
            out = qwen.answer(prompt, CHOOSE_SCHEMA, max_tokens=1_200)
        except JudgeError as exc:
            errors.append(f"{meeting['uid']}: {exc}")
            continue
        letter = str(out["content"].get("best", "")).strip()[:1]
        if letter in mapping:
            winners[meeting["uid"]] = mapping[letter]
            usage[meeting["uid"]] = out["usage"].get("completion_tokens")
        else:
            errors.append(f"{meeting['uid']}: invalid letter {letter!r}")
    return finish(
        "question-types",
        "qwen",
        {"winners": winners, "completion_tokens": usage},
        answered=len(winners),
        asked=len(meetings),
        errors=errors,
    )


def question_type_metrics(meetings, runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    winners: dict[str, list[str]] = collections.defaultdict(list)
    pub: dict[str, list[float]] = collections.defaultdict(list)
    grade: dict[str, list[float]] = collections.defaultdict(list)
    for run in runs:
        for name, value in run["answers"].items():
            if name.startswith("pub_"):
                pub[name[4:]].append(float(value["noul"]))
            elif name.startswith("score_"):
                grade[name[6:]].append(float(value["score"]))
            elif name.startswith("best_"):
                winners[name[5:]].append(run["maps"][name][value["choice"]])
    stable = sum(len(set(w)) == 1 for w in winners.values() if len(w) == len(runs))
    agree_grade = agree_pub = producer = 0
    mean = lambda d, i: sum(d[i]) / len(d[i])  # noqa: E731
    for meeting in meetings:
        ids = [q["id"] for q in meeting["quotes"]]
        first = winners[meeting["uid"]][0] if winners.get(meeting["uid"]) else None
        agree_grade += first == max(ids, key=lambda i: mean(grade, i))
        agree_pub += first == max(ids, key=lambda i: mean(pub, i))
        producer += (
            first == max(meeting["quotes"], key=lambda q: q.get("producer_score") or 0)["id"]
        )
    return {
        "meetings": len(meetings),
        "winner_stable_across_orders": stable,
        "winner_equals_top_grade": agree_grade,
        "winner_equals_top_publish_probability": agree_pub,
        "winner_equals_producer_top_score": producer,
        "publish_probability_max_abs_diff": round(
            max((abs(v[0] - v[-1]) for v in pub.values() if len(v) > 1), default=0.0), 4
        ),
        "grade_max_abs_diff": round(
            max((abs(v[0] - v[-1]) for v in grade.values() if len(v) > 1), default=0.0), 4
        ),
        "publish_probability_range": [
            round(min(mean(pub, i) for i in pub), 3),
            round(max(mean(pub, i) for i in pub), 3),
        ],
        "first_order_winners": {u: w[0] for u, w in winners.items()},
        "all_order_winners": dict(winners),
    }


def compare_winners(
    jev_result: Mapping[str, Any], qwen_result: Mapping[str, Any]
) -> dict[str, Any]:
    qwen = qwen_result.get("winners", {})
    per_order = []
    for run in jev_result.get("runs", []):
        jw = {
            name[5:]: run["maps"][name][value["choice"]]
            for name, value in run["answers"].items()
            if name.startswith("best_")
        }
        per_order.append(sum(qwen.get(u) == w for u, w in jw.items()))
    return {"qwen_equals_jev_winner_by_order": per_order, "meetings": len(qwen)}


def run_context_ladder(manifest, gold, jev, *, dry_run=False) -> dict[str, Any]:
    items = manifest["context_ladder"]["items"]
    plan: dict[str, Any] = {}
    batches: dict[str, list[list[dict[str, Any]]]] = {}
    for tier in CONTEXT_TIERS:
        cost = lambda it, t=tier: est_tokens(tag_question(it, t))  # noqa: E731
        batches[tier] = pack_by_tokens(items, cost, JEV_TOTAL_CEILING_TOKENS - 2_000)
        per_item = sum(cost(i) for i in items) / len(items)
        plan[tier] = {
            "batches": len(batches[tier]),
            "est_tokens_per_item": round(per_item, 1),
            "items_per_call_at_ceiling": int((JEV_TOTAL_CEILING_TOKENS - 2_000) // per_item),
            "projected_calls_for_backfill": int(
                BACKFILL_CANDIDATES / ((JEV_TOTAL_CEILING_TOKENS - 2_000) // per_item) + 1
            ),
        }
    if dry_run:
        return {"plan": plan}
    answers: dict[str, dict[str, float]] = {}
    usage: dict[str, list[Any]] = collections.defaultdict(list)
    errors: list[str] = []
    asked = answered = 0
    for tier in CONTEXT_TIERS:
        for n, batch in enumerate(batches[tier]):
            asked += 1
            try:
                out = jev.judge(context_body(batch, tier))
            except JudgeError as exc:
                errors.append(f"{tier}[{n}]: {exc}")
                continue
            answered += 1
            usage[tier].append(out["usage"])
            answers.setdefault(tier, {}).update(
                {k: float(v["noul"]) for k, v in out["answers"].items()}
            )
    metrics: dict[str, Any] = {}
    real = {i["id"] for i in items if not i.get("control")}
    for tier, ans in answers.items():
        real_answers = {k: v for k, v in ans.items() if k in real}
        control_answers = {k: v for k, v in ans.items() if k not in real}
        metrics[tier] = {
            # Real items are unverified rule matches, so this measures separation from the planted
            # wrong-tag controls (which must be rejected), not accuracy on the real items.
            "auc_real_vs_controls": None
            if auc(list(real_answers.values()), list(control_answers.values())) is None
            else round(auc(list(real_answers.values()), list(control_answers.values())), 4),
            "controls": len(control_answers),
            "controls_accepted_at_0.5": sum(v >= 0.5 for v in control_answers.values()),
            "control_max": round(max(control_answers.values()), 3) if control_answers else None,
            "control_mean": round(statistics.mean(control_answers.values()), 3)
            if control_answers
            else None,
            "real_items": len(real_answers),
            "real_items_accepted_at_0.5": sum(v >= 0.5 for v in real_answers.values()),
            "real_mean": round(statistics.mean(real_answers.values()), 3) if real_answers else None,
        }
    top = CONTEXT_TIERS[-1]
    for tier in CONTEXT_TIERS[:-1]:
        if tier in answers and top in answers:
            real_a = {k: v for k, v in answers[tier].items() if k in real}
            real_b = {k: v for k, v in answers[top].items() if k in real}
            metrics[f"{tier}_vs_{top}_real_items"] = {
                "verdict_flips": verdict_flips(real_a, real_b),
                "mean_abs_dp": round(mean_abs_diff(real_a, real_b), 4),
            }
    return finish(
        "context-ladder",
        "jev",
        {"plan": plan, "usage": dict(usage), "metrics": metrics, "answers": answers},
        answered=answered,
        asked=asked,
        errors=errors,
    )


def run_adjudicator(manifest, gold, qwen, *, dry_run=False) -> dict[str, Any]:
    items = manifest["bundling"]["items"]
    truth = gold["bundling"]
    packs = [items[i : i + GROQ_PACK_SIZE] for i in range(0, len(items), GROQ_PACK_SIZE)]
    if dry_run:
        return {
            "plan": {
                "qwen_calls": len(packs),
                "est_tokens_per_pack": est_tokens(adjudicate_prompt(packs[0])),
            }
        }
    verdicts: dict[str, bool] = {}
    usage: list[Any] = []
    errors: list[str] = []
    answered = 0
    for n, pack in enumerate(packs):
        try:
            out = qwen.answer(adjudicate_prompt(pack), ADJUDICATE_SCHEMA, max_tokens=3_000)
        except JudgeError as exc:
            errors.append(f"pack{n}: {exc}")
            continue
        answered += 1
        usage.append(out["usage"])
        for v in out["content"].get("verdicts", []):
            verdicts[str(v.get("id"))] = bool(v.get("supported"))
    correct = sum(verdicts.get(k) == truth[k] for k in truth)
    return finish(
        "adjudicator",
        "qwen",
        {
            "metrics": {"correct": correct, "items": len(truth), "answered_items": len(verdicts)},
            "verdicts": verdicts,
            "usage": usage,
        },
        answered=answered,
        asked=len(packs),
        errors=errors,
    )


# --------------------------------------------------------------------------- freeze


def bundling_items(seed: int = 42) -> list[dict[str, Any]]:
    """30 synthetic items: 10 supported, 10 near-miss (tabled or denied), 10 off-topic."""
    rng = random.Random(seed)
    subjects = [
        "a zoning change for the Elm Street parcel",
        "the Riverside park renovation contract",
        "a short-term rental permit cap",
        "the library hours ordinance",
        "a stormwater fee increase",
        "the downtown parking plan",
        "a bike lane on Main Street",
        "the fire station bond measure",
        "a variance for the Oak Hollow subdivision",
        "the animal shelter budget amendment",
    ]
    others = [
        "the city auditor's annual report",
        "a proclamation for Fire Prevention Week",
        "the regional transit interlocal agreement",
        "a rezoning request on Cedar Road",
        "a water rate study",
        "the police overtime policy",
        "a historic preservation grant",
        "the solid waste franchise",
        "an airport hangar lease",
        "a sidewalk repair program",
    ]
    names = [
        "Smith",
        "Garcia",
        "Nguyen",
        "Patel",
        "Brown",
        "Lopez",
        "Khan",
        "Olsen",
        "Reed",
        "Tran",
    ]
    filler_lines = [
        "Staff presented the background and the fiscal impact.",
        "Several residents spoke during public comment.",
        "Councilmember {a} asked about the timeline and the funding source.",
        "Councilmember {b} noted concerns raised by nearby neighbors.",
        "The city attorney confirmed the item was properly noticed.",
        "The item was discussed for about twenty minutes.",
    ]

    def passage(subject: str, outcome: str, index: int) -> str:
        a, b = rng.sample(names, 2)
        filler = " ".join(rng.choice(filler_lines) for _ in range(9)).format(a=a, b=b)
        endings = {
            "approve": f"Councilmember {a} moved to approve {subject}. Councilmember {b} seconded. "
            "The motion carried 6-1.",
            "table": f"Councilmember {a} moved to table {subject} until the next meeting. "
            "The motion "
            "to table carried 5-2; no action was taken on the item.",
            "deny": f"Councilmember {a} moved to deny {subject}. The motion carried 4-3 and the "
            "request was denied.",
        }
        return f"[Item {index}] {filler} {endings[outcome]}"

    items: list[dict[str, Any]] = []
    for subject in subjects:
        items.append(
            {
                "subject": subject,
                "text": passage(subject, "approve", len(items)),
                "truth": True,
                "kind": "supported",
            }
        )
    for subject in subjects:
        items.append(
            {
                "subject": subject,
                "text": passage(subject, rng.choice(["table", "deny"]), len(items)),
                "truth": False,
                "kind": "near_miss",
            }
        )
    for subject, other in zip(subjects, others, strict=True):
        items.append(
            {
                "subject": subject,
                "text": passage(other, "approve", len(items)),
                "truth": False,
                "kind": "off_topic",
            }
        )
    for index, item in enumerate(items):
        item["id"] = f"e{index}"
    return items


def parse_vtt(text: str) -> list[tuple[float, float, str]]:
    cues: list[tuple[float, float, str]] = []
    stamp = re.compile(r"(?:(\d+):)?(\d+):(\d+)[.,](\d+)\s*-->\s*(?:(\d+):)?(\d+):(\d+)[.,](\d+)")
    for block in re.split(r"\n\s*\n", text):
        lines = [line for line in block.split("\n") if line.strip()]
        for position, line in enumerate(lines):
            match = stamp.match(line)
            if match:
                g = match.groups()
                start = int(g[0] or 0) * 3600 + int(g[1]) * 60 + int(g[2]) + int(g[3]) / 1000
                end = int(g[4] or 0) * 3600 + int(g[5]) * 60 + int(g[6]) + int(g[7]) / 1000
                cues.append((start, end, " ".join(lines[position + 1 :])))
                break
    return cues


def cap_words(text: str, limit: int, *, center: bool = False) -> str:
    words = text.split()
    if len(words) <= limit:
        return text
    start = (len(words) - limit) // 2 if center else 0
    return " ".join(words[start : start + limit])


def build_tiers(cues, chapters, evidence_times: Sequence[float]) -> dict[str, Any] | None:
    """T0 matched spans, T1 a +-45 s window, T2 the chapter (centered, capped); None if unusable."""
    if not cues or not chapters or not evidence_times:
        return None
    t0 = evidence_times[0]
    starts = [c["start"] for c in chapters]
    index = max([i for i, s in enumerate(starts) if s <= t0] or [0])
    chapter_start = chapters[index]["start"]
    chapter_end = chapters[index + 1]["start"] if index + 1 < len(chapters) else cues[-1][1]

    def between(a: float, b: float) -> str:
        return " ".join(text for start, _end, text in cues if a <= start < b)

    spans = []
    for t in evidence_times[:4]:
        near = [text for s, e, text in cues if s <= t < e] or [
            text for s, _e, text in cues if abs(s - t) < 8
        ][:1]
        spans.append(near[0] if near else "")
    chapter_words = between(chapter_start, chapter_end).split()
    if len(chapter_words) > T2_WORD_CAP:
        position = len(between(chapter_start, t0).split())
        a = max(0, min(position - T2_WORD_CAP // 2, len(chapter_words) - T2_WORD_CAP))
        chapter_words = chapter_words[a : a + T2_WORD_CAP]
    return {
        "chapter": chapters[index].get("title", ""),
        "t0": t0,
        "T0": cap_words(" ... ".join(spans), T0_WORD_CAP),
        "T1": cap_words(between(t0 - 45, t0 + 45), T1_WORD_CAP, center=True),
        "T2": " ".join(chapter_words),
    }


def iter_episode_files(state_dir: Path):
    yield from sorted(Path(state_dir).glob("sources/*/episodes.json"))


def freeze_question_types(state_dir: Path, *, seed=11, meetings=6, per_meeting=4):
    candidates: dict[tuple[str, str, str], list[dict[str, Any]]] = collections.defaultdict(list)
    for path in iter_episode_files(state_dir):
        for record in load_json(path).get("episodes", {}).values():
            for cand in (record.get("moments") or {}).get("pullquote_candidates") or []:
                if cand.get("quote") and len(cand["quote"]) > 40:
                    candidates[(path.parent.name, record["uid"], record.get("title") or "")].append(
                        cand
                    )
    eligible = sorted(k for k, v in candidates.items() if len(v) >= per_meeting)
    rng = random.Random(seed)
    rng.shuffle(eligible)
    frozen = []
    for key in eligible[:meetings]:
        picked = rng.sample(sorted(candidates[key], key=lambda c: c["start"]), per_meeting)
        frozen.append(
            {
                "uid": key[1],
                "title": key[2],
                "source": key[0],
                "quotes": [
                    {
                        "id": f"m{len(frozen)}q{i}",
                        "quote": c["quote"],
                        "why": c.get("why", ""),
                        "producer_score": c.get("quality_score"),
                        "dur": round(c["end"] - c["start"], 1),
                    }
                    for i, c in enumerate(picked)
                ],
            }
        )
    return frozen


def freeze_context_ladder(
    state_dir: Path,
    fetch_text: Callable[[str], str],
    *,
    seed=21,
    real=34,
    controls=10,
    taxonomy=None,
):
    import yaml

    taxonomy = taxonomy or {
        t["id"]: t for t in yaml.safe_load(TAXONOMY_PATH.read_text(encoding="utf-8"))["tags"]
    }
    candidates = []
    for path in iter_episode_files(state_dir):
        for record in load_json(path).get("episodes", {}).values():
            transcript = record.get("transcript") or {}
            chapters = record.get("chapters") or []
            if not transcript.get("url") or transcript.get("format") != "vtt" or not chapters:
                continue
            for cand in record.get("llm_tag_candidates") or []:
                if (
                    cand.get("source_kind") != "rule"
                    or cand.get("scope") != "chapter"
                    or cand.get("candidate_state") == "historical"
                    or cand.get("id") not in taxonomy
                ):
                    continue
                times = [e["t"] for e in cand.get("evidence", []) if e.get("t") is not None]
                if times:
                    candidates.append((record["uid"], transcript["url"], chapters, cand, times))
    candidates.sort(key=lambda c: (c[0], c[3]["id"], c[3].get("chapter_id", "")))
    rng = random.Random(seed)
    rng.shuffle(candidates)
    per_tag: collections.Counter[str] = collections.Counter()
    cache: dict[str, list] = {}
    items: list[dict[str, Any]] = []
    for uid, url, chapters, cand, times in candidates:
        if len(items) >= real:
            break
        if per_tag[cand["id"]] >= 2:
            continue
        if url not in cache:
            try:
                cache[url] = parse_vtt(fetch_text(url))
            except Exception:
                cache[url] = []
        tiers = build_tiers(cache[url], chapters, times)
        if not tiers:
            continue
        per_tag[cand["id"]] += 1
        tag = taxonomy[cand["id"]]
        items.append(
            {
                "uid": uid,
                "tag": cand["id"],
                "label": tag["label"],
                "desc": " ".join(str(tag["description"]).split()),
                "control": False,
                "phrase": cand.get("rule_pattern"),
                **tiers,
            }
        )
    tag_ids = sorted(taxonomy)
    for source in rng.sample(items, min(controls, len(items))):
        wrong = rng.choice(
            [
                t
                for t in tag_ids
                if t != source["tag"]
                and taxonomy[t].get("group") != taxonomy[source["tag"]].get("group")
            ]
        )
        item = dict(source)
        item.update(
            tag=wrong,
            label=taxonomy[wrong]["label"],
            desc=" ".join(str(taxonomy[wrong]["description"]).split()),
            control=True,
            phrase=None,
        )
        items.append(item)
    for index, item in enumerate(items):
        item["id"] = f"i{index}"
    return items


def freeze(state_dir: Path, *, force: bool, fetch_text=None) -> dict[str, Any]:
    if MANIFEST_PATH.exists() and not force:
        raise SystemExit(f"{MANIFEST_PATH} exists; re-freezing is a reviewed change (use --force)")
    if fetch_text is None:
        import requests

        fetch_text = lambda url: requests.get(url, timeout=60).text  # noqa: E731
    bundling = bundling_items()
    ladder = freeze_context_ladder(state_dir, fetch_text)
    manifest = {
        "version": SET_VERSION,
        "frozen_on": datetime.now(UTC).date().isoformat(),
        "about": "Inputs only; truth is in gold.json. See evals/judge/README.md.",
        "bundling": {
            "seed": 42,
            "order_seed": 7,
            "items": [{k: v for k, v in i.items() if k not in ("truth", "kind")} for i in bundling],
        },
        "question_types": {"meetings": freeze_question_types(state_dir)},
        "context_ladder": {"items": ladder},
    }
    gold = {
        "version": SET_VERSION,
        "bundling": {i["id"]: i["truth"] for i in bundling},
        "bundling_kinds": {i["id"]: i["kind"] for i in bundling},
        "context_ladder": {"controls": sorted(i["id"] for i in ladder if i["control"])},
        "notes": "Controls are real excerpts paired with a tag from an unrelated taxonomy group; "
        "they must be rejected. "
        "No ground truth exists for pull-quote quality; that experiment measures consistency only.",
    }
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    GOLD_PATH.write_text(json.dumps(gold, indent=1) + "\n", encoding="utf-8")
    return {
        "bundling": len(bundling),
        "meetings": len(manifest["question_types"]["meetings"]),
        "context_items": len(ladder),
        "controls": len(gold["context_ladder"]["controls"]),
    }


def tier_accuracy_on_labels(
    answers: Mapping[str, Mapping[str, float]], labels: Mapping[str, bool], threshold: float = 0.5
) -> dict[str, dict[str, Any]]:
    """Accuracy of each tier on adjudicated items; a ``None`` label is Uncertain and is excluded."""
    out: dict[str, dict[str, Any]] = {}
    for tier, by_item in answers.items():
        ids = [k for k in labels if k in by_item and labels[k] is not None]
        correct = sum((by_item[k] >= threshold) == labels[k] for k in ids)
        out[tier] = {
            "labeled_items": len(ids),
            "correct": correct,
            "accuracy": round(correct / len(ids), 4) if ids else None,
        }
    return out


def simulate_escalation(
    answers: Mapping[str, Mapping[str, float]],
    labels: Mapping[str, bool],
    real_ids: Sequence[str],
    *,
    first: str,
    then: str,
    low: float,
    high: float,
    threshold: float = 0.5,
) -> dict[str, Any]:
    """Judge at ``first``; re-judge at ``then`` when the first probability is inside [low, high].

    Reports the escalated share over every real item (which sets the cost) and the accuracy on the
    adjudicated subset (which sets the quality); neither is meaningful without the other.
    """
    first_ans, then_ans = answers[first], answers[then]
    ids = [k for k in real_ids if k in first_ans and k in then_ans]
    escalated = {k for k in ids if low <= first_ans[k] <= high}
    final = {k: (then_ans[k] if k in escalated else first_ans[k]) for k in ids}
    labeled = [k for k in labels if k in final and labels[k] is not None]
    correct = sum((final[k] >= threshold) == labels[k] for k in labeled)
    return {
        "policy": f"{first}, escalate to {then} when p in [{low}, {high}]",
        "real_items": len(ids),
        "escalated": len(escalated),
        "escalated_fraction": round(len(escalated) / len(ids), 4) if ids else None,
        "labeled_items": len(labeled),
        "correct_on_labeled": correct,
        "accuracy_on_labeled": round(correct / len(labeled), 4) if labeled else None,
    }


def derive_context_metrics(result: Mapping[str, Any], gold: Mapping[str, Any], manifest=None):
    """Metrics that depend on adjudicated labels; computed at report time so labels can grow."""
    labels = (gold.get("context_ladder") or {}).get("adjudicated") or {}
    answers = result.get("answers") or {}
    if not labels or not answers:
        return {}
    derived: dict[str, Any] = {"accuracy_on_adjudicated": tier_accuracy_on_labels(answers, labels)}
    real_ids = sorted(answers.get("T0", {}))
    controls = set((gold.get("context_ladder") or {}).get("controls") or [])
    real_ids = [k for k in real_ids if k not in controls]
    if {"T1", "T2"} <= set(answers):
        derived["escalation_T1_to_T2"] = [
            simulate_escalation(
                answers, labels, real_ids, first="T1", then="T2", low=low, high=high
            )
            for low, high in ((0.3, 0.7), (0.2, 0.8))
        ]
    if {"T0", "T2"} <= set(answers):
        derived["escalation_T0_to_T2"] = [
            simulate_escalation(
                answers, labels, real_ids, first="T0", then="T2", low=low, high=high
            )
            for low, high in ((0.2, 0.8),)
        ]
    return derived


# --------------------------------------------------------------------------- report and CLI


def summarize(result: Mapping[str, Any], gold: Mapping[str, Any] | None = None) -> str:
    lines = [
        f"== {result['experiment']} / {result['judge']}  ({result['generated_at'][:10]}, "
        f"git {result.get('git_sha')}, set v{result['set_version']})"
    ]
    if result.get("inconclusive"):
        lines.append(
            f"   INCONCLUSIVE: {result['unanswered_fraction']:.0%} unanswered; {result['errors']}"
        )
    elif result.get("errors"):
        lines.append(f"   errors: {result['errors']}")
    for key, value in (result.get("metrics") or {}).items():
        lines.append(f"   {key}: {json.dumps(value)}")
    for tier, entry in (result.get("plan") or {}).items():
        lines.append(f"   plan {tier}: {json.dumps(entry)}")
    if gold and result.get("experiment") == "context-ladder":
        for key, value in derive_context_metrics(result, gold).items():
            lines.append(f"   {key}: {json.dumps(value)}")
    return "\n".join(lines)


def result_path(experiment: str, judge: str, out: str | None) -> Path:
    if out:
        return Path(out)
    suffix = "" if PROMPT_VERSION == "1" else f"-p{PROMPT_VERSION}"
    return RESULTS_DIR / f"{datetime.now(UTC).date().isoformat()}-{experiment}-{judge}{suffix}.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run one experiment against a judge")
    run.add_argument(
        "experiment", choices=["bundling", "question-types", "context-ladder", "adjudicator"]
    )
    run.add_argument("--judge", choices=["jev", "qwen"], required=True)
    run.add_argument("--dry-run", action="store_true", help="print the plan, make no calls")
    run.add_argument(
        "--out", help="result path (default: evals/judge/results/<date>-<experiment>-<judge>.json)"
    )
    frz = sub.add_parser("freeze", help="rebuild the frozen input set from a pulled state dir")
    frz.add_argument("--state-dir", type=Path, required=True)
    frz.add_argument("--force", action="store_true")
    rep = sub.add_parser("report", help="summarize result files")
    rep.add_argument("files", nargs="+", type=Path)
    cmp_ = sub.add_parser("compare", help="cross-judge winner agreement on question-types")
    cmp_.add_argument("jev", type=Path)
    cmp_.add_argument("qwen", type=Path)
    args = parser.parse_args(argv)

    if args.command == "freeze":
        print(json.dumps(freeze(args.state_dir, force=args.force), indent=1))
        return 0
    if args.command == "report":
        gold = load_json(GOLD_PATH) if GOLD_PATH.exists() else None
        for path in args.files:
            print(summarize(load_json(path), gold))
        return 0
    if args.command == "compare":
        print(json.dumps(compare_winners(load_json(args.jev), load_json(args.qwen)), indent=1))
        return 0

    manifest, gold = load_inputs()
    experiment, judge = args.experiment, args.judge
    needs = {"bundling": "jev", "context-ladder": "jev", "adjudicator": "qwen"}
    if experiment in needs and needs[experiment] != judge:
        raise SystemExit(f"{experiment} runs on {needs[experiment]}, not {judge}")
    jev = qwen = None
    if not args.dry_run:
        if judge == "jev":
            jev = JevClient(os.environ.get("BEATAPI_API_KEY", ""))
        else:
            qwen = GroqClient(os.environ.get("GROQ_API_KEY", ""))
    if experiment == "bundling":
        result = run_bundling(manifest, gold, jev, dry_run=args.dry_run)
    elif experiment == "question-types":
        result = run_question_types(manifest, gold, judge, jev, qwen, dry_run=args.dry_run)
    elif experiment == "context-ladder":
        result = run_context_ladder(manifest, gold, jev, dry_run=args.dry_run)
    else:
        result = run_adjudicator(manifest, gold, qwen, dry_run=args.dry_run)
    if args.dry_run:
        print(json.dumps(result, indent=1))
        return 0
    path = result_path(experiment, judge, args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    print(summarize(result))
    print(f"wrote {path}")
    return 2 if result.get("inconclusive") else 0


if __name__ == "__main__":
    raise SystemExit(main())
