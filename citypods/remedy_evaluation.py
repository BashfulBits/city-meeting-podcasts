"""Frozen remedy comparisons; truth and production admission are separate contracts."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from citypods.compute.llm_policy import route_reasoning_controls, route_request_params

PROMPT = (
    "Assess the supplied claim using only frozen evidence and approved policy. Official metadata "
    "is immutable. Shared city/topic tokens do not establish identity. Abstain when evidence is "
    "missing. Select an owner independently and cite only supplied evidence-reference IDs."
)


def canonical_hash(value: Any) -> str:
    """SHA-256 of canonical JSON, shared by inputs, controls and reports."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class EvidenceRef(StrictModel):
    id: str
    url: str
    recording_id: str | None = None
    agenda_id: str | None = None
    retrieved_at: str
    source_span: str = Field(max_length=4000)
    content_hash: str

    @model_validator(mode="after")
    def grounded(self):
        if len(self.retrieved_at) == 10:
            date.fromisoformat(self.retrieved_at)
            instant = None
        else:
            instant = datetime.fromisoformat(self.retrieved_at.replace("Z", "+00:00"))
        if instant is not None and instant.utcoffset() != UTC.utcoffset(instant):
            raise ValueError("evidence retrieval time must be UTC")
        if self.content_hash != canonical_hash(self.source_span):
            raise ValueError("evidence span content hash mismatch")
        return self


class Recording(StrictModel):
    uid: str | None = None
    provider_guid: str
    body: str
    title: str
    date: str
    canonical_url: str


class RemedyCase(StrictModel):
    id: str
    city: str
    source_key: str
    decision_type: str
    input_kind: Literal["real", "synthetic", "adapted"]
    policy_version: str
    policy_status: Literal["approved", "provisional"]
    body_label: str
    claim: str
    evidence_refs: list[EvidenceRef]
    recordings: list[Recording]
    existing_feeds: list[dict[str, Any]]
    family_policy: dict[str, Any]
    completeness: dict[str, Any]
    split_group: str

    @model_validator(mode="after")
    def unique_references(self):
        ids = [ref.id for ref in self.evidence_refs]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate evidence reference ID")
        return self


class Manifest(StrictModel):
    version: Literal[2]
    frozen_at: str
    split: Literal["regression", "holdout"]
    provenance: str
    cases: list[RemedyCase]
    superseded_hash: str | None = None

    @model_validator(mode="after")
    def frozen_in_utc(self):
        instant = datetime.fromisoformat(self.frozen_at.replace("Z", "+00:00"))
        if instant.utcoffset() != UTC.utcoffset(instant):
            raise ValueError("manifest frozen_at must be UTC")
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate case IDs")
        return self


class GoldCase(StrictModel):
    supported: StrictBool | None
    rationale: str
    evidence_refs: list[str]
    adjudicator: str
    provenance: Literal["seed", "verified"]
    policy_approval_ref: str | None
    critical_error_class: str | None
    revision: int = Field(ge=1)
    superseded_revision: int | None
    correction_reason: str | None = None

    @model_validator(mode="after")
    def corrected(self):
        if self.revision > 1 and (
            self.superseded_revision != self.revision - 1 or not self.correction_reason
        ):
            raise ValueError("gold correction requires previous revision and reason")
        return self


class Gold(StrictModel):
    version: Literal[2]
    cases: dict[str, GoldCase]


class RemedyEvalAnswer(StrictModel):
    case_id: str
    supported: StrictBool | None
    proposed_owner: str | None
    proposed_action: str | None
    evidence_ref_ids: list[str]
    missing_evidence: list[str]
    rationale: str = Field(max_length=2000)


class EvaluationSet(StrictModel):
    manifest: Manifest
    gold: Gold


class EvaluationReport(StrictModel):
    counts: dict[str, int]
    accepted_precision: float | None
    supported_claim_recall: float | None
    breakdown: dict[str, dict[str, int]]
    admission_eligible_cases: int
    owner_selection: dict[str, int]
    manifest_hash: str
    gold_hash: str


class AA(StrictModel):
    version: str
    variant: str
    score: float | int = Field(allow_inf_nan=False)
    source: str
    date: str


class AdmissionEntry(StrictModel):
    status: Literal["candidate", "admitted"]
    role: Literal["proposer", "reviewer", "adjudicator"]
    physical_route_ids: list[str]
    upstream_model: str
    model_family: str
    reasoning_level: Literal["minimal", "low", "medium", "high", "max"]
    request_params_hash: str
    aa: AA
    manifest_hash: str | None = None
    gold_hash: str | None = None
    prompt_hash: str | None = None
    schema_hash: str | None = None
    catalog_hash: str | None = None
    reviewed_result_paths: list[str] = Field(default_factory=list)
    reviewed_admission_ref: str | None = None


class AdmissionCheck(StrictModel):
    usable: bool
    reasons: list[str]


