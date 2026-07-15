"""Mandatory, fail-closed verification of LLM-classified civic platforms."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import yaml

from citypods.discovery.models import (
    Classification,
    DiscoveryRequest,
    DiscoveryResult,
    SearchResult,
    Verification,
)
from citypods.http import DEFAULT_TIMEOUT, make_session
from citypods.models import City, Episode
from citypods.providers import get_provider

_PROVIDER_ALIASES = {"primegov": "onemeeting", "agenda-pe": "granicus"}
_SIGNATURES: dict[str, tuple[str, ...]] = {
    "granicus": ("granicus.com", "ViewPublisher", "MediaPlayer.php"),
    "swagit": ("swagit.com",),
    "civicplus": ("RSSFeed.aspx", "CivicMedia", "tikilive"),
    "civicclerk": ("civicclerk.com",),
    "civicengage": ("AgendaCenter", "civicplus.com"),
    "legistar": ("legistar.com", "Calendar.aspx"),
    "onemeeting": ("primegov.com", "PublicPortal", "onemeeting"),
}


def _provider_name(platform: str | None) -> str | None:
    if platform is None:
        return None
    return _PROVIDER_ALIASES.get(platform, platform)


def _public_display_url(url: str) -> str:
    """Never persist a resolved signed-media query in a public issue artifact."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _signature_verified(platform: str, url: str) -> bool:
    """Fetch a candidate via the resolving SSRF gate and match a conservative signature."""
    needles = _SIGNATURES.get(platform)
    if not needles:
        return False
    try:
        with make_session() as session:
            response = session.get(url, timeout=DEFAULT_TIMEOUT, stream=True)
    except Exception:  # A failed or blocked candidate is not proposal evidence.
        return False
    if response.status_code >= 400:
        response.close()
        return False
    chunks: list[bytes] = []
    remaining = 200_000
    for chunk in response.iter_content(chunk_size=16_384):
        if not chunk:
            continue
        chunks.append(chunk[:remaining])
        remaining -= len(chunk)
        if remaining <= 0:
            break
    response.close()
    haystack = f"{response.url}\n{b''.join(chunks).decode(errors='replace')}".lower()
    return any(needle.lower() in haystack for needle in needles)


def _source_from_evidence(
    source: dict[str, Any] | None, *, results: list[SearchResult]
) -> dict[str, Any] | None:
    """Accept only the LLM's explicit, evidence-grounded provider source mapping.

    The classifier receives each adapter's actual required source schema.  Rechecking the URL
    provenance here makes the verification boundary robust even if a caller bypasses the parser.
    """
    if not source:
        return None
    retrieved = {row.url for row in results}
    for value in source.values():
        if (
            isinstance(value, str)
            and value.startswith(("http://", "https://"))
            and value not in retrieved
        ):
            return None
    return dict(source)


def _sample_media(provider_name: str, source: dict[str, Any]) -> tuple[str | None, str | None]:
    """Exercise the real adapter and then HEAD the resolved media through the SSRF session.

    The returned evidence URL is the stable public episode URL, not an expiring signed URL. The
    resolved URL is used only for the mandatory live HTTP verification.
    """
    try:
        provider = get_provider(provider_name)
        provider.validate(source)
        episodes = provider.fetch_episodes(source)
        if not episodes:
            return None, "provider returned no sample episodes"
        episode: Episode = episodes[0]
        resolved = provider.resolve_media_url(episode, source)
        with make_session() as session:
            response = session.head(resolved, timeout=DEFAULT_TIMEOUT, allow_redirects=True)
            if response.status_code >= 400:
                return None, f"resolved media returned HTTP {response.status_code}"
            content_type = response.headers.get("content-type", "").lower()
            if not any(
                token in content_type for token in ("video/", "audio/", "application/vnd.apple")
            ):
                return None, "resolved media did not advertise an audio/video content type"
        return _public_display_url(episode.video_url), ""
    except Exception:
        # Provider errors must not reveal externally supplied URLs or response bodies.
        return None, "provider could not resolve a playable sample"


def _aux_index_verified(provider_name: str, source: dict[str, Any]) -> tuple[bool, str]:
    try:
        provider = get_provider(provider_name)
        provider.validate(source)
        fetch = getattr(provider, "fetch_agenda_index", None)
        if not callable(fetch):
            return False, "provider does not expose an agenda index"
        records = fetch(source)
        return bool(records), "" if records else "agenda index returned no records"
    except Exception:
        return False, "provider could not retrieve an agenda index"


