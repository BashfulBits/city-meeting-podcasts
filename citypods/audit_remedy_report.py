"""Replay recorded P3 votes into a local, report-only decision artifact."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from citypods.audit_remedy import (
    P3_ROLE_PLANS,
    SLUG_RE,
    P3Adjudication,
    P3ReviewDecision,
    P3Vote,
    build_p3_review_decision,
    format_p3_manual_issue,
)


class FeedLabel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    slug: StrictStr = Field(min_length=1)
    title: StrictStr = Field(min_length=1)


class P3ReportInput(BaseModel):
    """Validated replay input; it cannot assert route admission."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: StrictInt
    role_plan: Literal["legacy-sol-v1", "flash-deepseek-v2", "flash-deepseek-human-v3"] = (
        "legacy-sol-v1"
    )
    finding_id: StrictStr = Field(min_length=1)
    evidence_hash: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    policy_hash: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_summary: StrictStr = Field(max_length=20_000)
    feeds: list[FeedLabel]
    initial_votes: list[P3Vote]
    initial_validators: dict[StrictStr, StrictBool]
    initial_failures: dict[StrictStr, StrictStr] = Field(default_factory=dict)
    adjudication: P3Adjudication | None = None
    adjudication_validator_passed: StrictBool = False

    def validate_contract(self) -> P3ReportInput:
        plan = P3_ROLE_PLANS[self.role_plan]
        initial_models = frozenset(plan.models[r] for r in plan.initial)

        def model_of(vote):
            return plan.model_for_route(vote.route_id)

        if self.schema_version != 1 or isinstance(self.schema_version, bool):
            raise ValueError("schema_version must be integer 1")
        if len({feed.slug for feed in self.feeds}) != len(self.feeds):
            raise ValueError("feed slugs must be unique")
        if any(not SLUG_RE.fullmatch(feed.slug) or not feed.title.strip() for feed in self.feeds):
            raise ValueError("feed labels require valid slugs and nonblank titles")
        if plan.chat is None and self.adjudication is not None:
            raise ValueError("human-review plan cannot carry automated adjudication")
        if len({model_of(vote) for vote in self.initial_votes}) != len(self.initial_votes):
            raise ValueError(
                "input must contain exactly the configured initial votes; duplicate vote"
            )
        failed = set(self.initial_failures)
        votes = {model_of(vote) for vote in self.initial_votes}
        if failed and (
            plan.chat is not None
            or not (failed | votes) <= initial_models
            or failed & votes
            or any(
                not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", k)
                for k in self.initial_failures.values()
            )
        ):
            raise ValueError("invalid initial failure evidence")
        if not failed and (
            len(self.initial_votes) != 2
            or {model_of(vote) for vote in self.initial_votes} != initial_models
        ):
            raise ValueError("input must contain exactly the configured initial-model votes")
        if set(self.initial_validators) != (votes if failed else initial_models):
            raise ValueError("validator results must bind exactly the two initial models")
        if any(vote.decision.finding_id != self.finding_id for vote in self.initial_votes):
            raise ValueError("initial vote finding IDs must match the report input")
        if any(
            vote.evidence_hash != self.evidence_hash or vote.policy_hash != self.policy_hash
            for vote in self.initial_votes
        ):
            raise ValueError("initial vote evidence or policy binding mismatch")
        if self.adjudication and (
            self.adjudication.evidence_hash != self.evidence_hash
            or self.adjudication.policy_hash != self.policy_hash
        ):
            raise ValueError("adjudication evidence or policy binding mismatch")
        chat = (self.adjudication.chat or self.adjudication.deepseek) if self.adjudication else None
        if chat:
            if chat.decision.finding_id != self.finding_id:
                raise ValueError("DeepSeek finding ID must match the report input")
            if chat.evidence_hash != self.evidence_hash or chat.policy_hash != self.policy_hash:
                raise ValueError("DeepSeek vote evidence or policy binding mismatch")
        return self


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def build_p3_report(
    record: P3ReportInput,
    *,
    admitted_models: set[str] | frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Return one replay report; model admission is trusted caller context, never file input."""
    record.validate_contract()
    if record.initial_failures:
        plan = P3_ROLE_PLANS[record.role_plan]
        admitted = set(plan.models.values()) <= set(admitted_models)
        decision = P3ReviewDecision(
            finding_id=record.finding_id,
            evidence_hash=record.evidence_hash,
            policy_hash=record.policy_hash,
            initial_votes=record.initial_votes,
            validator_passed=False,
            disposition="issue_candidate" if admitted else "policy_hold",
            reason=(
                "initial route failed; human review required: "
                + ", ".join(
                    f"{model} ({kind})" for model, kind in sorted(record.initial_failures.items())
                )
            )
            if admitted
            else "initial routes are not admitted for this run",
        )
    else:
        decision = build_p3_review_decision(
            finding_id=record.finding_id,
            evidence_hash=record.evidence_hash,
            policy_hash=record.policy_hash,
            initial_votes=record.initial_votes,
            initial_validators=record.initial_validators,
            adjudication=record.adjudication,
            adjudication_validator_passed=record.adjudication_validator_passed,
            admitted_models=admitted_models,
            role_plan=record.role_plan,
        )
    report: dict[str, Any] = {
        "schema_version": 1,
        "input_hash": hashlib.sha256(
            _canonical_json(
                record.model_dump(
                    mode="json",
                    exclude=(
                        ({"initial_failures"} if not record.initial_failures else set())
                        | ({"role_plan"} if record.role_plan == "legacy-sol-v1" else set())
                    ),
                )
            )
        ).hexdigest(),
        "decision": decision.model_dump(mode="json"),
        "evidence_summary": record.evidence_summary,
        "feeds": [feed.model_dump(mode="json") for feed in record.feeds],
    }
    if record.role_plan != "legacy-sol-v1":
        report["role_plan"] = record.role_plan
    if record.initial_failures:
        report["initial_failures"] = dict(record.initial_failures)
    if decision.disposition == "pr_candidate":
        report["proposal_candidate"] = _selected_proposal(decision, record.role_plan)
    elif decision.disposition == "issue_candidate":
        report["manual_issue_candidate"] = format_p3_manual_issue(
            decision,
            candidate_feeds=[(feed.slug, feed.title) for feed in record.feeds],
            evidence_markdown=record.evidence_summary,
        )
    return report


def _selected_proposal(
    decision: P3ReviewDecision, role_plan: str = "legacy-sol-v1"
) -> dict[str, Any]:
    plan = P3_ROLE_PLANS[role_plan]
    if decision.reason == "initial routes agree":
        vote = next(
            vote
            for vote in decision.initial_votes
            if plan.model_for_route(vote.route_id) == plan.models[plan.initial[0]]
        )
    else:
        adjudication = decision.adjudication
        if adjudication is None or (adjudication.deepseek or adjudication.chat) is None:
            raise ValueError("proposal candidate lacks a recorded selected vote")
        selected_model = (
            plan.models[plan.initial[0]]
            if adjudication.choice == "A"
            else plan.models[plan.initial[1]]
        )
        vote = next(
            vote
            for vote in decision.initial_votes
            if plan.model_for_route(vote.route_id) == selected_model
        )
    return {
        "route_id": vote.route_id,
        "upstream_model": vote.upstream_model,
        "reasoning_level": vote.reasoning_level,
        "response_hash": vote.response_hash,
        "assignment": vote.decision.model_dump(mode="json"),
    }


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_report_input(path: Path) -> P3ReportInput:
    payload = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)
    return P3ReportInput.model_validate(payload).validate_contract()


def write_report_exclusive(path: Path, report: dict[str, Any], *, input_path: Path) -> None:
    """Atomically create a report without replacing input or prior output."""
    output = path.parent.resolve() / path.name
    source = input_path.resolve()
    if output == source:
        raise ValueError("report output must differ from the input path")
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    fd, temp_name = tempfile.mkstemp(prefix=".p3-report-", dir=output.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temp_path, output)
    finally:
        temp_path.unlink(missing_ok=True)
