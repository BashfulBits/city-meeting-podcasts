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


def _verify_hf_token(api: object, hub_http_error: type[Exception]) -> None:
    """Verify the token before a gated-model error can obscure its cause."""
    try:
        api.whoami()  # type: ignore[attr-defined]
    except hub_http_error as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        if status_code in {401, 403}:
            raise RuntimeError(
                "HF_TOKEN is invalid or no longer authorized. Create a current read token for the "
                "Hugging Face account that accepted the pyannote model terms, then update the "
                "repository secret."
            ) from exc
        raise RuntimeError(
            "Hugging Face could not verify HF_TOKEN. Retry the preflight after the Hub is "
            "available."
        ) from exc


def _load_model(
    loader: object,
    model: str,
    *,
    label: str,
    token: str,
    gated_repo_error: type[Exception],
    repository_not_found_error: type[Exception],
    hub_http_error: type[Exception],
    transport_errors: tuple[type[Exception], ...] = (),
) -> None:
    """Load one configured pyannote resource with a diagnosis fit for Actions logs."""
    try:
        loader(model, token=token)  # type: ignore[operator]
    except gated_repo_error as exc:
        raise RuntimeError(
            f"HF_TOKEN is valid but lacks access to the gated {label} {model!r}. Accept the "
            f"terms at https://hf.co/{model} with the account that owns HF_TOKEN, then retry."
        ) from exc
    except repository_not_found_error as exc:
        raise RuntimeError(
            f"Configured {label} {model!r} was not found or is not accessible to HF_TOKEN. "
            "Check the configured model identifier and token access."
        ) from exc
    except transport_errors as exc:
        raise RuntimeError(
            f"Network transport failed while downloading the configured {label} {model!r}. "
            "Retry the preflight after Hugging Face access is available."
        ) from exc
    except hub_http_error as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        if status_code in {401, 403}:
            raise RuntimeError(
                f"Hugging Face denied download access to the configured {label} {model!r}. "
                "Verify HF_TOKEN access and the model's terms."
            ) from exc
        if status_code == 429:
            raise RuntimeError(
                f"Hugging Face rate-limited download of the configured {label} {model!r}. "
                "Wait for the limit to reset, then retry the preflight."
            ) from exc
        raise RuntimeError(
            f"Hugging Face could not download the configured {label} {model!r}. Retry the "
            "preflight after the Hub is available."
        ) from exc
    except Exception as exc:  # noqa: BLE001 - retain a specific diagnosis for runtime failures.
        raise RuntimeError(
            f"pyannote could not initialize the configured {label} {model!r} after Hugging Face "
            "access succeeded. Check the pinned diarization runtime."
        ) from exc


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
        from huggingface_hub import HfApi
        from huggingface_hub.errors import GatedRepoError, HfHubHTTPError, RepositoryNotFoundError
        from pyannote.audio import Model, Pipeline
        from requests import ConnectionError, Timeout
        from requests.exceptions import ChunkedEncodingError
    except ImportError as exc:
        raise RuntimeError(
            "pyannote.audio and huggingface_hub are required; install the pinned [asr] runtime"
        ) from exc

    _verify_hf_token(HfApi(token=token), HfHubHTTPError)
    _load_model(
        Pipeline.from_pretrained,
        model,
        label="diarization pipeline",
        token=token,
        gated_repo_error=GatedRepoError,
        repository_not_found_error=RepositoryNotFoundError,
        hub_http_error=HfHubHTTPError,
        transport_errors=(Timeout, ConnectionError, ChunkedEncodingError),
    )
    _load_model(
        Model.from_pretrained,
        embedding_model,
        label="embedding model",
        token=token,
        gated_repo_error=GatedRepoError,
        repository_not_found_error=RepositoryNotFoundError,
        hub_http_error=HfHubHTTPError,
        transport_errors=(Timeout, ConnectionError, ChunkedEncodingError),
    )
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
