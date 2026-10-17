"""Packets to dispatch jobs, and replies back to judgments (review/53 PR3).

``JevBackend`` compiles questions to BeatAPI's systemone types (validate/gate -> ``noul``, grade ->
``score``, choose -> ``choice``). ``ChatJudgeBackend`` asks sibling chat models through one
structured contract, ``judge-answers-v1``. Both return rows for :mod:`citypods.judging.ledger`; an
unanswered or malformed item yields no row.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from citypods.compute.base import InferenceJob
from citypods.compute.llm_policy import LLMRequestPolicy
from citypods.judging.tasks import Evidence, QuestionSpec, Subject

JUDGE_CONTRACT = "judge-answers-v1"
REASON_MAX = 300
CHOICE_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


@dataclass(frozen=True)
class Item:
    """One question about one subject (or, for ``choose``, about one group) at one tier."""

    question: QuestionSpec
    tier: str
    evidence: Evidence
    subjects: tuple[Subject, ...]  # one subject, or the group's subjects for choose
    sample: str = "routine"
    # choose only: option letter -> subject_id, in the order asked
    options: tuple[tuple[str, str], ...] = ()
    order: str = ""  # choose only: "" or "_r" (reversed order)
    # Set when an answer is collected from a pending marker: the qid the question was asked with.
    asked_qid: str = ""

    @property
    def qid(self) -> str:
        if self.asked_qid:
            return self.asked_qid
        head = (
            self.subjects[0].group
            if self.question.kind == "choose"
            else self.subjects[0].subject_id
        )
        return f"{head}:{self.question.id}{self.order}:{self.tier}"

    @property
    def question_id(self) -> str:
        return f"{self.question.id}{self.order}"

    def text(self) -> str:
        # For choose, the evidence already lists each option as "<letter>: <quote>"; subject ids
        # never reach a judge.
        return f"{self.question.instruction}\n\n{self.evidence.text}"


@dataclass(frozen=True)
class Packet:
    purpose: str
    role: str  # anchor | sibling | adjudicator
    judge_model: str
    items: tuple[Item, ...] = field(default=())

    @property
    def recipe_hash(self) -> str:
        identity = sorted(
            (item.qid, item.evidence.digest, item.question.prompt_version) for item in self.items
        )
        digest = hashlib.sha256(
            json.dumps([self.role, self.judge_model, identity]).encode()
        ).hexdigest()
        return f"judge-{self.role}-{digest[:32]}"


def ensure_contract():
    from citypods.compute.structured import register_response_model, response_model

    try:
        return response_model(JUDGE_CONTRACT)
    except ValueError:
        pass
    from pydantic import BaseModel, ConfigDict, Field

    class Answer(BaseModel):
        model_config = ConfigDict(extra="forbid")
        id: str = Field(min_length=1, max_length=200)
        verdict: bool | None = None  # validate: supported; gate: flagged
        level: int | None = Field(default=None, ge=0, le=10)  # grade: index into levels
        choice: str | None = Field(default=None, max_length=4)  # choose: option letter
        reason: str = Field(default="", max_length=REASON_MAX)

    class Response(BaseModel):
        model_config = ConfigDict(extra="forbid")
        answers: list[Answer] = Field(max_length=100)

    return register_response_model(JUDGE_CONTRACT, Response)


def _policy(purpose: str, model: str) -> LLMRequestPolicy:
    return LLMRequestPolicy(
        allowed_models=(model,),
        allow_paid=False,
        purpose=purpose,
        queue_only=True,
        timeout_class="long",
    )


def _row(packet: Packet, item: Item, subject: Subject, **values: Any) -> dict[str, Any]:
    return {
        "task": subject.task,
        "subject_id": subject.subject_id,
        "question_id": item.question_id,
        "kind": item.question.kind,
        "judge_model": packet.judge_model,
        "judge_role": packet.role,
        "prompt_version": item.question.prompt_version,
        "context_tier": item.tier,
        "evidence_digest": item.evidence.digest,
        "packet_id": packet.recipe_hash,
        "sample": item.sample,
        "probabilities": None,
        "confidence": None,
        "reason": None,
        "quote_valid": None,
        **values,
    }


# The JEV route the Worker serves (config/provider_limits.yml) and its measured input ceiling, in
# the provider's token units. A test pins both to the catalog so they cannot drift apart.
JEV_ROUTE_ID = "beatapi_jev_1_13_free"
JEV_HARD_INPUT_CEILING = 58_000
# The Worker multiplies a job's chars/4 estimate by the route's input ratio before comparing it
# with the ceiling (workers/llm-dispatch-v2/src/calibration.js). The ratio is learned, so a packet
# is sized with the Worker's own figure plus a margin for its p95 tail; when the figure cannot be
# read the packer assumes JEV counts 25% more tokens than chars/4.
JEV_UNMEASURED_RATIO = 1.25
JEV_RATIO_MARGIN = 1.05


def _jev_question(item: Item) -> dict[str, Any]:
    kind = item.question.kind
    if kind in ("validate", "gate"):
        return {"type": "noul", "instructions": item.text(), "criteria": {}}
    if kind == "grade":
        return {
            "type": "score",
            "instructions": item.text(),
            "criteria": list(item.question.levels),
        }
    return {
        "type": "choice",
        "instructions": item.text(),
        "criteria": {letter: f"Quote {letter}" for letter, _ in item.options},
    }


class JevBackend:
    role = "anchor"
    max_question_tokens = 28_000
    max_items = 60
    output_reservation = 1_024  # within the route's 4,096 output reservation (Slice 3a fit)

    def __init__(self, input_ratio: float | None = None) -> None:
        """Size packets so the Worker admits them: the whole packet, as the producer will
        estimate it, times the route's input ratio must stay within the hard input ceiling.

        The first PR4 runs measured only each item's text, so every packet near the limit was
        over the ceiling once the JSON the producer actually sends was counted (about 45 of 110
        anchor packets a run failed as ``job_unroutable`` or ``input_over_route_ceiling``)."""
        ratio = max(1.0, float(input_ratio)) if input_ratio else JEV_UNMEASURED_RATIO
        self.input_ratio = ratio
        self.max_total_tokens = int(JEV_HARD_INPUT_CEILING / (ratio * JEV_RATIO_MARGIN))
        self.max_question_tokens = min(JevBackend.max_question_tokens, self.max_total_tokens)

    def item_tokens(self, item: Item) -> int:
        """The item's share of the request the producer estimates: its question as serialized."""
        return estimate_tokens(json.dumps({item.qid: _jev_question(item)}, sort_keys=True))

    def build(self, packet: Packet) -> InferenceJob:
        questions: dict[str, Any] = {item.qid: _jev_question(item) for item in packet.items}
        return InferenceJob(
            task="judge",
            inputs={
                "systemone": {"state": {}, "questions": questions},
                "llm_policy": _policy(packet.purpose, packet.judge_model),
                "max_tokens": self.output_reservation,
            },
            recipe_hash=packet.recipe_hash,
        )

    def parse(self, packet: Packet, output: Any) -> list[tuple[Subject, dict[str, Any]]]:
        answers = output.get("answers") if isinstance(output, Mapping) else None
        if not isinstance(answers, Mapping):
            return []
        rows: list[tuple[Subject, dict[str, Any]]] = []
        for item in packet.items:
            answer = answers.get(item.qid)
            if not isinstance(answer, Mapping):
                continue
            kind = item.question.kind
            try:
                if kind in ("validate", "gate"):
                    value: Any = float(answer["noul"])
                    if not 0.0 <= value <= 1.0:
                        continue
                    extra: dict[str, Any] = {}
                elif kind == "grade":
                    value = float(answer["score"])
                    extra = {
                        "probabilities": dict(answer.get("probabilities") or {}) or None,
                        "confidence": answer.get("confidence"),
                    }
                else:
                    letter = str(answer["choice"])
                    winner = dict(item.options).get(letter)
                    if winner is None:
                        continue
                    value = winner
                    extra = {
                        "probabilities": dict(answer.get("probabilities") or {}) or None,
                        "confidence": answer.get("confidence"),
                    }
            except (KeyError, TypeError, ValueError):
                continue
            for subject in item.subjects:
                rows.append((subject, _row(packet, item, subject, value=value, **extra)))
        return rows


