"""Replay recorded P3 votes into a local, report-only decision artifact."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from citypods.audit_remedy import (
    P3_INITIAL_ROUTES,
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
    finding_id: StrictStr = Field(min_length=1)
    evidence_hash: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    policy_hash: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_summary: StrictStr = Field(max_length=20_000)
    feeds: list[FeedLabel]
    initial_votes: list[P3Vote]
    initial_validators: dict[StrictStr, StrictBool]
    adjudication: P3Adjudication | None = None
    adjudication_validator_passed: StrictBool = False

    def validate_contract(self) -> P3ReportInput:
        if self.schema_version != 1 or isinstance(self.schema_version, bool):
            raise ValueError("schema_version must be integer 1")
        if len({feed.slug for feed in self.feeds}) != len(self.feeds):
            raise ValueError("feed slugs must be unique")
        if any(not SLUG_RE.fullmatch(feed.slug) or not feed.title.strip() for feed in self.feeds):
            raise ValueError("feed labels require valid slugs and nonblank titles")
        if {vote.route_id for vote in self.initial_votes} != P3_INITIAL_ROUTES:
            raise ValueError("input must contain exactly the configured Sol and GLM Flash votes")
        if set(self.initial_validators) != P3_INITIAL_ROUTES:
            raise ValueError("validator results must bind exactly the two initial routes")
        if any(vote.decision.finding_id != self.finding_id for vote in self.initial_votes):
            raise ValueError("initial vote finding IDs must match the report input")
        if self.adjudication and self.adjudication.deepseek:
            if self.adjudication.deepseek.decision.finding_id != self.finding_id:
                raise ValueError("DeepSeek finding ID must match the report input")
        return self


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def build_p3_report(
    record: P3ReportInput,
    *,
    admitted_routes: set[str] | frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Return one replay report; route admission is trusted caller context, never file input."""
    record.validate_contract()
    decision = build_p3_review_decision(
        finding_id=record.finding_id,
        evidence_hash=record.evidence_hash,
        policy_hash=record.policy_hash,
        initial_votes=record.initial_votes,
        initial_validators=record.initial_validators,
        adjudication=record.adjudication,
        adjudication_validator_passed=record.adjudication_validator_passed,
        admitted_routes=admitted_routes,
    )
    report: dict[str, Any] = {
        "schema_version": 1,
        "input_hash": hashlib.sha256(_canonical_json(record.model_dump(mode="json"))).hexdigest(),
        "decision": decision.model_dump(mode="json"),
        "evidence_summary": record.evidence_summary,
        "feeds": [feed.model_dump(mode="json") for feed in record.feeds],
    }
    if decision.disposition == "pr_candidate":
        report["proposal_candidate"] = _selected_proposal(decision)
    elif decision.disposition == "issue_candidate":
        report["manual_issue_candidate"] = format_p3_manual_issue(
            decision,
            candidate_feeds=[(feed.slug, feed.title) for feed in record.feeds],
            evidence_markdown=record.evidence_summary,
        )
    return report


def _selected_proposal(decision: P3ReviewDecision) -> dict[str, Any]:
    if decision.reason == "initial routes agree":
        vote = next(
            vote for vote in decision.initial_votes if vote.route_id == "beatapi_gpt_6_1_sol_free"
        )
    else:
        adjudication = decision.adjudication
        if adjudication is None or adjudication.deepseek is None:
            raise ValueError("proposal candidate lacks a recorded selected vote")
        selected_route = (
            "beatapi_gpt_6_1_sol_free"
            if adjudication.choice == "A"
            else "orcarouter_zai_glm_5_3_flash_free"
        )
        vote = next(vote for vote in decision.initial_votes if vote.route_id == selected_route)
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
    output = path.resolve()
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
