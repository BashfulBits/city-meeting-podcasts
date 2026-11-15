"""Google Cloud adapters for the deferred decision pilot."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from citypods.judging.decision_batch_transport import PreconditionFailed

STORAGE = "https://storage.googleapis.com"
RUN = "https://run.googleapis.com/v2"
METADATA = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
)
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
LAUNCH_FLAGS = {"regular": "--no-delay-execution", "delayed": "--delay-execution"}
JOB_RE = re.compile(r"projects/[a-z0-9-]+/locations/[a-z0-9-]+/jobs/[a-z0-9-]+")
IDENTIFIER_RE = re.compile(r"[a-zA-Z0-9_-]{1,128}")


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _read(request: Request) -> bytes:
    with build_opener(_NoRedirects()).open(request, timeout=30) as response:
        data = response.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        raise ValueError("Google API response exceeds byte limit")
    return data


def metadata_access_token() -> str:
    """Use the Cloud Run job service identity, with no key files."""
    return json.loads(_read(Request(METADATA, headers={"Metadata-Flavor": "Google"})))[
        "access_token"
    ]


def _authorized(url: str, token: str, data: bytes | None = None) -> Request:
    headers = {"Authorization": f"Bearer {token}"}
    if data is not None:
        headers["Content-Type"] = "application/octet-stream"
    return Request(url, data=data, headers=headers)


class GCSObjectStore:
    """Read immutable generations and write using Google Cloud Storage preconditions."""

    def __init__(self, bucket: str, access_token: Callable[[], str]) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]", bucket):
            raise ValueError("invalid GCS bucket name")
        self.bucket = bucket
        self.access_token = access_token

    def get(self, key: str, generation: int | None = None) -> tuple[bytes, int] | None:
        url = f"{STORAGE}/storage/v1/b/{self.bucket}/o/{quote(key, safe='')}"
        token = self.access_token()
        try:
            if generation is None:
                metadata = json.loads(_read(_authorized(f"{url}?fields=generation", token)))
                generation = int(metadata["generation"])
            query = urlencode({"alt": "media", "generation": generation})
            return _read(_authorized(f"{url}?{query}", token)), generation
        except HTTPError as exc:
            if exc.code == 404:
                return None
            raise

    def put(self, key: str, data: bytes, if_generation_match: int) -> int:
        query = urlencode(
            {
                "uploadType": "media",
                "name": key,
                "ifGenerationMatch": if_generation_match,
                "fields": "generation",
            }
        )
        url = f"{STORAGE}/upload/storage/v1/b/{self.bucket}/o?{query}"
        try:
            return int(json.loads(_read(_authorized(url, self.access_token(), data)))["generation"])
        except HTTPError as exc:
            if exc.code == 412:
                raise PreconditionFailed(key) from None
            raise


def cloud_run_command(
    *, mode: str, job: str, region: str, manifest_id: str, attempt_id: str
) -> list[str]:
    """Build an asynchronous call using a preconfigured job and bounded override values."""
    if not JOB_RE.fullmatch(job) or job.split("/")[3] != region:
        raise ValueError("expected a fully qualified job in the configured region")
    if not IDENTIFIER_RE.fullmatch(manifest_id) or not IDENTIFIER_RE.fullmatch(attempt_id):
        raise ValueError("invalid manifest or attempt ID")
    _, project, _, _, _, job_name = job.split("/")
    return [
        "gcloud",
        "beta",
        "run",
        "jobs",
        "execute",
        job_name,
        f"--project={project}",
        f"--region={region}",
        "--async",
        LAUNCH_FLAGS[mode],
        f"--update-env-vars=DECISION_BATCH_MANIFEST_ID={manifest_id},"
        f"DECISION_BATCH_ATTEMPT_ID={attempt_id}",
    ]


class CloudRunLauncher:
    def __init__(self, job: str, access_token: Callable[[], str]) -> None:
        if not JOB_RE.fullmatch(job):
            raise ValueError("expected a fully qualified Cloud Run job")
        self.job = job
        self.access_token = access_token

    def verify_canary_job(self, image_digest: str) -> None:
        """Fail closed if the billed resources differ from the reserved canary envelope."""
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest):
            raise ValueError("an immutable canary image digest is required")
        url = f"{RUN}/{self.job}"
        job = json.loads(_read(_authorized(url, self.access_token())))
        execution = job.get("template", {})
        task = execution.get("template", {})
        containers = task.get("containers", [])
        if (
            self.job.split("/")[3] not in {"us-central1", "us-east4"}
            or execution.get("taskCount") != 1
            or execution.get("parallelism") != 1
            or task.get("maxRetries") != 0
            or task.get("timeout") != "300s"
            or task.get("gpuZonalRedundancyDisabled") is not True
            or task.get("nodeSelector", {}).get("accelerator") != "nvidia-l4"
            or len(containers) != 1
        ):
            raise ValueError("Cloud Run job does not match the one-task L4 canary envelope")
        container = containers[0]
        env = {entry["name"]: entry.get("value") for entry in container.get("env", [])}
        if (
            not container["image"].endswith(f"@{image_digest}")
            or env.get("CITYPODS_IMAGE_DIGEST") != image_digest
            or container.get("resources", {}).get("limits")
            != {"cpu": "4", "memory": "16Gi", "nvidia.com/gpu": "1"}
        ):
            raise ValueError("canary image or resource limits differ from the reservation")
        required = ["run.jobs.run", "run.jobs.runWithOverrides"]
        request = _authorized(
            f"{url}:testIamPermissions",
            self.access_token(),
            json.dumps({"permissions": required}).encode(),
        )
        request.add_header("Content-Type", "application/json")
        allowed = json.loads(_read(request)).get("permissions", [])
        if not set(required).issubset(allowed):
            raise PermissionError("dispatcher lacks Cloud Run launch permissions")

    def launch(self, manifest_id: str, attempt_id: str, mode: str) -> str:
        command = cloud_run_command(
            mode=mode,
            job=self.job,
            region=self.job.split("/")[3],
            manifest_id=manifest_id,
            attempt_id=attempt_id,
        )
        completed = subprocess.run(
            [*command, "--format=json", "--quiet"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if completed.returncode:
            raise RuntimeError("Cloud Run launch acknowledgement unavailable")
        # The beta SDK returns a v1 Execution resource, including for --async.
        name = json.loads(completed.stdout).get("metadata", {}).get("name")
        if not isinstance(name, str) or not IDENTIFIER_RE.fullmatch(name):
            raise ValueError("Cloud Run launch acknowledgement lacks an execution name")
        return f"{self.job}/executions/{name}"

    def execution_state(self, execution: str, manifest_id: str, attempt_id: str) -> str | None:
        """Read a matching execution's terminal Completed condition, if present."""
        prefix = f"{self.job}/executions/"
        if not execution.startswith(prefix) or not IDENTIFIER_RE.fullmatch(
            execution[len(prefix) :]
        ):
            raise ValueError("execution must belong to the configured job")
        description = json.loads(_read(_authorized(f"{RUN}/{execution}", self.access_token())))
        containers = description.get("template", {}).get("containers", [])
        if len(containers) != 1:
            raise ValueError("execution must contain one worker container")
        env = {entry["name"]: entry.get("value") for entry in containers[0].get("env", [])}
        if (
            env.get("DECISION_BATCH_MANIFEST_ID") != manifest_id
            or env.get("DECISION_BATCH_ATTEMPT_ID") != attempt_id
        ):
            raise ValueError("execution does not match the attempt")
        if not description.get("completionTime"):
            return None
        return next(
            (
                condition.get("state")
                for condition in description.get("conditions", [])
                if condition.get("type") == "Completed"
            ),
            None,
        )
