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

Issue #109 adds three-way triage so the audit only files actionable tickets:
  (a) pending backlog     — episodes in-flight but not yet materialized  → suppress
  (b) provider-dropped    — episodes left the provider window but are archived → expected
  (c) genuine regression  — previously-live content is now genuinely gone → file
Counts and the inferred cause appear in every filed finding's message.
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


# ---------------------------------------------------------------------------
# Archive-diff triage (issue #109)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArchiveDiff:
    """Counts from comparing what the provider returned this run with the append-only archive.

    Used to classify a suspicious finding into one of three buckets:
      (a) ``backlog``  > 0 and materialized == 0  → pipeline catching up, suppress
      (b) ``dropped``  > 0 and materialized  > 0  → window shifted, expected
      (c) neither                                 → genuine regression, file ticket
    """

    fetched: int       # episodes provider returned this run (after uid assignment)
    archived: int      # total unique episodes ever seen (fetched ∪ persisted)
    materialized: int  # archived episodes with a hosted audio URL
    dropped: int       # in archive but absent from this fetch (left provider window)
    backlog: int       # fetched but not yet materialized (HLS pipeline catching up)

    def summary(self) -> str:
        """Compact counts string embedded in filed finding messages."""
        return (
            f"fetched={self.fetched} archived={self.archived} "
            f"materialized={self.materialized} dropped={self.dropped} backlog={self.backlog}"
        )

    def cause(self) -> str:
        """Human-readable inferred cause for the ticket body."""
        if self.dropped > 0 and self.materialized > 0:
            return f"provider window shifted ({self.dropped} episode(s) archived, expected)"
        if self.backlog > 0 and self.materialized == 0:
            return f"pipeline catching up ({self.backlog} episode(s) not yet materialized)"
        return "genuine regression"


def compute_archive_diff(fetched_episodes: list[Episode], records: dict) -> ArchiveDiff:
    """Compare freshly-fetched episodes against the append-only archive."""
    fetched_uids = {e.uid for e in fetched_episodes if e.uid}
    archived_uids = set(records)
    materialized = sum(1 for r in records.values() if (r.get("audio") or {}).get("url"))
    dropped = len(archived_uids - fetched_uids)
    backlog = sum(
        1 for e in fetched_episodes if e.media_kind == "hls" and not e.hosted_audio_url
    )
    return ArchiveDiff(
        fetched=len(fetched_uids),
        archived=len(archived_uids),
        materialized=materialized,
        dropped=dropped,
        backlog=backlog,
    )


# ---------------------------------------------------------------------------
# Pure check functions
# ---------------------------------------------------------------------------


def check_empty(
    slug: str,
    episodes: list[Episode],
    min_meetings: int,
    *,
    diff: ArchiveDiff | None = None,
) -> Finding | None:
    """Flag a feed with zero or too-few episodes.

    With archive-diff triage (issue #109):
      - Provider window empty but archive has materialized episodes → window shift (b), suppress.
      - Fewer than min_meetings in window but archive meets the bar → backlog/windowing, suppress.
      - Genuinely empty with no archive either → regression (c), file.
    """
    n = len(episodes)
    if n == 0:
        if diff and diff.materialized > 0:
            return None  # (b) archived episodes exist — window is just empty
        msg = "feed has 0 episodes (source empty or parser broke)"
        if diff:
            msg += f"; {diff.summary()}; inferred: {diff.cause()}"
        return Finding(slug, "drift", ERROR, msg)
    if n < min_meetings:
        if diff and diff.archived >= min_meetings:
            return None  # (a/b) archive has enough — transient window or backlog
        msg = f"only {n} episode(s) (< min_meetings {min_meetings})"
        if diff:
            msg += f"; {diff.summary()}; inferred: {diff.cause()}"
        return Finding(slug, "empty", WARN, msg)
    return None


def check_staleness(
    slug: str,
    episodes: list[Episode],
    now: datetime,
    *,
    archive_newest: datetime | None = None,
    factor: float = 3.0,
    min_samples: int = 4,
    floor_days: float = 30.0,
) -> Finding | None:
    """Flag a feed whose newest episode is much older than its own typical cadence.

    Uses the median gap between consecutive meetings (robust to recess gaps), so a monthly
    board and a weekly council are each judged against their own rhythm. A ``floor_days``
    absolute minimum suppresses false positives on bursty feeds (e.g. a combined feed with
    several same-day meetings has a near-zero median, so a normal 10-day gap shouldn't flag).

    ``archive_newest`` (issue #109): when the provider window dropped a recent episode that IS
    in our archive, the most-recently-published date should come from the archive, not just the
    fetched list — otherwise a window shift falsely signals staleness. Cadence is still derived
    from the fetched episodes (stable, represents the current publishing pattern).
    """
    if len(episodes) < min_samples:
        return None  # too few meetings to establish a cadence
    dates = sorted((e.published for e in episodes), reverse=True)
    gaps = [(dates[i] - dates[i + 1]).total_seconds() for i in range(len(dates) - 1)]
    median = statistics.median(gaps)
    if median <= 0:
        return None
    # Use the most-recent date across fetched + archive to avoid false positives on window shifts.
    newest = max(archive_newest, dates[0]) if archive_newest else dates[0]
    age = (now - newest).total_seconds()
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


