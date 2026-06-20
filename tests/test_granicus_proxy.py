from __future__ import annotations

import pytest

from citypods.granicus_proxy import GranicusWorkerFallback

ARCHIVE_URL = (
    "https://archive-video.granicus.com/arlingtontx/"
    "arlingtontx_f65c7a2f-9c73-4d9b-b7b7-205f7c12c0bf.mp4"
)


def _fallback(monkeypatch) -> GranicusWorkerFallback:
    monkeypatch.setenv("GRANICUS_PROXY_BASE_URL", "worker.example")
    monkeypatch.setenv("GRANICUS_PROXY_TOKEN", "secret-token")
    monkeypatch.setattr("citypods.granicus_proxy.validate_source_url", lambda _url: None)
    fallback = GranicusWorkerFallback.from_env()
    assert fallback is not None
    return fallback


def test_from_env_requires_both_values(monkeypatch):
    monkeypatch.setenv("GRANICUS_PROXY_BASE_URL", "worker.example")
    monkeypatch.delenv("GRANICUS_PROXY_TOKEN", raising=False)
    with pytest.raises(ValueError, match="configured together"):
        GranicusWorkerFallback.from_env()


def test_proxy_url_accepts_only_canonical_native_granicus_archive(monkeypatch):
    fallback = _fallback(monkeypatch)
    assert fallback.proxy_url(ARCHIVE_URL) == (
        "https://worker.example/v1/archive/arlingtontx/"
        "arlingtontx_f65c7a2f-9c73-4d9b-b7b7-205f7c12c0bf.mp4"
    )
    assert fallback.proxy_url("https://example.com/video.mp4") is None
    assert fallback.proxy_url(f"{ARCHIVE_URL}?token=secret") is None
    assert (
        fallback.proxy_url("https://archive-video.granicus.com/arlingtontx/other_tenant.mp4")
        is None
    )


def test_rewrite_adds_auth_only_to_the_worker_input(monkeypatch):
    fallback = _fallback(monkeypatch)
    command = ["ffmpeg", "-user_agent", "browser", "-i", ARCHIVE_URL, "-vn", "out.m4a"]

    rewritten = fallback.rewrite_ffmpeg_command(command, (ARCHIVE_URL,))

    assert rewritten == [
        "ffmpeg",
        "-user_agent",
        "browser",
        "-headers",
        "Authorization: Bearer secret-token\r\n",
        "-i",
        "https://worker.example/v1/archive/arlingtontx/"
        "arlingtontx_f65c7a2f-9c73-4d9b-b7b7-205f7c12c0bf.mp4",
        "-vn",
        "out.m4a",
    ]
