"""Unit tests for citypods/bench.py (the asr-bench diagnostic command)."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
import requests

from citypods.bench import _get_ref_text
from citypods.models import Episode


def _ep(**overrides):
    base = dict(
        guid="g1",
        title="Meeting",
        published=datetime(2026, 5, 1, tzinfo=UTC),
        video_url="https://x/v.mp4",
        transcript_hosted_url="https://cdn/t.vtt",
        transcript_format="vtt",
    )
    base.update(overrides)
    return Episode(**base)


class _FakeResponse:
    def __init__(self, status_code=200, content=b""):
        self.status_code = status_code
        self.content = content


@contextmanager
def _fake_session(response=None, exc=None):
    class _Session:
        def get(self, url, timeout=30):
            if exc is not None:
                raise exc
            return response

    yield _Session()


def test_returns_none_without_hosted_transcript():
    ep = _ep(transcript_hosted_url=None)
    assert _get_ref_text(ep) is None


def test_returns_stripped_text_for_txt_format(monkeypatch):
    ep = _ep(transcript_format="txt")
    monkeypatch.setattr(
        "citypods.http.make_session",
        lambda: _fake_session(_FakeResponse(200, b"  hello world  \n")),
    )
    assert _get_ref_text(ep) == "hello world"


def test_returns_none_on_non_200_status(monkeypatch):
    ep = _ep()
    monkeypatch.setattr(
        "citypods.http.make_session", lambda: _fake_session(_FakeResponse(404, b""))
    )
    assert _get_ref_text(ep) is None


def test_request_exception_returns_none_instead_of_propagating(monkeypatch):
    # The one real failure mode this function should swallow: a network-level fetch failure.
    monkeypatch.setattr(
        "citypods.http.make_session",
        lambda: _fake_session(exc=requests.ConnectionError("boom")),
    )
    assert _get_ref_text(_ep()) is None


def test_non_request_exception_propagates_instead_of_being_swallowed(monkeypatch):
    # CR2-CP-42: a bare `except Exception` used to swallow this identically to "no transcript" —
    # a bug unrelated to the network fetch must now surface instead of vanishing silently.
    monkeypatch.setattr(
        "citypods.http.make_session",
        lambda: _fake_session(exc=RuntimeError("unexpected bug")),
    )
    with pytest.raises(RuntimeError, match="unexpected bug"):
        _get_ref_text(_ep())


def test_download_hosted_audio_public_alias_matches_internal_helper():
    # CR2-CP-39: bench.py must not import stages._download_audio directly across the module
    # boundary — a public alias exists for exactly this cross-module use.
    from citypods import stages

    assert stages.download_hosted_audio is stages._download_audio


def test_run_bench_no_models(capsys):
    from citypods.bench import run_bench

    ret = run_bench("city-slug", "ep-uid", [])
    assert ret == 1
    captured = capsys.readouterr()
    assert "No models specified." in captured.out


def test_run_bench_missing_city(capsys, monkeypatch):
    from citypods.bench import run_bench

    monkeypatch.setattr("citypods.text_metrics.require_jiwer", lambda: None)
    monkeypatch.setattr("citypods.config.load_site_config", lambda path: {})
    monkeypatch.setattr("citypods.config.load_city_configs", lambda path, defaults: [])

    ret = run_bench("city-slug", "ep-uid", ["base.en"])
    assert ret == 1
    captured = capsys.readouterr()
    assert "City not found: 'city-slug'" in captured.out


def test_bench_model_import_error(capsys, monkeypatch):
    from citypods.bench import _bench_model

    def mock_transcribe(*args, **kwargs):
        raise ImportError("faster-whisper not installed")

    monkeypatch.setattr("citypods.asr.transcribe", mock_transcribe)

    res = _bench_model(
        "base.en",
        "/tmp/audio.mp3",
        "prompt",
        "hello world",
        float("inf"),
        10,
        compute_type="int8",
        beam_size=5,
        language="en",
        cpu_threads=4,
    )
    assert res is None
    captured = capsys.readouterr()
    assert "faster-whisper not installed" in captured.out
