"""Live endpoint contract checks.

Every provider integration is a scrape or undocumented API; when a platform changes its
HTML/JSON we want to learn *which endpoint/pattern* broke, not just "a feed went empty". These
checks hit the real upstream through the existing provider methods and assert the minimal shape
the pipeline depends on. They are the *input* contract; the feed-health audit checks the *output*.

Shared by ``tests/live/test_contracts.py`` (opt-in ``-m live``) and ``scripts/check_endpoints.py``
(the operational monitor that can file GitHub issues). Network-touching by design — never run in
the default offline test suite.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from citypods.providers import get_provider

# Seconds of audio the media-fetch check copies — a *truncated* download that proves the endpoint is
# reachable + serves real media without pulling a whole meeting.
_MEDIA_FETCH_SECONDS = 3.0


@dataclass
class CheckResult:
    provider: str
    slug: str
    endpoint: str
    ok: bool
    detail: str


def _r(provider, slug, endpoint, ok, detail=""):
    return CheckResult(provider, slug, endpoint, ok, detail)


def check_city(slug: str, provider_name: str, source: dict) -> list[CheckResult]:
    """Run the contract checks applicable to one city's provider. Each check is isolated so one
    broken endpoint doesn't mask the others."""
    provider = get_provider(provider_name)
    out: list[CheckResult] = []

    # 1. Listing endpoint — must yield episodes with a usable media reference.
    try:
        episodes = provider.fetch_episodes(source)
        ok = bool(episodes) and all(e.video_url for e in episodes[:5])
        out.append(_r(provider_name, slug, "list", ok, f"{len(episodes)} episodes"))
    except Exception as exc:  # noqa: BLE001 — the failure IS the finding
        return [_r(provider_name, slug, "list", False, repr(exc))]

    if not episodes:
        return out
    newest = max(episodes, key=lambda e: e.published)

    # 2. Media resolution — the URL handed to ffmpeg must resolve (HLS chain / presigned MP4).
    resolved_url = ""
    try:
        resolved_url = provider.resolve_media_url(newest, source)
        ok = bool(resolved_url) and resolved_url.startswith("http")
        out.append(_r(provider_name, slug, "media", ok, resolved_url[:80]))
    except Exception as exc:  # noqa: BLE001
        out.append(_r(provider_name, slug, "media", False, repr(exc)))

    # 2b. Media FETCH — resolution returning a fine-looking URL is not enough: the CDN can still 403
    #     the actual byte fetch (e.g. Granicus blocks non-browser User-Agents). Truncated-download
    #     the first few seconds through the *production* fetch path (citypods.media._download_audio,
    #     same UA / protocol-whitelist / timeout ffmpeg uses in a real run) and require real bytes.
    #     This catches the silent "audio never downloads" class without log-diving.
    if resolved_url.startswith("http"):
        if shutil.which("ffmpeg") is None:
            out.append(_r(provider_name, slug, "media-fetch", True, "skipped (ffmpeg unavailable)"))
        else:
            from citypods.media import _download_audio

            try:
                with tempfile.TemporaryDirectory() as td:
                    dest = Path(td) / "probe.m4a"
                    ok = _download_audio(resolved_url, dest, max_seconds=_MEDIA_FETCH_SECONDS)
                    size = dest.stat().st_size if dest.exists() else 0
                    out.append(
                        _r(
                            provider_name,
                            slug,
                            "media-fetch",
                            bool(ok) and size > 0,
                            f"{size}B from first {_MEDIA_FETCH_SECONDS:g}s",
                        )
                    )
            except Exception as exc:  # noqa: BLE001
                out.append(_r(provider_name, slug, "media-fetch", False, repr(exc)))

    # 3. Chapters/transcript — only for providers that expose them; reachable + parseable.
    fetch_chapters = getattr(provider, "fetch_chapters", None)
    if fetch_chapters is not None:
        try:
            chapters, _transcript = fetch_chapters(newest, source)
            # endpoint reachable is the contract; an occasional clip with no index is fine, so we
            # only fail if the call raised. Surface the count for visibility.
            out.append(_r(provider_name, slug, "chapters", True, f"{len(chapters)} markers"))
        except Exception as exc:  # noqa: BLE001
            out.append(_r(provider_name, slug, "chapters", False, repr(exc)))

    # 4. View counts (Granicus) — the cap probe used by the audit.
    fetch_view_counts = getattr(provider, "fetch_view_counts", None)
    if fetch_view_counts is not None:
        try:
            counts = fetch_view_counts(source)
            out.append(_r(provider_name, slug, "view_counts", bool(counts), str(counts)))
        except Exception as exc:  # noqa: BLE001
            out.append(_r(provider_name, slug, "view_counts", False, repr(exc)))

    # 5. Video deep-link — sampled *page-liveness* check for providers that declare "deeplink".
    #    A HEAD on the player URL only confirms the page/redirect is alive; it does NOT verify
    #    the time anchor is honored (Granicus &starttime= and Swagit /play/{id}/{t} both return
    #    2xx regardless of the offset). So this catches a dead/relocated player, not a broken
    #    seek. Reuses ``newest`` from the listing check above (episodes is non-empty here).
    capabilities = getattr(provider, "capabilities", frozenset())
    if "deeplink" in capabilities and episodes:
        ref = (newest.links or {}).get("canonical_video") or newest.video_url
        video_deeplink = getattr(provider, "video_deeplink", None)
        if video_deeplink is not None:
            try:
                url = video_deeplink(ref, 30.0)  # sample: 30 seconds in
                if url:
                    from citypods.http import make_session

                    with make_session() as sess:
                        resp = sess.head(url, timeout=10, allow_redirects=True)
                    ok = resp.status_code < 400
                    out.append(
                        _r(
                            provider_name,
                            slug,
                            "deeplink",
                            ok,
                            f"page {resp.status_code} (seek not verified) {url[:50]}",
                        )
                    )
                else:
                    out.append(
                        _r(
                            provider_name,
                            slug,
                            "deeplink",
                            False,
                            "video_deeplink returned None for a valid ref",
                        )
                    )
            except Exception as exc:  # noqa: BLE001
                out.append(_r(provider_name, slug, "deeplink", False, repr(exc)))

    return out


def representative_cities(cities: list) -> list:
    """One city per provider (the first by slug) — enough to detect a platform-wide change without
    hammering every tenant. ``scripts/check_endpoints.py`` can override to scan all cities."""
    seen: dict[str, object] = {}
    for c in sorted(cities, key=lambda c: c.slug):
        seen.setdefault(c.provider, c)
    return list(seen.values())
