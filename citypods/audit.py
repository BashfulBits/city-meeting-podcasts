"""Feed-health checks shared by ``citypods doctor`` (local) and the scheduled audit job.

Each check encodes a failure mode we actually hit while onboarding the first cities, so
the audit catches the next instance automatically instead of by manual investigation:

  * ``stale``          — Denton's Granicus silently stopped getting committee meetings.
  * ``view-cap``       — Granicus RSS is hard-capped at 100 items/view, so busy views drop
                         low-frequency bodies (the reason Denton/Fort Worth needed Swagit /
                         multi-view).
  * ``empty`` / ``drift`` — a wrong ``body`` filter, denylist, or provider HTML/API change
                         leaves a feed with too few / zero episodes.
  * ``rehost-backlog`` — the audio pipeline is wholly stalled (bad storage creds / ffmpeg),
                         so an HLS-only feed never gets a playable enclosure.
  * ``dead-enclosure`` — expiring Swagit presigned URLs / dead Granicus DownloadFile links.

The check functions are pure (no network) so they unit-test from fixtures; ``audit_city``
does the one fetch and wires them together, taking injectable ``head`` / ``view_counts`` so
the network-touching checks stay testable too.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from citypods.bodies import filter_by_body
from citypods.feeds import enclosure_url
from citypods.models import City, Episode
from citypods.providers.base import ProviderError

ERROR = "error"
WARN = "warn"


@dataclass(frozen=True)
class Finding:
    slug: str
    check: str
    severity: str
    message: str


def check_empty(slug: str, episodes: list[Episode], min_meetings: int) -> Finding | None:
    n = len(episodes)
    if n == 0:
        return Finding(slug, "drift", ERROR, "feed has 0 episodes (source empty or parser broke)")
    if n < min_meetings:
        return Finding(slug, "empty", WARN, f"only {n} episode(s) (< min_meetings {min_meetings})")
    return None


def check_staleness(
    slug: str,
    episodes: list[Episode],
    now: datetime,
    *,
    factor: float = 3.0,
    min_samples: int = 4,
    floor_days: float = 30.0,
) -> Finding | None:
    """Flag a feed whose newest episode is much older than its own typical cadence.

    Uses the median gap between consecutive meetings (robust to recess gaps), so a monthly
    board and a weekly council are each judged against their own rhythm. A ``floor_days``
    absolute minimum suppresses false positives on bursty feeds (e.g. a combined feed with
    several same-day meetings has a near-zero median, so a normal 10-day gap shouldn't flag).
    """
    if len(episodes) < min_samples:
        return None  # too few meetings to establish a cadence
    dates = sorted((e.published for e in episodes), reverse=True)
    gaps = [(dates[i] - dates[i + 1]).total_seconds() for i in range(len(dates) - 1)]
    median = statistics.median(gaps)
    if median <= 0:
        return None
    age = (now - dates[0]).total_seconds()
    if age > max(factor * median, floor_days * 86400):
        days, cadence = age / 86400, median / 86400
        return Finding(
            slug,
            "stale",
            WARN,
            f"newest episode is {days:.0f}d old; typical cadence ~{cadence:.0f}d",
        )
    return None


def check_view_cap(slug: str, view_counts: list[int], *, cap: int = 100) -> Finding | None:
    """A Granicus view returning exactly the 100-item cap is probably truncated, so
    low-frequency bodies may be missing — consider multi-view (``feed_urls``) or Swagit."""
    saturated = [c for c in view_counts if c >= cap]
    if saturated:
        return Finding(
            slug,
            "view-cap",
            WARN,
            f"{len(saturated)} view(s) at the {cap}-item cap; bodies may be missing",
        )
    return None


def check_rehost_backlog(slug: str, episodes: list[Episode], manifest: dict) -> Finding | None:
    """Flag only a *wholly* stalled audio pipeline: there are HLS episodes that need
    re-hosting but none have been hosted at all (transient per-run backlog is normal and
    not flagged)."""
    hls = [e for e in episodes if e.media_kind == "hls"]
    if not hls:
        return None
    hosted = sum(1 for e in hls if manifest.get(e.guid, {}).get("url"))
    if hosted == 0:
        return Finding(
            slug,
            "rehost-backlog",
            ERROR,
            f"0 of {len(hls)} HLS episodes have hosted audio (pipeline stalled?)",
        )
    return None


def check_enclosures(
    slug: str,
    episodes: list[Episode],
    head: Callable[[str], int],
    *,
    sample: int = 3,
) -> list[Finding]:
    """HEAD the newest few enclosures; a 4xx/5xx means the audio link is dead/expired."""
    findings: list[Finding] = []
    checked = 0
    for e in episodes:
        url = enclosure_url(e, "audio")
        if not url:
            continue
        try:
            status = head(url)
        except Exception as exc:  # noqa: BLE001 - network errors are themselves a finding
            findings.append(Finding(slug, "dead-enclosure", ERROR, f"{e.guid}: {exc}"))
        else:
            if status >= 400:
                findings.append(Finding(slug, "dead-enclosure", ERROR, f"{e.guid}: HTTP {status}"))
        checked += 1
        if checked >= sample:
            break
    return findings


def audit_city(
    city: City,
    *,
    provider,
    now: datetime,
    min_meetings: int = 3,
    manifest: dict | None = None,
    view_counts: list[int] | None = None,
    head: Callable[[str], int] | None = None,
) -> list[Finding]:
    """Fetch a city once and run every applicable check, returning all findings."""
    try:
        episodes = provider.fetch_episodes(city.source)
    except ProviderError as exc:
        return [Finding(city.slug, "unreachable", ERROR, str(exc))]

    episodes = filter_by_body(episodes, city.source.get("body"))
    episodes.sort(key=lambda e: e.published, reverse=True)
    episodes = episodes[: city.max_episodes]

    findings: list[Finding] = []
    empty = check_empty(city.slug, episodes, min_meetings)
    if empty:
        findings.append(empty)
    if not empty or empty.severity != ERROR:  # skip further checks on a totally empty feed
        stale = check_staleness(city.slug, episodes, now)
        if stale:
            findings.append(stale)
        if view_counts is not None:
            cap = check_view_cap(city.slug, view_counts)
            if cap:
                findings.append(cap)
        if manifest is not None:
            backlog = check_rehost_backlog(city.slug, episodes, manifest)
            if backlog:
                findings.append(backlog)
        if head is not None:
            findings.extend(check_enclosures(city.slug, episodes, head))
    return findings


def _net_head() -> Callable[[str], int]:
    """A HEAD probe that follows redirects (Granicus DownloadFile 302) and falls back to a
    ranged GET for CDNs that reject HEAD."""
    from citypods.http import DEFAULT_TIMEOUT, make_session

    session = make_session()

    def head(url: str) -> int:
        resp = session.head(url, timeout=DEFAULT_TIMEOUT, allow_redirects=True)
        if resp.status_code in (403, 405, 501):  # some CDNs disallow HEAD
            resp = session.get(url, timeout=DEFAULT_TIMEOUT, allow_redirects=True, stream=True)
            resp.close()
        return resp.status_code

    return head


def audit_all(
    cities: list[City],
    *,
    site_config: dict,
    output_dir: str | Path = "docs",
    check_enclosures_net: bool = False,
    now: datetime | None = None,
) -> list[Finding]:
    """Run every check across all cities. One fetch per city; ``view_counts`` and the
    audio manifest are gathered per provider so the provider-specific checks apply."""
    from citypods.media import load_manifest
    from citypods.providers import get_provider
    from citypods.state import resolve_state_dir

    now = now or datetime.now(UTC)
    state_dir = resolve_state_dir(site_config, Path(output_dir))
    min_meetings = int(site_config.get("defaults", {}).get("min_meetings_per_body", 3))
    head = _net_head() if check_enclosures_net else None

    findings: list[Finding] = []
    for city in cities:
        provider = get_provider(city.provider)
        view_counts = None
        if hasattr(provider, "fetch_view_counts"):
            try:
                view_counts = provider.fetch_view_counts(city.source)
            except ProviderError:
                view_counts = None  # an unreachable source is caught by audit_city's fetch
        manifest = load_manifest(state_dir, city.slug)
        findings.extend(
            audit_city(
                city,
                provider=provider,
                now=now,
                min_meetings=min_meetings,
                manifest=manifest,
                view_counts=view_counts,
                head=head,
            )
        )
    return findings
