from __future__ import annotations

import sys
import types

import pytest

from scripts.preflight_diarization import configured_models, run_preflight


@pytest.fixture
def sherpa_installed(monkeypatch):
    """Satisfy the preflight's sherpa-onnx presence probe without the `[diarize]` extra.

    CI installs `[dev,wer,llm]`, not the diarize runtime, so the tests past that probe -- which
    are about model download/validation, not about sherpa's own behaviour -- would otherwise
    fail there while passing on any developer machine that happens to have it. A stub keeps them
    honest in both places; the probe only asks whether the import succeeds.
    """
    if "sherpa_onnx" not in sys.modules:
        monkeypatch.setitem(sys.modules, "sherpa_onnx", types.ModuleType("sherpa_onnx"))


def test_configured_models_requires_speakers_enabled():
    with pytest.raises(RuntimeError, match="speakers.enabled must be true"):
        configured_models({"speakers": {"enabled": False}})


def test_configured_models_requires_expected_diarize_model():
    with pytest.raises(RuntimeError, match="R7 model must be"):
        configured_models(
            {"speakers": {"enabled": True, "model": "wrong-model", "embedding_model": "x"}}
        )


def test_configured_models_requires_embedding_model():
    from citypods.diarize import DEFAULT_DIARIZE_MODEL

    with pytest.raises(RuntimeError, match="embedding_model is required"):
        configured_models(
            {"speakers": {"enabled": True, "model": DEFAULT_DIARIZE_MODEL, "embedding_model": ""}}
        )


def test_configured_models_returns_the_pair():
    from citypods.diarize import DEFAULT_DIARIZE_MODEL

    model, embedding_model = configured_models(
        {
            "speakers": {
                "enabled": True,
                "model": DEFAULT_DIARIZE_MODEL,
                "embedding_model": "nemo-titanet-small",
            }
        }
    )
    assert model == DEFAULT_DIARIZE_MODEL
    assert embedding_model == "nemo-titanet-small"


def test_run_preflight_rejects_unknown_embedding_recipe(tmp_path, monkeypatch):
    from citypods.diarize import DEFAULT_DIARIZE_MODEL

    site_config = tmp_path / "site_config.yml"
    site_config.write_text(
        f"speakers:\n  enabled: true\n  model: {DEFAULT_DIARIZE_MODEL}\n"
        "  embedding_model: not-a-real-recipe\n"
    )
    with pytest.raises(RuntimeError, match="is not a known recipe"):
        run_preflight(str(site_config))


def test_run_preflight_downloads_and_validates_both_models(tmp_path, monkeypatch, sherpa_installed):
    from citypods.diarize import DEFAULT_DIARIZE_MODEL

    site_config = tmp_path / "site_config.yml"
    site_config.write_text(
        f"speakers:\n  enabled: true\n  model: {DEFAULT_DIARIZE_MODEL}\n"
        "  embedding_model: nemo-titanet-small\n"
    )
    calls: list[str] = []
    monkeypatch.setattr(
        "citypods.diarize._ensure_segmentation_model", lambda: calls.append("segmentation")
    )
    monkeypatch.setattr(
        "citypods.diarize._ensure_embedding_model", lambda name: calls.append(f"embedding:{name}")
    )
    model, embedding_model = run_preflight(str(site_config))
    assert model == DEFAULT_DIARIZE_MODEL
    assert embedding_model == "nemo-titanet-small"
    assert calls == ["segmentation", "embedding:nemo-titanet-small"]


def test_run_preflight_explains_a_download_failure(tmp_path, monkeypatch, sherpa_installed):
    from citypods.diarize import DEFAULT_DIARIZE_MODEL

    site_config = tmp_path / "site_config.yml"
    site_config.write_text(
        f"speakers:\n  enabled: true\n  model: {DEFAULT_DIARIZE_MODEL}\n"
        "  embedding_model: nemo-titanet-small\n"
    )

    def _boom():
        raise RuntimeError("network unreachable")

    monkeypatch.setattr("citypods.diarize._ensure_segmentation_model", _boom)
    with pytest.raises(RuntimeError, match="Could not download/validate the pyannote-segmentation"):
        run_preflight(str(site_config))
