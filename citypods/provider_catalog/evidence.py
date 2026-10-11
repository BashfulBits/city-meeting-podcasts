"""Safe immutable evidence and deterministic identities for review/48 config proposals."""

from __future__ import annotations

import hashlib
import io
import json
import re
import subprocess
import zipfile
import zlib
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def parse_documented_token_rate_headers(headers):
    """Parse bounded Groq request/token rate headers; missing or malformed values stay null."""

    def integer(name):
        value = next((v for k, v in headers.items() if str(k).lower() == name), None)
        if (
            not isinstance(value, str)
            or len(value) > 128
            or not value.isascii()
            or not re.fullmatch(r"[0-9]+", value.strip())
        ):
            return None
        parsed = int(value.strip())
        return parsed if parsed <= 2**53 - 1 else None

    def duration(name):
        raw = next((v for k, v in headers.items() if str(k).lower() == name), None)
        if not isinstance(raw, str) or len(raw) > 128 or not raw.isascii():
            return None
        match = re.fullmatch(
            r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m)?"
            r"(?:(\d+(?:\.\d+)?)s)?",
            raw.strip(),
        )
        if not match or not any(match.groups()):
            return None
        try:
            hours, minutes, seconds = (Decimal(value or "0") for value in match.groups())
            millis = ((hours * 3600 + minutes * 60 + seconds) * 1000).to_integral_value(
                rounding=ROUND_CEILING
            )
            return int(millis) if 0 <= millis <= 86_400_000 else None
        except (InvalidOperation, OverflowError):
            return None

    retry = next((v for k, v in headers.items() if str(k).lower() == "retry-after"), None)
    retry_ms = None
    if (
        isinstance(retry, str)
        and len(retry) <= 128
        and retry.isascii()
        and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", retry.strip())
    ):
        try:
            millis = (Decimal(retry.strip()) * 1000).to_integral_value(rounding=ROUND_CEILING)
            if 0 <= millis <= 3_600_000:
                retry_ms = int(millis)
        except (InvalidOperation, OverflowError):
            pass

    return {
        "rate_limit_limit_requests": integer("x-ratelimit-limit-requests"),
        "rate_limit_remaining_requests": integer("x-ratelimit-remaining-requests"),
        "rate_limit_reset_requests_ms": duration("x-ratelimit-reset-requests"),
        "rate_limit_limit_tokens": integer("x-ratelimit-limit-tokens"),
        "rate_limit_remaining_tokens": integer("x-ratelimit-remaining-tokens"),
        "rate_limit_reset_tokens_ms": duration("x-ratelimit-reset-tokens"),
        "retry_after_ms": retry_ms,
    }


@dataclass(frozen=True)
class LimitObservation:
    metric: str
    value: int
    observed_at: str
    provider: str
    account_id: str
    route_id: str | None = None
    scope: str = "unknown"
    source: str = "header"
    run_id: str = ""

    def __post_init__(self):
        if isinstance(self.value, bool) or not isinstance(self.value, int) or self.value <= 0:
            raise ValueError("limit observations require positive integer values")
        if self.scope not in {"unknown", "route", "provider_account"}:
            raise ValueError("unknown limit scope")
        if self.metric not in {"rpm", "tpm", "rpd", "output_context_limit"}:
            raise ValueError("unsupported observed metric")


@dataclass(frozen=True)
class CatalogEvidence:
    provider: str
    observed_at: str
    complete: bool
    # ID, chat eligibility, free evidence, input context, output context, canonical identity.
    models: tuple[tuple[str, bool, bool | None, int | None, int | None, str | None], ...]


def catalog_digest(evidence: CatalogEvidence) -> str:
    """Time is provenance, not catalog content; listing order cannot change content identity."""
    return digest(
        {
            "provider": evidence.provider,
            "complete": evidence.complete,
            "models": sorted(evidence.models),
        }
    )


@dataclass(frozen=True)
class RouteEvidence:
    provider: str
    upstream_model: str
    account_id: str
    observed_at: str
    catalog_digest: str
    catalog_complete: bool
    free: bool
    input_context_limit: int | None
    output_context_limit: int | None
    model_key: str | None
    verdict: str
    structured_output_method: str | None
    structured_output_verified_on: str | None
    limits: tuple[LimitObservation, ...] = ()
    # Method, outcome, first-byte and completion latency; no untrusted response body.
    methods: tuple[tuple[str, str, float | None, float | None], ...] = ()
    contended: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> RouteEvidence:
        value = dict(value)
        value["limits"] = tuple(LimitObservation(**x) for x in value.get("limits") or ())
        value["methods"] = tuple(tuple(x) for x in value.get("methods") or ())
        return cls(**value)


def candidate_digest(evidence: RouteEvidence, lanes: tuple[str, ...]) -> str:
    return digest({"evidence": evidence.to_dict(), "lanes": sorted(lanes)})


def positive_bound(record, keys) -> int | None:
    for key in keys:
        value = record
        for part in key.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if isinstance(value, str) and value.isdigit():
            value = int(value)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return None


RATE_WORKFLOW = ".github/workflows/provider-catalog-reconcile.yml"
RATE_ARTIFACT_FILE = "provider-catalog-rate-evidence.json"
RATE_ARTIFACT_MAX_BYTES = 1_000_000


