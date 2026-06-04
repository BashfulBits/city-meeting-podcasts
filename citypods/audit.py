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
  * ``dead-audio``     — one project-wide alert when too many episodes can't be materialized at
                         all (keyless Swagit source with no usable page media, issue #120).
  * ``deferred-audio`` — one project-wide tracker of episodes parked awaiting a pending feature
                         (multi-segment Swagit concat, #122), so its prevalence stays visible.

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
from citypods.providers.base import MEDIA_DEAD, MEDIA_DEFERRED, ProviderError
from citypods.timeline import timeline_digest

ERROR = "error"
WARN = "warn"

# Project-wide audio-failure alert (issue #120): file a single aggregate finding when the number
# of episodes that can't be materialized crosses this many across all feeds, so a creeping rise in
# dead audio surfaces as one auto-closing ticket rather than per-feed noise (deferred meetings are
# tracked by their feature issue #122, not here). Override via defaults.dead_audio_alert_threshold.
DEAD_AUDIO_ALERT_THRESHOLD = 10


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

    fetched: int  # episodes provider returned this run (after uid assignment)
    archived: int  # total unique episodes ever seen (fetched ∪ persisted)
    materialized: int  # archived episodes with a hosted audio URL
    dropped: int  # in archive but absent from this fetch (left provider window)
    backlog: int  # fetched but not yet materialized (HLS pipeline catching up)

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


def compute_archive_diff(
    fetched_episodes: list[Episode], records: dict, *, body: str | None = None
) -> ArchiveDiff:
    """Compare freshly-fetched episodes against the append-only archive.

    The record store is shared across every body on the same source (``source_key`` strips the
    per-board ``body`` filter), so it holds *all* bodies' episodes. When ``body`` is given, scope
    both sides of the diff to that body's slice — otherwise one body's materialized episodes would
    make the diff suppress a genuine empty/too-few finding for a *different* body whose ``body:``
    filter has stopped matching (HTML/name change, typo) on the same shared view.
    """
    if body:
        from citypods.bodies import matches

        fetched_episodes = filter_by_body(fetched_episodes, body)
        records = {uid: r for uid, r in records.items() if matches(r.get("body"), body)}
    fetched_uids = {e.uid for e in fetched_episodes if e.uid}
    archived_uids = set(records)
    materialized = sum(1 for r in records.values() if (r.get("audio") or {}).get("url"))
    dropped = len(archived_uids - fetched_uids)
    backlog = sum(1 for e in fetched_episodes if e.media_kind == "hls" and not e.hosted_audio_url)
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


def count_audio_failures(records: dict) -> tuple[int, int]:
    """Count records currently failing materialization, as ``(deferred, dead)`` (issue #120).

    ``deferred`` = recoverable once a pending feature ships (multi-segment Swagit concat, #122);
    ``dead`` = no usable media exists. Reads the persisted record store, so it reflects every
    parked episode regardless of whether it's in this run's fetch window."""
    deferred = dead = 0
    for rec in records.values():
        code = (rec.get("audio") or {}).get("error")
        if code == MEDIA_DEFERRED:
            deferred += 1
        elif code == MEDIA_DEAD:
            dead += 1
    return deferred, dead


def check_dead_audio_aggregate(
    deferred_total: int, dead_total: int, *, threshold: int = DEAD_AUDIO_ALERT_THRESHOLD
) -> Finding | None:
    """Project-wide alert: one finding when ``dead_total`` crosses ``threshold`` (issue #120).

    Filed against the pseudo-slug ``(all)`` so the feed-health reconciler keeps it as a single
    deduplicated, auto-closing issue. Deferred episodes are reported for context but don't trip
    the alert — they're tracked by their feature issue (#122) and clear when it ships."""
    if dead_total < threshold:
        return None
    msg = (
        f"{dead_total} episode(s) across all feeds have no materializable audio "
        f"(keyless source with no usable page media — issue #120)."
    )
    if deferred_total:
        msg += f" Separately, {deferred_total} await multi-segment audio concat (#122)."
    return Finding("(all)", "dead-audio", ERROR, msg)


def check_meetings_url(
    slug: str,
    url: str,
    probe: Callable[[str], tuple[int, str]],
) -> Finding | None:
    """HEAD the city's configured ``meetings_url`` and flag if it's dead or quietly moved.

    ``probe(url)`` returns ``(status_code, final_url)`` — the final URL after any redirects.

    Two failure modes:
      * ``meetings-url-dead``    (ERROR) — 4xx/5xx; the page is gone.
      * ``meetings-url-changed`` (WARN)  — the server redirected to a dramatically different
        path (e.g. the configured deep link now bounces to the site root), which is a strong
        signal the city reorganised its meeting pages and the YAML needs a human update.

    "Dramatically different" is judged by path depth: if the configured URL has ≥ 3 path
    segments and the final URL has ≤ 1, it has almost certainly been redirected to the
    homepage and the original meeting page no longer exists at that URL.
    """
    from urllib.parse import urlsplit

    try:
        status, final_url = probe(url)
    except Exception as exc:
        return Finding(slug, "meetings-url-dead", ERROR, f"meetings_url probe failed: {exc}")

    if status >= 400:
        return Finding(
            slug,
            "meetings-url-dead",
            ERROR,
            f"meetings_url returned HTTP {status}: {url}",
        )

    orig_depth = len([s for s in urlsplit(url).path.split("/") if s])
    final_depth = len([s for s in urlsplit(final_url).path.split("/") if s])
    if orig_depth >= 3 and final_depth <= 1 and final_url.rstrip("/") != url.rstrip("/"):
        return Finding(
            slug,
            "meetings-url-changed",
            WARN,
            f"meetings_url redirected to a much shorter path — city may have reorganised "
            f"its meeting pages (configured: {url!r} → final: {final_url!r}). "
            "Verify and update meetings_url in the city YAML.",
        )

    return None


def check_deferred_audio_aggregate(
    deferred_total: int, *, examples: list[tuple[str, int]] | None = None, issue: int = 122
) -> Finding | None:
    """Project-wide prevalence tracker for deferred (recoverable) audio (issue #120).

    Fires whenever any episode is parked awaiting a pending feature (multi-segment Swagit concat),
    so the count of meetings that feature would unblock stays visible for prioritization. Filed as
    ``severity:warn`` against pseudo-slug ``(all)``; the feed-health reconciler keeps it a single
    deduplicated issue that auto-closes once ``#issue`` ships and the count returns to 0."""
    if deferred_total <= 0:
        return None
    msg = (
        f"{deferred_total} episode(s) across all feeds await multi-segment audio concat "
        f"(deferred via backoff). Implementing #{issue} would unblock them."
    )
    if examples:
        top = sorted(examples, key=lambda kv: kv[1], reverse=True)[:3]
        msg += " Most affected sources: " + ", ".join(f"{s} ({n})" for s, n in top) + "."
    return Finding("(all)", "deferred-audio", WARN, msg)


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
# Timeline integrity (INFRA-9, #150)
# ---------------------------------------------------------------------------

# Tolerance for floating-point duration comparisons (one video frame at 30fps ≈ 33ms).
_FRAME_TOLERANCE = 0.1


def check_timeline_integrity(slug: str, episodes: list[Episode]) -> list[Finding]:
    """Validate the Edit Decision Lists stored on episodes that have non-identity timelines.

    Checks (per episode with a non-identity Timeline):
      1. **Segment ordering**: segments are monotonically ordered and non-overlapping.
      2. **Coverage start**: first segment starts at 0.
      3. **Duration match**: Σ segment lengths == ``audio_duration_served`` (±frame).
      4. **Source span bounds**: source segment ``[source_start, source_end]`` lies within
         ``SourceMedia.duration`` (when the source duration is known).
      5. **Chapter alignment**: served-time chapters (``chapters_basis == "served"``) fall
         within ``[0, audio_duration_served]``.

    Pure (no network) — safe to run offline.
    """
    findings: list[Finding] = []
    for ep in episodes:
        if ep.timeline is None or timeline_digest(ep.timeline) == "":
            continue  # identity timeline — nothing to verify

        tl = ep.timeline
        segs = tl.segments
        uid = ep.uid or ep.guid

        if not segs:
            findings.append(
                Finding(slug, "timeline-empty", ERROR, f"{uid}: timeline.segments is empty")
            )
            continue

        # 1. Monotonicity, non-overlap, and no internal gaps (served time is contiguous)
        prev_end = 0.0
        for i, s in enumerate(segs):
            if s.served_start < prev_end - _FRAME_TOLERANCE:
                findings.append(
                    Finding(
                        slug,
                        "timeline-overlap",
                        ERROR,
                        f"{uid}: segment {i} starts at {s.served_start:.3f}s "
                        f"before previous end {prev_end:.3f}s",
                    )
                )
            elif i > 0 and s.served_start > prev_end + _FRAME_TOLERANCE:
                # Served time is the continuous enclosure clock, so a hole between segments
                # means the EDL fails to account for some served audio (a planner bug). This is
                # the mirror image of the overlap check above; together they enforce contiguity.
                # (The start gap at i==0 is reported separately by check #2.)
                findings.append(
                    Finding(
                        slug,
                        "timeline-gap",
                        ERROR,
                        f"{uid}: gap before segment {i}: previous end {prev_end:.3f}s "
                        f"→ next start {s.served_start:.3f}s",
                    )
                )
            prev_end = s.served_end

        # 2. Coverage starts at 0
        if segs[0].served_start > _FRAME_TOLERANCE:
            findings.append(
                Finding(
                    slug,
                    "timeline-gap-start",
                    ERROR,
                    f"{uid}: first segment starts at {segs[0].served_start:.3f}s (expected 0)",
                )
            )

        # 3. Duration match + end coverage (only when audio_duration_served is recorded).
        # This is only meaningful because the encoder derives audio_duration_served from the
        # EDL itself (INFRA-3 review item #7), not from ep.duration (the *source* duration) —
        # otherwise a trimmed episode would mismatch here purely by construction.
        served_dur = ep.audio_duration_served
        if served_dur is not None:
            seg_total = sum(s.served_end - s.served_start for s in segs)
            delta = abs(seg_total - served_dur)
            if delta > _FRAME_TOLERANCE:
                findings.append(
                    Finding(
                        slug,
                        "timeline-duration-mismatch",
                        ERROR,
                        f"{uid}: segment total {seg_total:.3f}s != "
                        f"audio_duration_served {served_dur:.3f}s "
                        f"(delta {delta:.3f}s)",
                    )
                )
            # End coverage: the last segment must reach the served duration. Sum-vs-duration
            # alone can be fooled by an internal gap plus an equal overrun; this pins the end.
            if abs(segs[-1].served_end - served_dur) > _FRAME_TOLERANCE:
                findings.append(
                    Finding(
                        slug,
                        "timeline-short-coverage",
                        ERROR,
                        f"{uid}: last segment ends at {segs[-1].served_end:.3f}s != "
                        f"audio_duration_served {served_dur:.3f}s",
                    )
                )

        # 4. Source spans within SourceMedia.duration
        src_by_id = {s.id: s for s in (ep.sources or [])}
        for i, seg in enumerate(segs):
            if seg.kind != "source" or seg.source_id not in src_by_id:
                continue
            src = src_by_id[seg.source_id]
            if src.duration is None:
                continue
            if seg.source_start is not None and seg.source_start < -_FRAME_TOLERANCE:
                findings.append(
                    Finding(
                        slug,
                        "timeline-source-underrun",
                        ERROR,
                        f"{uid}: segment {i} source_start {seg.source_start:.3f}s < 0",
                    )
                )
            if seg.source_end is not None and seg.source_end > src.duration + _FRAME_TOLERANCE:
                findings.append(
                    Finding(
                        slug,
                        "timeline-source-overrun",
                        ERROR,
                        f"{uid}: segment {i} source_end {seg.source_end:.3f}s > "
                        f"SourceMedia.duration {src.duration:.3f}s",
                    )
                )

        # 5. Served-time chapters within [0, served_duration]. Basis is "served" or
        # "served:<edl-version>" (INFRA-5 stamps the version), so match the prefix.
        if ep.chapters_basis.startswith("served") and served_dur is not None:
            for ch in ep.chapters or []:
                start = ch.get("start")
                if start is None:
                    continue
                if start < -_FRAME_TOLERANCE or start > served_dur + _FRAME_TOLERANCE:
                    findings.append(
                        Finding(
                            slug,
                            "timeline-chapter-out-of-range",
                            WARN,
                            f"{uid}: chapter '{ch.get('title', '')}' at "
                            f"{start:.1f}s outside served "
                            f"[0, {served_dur:.1f}]",
                        )
                    )

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

    # Scope the archive diff to this feed's own body: the record store is shared across every body
    # on the same source, so an unscoped diff would let other bodies' materialized episodes suppress
    # a genuine per-body regression (its ``body:`` filter stopped matching, dropping it to 0).
    body = city.source.get("body")
    diff = compute_archive_diff(episodes, records, body=body) if records is not None else None

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

    episodes = filter_by_body(episodes, body)
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
        # Timeline integrity: offline, always runs when records are present.
        if records is not None:
            findings.extend(check_timeline_integrity(city.slug, episodes))
    return findings


def _net_probe() -> Callable[[str], tuple[int, str]]:
    """HEAD probe that returns ``(status_code, final_url)`` after following redirects."""
    from citypods.http import DEFAULT_TIMEOUT, make_session

    session = make_session()

    def probe(url: str) -> tuple[int, str]:
        resp = session.head(url, timeout=DEFAULT_TIMEOUT, allow_redirects=True)
        if resp.status_code in (403, 405, 501):
            resp = session.get(url, timeout=DEFAULT_TIMEOUT, allow_redirects=True, stream=True)
            resp.close()
        return resp.status_code, resp.url

    return probe


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
    check_meetings_urls_net: bool = False,
    now: datetime | None = None,
) -> list[Finding]:
    """Run every check across all cities. One fetch per city; ``view_counts`` and the
    per-source record store are gathered so the provider-specific checks apply."""
    from citypods.providers import get_provider
    from citypods.records import load_records, source_key
    from citypods.state import resolve_state_dir

    now = now or datetime.now(UTC)
    state_dir = resolve_state_dir(site_config, Path(output_dir))
    defaults = site_config.get("defaults", {})
    min_meetings = int(defaults.get("min_meetings_per_body", 3))
    dead_threshold = int(defaults.get("dead_audio_alert_threshold", DEAD_AUDIO_ALERT_THRESHOLD))
    head = _net_head() if check_enclosures_net else None

    findings: list[Finding] = []
    # Audio-failure tally is per *source* (per-body feeds share one record store), so accumulate
    # over unique source keys to avoid double-counting (issue #120). A representative slug per
    # source labels the prevalence examples.
    failures_by_source: dict[str, tuple[int, int]] = {}
    slug_by_source: dict[str, str] = {}
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
        src_key = source_key(city)
        records = load_records(state_dir, src_key)
        failures_by_source.setdefault(src_key, count_audio_failures(records))
        slug_by_source.setdefault(src_key, city.slug)
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

    deferred_total = sum(d for d, _ in failures_by_source.values())
    dead_total = sum(d for _, d in failures_by_source.values())
    dead = check_dead_audio_aggregate(deferred_total, dead_total, threshold=dead_threshold)
    if dead:
        findings.append(dead)
    deferred_examples = [
        (slug_by_source[k], d) for k, (d, _) in failures_by_source.items() if d > 0
    ]
    deferred = check_deferred_audio_aggregate(deferred_total, examples=deferred_examples)
    if deferred:
        findings.append(deferred)

    # meetings_url health — one probe per unique URL, attributed to the shortest slug so
    # cities with many boards (e.g. Dallas's 38 feeds) don't produce 38 identical issues.
    if check_meetings_urls_net:
        probe = _net_probe()
        seen_urls: dict[str, str] = {}  # url → representative slug (shortest)
        for city in cities:
            url = city.meetings_url or city.city_website
            if not url:
                continue
            existing = seen_urls.get(url)
            if existing is None or len(city.slug) < len(existing):
                seen_urls[url] = city.slug
        for url, slug in seen_urls.items():
            finding = check_meetings_url(slug, url, probe)
            if finding:
                findings.append(finding)

    return findings
