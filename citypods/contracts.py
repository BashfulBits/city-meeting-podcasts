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


def _is_spa_seek_url(url: str) -> bool:
    """True for a path-style time anchor like ``…/play/{id}/{seconds}`` (no query string) — a
    client-side SPA route that a server typically 404s on a direct HEAD/GET even though it works in
    the browser (Swagit's player). Query-param anchors (Granicus ``…?starttime=``) are not SPA."""
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    seg = parts.path.rstrip("/").rsplit("/", 1)
    return len(seg) == 2 and seg[1].isdigit() and not parts.query


def _tail(text: str, *, limit: int = 700) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return "..." + text[-limit:]


def _safe_url(url: str) -> str:
    """Strip a presigned/credentialed query string before a URL can reach a public GitHub
    issue or CheckResult.detail (CR2-CP-28/MR-CP-04) — every "detail becomes a public issue"
    call site in this module routes through this, not a raw ``resolved_url[:N]`` truncation."""
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    safe = f"{parts.scheme}://{parts.netloc}{parts.path}"
    if parts.query:
        safe += "?<redacted>"
    return safe


def _media_fetch_detail(
    *,
    resolved_url: str,
    size: int,
    seconds: float,
    ok: bool,
    logs: list[str],
) -> str:
    base = f"{size}B from first {seconds:g}s"
    if ok:
        return base

    details = [base, f"url={_safe_url(resolved_url)}"]
    if logs:
        details.append(f"ffmpeg={_tail(logs[-1])}")
    else:
        details.append("ffmpeg=no diagnostic log captured")
    return "\n".join(details)


def check_city(slug: str, provider_name: str, source: dict) -> list[CheckResult]:
    """Run the contract checks applicable to one city's provider. Each check is isolated so one
    broken endpoint doesn't mask the others."""
    out: list[CheckResult] = []

    # get_provider raises ProviderError for an unregistered name; keep it inside the same
    # isolation this function's docstring promises (CR2-SC-03) — an unregistered provider must
    # produce a "list" failure result for this one city, not abort the caller's whole scan.
    try:
        provider = get_provider(provider_name)
    except Exception as exc:  # noqa: BLE001 — the failure IS the finding
        return [_r(provider_name, slug, "list", False, repr(exc))]

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
        out.append(_r(provider_name, slug, "media", ok, _safe_url(resolved_url)[:80]))
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
                    dest = Path(td) / "probe.mka"
                    logs: list[str] = []
                    ok = _download_audio(
                        resolved_url,
                        dest,
                        max_seconds=_MEDIA_FETCH_SECONDS,
                        log=logs.append,
                    )
                    size = dest.stat().st_size if dest.exists() else 0
                    media_ok = bool(ok) and size > 0
                    out.append(
                        _r(
                            provider_name,
                            slug,
                            "media-fetch",
                            media_ok,
                            _media_fetch_detail(
                                resolved_url=resolved_url,
                                size=size,
                                seconds=_MEDIA_FETCH_SECONDS,
                                ok=media_ok,
                                logs=logs,
                            ),
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

    # 4. View counts (Granicus) — the cap probe used by the audit.  An empty result is a valid
    #    "not applicable" response for uncapped archive-backed providers, so reaching this method
    #    successfully is the contract rather than finding a saturated view.
    fetch_view_counts = getattr(provider, "fetch_view_counts", None)
    if fetch_view_counts is not None:
        try:
            counts = fetch_view_counts(source)
            out.append(_r(provider_name, slug, "view_counts", True, str(counts)))
        except Exception as exc:  # noqa: BLE001
            out.append(_r(provider_name, slug, "view_counts", False, repr(exc)))

    # 5. Video deep-link — page-liveness / URL-scheme check for providers that declare "deeplink".
    #    A HEAD confirms a server-resolvable player is alive (Granicus MediaPlayer.php?starttime=).
    #    Swagit's /play/{id}/{t} is a client-side SPA route: the server 404s it on a direct request
    #    (even the real chapter-anchor timestamps the watch page links) though it works in-browser.
    #    So on a 4xx for an SPA-style path-timestamp deeplink, fall back to confirming the scheme is
    #    still current by finding the deeplink's path on the live watch page. Either way catches a
    #    dead/relocated player or a changed scheme; the time anchor itself isn't server-verifiable.
    capabilities = getattr(provider, "capabilities", frozenset())
    if "deeplink" in capabilities and episodes:
        ref = (newest.links or {}).get("canonical_video") or newest.video_url
        video_deeplink = getattr(provider, "video_deeplink", None)
        if video_deeplink is not None:
            try:
                url = video_deeplink(ref, 30.0)  # sample: 30 seconds in
                if not url:
                    out.append(
                        _r(provider_name, slug, "deeplink", False, "returned None for a valid ref")
                    )
                else:
                    from urllib.parse import urlsplit

                    from citypods.http import make_session

                    with make_session() as sess:
                        resp = sess.head(url, timeout=10, allow_redirects=True)
                        ok = resp.status_code < 400
                        detail = f"page {resp.status_code} (seek not verified)"
                        if not ok and _is_spa_seek_url(url):
                            # SPA route: a server 4xx is expected. Confirm the scheme is current by
                            # finding the deeplink's path prefix (…/play/{id}) in the watch page.
                            path_prefix = urlsplit(url.rsplit("/", 1)[0]).path
                            page = sess.get(ref, timeout=10)
                            if page.status_code < 400 and path_prefix and path_prefix in page.text:
                                ok = True
                                detail = (
                                    f"SPA seek route (server {resp.status_code}; scheme current)"
                                )
                    out.append(_r(provider_name, slug, "deeplink", ok, f"{detail} {url[:50]}"))
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