def _new_city_yaml(request: DiscoveryRequest, provider: str, source: dict[str, Any]) -> str:
    """Generate safe defaults approved by the maintainer; all values remain PR-reviewable."""
    city_label = f"{request.city_name}, {request.state}"
    entity = {
        "city_website": "",
        "meetings_url": "",
        "state": request.state,
    }
    feed = {
        "slug": request.city_slug,
        "city": request.city_slug,
        "provider": provider,
        "source": source,
        "podcast_title": f"{city_label} Public Meetings",
        "podcast_author": f"City of {city_label}",
        "podcast_email": "",
        "podcast_description": f"Official public meeting recordings from {city_label}.",
    }
    return (
        f"# config/cities/{request.city_slug}.yml\n"
        + yaml.safe_dump(entity, sort_keys=False)
        + f"\n# config/feeds/{request.city_slug}.yml\n"
        + yaml.safe_dump(feed, sort_keys=False)
    )


def _aux_yaml(city: City, provider: str, source: dict[str, Any]) -> str:
    target = (
        f"config/cities/{city.city_entity}.yml"
        if city.city_entity
        else f"config/feeds/{city.slug}.yml"
    )
    return f"# {target}\n" + yaml.safe_dump(
        {"aux_provider": provider, "aux_source": source}, sort_keys=False
    )


def _first_matching_url(platform: str, urls: tuple[str, ...]) -> str | None:
    for url in urls:
        if _signature_verified(platform, url):
            return url
    return None


def _source_urls(source: dict[str, Any] | None) -> tuple[str, ...]:
    if not source:
        return ()
    return tuple(
        value
        for value in source.values()
        if isinstance(value, str) and value.startswith(("http://", "https://"))
    )


def verify_discovery(
    request: DiscoveryRequest,
    classification: Classification,
    results: list[SearchResult],
    *,
    existing_city: City | None = None,
) -> DiscoveryResult:
    """Verify a discovery result and assemble a PR-safe or research-only evidence package."""
    needs_more_information = (
        request.mode == "new-city" and classification.city_identity != "confirmed"
    )
    if needs_more_information:
        return DiscoveryResult(
            request=request,
            search_results=tuple(results),
            classification=classification,
            verification=Verification(
                None,
                None,
                False,
                False,
                None,
                None,
                "retrieved evidence could not be confirmed for the requested city",
            ),
            research_only=False,
            needs_more_information=True,
            evidence_created_at=datetime.now(UTC).isoformat(),
        )
    agenda_platform = _provider_name(classification.agenda_platform)
    video_platform = _provider_name(classification.video_platform)
    city_url = request.city_website or next(
        (row.url for row in results if "city" in row.title.lower()), None
    )
    listing_url = classification.candidate_urls[0] if classification.candidate_urls else None
    if request.mode == "auxiliary":
        if existing_city is None:
            raise ValueError("auxiliary verification requires the current city configuration")
        if not agenda_platform:
            verification = Verification(None, None, False, False, None, None, "no agenda platform")
        else:
            source = _source_from_evidence(classification.agenda_source, results=results)
            signature_url = _first_matching_url(
                agenda_platform, classification.candidate_urls + _source_urls(source)
            )
            agenda_ok, agenda_reason = (
                _aux_index_verified(agenda_platform, source)
                if source is not None
                else (False, "incomplete provider source configuration")
            )
            # Maintainer-approved R12 rule: agenda adapter verification plus a primary-provider
            # sample proves the additional source does not compromise existing playable coverage.
            sample, sample_reason = _sample_media(existing_city.provider, existing_city.source)
            verification = Verification(
                agenda_platform,
                signature_url,
                signature_url is not None,
                agenda_ok,
                sample,
                source,
                agenda_reason or sample_reason,
            )
        yaml_diff = (
            _aux_yaml(existing_city, agenda_platform, verification.source)
            if verification.applyable and agenda_platform and verification.source
            else None
        )
    else:
        if not video_platform:
            verification = Verification(None, None, False, False, None, None, "no video platform")
        else:
            source = _source_from_evidence(classification.video_source, results=results)
            signature_url = _first_matching_url(
                video_platform, classification.candidate_urls + _source_urls(source)
            )
            sample, reason = (
                _sample_media(video_platform, source)
                if source is not None
                else (None, "incomplete provider source configuration")
            )
            verification = Verification(
                video_platform,
                signature_url,
                signature_url is not None,
                sample is not None,
                sample,
                source,
                reason,
            )
        yaml_diff = (
            _new_city_yaml(request, video_platform, verification.source)
            if verification.applyable and video_platform and verification.source
            else None
        )
    return DiscoveryResult(
        request=request,
        search_results=tuple(results),
        classification=classification,
        verification=verification,
        proposed_yaml=yaml_diff,
        city_website_url=city_url,
        meeting_listing_url=listing_url,
        research_only=not verification.applyable,
        needs_more_information=False,
        evidence_created_at=datetime.now(UTC).isoformat(),
    )
