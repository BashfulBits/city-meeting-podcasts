from __future__ import annotations

import pytest
from requests import ConnectionError, Timeout
from requests.exceptions import ChunkedEncodingError

from scripts.preflight_diarization import _load_model, _verify_hf_token


class FakeHubHTTPError(Exception):
    def __init__(self, status_code: int | None = None):
        self.response = type("Response", (), {"status_code": status_code})()


class FakeGatedRepoError(FakeHubHTTPError):
    pass


class FakeRepositoryNotFoundError(FakeHubHTTPError):
    pass


def test_verify_hf_token_explains_an_invalid_token():
    class FakeApi:
        @staticmethod
        def whoami():
            raise FakeHubHTTPError(401)

    with pytest.raises(RuntimeError, match="HF_TOKEN is invalid or no longer authorized"):
        _verify_hf_token(FakeApi(), FakeHubHTTPError)


def test_verify_hf_token_separates_hub_availability_from_token_validity():
    class FakeApi:
        @staticmethod
        def whoami():
            raise FakeHubHTTPError(503)

    with pytest.raises(RuntimeError, match="Hugging Face could not verify HF_TOKEN"):
        _verify_hf_token(FakeApi(), FakeHubHTTPError)


def test_load_model_explains_gated_model_access():
    def loader(_model, *, token):
        assert token == "hf-test"
        raise FakeGatedRepoError()

    with pytest.raises(RuntimeError, match="lacks access to the gated diarization pipeline"):
        _load_model(
            loader,
            "pyannote/speaker-diarization-community-1",
            label="diarization pipeline",
            token="hf-test",
            gated_repo_error=FakeGatedRepoError,
            repository_not_found_error=FakeRepositoryNotFoundError,
            hub_http_error=FakeHubHTTPError,
        )


def test_load_model_explains_unknown_or_inaccessible_model():
    def loader(_model, *, token):
        assert token == "hf-test"
        raise FakeRepositoryNotFoundError()

    with pytest.raises(RuntimeError, match="was not found or is not accessible to HF_TOKEN"):
        _load_model(
            loader,
            "pyannote/embedding",
            label="embedding model",
            token="hf-test",
            gated_repo_error=FakeGatedRepoError,
            repository_not_found_error=FakeRepositoryNotFoundError,
            hub_http_error=FakeHubHTTPError,
        )


@pytest.mark.parametrize(
    ("status_code", "message"),
    [
        (403, "Hugging Face denied download access"),
        (429, "Hugging Face rate-limited download"),
    ],
)
def test_load_model_separates_access_denial_and_rate_limits(status_code, message):
    def loader(_model, *, token):
        assert token == "hf-test"
        raise FakeHubHTTPError(status_code)

    with pytest.raises(RuntimeError, match=message):
        _load_model(
            loader,
            "pyannote/embedding",
            label="embedding model",
            token="hf-test",
            gated_repo_error=FakeGatedRepoError,
            repository_not_found_error=FakeRepositoryNotFoundError,
            hub_http_error=FakeHubHTTPError,
        )


@pytest.mark.parametrize("transport_error", [Timeout, ConnectionError, ChunkedEncodingError])
def test_load_model_separates_hugging_face_transport_failures(transport_error):
    def loader(_model, *, token):
        assert token == "hf-test"
        raise transport_error()

    with pytest.raises(RuntimeError, match="Network transport failed"):
        _load_model(
            loader,
            "pyannote/embedding",
            label="embedding model",
            token="hf-test",
            gated_repo_error=FakeGatedRepoError,
            repository_not_found_error=FakeRepositoryNotFoundError,
            hub_http_error=FakeHubHTTPError,
            transport_errors=(Timeout, ConnectionError, ChunkedEncodingError),
        )


def test_load_model_preserves_runtime_failure_after_access_succeeds():
    def loader(_model, *, token):
        assert token == "hf-test"
        raise ValueError("incompatible model")

    with pytest.raises(RuntimeError, match="after Hugging Face access succeeded"):
        _load_model(
            loader,
            "pyannote/embedding",
            label="embedding model",
            token="hf-test",
            gated_repo_error=FakeGatedRepoError,
            repository_not_found_error=FakeRepositoryNotFoundError,
            hub_http_error=FakeHubHTTPError,
        )
