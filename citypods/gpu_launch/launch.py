"""Plan, seal and launch one batch on the most efficient GPU class.

This is the reusable entry point for any producer: give it sealed-input chunks, a model profile
and a padded-token estimate. It picks the GPU, pins the current kernel cache and the planned token
budget in an immutable manifest, reserves the worst-case spend in the admission ledger, and starts
the fixed Cloud Run job for that GPU with the planned timeout. Volumes that need several launches
are returned to the caller as a ``SplitRequired`` plan; partitioning chunks is the producer's job.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from citypods.gpu_launch import kernel_cache
from citypods.gpu_launch.planner import LaunchPlan, plan_launch
from citypods.gpu_launch.profiles import GPU_CLASSES, ModelProfile
from citypods.judging.decision_batch_transport import (
    ObjectStore,
    reserve_and_launch,
    seal_manifest,
)

LIMITS = {"max_rows": 1000, "max_bytes": 8 * 1024 * 1024, "max_estimated_tokens": 3_000_000}


class SplitRequired(ValueError):
    """The volume exceeds one launch on the chosen GPU; the producer must partition it."""

    def __init__(self, plan: LaunchPlan) -> None:
        super().__init__(f"{plan.chosen.launches} launches required on {plan.chosen.gpu}")
        self.plan = plan


class JobLauncher(Protocol):
    """A Cloud Run job handle; ``CloudRunLauncher`` implements it."""

    def verify_job(self, image_digest: str, gpu: Any) -> None: ...

    def launch(
        self, manifest_id: str, attempt_id: str, mode: str, task_timeout: int | None = None
    ) -> str: ...


class _TimedLauncher:
    """Bind the planned timeout so ``reserve_and_launch`` keeps its three-argument contract."""

    def __init__(self, launcher: JobLauncher, timeout: int) -> None:
        self._launcher = launcher
        self._timeout = timeout

    def launch(self, manifest_id: str, attempt_id: str, mode: str) -> str:
        return self._launcher.launch(manifest_id, attempt_id, mode, task_timeout=self._timeout)


def plan_and_launch(
    store: ObjectStore,
    launchers: Mapping[str, JobLauncher],
    *,
    chunks: Sequence[Mapping[str, Any]],
    profile: ModelProfile,
    image_digest: str,
    attempt_id: str,
    padded_tokens: int,
    windows: Mapping[str, int],
    mode: str = "regular",
    cache: str = "configs",
    **plan_options: Any,
) -> tuple[LaunchPlan, str, str]:
    """Return ``(plan, manifest_id, operation)``.

    ``windows`` maps each billing window to its allowance in microdollars; the plan's per-launch
    worst case is reserved against every window. ``launchers`` maps GPU class names to the job that
    runs on them, so a GPU is only eligible if the caller provisioned a job for it.
    """
    plan = plan_launch(
        profile,
        padded_tokens,
        gpus=[g for g in GPU_CLASSES if g in launchers],
        cache=cache,
        **plan_options,
    )
    chosen = plan.chosen
    if chosen.launches != 1:
        raise SplitRequired(plan)
    gpu = GPU_CLASSES[chosen.gpu]
    pin = None
    if cache == "archive":
        key = kernel_cache.cache_key(
            gpu_arch=gpu.cuda_arch,
            image_digest=image_digest,
            model_revision=profile.revision,
            adapter=profile.model_id,
            token_budget=chosen.token_budget,
        )
        pin = kernel_cache.current_pin(store, key)
        if pin is None:
            raise ValueError(f"no kernel cache published for {profile.model_id} on {gpu.name}")
    manifest_id = seal_manifest(
        store,
        chunks,
        model_revision=profile.revision,
        image_digest=image_digest,
        settings={
            "launch_mode": mode,
            "max_len": profile.max_len,
            "token_budget": chosen.token_budget,
        },
        gpu=gpu.name,
        kernel_cache=pin,
        **LIMITS,
    )
    launcher = launchers[gpu.name]
    operation = reserve_and_launch(
        store,
        _TimedLauncher(launcher, chosen.timeout_seconds),
        manifest_id,
        attempt_id,
        reservations=dict.fromkeys(windows, chosen.reservation_microdollars),
        mode=mode,
        allowances=dict(windows),
        verify_image=lambda digest: launcher.verify_job(digest, gpu),
    )
    return plan, manifest_id, operation
