#!/usr/bin/env python3
"""Fail-fast checks for the R7 native sherpa-onnx diarization runtime.

This intentionally downloads and validates the configured segmentation + embedding models but
does not process meeting audio.  It catches a missing `sherpa-onnx` install, a misconfigured
model name, or a network/GitHub-release problem before a long diarization lane starts.  The
lane itself reuses the model cache populated here (`CITYPODS_DIARIZE_MODEL_CACHE`, default
`~/.cache/citypods-diarize`).

Unlike the pyannote-audio engine this superseded (review/31 §A.1a), neither model is
Hugging-Face-gated, so there is no token/auth step -- the only failure modes left are "the
dependency isn't installed" and "the fixed download URL isn't reachable right now."
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping

from citypods.config import load_site_config
from citypods.diarize import DEFAULT_DIARIZE_MODEL


def configured_models(site_config: Mapping[str, object]) -> tuple[str, str]:
    speakers = site_config.get("speakers")
    if not isinstance(speakers, Mapping) or not speakers.get("enabled"):
        raise RuntimeError("R7 speakers.enabled must be true for the diarization lane")
    model = str(speakers.get("model") or "")
    embedding_model = str(speakers.get("embedding_model") or "")
    if model != DEFAULT_DIARIZE_MODEL:
        raise RuntimeError(
            f"R7 model must be {DEFAULT_DIARIZE_MODEL!r}; configured {model or '<empty>'!r}"
        )
    if not embedding_model:
        raise RuntimeError("R7 speakers.embedding_model is required")
    return model, embedding_model


def run_preflight(site_config_path: str = "config/site_config.yml") -> tuple[str, str]:
    model, embedding_model = configured_models(load_site_config(site_config_path))

    from citypods.diarize import (
        _EMBEDDING_RECIPES,
        _ensure_embedding_model,
        _ensure_segmentation_model,
    )

    # Config checks first, then the dependency: cheapest and most specific diagnosis wins, and a
    # config typo must not be reported as a missing install. It also lets CI exercise these
    # branches without the `[diarize]` extra.
    if embedding_model not in _EMBEDDING_RECIPES:
        raise RuntimeError(
            f"R7 speakers.embedding_model {embedding_model!r} is not a known recipe; "
            f"choose one of {sorted(_EMBEDDING_RECIPES)}"
        )

    try:
        import sherpa_onnx  # noqa: F401
    except ImportError as exc:
        raise RuntimeError("sherpa-onnx is required; install the pinned [diarize] runtime") from exc

    try:
        _ensure_segmentation_model()
    except Exception as exc:  # noqa: BLE001 - retain a specific diagnosis for CI logs.
        raise RuntimeError(
            "Could not download/validate the pyannote-segmentation-3.0 ONNX model. Retry the "
            "preflight once the sherpa-onnx GitHub release is reachable."
        ) from exc
    try:
        _ensure_embedding_model(embedding_model)
    except Exception as exc:  # noqa: BLE001 - retain a specific diagnosis for CI logs.
        raise RuntimeError(
            f"Could not download/validate the configured embedding model {embedding_model!r}. "
            "Retry the preflight once the sherpa-onnx GitHub release is reachable."
        ) from exc
    return model, embedding_model


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site-config", default="config/site_config.yml")
    args = parser.parse_args(argv)
    try:
        model, embedding_model = run_preflight(args.site_config)
    except RuntimeError as exc:
        print(f"R7 diarization preflight failed: {exc}", file=sys.stderr)
        return 1
    print(f"R7 diarization preflight passed: {model} + {embedding_model}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