class Limits(StrictModel):
    decisions_per_pr: int = Field(ge=1, le=5)
    prs_per_rolling_week: int = Field(ge=1, le=8)
    open_prs: int = Field(ge=1, le=8)
    active_directional_issues: int = Field(ge=1, le=12)
    evidence_max_age_hours: int = Field(ge=1, le=24)


class RemedyConfig(StrictModel):
    version: Literal[1]
    mode: Literal["shadow", "manual", "qualified_alias"]
    admissions: list[AdmissionEntry]
    qualified_alias_types: list[dict[str, Any]]
    limits: Limits
    onboarding_exceptions: list[dict[str, Any]]


def load_cases(manifest_path, gold_path) -> EvaluationSet:
    manifest = Manifest.model_validate(json.loads(Path(manifest_path).read_text()))
    gold = Gold.model_validate(json.loads(Path(gold_path).read_text()))
    _validate_against_regression(manifest)
    ids = [case.id for case in manifest.cases]
    if len(ids) != len(set(ids)) or set(ids) != set(gold.cases):
        raise ValueError("duplicate or disjoint manifest/gold case IDs")
    for case in manifest.cases:
        if not set(gold.cases[case.id].evidence_refs) <= {ref.id for ref in case.evidence_refs}:
            raise ValueError("gold cites unknown evidence IDs")
    return EvaluationSet(manifest=manifest, gold=gold)


def validate_holdout(holdout: Manifest, regression: Manifest) -> None:
    """Source/family groups and recording identities cannot leak between splits."""
    if holdout.split != "holdout":
        raise ValueError("expected holdout split")
    groups = {case.split_group for case in regression.cases}
    families = {
        (case.source_key, case.family_policy.get("aggregate_family", case.decision_type))
        for case in regression.cases
    }
    recordings = {
        (case.source_key, recording.uid or recording.provider_guid)
        for case in regression.cases
        for recording in case.recordings
    }
    for case in holdout.cases:
        if (
            case.split_group in groups
            or (case.source_key, case.family_policy.get("aggregate_family", case.decision_type))
            in families
            or any(
                (case.source_key, recording.uid or recording.provider_guid) in recordings
                for recording in case.recordings
            )
        ):
            raise ValueError("holdout overlaps regression source/family or recordings")


def _validate_against_regression(manifest: Manifest) -> None:
    if manifest.split != "holdout":
        return
    baseline_path = Path(__file__).resolve().parents[1] / "evals/remedy/manifest.json"
    baseline = Manifest.model_validate(json.loads(baseline_path.read_text()))
    if baseline.split != "regression":
        raise ValueError("canonical tuned regression baseline is unavailable")
    validate_holdout(manifest, baseline)


def freeze_cases(evidence, policies, *, split, provenance) -> dict:
    cases = []
    for item in evidence["cases"]:
        case = dict(item)
        case["family_policy"] = policies.get(case["city"], case.get("family_policy", {}))
        cases.append(RemedyCase.model_validate(case))
    manifest = Manifest(
        version=2,
        frozen_at=datetime.now(UTC).isoformat(),
        split=split,
        provenance=provenance,
        cases=cases,
    )
    ids = [case.id for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate case IDs")
    _validate_against_regression(manifest)
    return manifest.model_dump()


def case_messages(case: RemedyCase) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": PROMPT},
        {"role": "user", "content": json.dumps(case.model_dump(), sort_keys=True)},
    ]


def validate_answer(answer: Any, case: RemedyCase) -> RemedyEvalAnswer:
    parsed = RemedyEvalAnswer.model_validate(answer)
    if parsed.case_id != case.id:
        raise ValueError("response case ID mismatch")
    if not set(parsed.evidence_ref_ids) <= {ref.id for ref in case.evidence_refs}:
        raise ValueError("response cites unknown evidence IDs")
    return parsed


