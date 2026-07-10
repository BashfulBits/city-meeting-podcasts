"""Tests for scripts/compute/beam_app.py's GPU-resolution override contract.

``beam deploy`` imports this module on the *caller's* machine before the remote image build, so
``_resolve_gpu()`` was changed to defer its ``citypods.compute.policy``/``citypods.config`` import
until it actually needs the site-config fallback -- an explicit ``CITYPODS_BEAM_GPU`` override
must short-circuit without ever touching ``config/site_config.yml``.

The ``beam`` SDK isn't installed in this dev/test environment (and never needs to be, per the
module's own docstring), so the module is imported here via ``importlib`` against a lightweight
stand-in ``beam`` module, mirroring the ``_load_script`` pattern used elsewhere in this test suite
(see ``tests/test_reclaim.py``) for loading non-package scripts with heavy runtime dependencies.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_BEAM_APP_PATH = _REPO_ROOT / "scripts" / "compute" / "beam_app.py"
_CONSTRAINTS_PATH = _REPO_ROOT / "constraints" / "asr.txt"

# Mirrors beam_app.py's _TRANSCRIBE_PACKAGES pin list. _pinned_versions() runs unconditionally at
# import time (regardless of which GPU branch _resolve_gpu() takes), so every name it looks up
# afterwards must resolve -- kept as a literal copy rather than importing the real module (which
# is exactly what we're trying to make importable).
_TRANSCRIBE_PACKAGES = [
    "boto3",
    "botocore",
    "certifi",
    "charset-normalizer",
    "click",
    "defusedxml",
    "filelock",
    "flatbuffers",
    "fsspec",
    "h11",
    "hf-xet",
    "httpcore",
    "httpx",
    "huggingface-hub",
    "idna",
    "jinja2",
    "jmespath",
    "markupsafe",
    "numpy",
    "onnxruntime",
    "packaging",
    "pillow",
    "protobuf",
    "python-dateutil",
    "pyyaml",
    "requests",
    "s3transfer",
    "setuptools",
    "six",
    "tokenizers",
    "tqdm",
    "typing-extensions",
    "urllib3",
    "anyio",
    "av",
    "ctranslate2",
    "faster-whisper",
]
_FAKE_CONSTRAINTS_TEXT = "\n".join(f"{name}==0.0.0" for name in _TRANSCRIBE_PACKAGES)


def _install_fake_beam_sdk() -> ModuleType:
    beam_mod = ModuleType("beam")
    beam_mod.Image = MagicMock(name="beam.Image")
    # A real beam.schedule(...) call decorates run_scheduled; identity-decorate so the function
    # stays intact without needing beam's real scheduling machinery.
    beam_mod.schedule = lambda **kwargs: (lambda fn: fn)
    sys.modules["beam"] = beam_mod
    return beam_mod


@pytest.fixture
def beam_app_module(monkeypatch):
    _install_fake_beam_sdk()

    # Module-level `GPU = _resolve_gpu()` and the pinned-dependency parsing both run
    # unconditionally at import time. Give GPU resolution a deterministic env override so import
    # succeeds without requiring a real config/site_config.yml on disk; the fallback branch is
    # exercised directly against `_resolve_gpu()` in the tests below instead.
    monkeypatch.setenv("CITYPODS_BEAM_GPU", "IMPORT-TIME-PLACEHOLDER")

    original_read_text = Path.read_text

    def _fake_read_text(self, *args, **kwargs):
        if self == _CONSTRAINTS_PATH:
            return _FAKE_CONSTRAINTS_TEXT
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _fake_read_text)

    sys.modules.pop("scripts.compute.beam_app", None)
    spec = importlib.util.spec_from_file_location("scripts.compute.beam_app", _BEAM_APP_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["scripts.compute.beam_app"] = module
    spec.loader.exec_module(module)
    try:
        yield module
    finally:
        sys.modules.pop("scripts.compute.beam_app", None)
        sys.modules.pop("beam", None)


def test_resolve_gpu_prefers_explicit_env_override(beam_app_module, monkeypatch):
    monkeypatch.setenv("CITYPODS_BEAM_GPU", "A10G")
    assert beam_app_module._resolve_gpu() == "A10G"


def test_resolve_gpu_env_override_short_circuits_policy_lookup(beam_app_module, monkeypatch):
    """When CITYPODS_BEAM_GPU is set, _resolve_gpu() must not import/call the policy machinery at
    all -- the whole point of deferring the import is to let a one-off canary override the GPU
    without needing config/site_config.yml on the caller's machine."""
    monkeypatch.setenv("CITYPODS_BEAM_GPU", "A10G")

    def _boom(*_a, **_k):
        raise AssertionError("backend_policy/load_site_config must not be called")

    monkeypatch.setattr("citypods.compute.policy.backend_policy", _boom)
    monkeypatch.setattr("citypods.config.load_site_config", _boom)

    assert beam_app_module._resolve_gpu() == "A10G"