class ChatJudgeBackend:
    role = "sibling"

    def __init__(
        self,
        *,
        max_total_tokens: int,
        max_items: int,
        output_reservation: int = 2048,
        output_cap: int | None = None,
    ):
        """``output_cap`` sends that many output tokens instead of the route's own limit. A model
        that can fall into a repetition loop (Gemma 4 26B) then stops in seconds, not at 32,768."""
        self.output_cap = output_cap
        self.max_total_tokens = max_total_tokens
        self.max_question_tokens = max_total_tokens
        self.max_items = max_items
        self.output_reservation = output_reservation

    SYSTEM = (
        "You are an independent judge. Answer every item using only the evidence given with it. "
        "Never rewrite or create a candidate. For each item return its id and exactly the field "
        "its kind asks for: validate -> verdict (true if supported); gate -> verdict (true if it "
        "must be held back); grade -> level (0 = lowest listed level); choose -> choice (the "
        "option letter). Put a short quote from the evidence in reason."
    )

    def build(self, packet: Packet) -> InferenceJob:
        ensure_contract()
        items = []
        for item in packet.items:
            entry: dict[str, Any] = {
                "id": item.qid,
                "kind": item.question.kind,
                "item": item.text(),
            }
            if item.question.kind == "grade":
                entry["levels"] = list(item.question.levels)
            items.append(entry)
        return InferenceJob(
            task="judge",
            inputs={
                "messages": [
                    {"role": "system", "content": self.SYSTEM},
                    {"role": "user", "content": json.dumps({"items": items}, ensure_ascii=False)},
                ],
                "structured_output": JUDGE_CONTRACT,
                "llm_policy": _policy(packet.purpose, packet.judge_model),
                **(
                    {"max_tokens": self.output_cap}
                    if self.output_cap
                    else {"max_tokens": self.output_reservation, "max_tokens_mode": "route_max"}
                ),
            },
            recipe_hash=packet.recipe_hash,
        )

    def parse(self, packet: Packet, output: Any) -> list[tuple[Subject, dict[str, Any]]]:
        from citypods.compute.structured import parse_structured_json

        try:
            content = output["choices"][0]["message"]["content"]
            reply = ensure_contract().model_validate(
                parse_structured_json(content, context="judge response")
            )
        except (KeyError, IndexError, TypeError, ValueError):
            return []
        by_id = {answer.id: answer for answer in reply.answers}
        rows: list[tuple[Subject, dict[str, Any]]] = []
        for item in packet.items:
            answer = by_id.get(item.qid)
            if answer is None:
                continue
            kind = item.question.kind
            if kind in ("validate", "gate"):
                if answer.verdict is None:
                    continue
                value: Any = bool(answer.verdict)
            elif kind == "grade":
                if answer.level is None or answer.level >= len(item.question.levels):
                    continue
                value = int(answer.level)
            else:
                value = dict(item.options).get(str(answer.choice or ""))
                if value is None:
                    continue
            for subject in item.subjects:
                rows.append(
                    (
                        subject,
                        _row(packet, item, subject, value=value, reason=answer.reason or None),
                    )
                )
        return rows


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def choose_options(subjects: Sequence[Subject], *, reverse: bool) -> tuple[tuple[str, str], ...]:
    ordered = sorted(s.subject_id for s in subjects)
    if reverse:
        ordered.reverse()
    return tuple(zip(CHOICE_LETTERS, ordered, strict=False))