def provider_rate_digest(limits, provider):
    """Bind shared capacity to its provider settings and full physical-route inventory."""
    return digest(
        {
            "provider": (limits.get("providers") or {}).get(provider),
            "routes": sorted(
                (r for r in limits.get("routes") or [] if r.get("provider") == provider),
                key=lambda r: r["route_id"],
            ),
        }
    )


def rate_artifact(observations, limits, *, repository, run_id, head_sha, now, attempted_routes=()):
    """Encode positive payload-free observations; the producer is not its own trust verifier."""
    if now.tzinfo is None:
        raise ValueError("rate artifacts require a timezone-aware current time")
    payload = {
        "version": 1,
        "repository": repository,
        "run_id": str(run_id),
        "workflow_path": RATE_WORKFLOW,
        "branch": "main",
        "head_sha": head_sha,
        "observed_at": now.astimezone(UTC).isoformat(),
        "route_digests": {r["route_id"]: digest(r) for r in limits.get("routes") or []},
        "provider_digests": {
            p: provider_rate_digest(limits, p) for p in limits.get("providers") or {}
        },
        "observations": [asdict(o) for o in observations],
        "attempted_routes": sorted(set(attempted_routes)),
    }
    return {"payload": payload, "payload_digest": digest(payload)}


def _gh_json(path):
    return json.loads(subprocess.check_output(["gh", "api", path]))


def _gh_download(path):
    return subprocess.check_output(["gh", "api", path])


