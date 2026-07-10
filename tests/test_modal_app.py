"""Tests for scripts/compute/modal_app.py's GPU-resolution override contract.

Mirrors tests/test_beam_app.py: ``_resolve_gpu()`` now defers its ``citypods.compute.policy``/
``citypods.config`` import until it actually needs the site-config fallback, so an explicit
``CITYPODS_MODAL_GPU`` override must short-circuit without touching ``config/site_config.yml``.

The ``modal`` SDK isn't installed in this dev/test environment, so the module is imported here via
``importlib`` against a lightweight stand-in ``modal`` module (same ``_load_script``-style pattern
used elsewhere in this suite, e.g. tests/test_reclaim.py).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MODAL_APP_PATH = _REPO_ROOT / "scripts" / "compute" / "modal_app.py"


def _install_fake_modal_sdk() -> ModuleType:
    modal_mod = ModuleType("modal")
    modal_mod.App = MagicMock(name="modal.App")
    modal_mod.Image = MagicMock(name="modal.Image")
    modal_mod.Secret = MagicMock(name="modal.Secret")
    modal_mod.Cron = MagicMock(name="modal.Cron")
    modal_mod.current_function_call_id = MagicMock(return_value="fake-call-id")
    modal_mod.current_input_id = MagicMock(return_value="fake-input-id")

    # `app = modal.App(APP_NAME)` reuses this single instance for both decorators below; identity
    # decorators keep run_scheduled()/main() intact without needing modal's real runtime.
    app_instance = modal_mod.App.return_value
    app_instance.function = MagicMock(return_value=lambda fn: fn)
    app_instance.local_entrypoint = MagicMock(return_value=lambda fn: fn)

    sys.modules["modal"] = modal_mod
    return modal_mod


@pytest.fixture
def modal_app_module(monkeypatch):
    _install_fake_modal_sdk()

    # Module-level `GPU = _resolve_gpu()` runs unconditionally at import time; give it a
    # deterministic env override so import succeeds without requiring a real
    # config/site_config.yml on disk. The fallback branch is exercised directly against
    # `_resolve_gpu()` in the tests below instead.
    monkeypatch.setenv("CITYPODS_MODAL_GPU", "IMPORT-TIME-PLACEHOLDER")

    sys.modules.pop("scripts.compute.modal_app", None)
    spec = importlib.util.spec_from_file_location("scripts.compute.modal_app", _MODAL_APP_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["scripts.compute.modal_app"] = module
    spec.loader.exec_module(module)
    try:
        yield module
    finally:
        sys.modules.pop("scripts.compute.modal_app", None)
        sys.modules.pop("modal", None)


def test_resolve_gpu_prefers_explicit_env_override(modal_app_module, monkeypatch):
    monkeypatch.setenv("CITYPODS_MODAL_GPU", "A100")
    assert modal_app_module._resolve_gpu() == "A100"


def test_resolve_gpu_env_override_short_circuits_policy_lookup(modal_app_module, monkeypatch):
    """When CITYPODS_MODAL_GPU is set, _resolve_gpu() must not import/call the policy machinery
    at all -- the whole point of deferring the import is to let a one-off canary override the GPU
    without needing config/site_config.yml on the caller's machine."""
    monkeypatch.setenv("CITYPODS_MODAL_GPU", "A100")

    def _boom(*_a, **_k):
        raise AssertionError("backend_policy/load_site_config must not be called")

    monkeypatch.setattr("citypods.compute.policy.backend_policy", _boom)
    monkeypatch.setattr("citypods.config.load_site_config", _boom)

    assert modal_app_module._resolve_gpu() == "A100"


def test_resolve_gpu_falls_back_to_site_policy_when_env_unset(modal_app_module, monkeypatch):
    monkeypatch.delenv("CITYPODS_MODAL_GPU", raising=False)
    monkeypatch.setattr(
        "citypods.config.load_site_config",
        lambda path: {
            "defaults": {"compute_backends": {"modal": {"hardware": {"gpu_type": "A10G"}}}}
        },
    )

    assert modal_app_module._resolve_gpu() == "A10G"


def test_resolve_gpu_falls_back_to_hardcoded_default_when_policy_has_no_gpu_type(
    modal_app_module, monkeypatch
):
    monkeypatch.delenv("CITYPODS_MODAL_GPU", raising=False)
    monkeypatch.setattr("citypods.config.load_site_config", lambda path: {"defaults": {}})

    assert modal_app_module._resolve_gpu() == "L4"


def test_resolve_gpu_empty_string_env_override_does_not_shadow_policy(
    modal_app_module, monkeypatch
):
    """An empty-string override (e.g. an unset GH Actions env interpolation) is falsy and must
    fall through to the site policy, not be treated as an explicit choice of ''."""
    monkeypatch.setenv("CITYPODS_MODAL_GPU", "")
    monkeypatch.setattr(
        "citypods.config.load_site_config",
        lambda path: {
            "defaults": {"compute_backends": {"modal": {"hardware": {"gpu_type": "A10G"}}}}
        },
    )

    assert modal_app_module._resolve_gpu() == "A10G"


def test_resolve_gpu_loads_site_config_from_the_canonical_relative_path(
    modal_app_module, monkeypatch
):
    calls: dict[str, object] = {}

    def _fake_load(path):
        calls["path"] = path
        return {"defaults": {}}

    def _fake_policy(site_config, backend):
        calls["backend"] = backend
        return SimpleNamespace(hardware=SimpleNamespace(gpu_type=""))

    monkeypatch.delenv("CITYPODS_MODAL_GPU", raising=False)
    monkeypatch.setattr("citypods.config.load_site_config", _fake_load)
    monkeypatch.setattr("citypods.compute.policy.backend_policy", _fake_policy)

    modal_app_module._resolve_gpu()

    assert calls["path"] == "config/site_config.yml"
    assert calls["backend"] == "modal"


def test_module_level_gpu_and_runtime_env_reflect_import_time_resolution(modal_app_module):
    """GPU/RUNTIME_ENV are computed once at import time from _resolve_gpu()'s result and wired
    into the app.function() decorator's gpu kwarg; confirm the resolved value actually
    propagates."""
    assert modal_app_module.GPU == "IMPORT-TIME-PLACEHOLDER"
    assert modal_app_module.RUNTIME_ENV["CITYPODS_WORKER_GPU_TYPE"] == "IMPORT-TIME-PLACEHOLDER"