def check_rehost_backlog(slug: str, episodes: list[Episode]) -> Finding | None:
    """Flag only a *wholly* stalled audio pipeline: there are HLS episodes that need
    re-hosting but none have been hosted at all (transient per-run backlog is normal and
    not flagged). Episodes carry ``hosted_audio_url`` from the record store via
    ``merge_persisted``."""
    hls = [e for e in episodes if e.media_kind == "hls"]
    if not hls:
        return None
    hosted = sum(1 for e in hls if e.hosted_audio_url)
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
    resolve: Callable[[Episode], str] | None = None,
) -> list[Finding]:
    """HEAD the newest few enclosures; a 4xx/5xx means the audio link is dead/expired.

    Self-healing re-resolve (issue #109, former #45): when ``resolve`` is provided and a URL
    is dead, the provider's ``resolve_media_url`` is called to fetch a fresh URL before filing.
    If the new URL is healthy the finding is suppressed (the next build will persist it).
    If still dead, the finding is filed with a note that re-resolve was attempted.
    The audit stays read-only — persisting the fixed URL is the build's responsibility.
    """
    findings: list[Finding] = []
    checked = 0
    for e in episodes:
        url = enclosure_url(e, "audio")
        if not url:
            continue

        try:
            status = head(url)
            is_dead = status >= 400
            exc_msg: str | None = None
        except Exception as exc:  # noqa: BLE001 - network errors are themselves a finding
            is_dead = True
            exc_msg = str(exc)

        if is_dead:
            if resolve is not None:
                try:
                    new_url = resolve(e)
                    new_status = head(new_url)
                    if new_status < 400:
                        # Self-healed — suppress the finding; next build persists the new URL.
                        checked += 1
                        if checked >= sample:
                            break
                        continue
                    detail = f"re-resolved to {new_url!r}: HTTP {new_status}"
                except Exception as re_exc:  # noqa: BLE001
                    detail = f"re-resolve failed: {re_exc}"
            else:
                detail = None

            if exc_msg:
                base = f"{e.guid}: {exc_msg}"
            else:
                base = f"{e.guid}: HTTP {status}"
            msg = f"{base} ({detail})" if detail else base
            findings.append(Finding(slug, "dead-enclosure", ERROR, msg))

        checked += 1
        if checked >= sample:
            break
    return findings


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def audit_city(
    city: City,
    *,
    provider,
    now: datetime,
    min_meetings: int = 3,
    records: dict | None = None,
    view_counts: list[int] | None = None,
    head: Callable[[str], int] | None = None,
    resolve: Callable[[Episode], str] | None = None,
) -> list[Finding]:
    """Fetch a city once and run every applicable check, returning all findings.

    ``records`` is the full append-only archive (issue #109): it drives the three-way triage
    (backlog / dropped / regression) and the archive_newest staleness correction.
    ``resolve`` is the provider's media-URL re-resolver for dead-enclosure self-healing.
    """
    from citypods.records import assign_uids, merge_persisted

    try:
        episodes = provider.fetch_episodes(city.source)
    except ProviderError as exc:
        return [Finding(city.slug, "unreachable", ERROR, str(exc))]

    # Stable identity + persisted artifacts (hosted audio) so the backlog check can tell a
    # stalled pipeline from a normal in-progress backfill.
    assign_uids(city, episodes)
    if records is not None:
        merge_persisted(episodes, records)

    # Compute archive diff before body filtering (diff covers the whole source, not one body).
    diff = compute_archive_diff(episodes, records) if records is not None else None

    # Oldest date in the archive (across all episodes, pre-filter) for staleness correction.
    archive_newest: datetime | None = None
    if records:
        dates = []
        for rec in records.values():
            pub = rec.get("published")
            if pub:
                try:
                    dates.append(datetime.fromisoformat(pub))
                except ValueError:
                    pass
        if dates:
            archive_newest = max(dates)

    episodes = filter_by_body(episodes, city.source.get("body"))
    episodes.sort(key=lambda e: e.published, reverse=True)
    episodes = episodes[: city.max_episodes]

    findings: list[Finding] = []
    empty = check_empty(city.slug, episodes, min_meetings, diff=diff)
    if empty:
        findings.append(empty)
    if not empty or empty.severity != ERROR:  # skip further checks on a totally empty feed
        stale = check_staleness(city.slug, episodes, now, archive_newest=archive_newest)
        if stale:
            findings.append(stale)
        if view_counts is not None:
            cap = check_view_cap(city.slug, view_counts)
            if cap:
                findings.append(cap)
        if records is not None:
            backlog = check_rehost_backlog(city.slug, episodes)
            if backlog:
                findings.append(backlog)
        if head is not None:
            findings.extend(check_enclosures(city.slug, episodes, head, resolve=resolve))
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
    per-source record store are gathered so the provider-specific checks apply."""
    from citypods.providers import get_provider
    from citypods.records import load_records, source_key
    from citypods.state import resolve_state_dir

    now = now or datetime.now(UTC)
    state_dir = resolve_state_dir(site_config, Path(output_dir))
    min_meetings = int(site_config.get("defaults", {}).get("min_meetings_per_body", 3))
    head = _net_head() if check_enclosures_net else None

    findings: list[Finding] = []
    for city in cities:
        provider = get_provider(city.provider)

        # Build the re-resolve callable for dead-enclosure self-healing (issue #109, former #45).
        # Only active when we're also HEAD-probing enclosures; keeps the audit read-only otherwise.
        resolve: Callable[[Episode], str] | None = None
        if head is not None and hasattr(provider, "resolve_media_url"):
            src = city.source  # capture for the lambda
            resolve = lambda ep, _p=provider, _s=src: _p.resolve_media_url(ep, _s)  # noqa: E731

        view_counts = None
        if hasattr(provider, "fetch_view_counts"):
            try:
                view_counts = provider.fetch_view_counts(city.source)
            except ProviderError:
                view_counts = None  # an unreachable source is caught by audit_city's fetch
        records = load_records(state_dir, source_key(city))
        findings.extend(
            audit_city(
                city,
                provider=provider,
                now=now,
                min_meetings=min_meetings,
                records=records,
                view_counts=view_counts,
                head=head,
                resolve=resolve,
            )
        )
    return findings