def _main_ancestor(sha):
    return (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", sha, "origin/main"],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


def discover_rate_references(*, repository, now, api=_gh_json, download=_gh_download):
    """Discover history independently of editable issue references; verification still follows.

    Two bounded pages cover the weekly and daily schedule over the 90-day window. Failure to
    enumerate history defers the whole decision, rather than allowing an edited issue to omit
    an inconvenient high sample. Only exact named, size-bounded artifacts are downloaded.
    """
    if not isinstance(repository, str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository
    ):
        raise ValueError("invalid repository identity")
    prefix = f"repos/{repository}"
    repo = api(prefix)
    branch = repo["default_branch"]
    # Repository-controlled branch names are query values, never executable commands.
    from urllib.parse import urlencode

    references = []
    exhausted = False
    for page in (1, 2):
        query = urlencode(
            {
                "branch": branch,
                "event": "schedule",
                "status": "success",
                "per_page": 100,
                "page": page,
            }
        )
        runs = api(f"{prefix}/actions/workflows/provider-catalog-reconcile.yml/runs?{query}")
        rows = runs["workflow_runs"]
        if len(rows) < 100:
            exhausted = True
        for run in rows:
            stamp = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
            if stamp.tzinfo is None or stamp > now:
                raise ValueError("invalid workflow run time")
            if stamp < now - timedelta(days=90):
                exhausted = True
                continue
            run_id = str(run["id"])
            if not re.fullmatch(r"[0-9]+", run_id):
                raise ValueError("invalid workflow run identity")
            listing = api(f"{prefix}/actions/runs/{run_id}/artifacts?per_page=100")
            matches = [
                a
                for a in listing["artifacts"]
                if a.get("name") == f"provider-catalog-rate-evidence-{run_id}"
            ]
            if not matches:
                continue  # pre-Slice-4/due-only runs did not produce maintenance evidence
            if len(matches) != 1 or matches[0].get("expired"):
                raise ValueError("rate history artifact missing or ambiguous")
            artifact = matches[0]
            if type(artifact.get("id")) is not int or not (
                0 < artifact.get("size_in_bytes", 0) <= RATE_ARTIFACT_MAX_BYTES
            ):
                raise ValueError("invalid rate history artifact")
            raw = download(f"{prefix}/actions/artifacts/{artifact['id']}/zip")
            if len(raw) > RATE_ARTIFACT_MAX_BYTES:
                raise ValueError("oversized rate history download")
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                files = archive.infolist()
                if (
                    len(files) != 1
                    or files[0].filename != RATE_ARTIFACT_FILE
                    or (files[0].file_size > RATE_ARTIFACT_MAX_BYTES)
                ):
                    raise ValueError("invalid rate history archive")
                envelope = json.loads(archive.read(files[0]))
            references.append({"run_id": run_id, "payload_digest": envelope["payload_digest"]})
        if exhausted:
            break
    if not exhausted:
        raise ValueError("rate history exceeds bounded enumeration; defer")
    return tuple(references)


def verified_rate_history(
    references,
    limits,
    *,
    repository,
    now,
    api=_gh_json,
    download=_gh_download,
    ancestor=_main_ancestor,
):
    """Rehydrate only authenticated artifact values, never observations edited into an issue.

    Returning deferrals alongside valid history permits independent scopes to continue safely.
    A matching run ID is insufficient: run provenance, envelope digest, target digest and ZIP
    contents must all agree. Metadata/download failures never weaken these checks.
    """
    observations, accepted, deferred = [], [], []
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        return (), (), ("invalid repository identity",)
    prefix = f"repos/{repository}"
    try:
        repo = api(prefix)
        workflow = api(f"{prefix}/actions/workflows/provider-catalog-reconcile.yml")
        if (
            repo.get("full_name") != repository
            or not isinstance(repo.get("default_branch"), str)
            or not repo["default_branch"]
            or workflow.get("path") != RATE_WORKFLOW
            or isinstance(workflow.get("id"), bool)
            or not isinstance(workflow.get("id"), int)
            or workflow["id"] <= 0
        ):
            raise ValueError("invalid workflow/repository identity")
    except (OSError, ValueError, TypeError, AttributeError, subprocess.CalledProcessError):
        return (), (), ("rate artifact metadata unavailable",)
    routes = {r["route_id"]: r for r in limits.get("routes") or []}
    seen = set()
    for reference in list(references)[:256]:
        try:
            run_id = reference["run_id"]
            expected = reference["payload_digest"]
            if (
                not isinstance(run_id, str)
                or not re.fullmatch(r"[0-9]+", run_id)
                or not isinstance(expected, str)
                or not re.fullmatch(r"[0-9a-f]{64}", expected)
            ):
                raise ValueError("invalid artifact reference")
            if (run_id, expected) in seen:
                continue
            seen.add((run_id, expected))
            run = api(f"{prefix}/actions/runs/{run_id}")
            if (
                str(run.get("id")) != run_id
                or run.get("conclusion") != "success"
                or run.get("status") != "completed"
                or run.get("event") != "schedule"
                or run.get("workflow_id") != workflow.get("id")
                or run.get("head_branch") != repo.get("default_branch")
                or (run.get("head_repository") or {}).get("full_name") != repository
                or not re.fullmatch(r"[0-9a-f]{40}", str(run.get("head_sha")))
                or not ancestor(run["head_sha"])
            ):
                raise ValueError("untrusted workflow run")
            listing = api(f"{prefix}/actions/runs/{run_id}/artifacts?per_page=100")
            matches = [
                a
                for a in listing.get("artifacts") or []
                if a.get("name") == f"provider-catalog-rate-evidence-{run_id}"
            ]
            if len(matches) != 1:
                raise ValueError("missing or ambiguous artifact")
            artifact = matches[0]
            artifact_id = artifact.get("id")
            if (
                artifact.get("expired")
                or isinstance(artifact_id, bool)
                or not isinstance(artifact_id, int)
                or artifact_id <= 0
                or not 0 < artifact.get("size_in_bytes", 0) <= RATE_ARTIFACT_MAX_BYTES
            ):
                raise ValueError("expired or oversized artifact")
            raw = download(f"{prefix}/actions/artifacts/{artifact_id}/zip")
            if len(raw) > RATE_ARTIFACT_MAX_BYTES:
                raise ValueError("oversized artifact download")
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                files = archive.infolist()
                if (
                    len(files) != 1
                    or files[0].filename != RATE_ARTIFACT_FILE
                    or files[0].file_size > RATE_ARTIFACT_MAX_BYTES
                ):
                    raise ValueError("unexpected artifact contents")
                envelope = json.loads(archive.read(files[0]))
            payload = envelope["payload"]
            if envelope.get("payload_digest") != expected or digest(payload) != expected:
                raise ValueError("artifact payload digest mismatch")
            stamp = datetime.fromisoformat(payload["observed_at"])
            if (
                stamp.tzinfo is None
                or not now - timedelta(days=90) <= stamp <= now
                or payload.get("version") != 1
                or payload.get("run_id") != run_id
                or payload.get("repository") != repository
                or payload.get("workflow_path") != RATE_WORKFLOW
                or payload.get("branch") != run["head_branch"]
                or payload.get("head_sha") != run["head_sha"]
            ):
                raise ValueError("artifact provenance mismatch")
            hydrated = []
            for value in payload.get("observations") or []:
                observation = LimitObservation(**value)
                provider = observation.provider
                accounts = (limits.get("providers") or {}).get(provider, {}).get("accounts") or []
                if (
                    observation.run_id != run_id
                    or observation.account_id not in {a.get("id") for a in accounts}
                    or payload.get("provider_digests", {}).get(provider)
                    != provider_rate_digest(limits, provider)
                ):
                    continue
                if observation.scope == "route":
                    route = routes.get(observation.route_id)
                    if (
                        not route
                        or route.get("provider") != provider
                        or route.get("account_id") != observation.account_id
                        or payload.get("route_digests", {}).get(observation.route_id)
                        != digest(route)
                    ):
                        continue
                elif observation.scope == "provider_account":
                    if payload.get("provider_digests", {}).get(provider) != provider_rate_digest(
                        limits, provider
                    ):
                        continue
                else:
                    continue
                hydrated.append(observation)
            attempted = payload.get("attempted_routes", [])
            if not isinstance(attempted, list) or any(
                not isinstance(rid, str) for rid in attempted
            ):
                raise ValueError("invalid attempted-route metadata")
            attempted = [
                rid
                for rid in attempted
                if rid in routes
                and payload.get("route_digests", {}).get(rid) == digest(routes[rid])
                and payload.get("provider_digests", {}).get(routes[rid]["provider"])
                == provider_rate_digest(limits, routes[rid]["provider"])
            ]
            observations.extend(hydrated)
            accepted.append(
                {
                    "run_id": run_id,
                    "payload_digest": expected,
                    "observed_at": stamp.isoformat(),
                    "attempted_routes": attempted,
                }
            )
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            subprocess.CalledProcessError,
            zipfile.BadZipFile,
            RuntimeError,
            zlib.error,
        ):
            deferred.append("rate artifact could not be verified")
    return tuple(observations), tuple(accepted), tuple(deferred)


