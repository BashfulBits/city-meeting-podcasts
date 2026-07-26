from __future__ import annotations

import pytest

from citypods.granicus_proxy import GranicusWorkerFallback, redact_worker_endpoint

ARCHIVE_URL = (
    "https://archive-video.granicus.com/arlingtontx/"
    "arlingtontx_f65c7a2f-9c73-4d9b-b7b7-205f7c12c0bf.mp4"
)
WORKER_URL = (
    "https://worker.example/v1/archive/arlingtontx/"
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


def test_proxy_url_accepts_bounded_granicus_provider_requests(monkeypatch):
    fallback = _fallback(monkeypatch)
    assert fallback.proxy_url(
        "https://arlingtontx.granicus.com/ViewPublisherRSS.php?view_id=2&mode=vpodcast"
    ) == (
        "https://worker.example/v1/granicus/arlingtontx.granicus.com/"
        "ViewPublisherRSS.php?view_id=2&mode=vpodcast"
    )
    assert fallback.proxy_url(
        "https://arlingtontx.granicus.com/DownloadFile.php?clip_id=2"
    ) == (
        "https://worker.example/v1/granicus/arlingtontx.granicus.com/"
        "DownloadFile.php?clip_id=2"
    )
    too_many_query_pairs = "&".join(f"view_id={index}" for index in range(9))
    assert (
        fallback.proxy_url(
            f"https://arlingtontx.granicus.com/JSON.php?{too_many_query_pairs}"
        )
        is None
    )
    assert (
        fallback.proxy_url(
            "https://arlingtontx.granicus.com/JSON.php?view_id=" + "x" * 241
        )
        is None
    )
    assert fallback.proxy_url("https://arlingtontx.granicus.com/private") is None
    assert fallback.proxy_url("https://evil.example/Archive.php?view_id=2") is None
    assert fallback.proxy_url("https://arlingtontx.granicus.com/Archive.php?token=secret") is None
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
        WORKER_URL,
        "-vn",
        "out.m4a",
    ]


def test_redact_worker_endpoint_scrubs_only_worker_urls():
    worker_cmd = [
        "ffmpeg",
        "-headers",
        "Authorization: Bearer secret-token\r\n",
        "-i",
        WORKER_URL,
        "out.mka",
    ]
    # ffmpeg echoes the failing input URL; the Worker endpoint must not survive into a log line.
    scrubbed = redact_worker_endpoint(f"{WORKER_URL}: Server returned 403 Forbidden", worker_cmd)
    assert "worker.example" not in scrubbed
    assert scrubbed == "<granicus-worker>: Server returned 403 Forbidden"

    # The bearer token is not a URL, so the scrubber never touches it (and it is never in stderr).
    assert "secret-token" not in redact_worker_endpoint("Authorization redacted", worker_cmd)

    # A direct (non-Worker) command has no /v1/archive/ HTTPS input → stderr is returned unchanged.
    direct_cmd = ["ffmpeg", "-i", ARCHIVE_URL, "out.mka"]
    direct_stderr = f"{ARCHIVE_URL}: Server returned 403 Forbidden"
    assert redact_worker_endpoint(direct_stderr, direct_cmd) == direct_stderr