def test_resolve_gpu_falls_back_to_site_policy_when_env_unset(beam_app_module, monkeypatch):
    monkeypatch.delenv("CITYPODS_BEAM_GPU", raising=False)
    monkeypatch.setattr(
        "citypods.config.load_site_config",
        lambda path: {
            "defaults": {"compute_backends": {"beam": {"hardware": {"gpu_type": "A100"}}}}
        },
    )

    assert beam_app_module._resolve_gpu() == "A100"


def test_resolve_gpu_falls_back_to_hardcoded_default_when_policy_has_no_gpu_type(
    beam_app_module, monkeypatch
):
    monkeypatch.delenv("CITYPODS_BEAM_GPU", raising=False)
    monkeypatch.setattr("citypods.config.load_site_config", lambda path: {"defaults": {}})

    assert beam_app_module._resolve_gpu() == "RTX4090"


def test_resolve_gpu_empty_string_env_override_does_not_shadow_policy(beam_app_module, monkeypatch):
    """An empty-string override (e.g. an unset GH Actions env interpolation) is falsy and must
    fall through to the site policy, not be treated as an explicit choice of ''."""
    monkeypatch.setenv("CITYPODS_BEAM_GPU", "")
    monkeypatch.setattr(
        "citypods.config.load_site_config",
        lambda path: {
            "defaults": {"compute_backends": {"beam": {"hardware": {"gpu_type": "A100"}}}}
        },
    )

    assert beam_app_module._resolve_gpu() == "A100"


def test_resolve_gpu_loads_site_config_from_the_canonical_relative_path(
    beam_app_module, monkeypatch
):
    calls: dict[str, object] = {}

    def _fake_load(path):
        calls["path"] = path
        return {"defaults": {}}

    def _fake_policy(site_config, backend):
        calls["backend"] = backend
        return SimpleNamespace(hardware=SimpleNamespace(gpu_type=""))

    monkeypatch.delenv("CITYPODS_BEAM_GPU", raising=False)
    monkeypatch.setattr("citypods.config.load_site_config", _fake_load)
    monkeypatch.setattr("citypods.compute.policy.backend_policy", _fake_policy)

    beam_app_module._resolve_gpu()

    assert calls["path"] == "config/site_config.yml"
    assert calls["backend"] == "beam"


def test_module_level_gpu_and_runtime_env_reflect_import_time_resolution(beam_app_module):
    """GPU/RUNTIME_ENV are computed once at import time from _resolve_gpu()'s result and wired
    into the schedule() call's env/gpu kwargs; confirm the resolved value actually propagates."""
    assert beam_app_module.GPU == "IMPORT-TIME-PLACEHOLDER"
    assert beam_app_module.RUNTIME_ENV["CITYPODS_WORKER_GPU_TYPE"] == "IMPORT-TIME-PLACEHOLDER"