def logical_identity(provider, upstream_model, rules, record, routes) -> str | None:
    """Exact configured IDs or a plugin's canonical identity; never AA/brand heuristics."""
    exact = {
        str(r.get("model_key") or r["model"])
        for r in routes
        if r.get("upstream_model") == upstream_model
    }
    canonical = rules.model_identity(upstream_model, record)
    if len(exact) > 1 or (exact and canonical and canonical not in exact):
        return None
    return next(iter(exact)) if exact else canonical


@dataclass(frozen=True)
class ContextRequest:
    """Transient experiment input. Never serialize messages or the shaped body into artifacts."""

    route_id: str
    provider: str
    account_id: str
    upstream_model: str
    identity_digest: str
    dimension: str
    fixture_version: str
    estimator_version: str
    local_input_estimate: int
    reserved_input: int
    requested_output: int
    attempt_ordinal: int
    messages: tuple[dict, ...]
    shaped_body: dict

    def __post_init__(self):
        if self.dimension not in {"input", "output"}:
            raise ValueError("invalid context dimension")
        for name in (
            "local_input_estimate",
            "reserved_input",
            "requested_output",
            "attempt_ordinal",
        ):
            if type(getattr(self, name)) is not int or not 0 < getattr(self, name) <= 2**53 - 1:
                raise ValueError("invalid context request size")
        if not re.fullmatch(r"[a-f0-9]{64}", self.identity_digest):
            raise ValueError("invalid context identity")