def compare_results(results, gold, *, manifest) -> EvaluationReport:
    manifest = Manifest.model_validate(manifest) if isinstance(manifest, dict) else manifest
    gold = Gold.model_validate(gold) if isinstance(gold, dict) else gold
    _validate_against_regression(manifest)
    cases = {case.id: case for case in manifest.cases}
    counts = dict.fromkeys(
        (
            "known_truth",
            "unknown_truth",
            "correct",
            "incorrect",
            "abstained",
            "failed",
            "unattempted",
            "accepted",
            "accepted_correct",
            "critical_errors",
        ),
        0,
    )
    owner_scores = {"evaluated": 0, "correct": 0}
    breakdown = {}
    positives = found = 0
    eligible = 0
    seen = set()
    for result in results:
        case_id = result["case_id"]
        if case_id not in cases or case_id in seen:
            raise ValueError("unknown or duplicate result case ID")
        seen.add(case_id)
        case, truth = cases[case_id], gold.cases[case_id]
        if result.get("status") == "unattempted":
            counts["unattempted"] += 1
            continue
        if result.get("status") != "completed":
            counts["failed"] += 1
            continue
        try:
            answer = validate_answer(result.get("answer"), case)
        except ValueError:
            counts["failed"] += 1
            continue
        if truth.supported is None:
            counts["unknown_truth"] += 1
            continue
        counts["known_truth"] += 1
        eligible += int(
            manifest.split == "holdout"
            and truth.provenance == "verified"
            and case.input_kind == "real"
            and case.policy_status == "approved"
            and bool(case.evidence_refs)
            and bool(truth.policy_approval_ref)
        )
        positives += int(truth.supported)
        found += int(truth.supported and answer.supported is True)
        bucket = breakdown.setdefault(
            f"{case.decision_type}|{case.source_key}|{case.split_group}|{truth.provenance}",
            {"known_truth": 0, "correct": 0, "incorrect": 0, "abstained": 0},
        )
        bucket["known_truth"] += 1
        if answer.supported is None:
            bucket["abstained"] += 1
            counts["abstained"] += 1
            continue
        correct = answer.supported == truth.supported
        counts["correct" if correct else "incorrect"] += 1
        bucket["correct" if correct else "incorrect"] += 1
        if answer.supported:
            counts["accepted"] += 1
            counts["accepted_correct"] += int(correct)
        counts["critical_errors"] += int(not correct and bool(truth.critical_error_class))
    counts["unattempted"] += len(cases) - len(seen)
    return EvaluationReport(
        counts=counts,
        accepted_precision=(
            counts["accepted_correct"] / counts["accepted"] if counts["accepted"] else None
        ),
        supported_claim_recall=found / positives if positives else None,
        breakdown=breakdown,
        admission_eligible_cases=eligible,
        owner_selection=owner_scores,
        manifest_hash=canonical_hash(manifest.model_dump()),
        gold_hash=canonical_hash(gold.model_dump()),
    )


def validate_admission(entry, catalog, evaluation=None, *, for_evaluation=False) -> AdmissionCheck:
    entry = AdmissionEntry.model_validate(entry) if isinstance(entry, dict) else entry
    reasons = []
    if not entry.physical_route_ids:
        reasons.append("empty physical route allowlist")
    if entry.aa.version != "4.3.2" or entry.aa.score < 39:
        reasons.append("AA evidence does not meet comparable Gemini 3.7 High baseline")
    if entry.reasoning_level not in entry.aa.variant.lower():
        reasons.append("AA reasoning variant mismatch")
    try:
        observed = datetime.fromisoformat(entry.aa.date.replace("Z", "+00:00"))
        if observed.date() > datetime.now(UTC).date() or not entry.aa.source.startswith("https://"):
            reasons.append("invalid dated AA source")
    except ValueError:
        reasons.append("invalid AA evidence date")
    for route_id in entry.physical_route_ids:
        route = catalog.get(route_id)
        if route is None:
            reasons.append(f"unknown physical route: {route_id}")
            continue
        if not route.free or "direct" not in route.transports or route.experimental:
            reasons.append(f"not an ordinary free direct route: {route_id}")
        if not route.upstream_model or route.upstream_model != entry.upstream_model:
            reasons.append(f"physical upstream mismatch: {route_id}")
        controls = route_reasoning_controls(route, entry.reasoning_level)
        if not controls:
            reasons.append(f"unverified effort controls: {route_id}")
        params = route_request_params(route)
        params.update(controls)
        if canonical_hash(params) != entry.request_params_hash:
            reasons.append(f"effective request-parameter mismatch: {route_id}")
    if not for_evaluation and entry.status != "admitted":
        reasons.append("candidate is evaluation-only")
    if evaluation is not None:
        # Execution binds frozen inputs, not the truth/results of a prior qualification.
        # An admitted route can be re-evaluated without passing gold into run context.
        fields = ("manifest_hash", "prompt_hash", "schema_hash", "catalog_hash")
        if not for_evaluation:
            fields += ("gold_hash",)
        for name in fields:
            supplied = getattr(entry, name)
            if supplied is not None and evaluation.get(name) != supplied:
                reasons.append(f"evaluation version mismatch: {name}")
    if not for_evaluation:
        required = ("manifest_hash", "gold_hash", "prompt_hash", "schema_hash", "catalog_hash")
        if any(not getattr(entry, name) for name in required):
            reasons.append("missing version hashes")
        if not entry.reviewed_result_paths or not entry.reviewed_admission_ref:
            reasons.append("missing reviewed project results/admission reference")
        if evaluation is None:
            reasons.append("missing project evaluation")
        else:
            for name in required:
                if evaluation.get(name) != getattr(entry, name):
                    reasons.append(f"evaluation version mismatch: {name}")
            if (
                not evaluation.get("qualified")
                or evaluation.get("admission_eligible_cases", 0) <= 0
            ):
                reasons.append("project evaluation not qualified on verified holdout truth")
            if evaluation.get("counts", {}).get("critical_errors", 0):
                reasons.append("critical project evaluation errors")
    return AdmissionCheck(usable=not reasons, reasons=reasons)
