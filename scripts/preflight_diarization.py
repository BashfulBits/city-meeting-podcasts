#!/usr/bin/env python3
"""Fail-fast checks for the R7 native pyannote runtime.

This intentionally loads the configured pipeline and embedding model, but does not process
meeting audio.  It catches missing optional dependencies, missing Hugging Face access, and
unaccepted gated model terms before a long diarization lane starts.  The lane itself reuses the
Hugging Face cache populated here.
"""

from __future__ import annotations

import argparse
import os
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
    token = os.environ.get("HF_TOKEN")
    if not token:
        raise RuntimeError("HF_TOKEN is required to load the gated pyannote models")

    try:
        from pyannote.audio import Model, Pipeline
    except ImportError as exc:
        raise RuntimeError(
            "pyannote.audio is not installed; install the pinned [asr] runtime"
        ) from exc

    try:
        Pipeline.from_pretrained(model, token=token)
        Model.from_pretrained(embedding_model, token=token)
    except Exception as exc:  # noqa: BLE001 - convert provider errors to an actionable preflight.
        raise RuntimeError(
            "Hugging Face could not load the configured pyannote models. "
            "Accept the model terms and verify HF_TOKEN access."
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