@dataclass(frozen=True)
class ContextObservation:
    """Payload-free provider counts. Pure parsers leave provenance for the authenticated caller."""

    schema_version: int
    identity_digest: str
    route_id: str
    provider: str
    account_id: str
    upstream_model: str
    gateway_digest: str
    dimension: str
    fixture_version: str
    estimator_version: str
    local_input_estimate: int
    reserved_input: int
    requested_output: int
    reported_input: int | None
    reported_output: int | None
    reported_total: int | None
    reported_ceiling: int | None
    count_basis: str
    reasoning_basis: str
    finish_reason: str
    evidence_kind: str
    outcome: str
    observed_at: str = ""
    run_id: str = ""
    head_sha: str = ""
    attempt_id: str = ""
    parser_version: str = "chat-usage-v1"
    diagnostic_code: str = "not_recorded"
    http_status: int | None = None
    http_error_class: str = "unknown"
    provider_error_class: str = "none"
    response_media_type: str = "unknown"
    response_bytes: int | None = None
    duration_ms: int | None = None
    first_byte_ms: int | None = None
    admitted_at_ms: int | None = None
    request_started_at_ms: int | None = None
    request_finished_at_ms: int | None = None
    rate_limit_limit_tokens: int | None = None
    rate_limit_remaining_tokens: int | None = None
    rate_limit_reset_tokens_ms: int | None = None
    rate_limit_limit_requests: int | None = None
    rate_limit_remaining_requests: int | None = None
    rate_limit_reset_requests_ms: int | None = None
    retry_after_ms: int | None = None
    error_response_body: str = ""
    error_response_body_truncated: bool = False

    def __post_init__(self):
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unknown context schema")
        if self.dimension not in {"input", "output"} or self.count_basis not in {
            "input",
            "output",
            "combined_reserved",
            "combined_generated",
            "unknown",
        }:
            raise ValueError("unknown counting basis")
        if self.reasoning_basis not in {"included", "excluded", "unknown"}:
            raise ValueError("unknown reasoning basis")
        if self.evidence_kind not in {
            "processed_input",
            "generated_output",
            "parameter_only",
            "size_rejection",
        } or self.outcome not in {"verified", "inconclusive", "quota", "transport", "unsupported"}:
            raise ValueError("unknown context evidence")
        for name in (
            "local_input_estimate",
            "reserved_input",
            "requested_output",
            "reported_input",
            "reported_output",
            "reported_total",
            "reported_ceiling",
        ):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or not 0 <= value <= 2**53 - 1):
                raise ValueError("invalid context count")
        if all(
            v is not None for v in (self.reported_input, self.reported_output, self.reported_total)
        ) and (self.reported_input + self.reported_output != self.reported_total):
            raise ValueError("inconsistent context total")
        if self.outcome == "verified":
            count = self.reported_input if self.dimension == "input" else self.reported_output
            if not count or self.count_basis not in (
                {"input", "combined_reserved", "combined_generated"}
                if self.dimension == "input"
                else {"output"}
            ):
                raise ValueError("positive evidence requires comparable actual counts")
            if self.count_basis.startswith("combined_") and not self.reported_total:
                raise ValueError("combined evidence requires actual total counts")
            if self.dimension == "output" and self.reasoning_basis == "unknown":
                raise ValueError("output reasoning semantics are unknown")
        if self.evidence_kind == "size_rejection" and (
            not self.reported_ceiling or self.count_basis == "unknown"
        ):
            raise ValueError("rejection requires a documented ceiling basis")
        if self.diagnostic_code not in {
            "not_recorded",
            "unclassified_response",
            "response_received",
            "transport_error",
            "transport_timeout",
            "transport_connection_error",
            "transport_request_error",
            "worker_response_timeout",
            "worker_transport_error",
            "worker_credentials_unavailable",
            "worker_preflight_error",
            "response_too_large",
            "invalid_utf8",
            "invalid_sse_json",
            "invalid_sse_envelope",
            "conflicting_sse_usage",
            "invalid_sse_choice",
            "invalid_sse_delta",
            "invalid_sse_content",
            "conflicting_sse_finish_reason",
            "incomplete_sse",
            "http_429_rate_limited",
            "http_non_success",
            "invalid_json",
            "unexpected_response_envelope",
            "usage_missing_or_invalid",
            "completion_shape_invalid",
            "usage_counts_invalid",
            "usage_total_inconsistent",
            "refusal_or_tool_call",
            "completion_content_or_finish_invalid",
            "reasoning_details_invalid",
            "reasoning_count_invalid",
            "fixture_not_verified",
            "input_fixture_not_completed",
            "input_count_not_positive",
            "input_sentinel_mismatch",
            "verified_input_fixture",
            "output_fixture_not_reached",
            "verified_output_fixture",
            "unsupported_endpoint",
        }:
            raise ValueError("unknown context diagnostic code")
        if self.http_error_class not in {
            "none",
            "rate_limited",
            "authentication",
            "invalid_request",
            "client_error",
            "server_error",
            "unknown",
        }:
            raise ValueError("unknown context HTTP error class")
        if self.provider_error_class not in {
            "none",
            "rate_limited",
            "authentication",
            "invalid_request",
            "context_size_candidate",
            "quota",
            "server_error",
            "unknown",
        }:
            raise ValueError("unknown context provider error class")
        if self.response_media_type not in {
            "unknown",
            "application/json",
            "text/event-stream",
            "other",
        }:
            raise ValueError("unknown context response media type")
        if self.http_status is not None and (
            type(self.http_status) is not int or not 100 <= self.http_status <= 599
        ):
            raise ValueError("invalid context HTTP status")
        bounded = {
            "response_bytes": 4 * 1024 * 1024,
            "duration_ms": 120_000,
            "first_byte_ms": 120_000,
            "rate_limit_reset_tokens_ms": 86_400_000,
            "rate_limit_reset_requests_ms": 86_400_000,
            "retry_after_ms": 3_600_000,
        }
        for name, maximum in bounded.items():
            value = getattr(self, name)
            if value is not None and (type(value) is not int or not 0 <= value <= maximum):
                raise ValueError(f"invalid context diagnostic {name}")
        if not isinstance(self.error_response_body, str):
            raise ValueError("invalid bounded error response body")
        try:
            body_size = len(self.error_response_body.encode("utf-8"))
        except UnicodeError as exc:
            raise ValueError("invalid bounded error response body") from exc
        if (
            body_size > 4096
            or type(self.error_response_body_truncated) is not bool
            or (self.error_response_body and not 400 <= (self.http_status or 0) <= 599)
            or (self.error_response_body_truncated and not self.error_response_body)
        ):
            raise ValueError("invalid bounded error response body")
        for name in (
            "admitted_at_ms",
            "request_started_at_ms",
            "request_finished_at_ms",
            "rate_limit_limit_tokens",
            "rate_limit_remaining_tokens",
            "rate_limit_limit_requests",
            "rate_limit_remaining_requests",
        ):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or not 0 <= value <= 2**53 - 1):
                raise ValueError(f"invalid context diagnostic {name}")
        for name in ("identity_digest", "gateway_digest"):
            if not isinstance(getattr(self, name), str) or not re.fullmatch(
                r"[a-f0-9]{64}", getattr(self, name)
            ):
                raise ValueError("invalid context digest")

    @classmethod
    def from_request(cls, request, **values):
        """Populate fixed metadata; untrusted response content never enters this record."""
        return (
            cls(
                schema_version=1,
                identity_digest=request.identity_digest,
                route_id=request.route_id,
                provider=request.provider,
                account_id=request.account_id,
                upstream_model=request.upstream_model,
                gateway_digest=digest(request.shaped_body["url"]),
                dimension=request.dimension,
                fixture_version=request.fixture_version,
                estimator_version=request.estimator_version,
                local_input_estimate=request.local_input_estimate,
                reserved_input=request.reserved_input,
                requested_output=request.requested_output,
                reported_input=None,
                reported_output=None,
                reported_total=None,
                reported_ceiling=None,
                count_basis="unknown",
                reasoning_basis="unknown",
                finish_reason="",
                evidence_kind="parameter_only",
                outcome="unsupported",
                diagnostic_code="unsupported_endpoint",
            )
            if not values
            else cls(**{**asdict(cls.from_request(request)), **values})
        )


def context_identity(
    route,
    provider_config,
    *,
    dimension,
    count_basis,
    parser_version,
    opposite_reservation,
    fixture_version=None,
    estimator_version="chars4-v1",
    gateway_path=None,
):
    """Numeric configuration changes do not invalidate a physical measurement identity."""
    return digest(
        {
            "provider": route["provider"],
            "account_id": route["account_id"],
            "route_id": route["route_id"],
            "upstream_model": route["upstream_model"],
            "gateway": gateway_path
            or [provider_config.get("api_base"), provider_config.get("chat_path")],
            "dimension": dimension,
            "count_basis": count_basis,
            "parser_version": parser_version,
            "fixture_version": fixture_version or f"context-{dimension}-v1",
            "estimator_version": estimator_version,
            "opposite_reservation": opposite_reservation,
        }
    )


