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
from typing import Any


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


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