CONTEXT_ARTIFACT_MAX_BYTES = 4 * 1024 * 1024
CONTEXT_ARTIFACT_FILE = "provider-catalog-context-evidence.json"


def _context_catalog_digest(value):
    """Match Worker canonicalJson for the compiled catalog, including JS number formatting."""
    import math
    from decimal import Decimal

    def canonical(item):
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError("invalid catalog object key")
            keys = sorted(item, key=lambda key: key.encode("utf-16-be"))
            return "{" + ",".join(canonical(key) + ":" + canonical(item[key]) for key in keys) + "}"
        if isinstance(item, list):
            return "[" + ",".join(canonical(element) for element in item) + "]"
        if type(item) in {int, float}:
            if abs(item) > 2**53 - 1 or not math.isfinite(item):
                raise ValueError("unsafe catalog number")
            if item == int(item):
                return str(int(item))  # JSON.stringify represents both 1.0 and -0 as integers
            number = repr(item)
            if 1e-6 <= abs(item) < 1e21:
                return format(Decimal(number), "f")
            return re.sub(r"e([+-])0+", r"e\1", number)
        return json.dumps(item, ensure_ascii=False, separators=(",", ":"), allow_nan=False)

    return hashlib.sha256(canonical(value).encode()).hexdigest()


def context_artifact(
    observations,
    limits,
    *,
    repository,
    run_id,
    head_sha,
    now,
    catalog_digest,
    attempted_routes=(),
    states=(),
    pause_evidence=(),
):
    """Typed payload-free weekly evidence; summaries never renew the age of original proof."""
    if now.tzinfo is None or len(observations) > 24 or len(limits.get("routes", [])) > 128:
        raise ValueError("context artifact exceeds reviewed bounds")
    if len(pause_evidence) > 128:
        raise ValueError("context artifact has too many pause diagnostics")
    for item in pause_evidence:
        if (
            not isinstance(item, dict)
            or set(item)
            != {
                "provider",
                "drained",
                "in_flight_at_start",
                "waited_ms",
                "pause_expired",
                "contended",
            }
            or not isinstance(item["provider"], str)
            or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", item["provider"])
            or type(item["drained"]) is not bool
            or type(item["in_flight_at_start"]) is not int
            or not 0 <= item["in_flight_at_start"] <= 2**53 - 1
            or type(item["waited_ms"]) is not int
            or not 0 <= item["waited_ms"] <= 600_000
            or type(item["pause_expired"]) is not bool
            or type(item["contended"]) is not bool
            or item["contended"] != (not item["drained"] or item["pause_expired"])
        ):
            raise ValueError("invalid context pause diagnostics")
    payload = {
        "version": 1,
        "repository": repository,
        "run_id": str(run_id),
        "workflow_path": RATE_WORKFLOW,
        "branch": "main",
        "head_sha": head_sha,
        "observed_at": now.astimezone(UTC).isoformat(),
        "catalog_digest": catalog_digest,
        "observations": [asdict(o) for o in observations],
        "attempted_routes": sorted(set(attempted_routes)),
        "summaries": [asdict(state) for state in states],
        "pause_evidence": list(pause_evidence),
    }
    envelope = {"payload": payload, "payload_digest": digest(payload)}
    if len(json.dumps(envelope).encode()) > CONTEXT_ARTIFACT_MAX_BYTES:
        raise ValueError("oversized context artifact")
    return envelope


def discover_context_references(
    *, repository, now, api=_gh_json, download=_gh_download, manual=False
):
    """Discover history independently of editable issue references; verification still follows.

    Two bounded pages cover scheduled runs; manual discovery permits four bounded pages.
    Enumeration failure defers the whole decision, rather than letting issue edits omit
    an inconvenient high sample. Only exact named, size-bounded artifacts are downloaded.
    """
    if not isinstance(repository, str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository
    ):
        raise ValueError("invalid repository identity")
    prefix = f"repos/{repository}"
    repo = api(prefix)
    branch = repo["default_branch"]
    # Repository-controlled branch names are query values, never executable commands.
    from urllib.parse import urlencode

    references = []
    exhausted = False
    for page in range(1, 5 if manual else 3):
        query = urlencode(
            {
                "branch": branch,
                "event": "workflow_dispatch" if manual else "schedule",
                "status": "success",
                "per_page": 100,
                "page": page,
            }
        )
        runs = api(f"{prefix}/actions/workflows/provider-catalog-reconcile.yml/runs?{query}")
        rows = runs["workflow_runs"]
        if len(rows) < 100:
            exhausted = True
        for run in rows:
            stamp = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
            if stamp.tzinfo is None or stamp > now:
                raise ValueError("invalid workflow run time")
            if stamp < now - timedelta(days=90):
                exhausted = True
                continue
            run_id = str(run["id"])
            if not re.fullmatch(r"[0-9]+", run_id):
                raise ValueError("invalid workflow run identity")
            listing = api(f"{prefix}/actions/runs/{run_id}/artifacts?per_page=100")
            matches = [
                a
                for a in listing["artifacts"]
                if a.get("name")
                == f"provider-catalog-{'manual-context' if manual else 'context'}-evidence-{run_id}"
            ]
            if not matches:
                continue  # pre-Slice-5/daily runs did not produce maintenance evidence
            if len(matches) != 1 or matches[0].get("expired"):
                raise ValueError("context history artifact missing or ambiguous")
            artifact = matches[0]
            if (
                type(artifact.get("id")) is not int
                or type(artifact.get("size_in_bytes")) is not int
                or not (0 < artifact.get("size_in_bytes", 0) <= CONTEXT_ARTIFACT_MAX_BYTES)
            ):
                raise ValueError("invalid context history artifact")
            raw = download(f"{prefix}/actions/artifacts/{artifact['id']}/zip")
            if len(raw) > CONTEXT_ARTIFACT_MAX_BYTES:
                raise ValueError("oversized context history download")
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                files = archive.infolist()
                if (
                    len(files) != 1
                    or files[0].filename != CONTEXT_ARTIFACT_FILE
                    or (files[0].file_size > CONTEXT_ARTIFACT_MAX_BYTES)
                ):
                    raise ValueError("invalid context history archive")
                envelope = json.loads(archive.read(files[0]))
            references.append({"run_id": run_id, "payload_digest": envelope["payload_digest"]})
        if exhausted:
            break
    if not exhausted:
        raise ValueError("context history exceeds bounded enumeration; defer")
    return tuple(references)


def verified_context_history(
    references,
    limits,
    *,
    repository,
    now,
    api=_gh_json,
    download=_gh_download,
    ancestor=_main_ancestor,
    manual=False,
):
    """Rehydrate only authenticated artifact values, never observations edited into an issue.

    Returning deferrals alongside valid history permits independent scopes to continue safely.
    A matching run ID is insufficient: run provenance, envelope digest, target digest and ZIP
    contents must all agree. Metadata/download failures never weaken these checks.
    """
    observations, accepted, deferred = [], [], []
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        return (), (), ("invalid repository identity",)
    prefix = f"repos/{repository}"
    try:
        repo = api(prefix)
        workflow = api(f"{prefix}/actions/workflows/provider-catalog-reconcile.yml")
        if (
            repo.get("full_name") != repository
            or not isinstance(repo.get("default_branch"), str)
            or not repo["default_branch"]
            or workflow.get("path") != RATE_WORKFLOW
            or isinstance(workflow.get("id"), bool)
            or not isinstance(workflow.get("id"), int)
            or workflow["id"] <= 0
        ):
            raise ValueError("invalid workflow/repository identity")
    except (OSError, ValueError, TypeError, AttributeError, subprocess.CalledProcessError):
        return (), (), ("context artifact metadata unavailable",)
    routes = {r["route_id"]: r for r in limits.get("routes") or []}
    seen = set()
    if len(references) > 256 or len(routes) > 128:
        return (), (), ("context history exceeds reviewed bound",)
    for reference in references:
        try:
            run_id = reference["run_id"]
            expected = reference["payload_digest"]
            if (
                not isinstance(run_id, str)
                or not re.fullmatch(r"[0-9]+", run_id)
                or not isinstance(expected, str)
                or not re.fullmatch(r"[0-9a-f]{64}", expected)
            ):
                raise ValueError("invalid artifact reference")
            if (run_id, expected) in seen:
                continue
            seen.add((run_id, expected))
            run = api(f"{prefix}/actions/runs/{run_id}")
            if (
                str(run.get("id")) != run_id
                or run.get("conclusion") != "success"
                or run.get("status") != "completed"
                or run.get("event") != ("workflow_dispatch" if manual else "schedule")
                or run.get("workflow_id") != workflow.get("id")
                or run.get("head_branch") != repo.get("default_branch")
                or (run.get("head_repository") or {}).get("full_name") != repository
                or not re.fullmatch(r"[0-9a-f]{40}", str(run.get("head_sha")))
                or not ancestor(run["head_sha"])
            ):
                raise ValueError("untrusted workflow run")
            listing = api(f"{prefix}/actions/runs/{run_id}/artifacts?per_page=100")
            matches = [
                a
                for a in listing.get("artifacts") or []
                if a.get("name")
                == f"provider-catalog-{'manual-context' if manual else 'context'}-evidence-{run_id}"
            ]
            if len(matches) != 1:
                raise ValueError("missing or ambiguous artifact")
            artifact = matches[0]
            artifact_id = artifact.get("id")
            if (
                artifact.get("expired")
                or isinstance(artifact_id, bool)
                or not isinstance(artifact_id, int)
                or artifact_id <= 0
                or type(artifact.get("size_in_bytes")) is not int
                or not 0 < artifact.get("size_in_bytes", 0) <= CONTEXT_ARTIFACT_MAX_BYTES
            ):
                raise ValueError("expired or oversized artifact")
            raw = download(f"{prefix}/actions/artifacts/{artifact_id}/zip")
            if len(raw) > CONTEXT_ARTIFACT_MAX_BYTES:
                raise ValueError("oversized artifact download")
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                files = archive.infolist()
                if (
                    len(files) != 1
                    or files[0].filename != CONTEXT_ARTIFACT_FILE
                    or files[0].file_size > CONTEXT_ARTIFACT_MAX_BYTES
                ):
                    raise ValueError("unexpected artifact contents")
                envelope = json.loads(archive.read(files[0]))
            payload = envelope["payload"]
            if envelope.get("payload_digest") != expected or digest(payload) != expected:
                raise ValueError("artifact payload digest mismatch")
            stamp = datetime.fromisoformat(payload["observed_at"])
            if (
                stamp.tzinfo is None
                or not now - timedelta(days=90) <= stamp <= now
                or type(payload.get("version")) is not int
                or payload.get("version") != 1
                or payload.get("run_id") != run_id
                or payload.get("repository") != repository
                or payload.get("workflow_path") != RATE_WORKFLOW
                or payload.get("branch") != run["head_branch"]
                or payload.get("head_sha") != run["head_sha"]
                or (
                    manual
                    and (
                        payload.get("kind") != "manual_context"
                        or payload.get("run_status", "success") != "success"
                    )
                )
            ):
                raise ValueError("artifact provenance mismatch")
            from citypods.provider_catalog.probe import chat_url
            from citypods.provider_catalog.registry import rules_for

            values = payload.get("observations")
            if not isinstance(values, list) or len(values) > 24:
                raise ValueError("invalid context observation count")
            if not isinstance(payload.get("catalog_digest"), str) or not re.fullmatch(
                r"[a-f0-9]{64}", payload["catalog_digest"]
            ):
                raise ValueError("invalid context catalog digest")
            hydrated = []
            identities = set()
            attempts = set()
            for value in values:
                observation = ContextObservation(**value)
                route = routes.get(observation.route_id)
                observed = datetime.fromisoformat(observation.observed_at)
                if (
                    observation.run_id != run_id
                    or observation.head_sha != run["head_sha"]
                    or observed.tzinfo is None
                    or not now - timedelta(days=90) <= observed <= stamp
                    or not re.fullmatch(r"[a-f0-9]{64}", observation.attempt_id)
                    or observation.attempt_id in attempts
                ):
                    raise ValueError("invalid context observation provenance")
                attempts.add(observation.attempt_id)
                if (
                    not route
                    or route.get("free") is not True
                    or route.get("rpd") == 0
                    or observation.provider != route.get("provider")
                    or observation.account_id != route.get("account_id")
                    or observation.upstream_model != route.get("upstream_model")
                ):
                    continue
                cfg = limits["providers"][route["provider"]]
                accounts = {a.get("id") for a in cfg.get("accounts", [])}
                expected_identity = context_identity(
                    route,
                    cfg,
                    dimension=observation.dimension,
                    count_basis=(
                        observation.dimension
                        if observation.count_basis == "unknown"
                        else observation.count_basis
                    ),
                    parser_version="chat-usage-v1",
                    opposite_reservation=256 if observation.dimension == "input" else 2048,
                    gateway_path=chat_url(rules_for(route["provider"]), cfg),
                )
                if (
                    observation.account_id not in accounts
                    or observation.identity_digest != expected_identity
                    or observation.gateway_digest
                    != digest(chat_url(rules_for(route["provider"]), cfg))
                    or observation.fixture_version != f"context-{observation.dimension}-v1"
                    or observation.estimator_version != "chars4-v1"
                    or observation.parser_version != "chat-usage-v1"
                ):
                    continue
                identities.add(observation.identity_digest)
                hydrated.append(observation)
            if len(identities) > 128:
                raise ValueError("context coverage exceeds reviewed bound")
            attempted = payload.get("attempted_routes")
            if (
                not isinstance(attempted, list)
                or len(attempted) > 128
                or any(not isinstance(rid, str) for rid in attempted)
            ):
                raise ValueError("invalid context attempted-route metadata")
            attempted = [
                rid
                for rid in attempted
                if rid in routes and routes[rid].get("free") is True and routes[rid].get("rpd") != 0
            ]
            observations.extend(hydrated)
            accepted.append(
                {
                    "run_id": run_id,
                    "payload_digest": expected,
                    "observed_at": stamp.isoformat(),
                    "attempted_routes": attempted,
                }
            )
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            subprocess.CalledProcessError,
            zipfile.BadZipFile,
            RuntimeError,
            zlib.error,
        ):
            deferred.append("context artifact could not be verified")
    # Keep original unexpired bracket anchors plus recent diagnostics within sixteen records.
    # Summaries cannot extend proof lifetime or evade the per-identity observation bound.
    from citypods.provider_catalog.limits import context_history_states

    states = context_history_states(observations, now=now)
    bounded = {}
    for observation in sorted(observations, key=lambda o: (o.observed_at, o.run_id, o.attempt_id)):
        key = (observation.identity_digest, observation.dimension)
        bounded.setdefault(key, []).append(observation)
    if len(bounded) > 128:
        return (), (), ("context identity coverage exceeds reviewed bound",)
    retained = []
    for key, values in bounded.items():
        state = states[key]
        anchors = [o for o in (state.success, state.rejection) if o is not None]
        recent = [o for o in values if o not in anchors][-(16 - len(anchors)) :]
        retained.extend(sorted((*anchors, *recent), key=lambda o: (o.observed_at, o.attempt_id)))
    return tuple(retained), tuple(accepted), tuple(deferred)
