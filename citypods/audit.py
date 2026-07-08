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
  * ``deferred-audio`` — one project-wide tracker of episodes parked in materialization backoff
                         (MEDIA_DEFERRED), so any recoverable backlog stays visible.
  * ``media-too-large`` — the source-media size guard (issue #497) rejected an episode before
                         ffmpeg started; rare by design, so each is its own visible finding
                         rather than aggregate/backoff noise.

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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from citypods.bodies import filter_by_body
from citypods.feeds import enclosure_url
from citypods.integrity import (
    REPAIR_AUDIO_REMATERIALIZE,
    REPAIR_TIMELINE_REPLAN,
    REPAIR_TRANSCRIPT_REGENERATE,
    build_timeline_audio_integrity,
    clear_resolved_timeline_audio_integrity,
    set_timeline_audio_integrity,
    timeline_audio_integrity,
)
from citypods.media import AudioDurationProbe
from citypods.models import City, Episode
from citypods.providers.base import MEDIA_DEAD, MEDIA_DEFERRED, ProviderError
from citypods.security import MediaSourceTooLargeError
from citypods.timeline import edl_duration, timeline_digest

ERROR = "error"
WARN = "warn"

# Project-wide audio-failure alert (issue #120): file a single aggregate finding when the number
# of episodes that can't be materialized crosses this many across all feeds, so a creeping rise in
# dead audio surfaces as one auto-closing ticket rather than per-feed noise (deferred meetings are
# tracked separately by check_deferred_audio_aggregate).
# Override via defaults.dead_audio_alert_threshold.
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


def aggregate_view_cap_findings(
    findings: list[Finding],
    contexts: Mapping[str, tuple[str, str]],
) -> list[Finding]:
    """Collapse per-feed ``view-cap`` findings into one issue per provider.

    ``contexts`` maps feed slug -> ``(provider, city/entity slug)``. The raw check is still
    per-feed because the saturated view counts are source-specific, but GitHub issue noise is
    provider-shaped: "Granicus RSS is capped" with the affected feeds listed.
    """
    grouped: dict[str, list[tuple[str, str, str]]] = {}
    kept: list[Finding] = []
    for finding in findings:
        if finding.check != "view-cap":
            kept.append(finding)
            continue
        context = contexts.get(finding.slug)
        if context is None:
            kept.append(finding)
            continue
        provider, city_slug = context
        grouped.setdefault(provider, []).append((city_slug, finding.slug, finding.message))

    for provider, hits in sorted(grouped.items()):
        rows = "\n".join(
            f"- `{city_slug}` / `{feed_slug}` — {message}"
            for city_slug, feed_slug, message in sorted(hits)
        )
        kept.append(
            Finding(
                provider,
                "view-cap",
                WARN,
                f"{provider} has {len(hits)} feed(s) with provider views at the 100-item "
                "cap; provider windows may be truncated until the affected feeds move to a "
                f"full-history source.\n\nAffected feeds:\n{rows}",
            )
        )
    return kept


def count_audio_failures(records: dict) -> tuple[int, int]:
    """Count records currently failing materialization, as ``(deferred, dead)`` (issue #120).

    ``deferred`` = recoverable (MEDIA_DEFERRED; episode is in backoff and will re-attempt);
    ``dead`` = no usable media exists (MEDIA_DEAD). Reads the persisted record store, so it
    reflects every parked episode regardless of whether it's in this run's fetch window."""
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
    the alert — they're in backoff and will re-attempt automatically."""
    if dead_total < threshold:
        return None
    msg = (
        f"{dead_total} episode(s) across all feeds have no materializable audio "
        f"(keyless source with no usable page media — issue #120)."
    )
    if deferred_total:
        msg += (
            f" Separately, {deferred_total} episode(s) are in materialization backoff (will retry)."
        )
    return Finding("(all)", "dead-audio", ERROR, msg)


def check_meetings_url(
    slug: str,
    url: str,
    probe: Callable[[str], tuple[int, str]],
) -> Finding | None:
    """Probe the city's configured ``meetings_url`` and flag browser-visible breakage.

    ``probe(url)`` returns ``(status_code, final_url)`` — the final URL after any redirects.

    Two failure modes:
      * ``meetings-url-dead``    (ERROR) — hard browser-visible dead statuses.
      * ``meetings-url-changed`` (WARN)  — the server redirected to a dramatically different
        path (e.g. the configured deep link now bounces to the site root), which is a strong
        signal the city reorganised its meeting pages and the YAML needs a human update.

    Browser/WAF/rate-limit responses (403/429) and probe-layer exceptions are inconclusive for
    this check: the page may still be the correct public URL for humans, so do not file.

    "Dramatically different" is judged by path depth: if the configured URL has ≥ 3 path
    segments and the final URL has ≤ 1, it has almost certainly been redirected to the
    homepage and the original meeting page no longer exists at that URL.
    """
    from urllib.parse import urlsplit

    try:
        status, final_url = probe(url)
    except Exception:
        return None

    if status in {404, 410, 451}:
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
    """Project-wide prevalence tracker for deferred (recoverable) audio.

    Fires whenever any episode is parked in materialization backoff (MEDIA_DEFERRED). Filed as
    ``severity:warn`` against pseudo-slug ``(all)``; the feed-health reconciler keeps it a single
    deduplicated issue that auto-closes once the count returns to 0."""
    if deferred_total <= 0:
        return None
    msg = (
        f"{deferred_total} episode(s) across all feeds are in materialization backoff "
        f"(MEDIA_DEFERRED; will retry automatically)."
    )
    if examples:
        top = sorted(examples, key=lambda kv: kv[1], reverse=True)[:3]
        msg += " Most affected sources: " + ", ".join(f"{s} ({n})" for s, n in top) + "."
    return Finding("(all)", "deferred-audio", WARN, msg)


def check_media_too_large(slug: str, records: dict) -> list[Finding]:
    """One finding per episode the source-media size guard rejected (issue #497).

    This should be rare by design (the cap is a very conservative multiple of the longest known
    meeting), so unlike a transient network error it must never be silently swallowed into the
    generic backoff/retry noise — each occurrence is filed as its own always-visible, human-
    reviewed finding rather than folded into an aggregate count, and the feed-health reconciler
    consolidates same-named findings across feeds into one issue (see ``scripts/audit_feeds.py``).
    Reads the persisted record store, so it reflects every rejection regardless of whether the
    episode is in this run's fetch window.
    """
    findings: list[Finding] = []
    for uid, rec in records.items():
        audio = rec.get("audio") or {}
        if audio.get("error") != MediaSourceTooLargeError.code:
            continue
        title = rec.get("title") or uid
        links = rec.get("links") or {}
        link = links.get("canonical_video") or links.get("agenda")
        ref = f" — {link}" if link else f" (uid={uid!r}; no public link on record for verification)"
        findings.append(
            Finding(
                slug,
                "media-too-large",
                ERROR,
                f"{title!r}{ref} was rejected before ffmpeg started: the source honestly "
                "advertised a size over the configured `source_media_max_bytes` ceiling. Verify "
                "whether this is a genuinely oversized/misencoded source or the cap needs "
                "raising — see the comment on `source_media_max_bytes` in "
                "`config/site_config.yml` for how it was derived.",
            )
        )
    return findings


_STALL_LOOK_BACK = 5  # examine this many recent runs
_STALL_THRESHOLD = 3  # pipeline is "active" when >= this many of those runs encoded audio

_PROVIDER_ERR_LOOK_BACK = 5
_PROVIDER_ERR_THRESHOLD = 2  # warn when a provider has errors in >= this many recent runs


def _active_encode_runs(run_history: list[dict], *, limit: int = _STALL_LOOK_BACK) -> int:
    """Count how many of the last *limit* runs had at least one real encode."""
    return sum(1 for r in run_history[-limit:] if r.get("materialize_encoded", 0) > 0)


def check_rehost_backlog(
    slug: str,
    episodes: list[Episode],
    *,
    run_history: list[dict] | None = None,
) -> Finding | None:
    """Triage the HLS audio backlog into three outcomes:

    * **suppress** — feed is catching-up (some episodes hosted, or pipeline not yet active
      enough to call anything stalled).  No issue filed; normal operation.
    * **warn** — feed is stalled: zero hosted audio despite the pipeline running actively
      in recent runs (other feeds are encoding; this one is not).
    * Provider failures (dead enclosure, unreachable source) are separate checks and stay
      at ERROR — ``check_rehost_backlog`` never touches those.

    ``run_history`` is the decoded ``run_history.jsonl`` tail (newest-last).  When absent
    the check silently suppresses rather than false-positive on new deployments.
    """
    hls = [e for e in episodes if e.media_kind == "hls"]
    if not hls:
        return None
    hosted = sum(1 for e in hls if e.hosted_audio_url)

    if hosted > 0:
        # At least one episode is hosted — pipeline is working for this feed.  Suppress.
        return None

    # Nothing hosted yet.  Only warn when we have enough evidence that the pipeline IS
    # running (encodes happening elsewhere) but this feed is getting nothing.
    active = _active_encode_runs(run_history) if run_history is not None else 0
    if active < _STALL_THRESHOLD:
        # Too few runs to distinguish "new city" from "stalled" — suppress.
        return None

    backlog = len(hls)
    return Finding(
        slug,
        "rehost-backlog",
        WARN,
        f"0 of {backlog} HLS episode(s) hosted; pipeline has encoded audio in {active} of the "
        f"last {_STALL_LOOK_BACK} runs but none for this feed — pipeline may be stalled.",
    )


def check_provider_error_rates(
    run_history: list[dict],
    *,
    look_back: int = _PROVIDER_ERR_LOOK_BACK,
    threshold: int = _PROVIDER_ERR_THRESHOLD,
) -> list[Finding]:
    """Return a WARN finding for each provider with source-fetch failures in >= *threshold*
    of the last *look_back* runs.

    Provider errors are recorded in ``run_history.jsonl`` under ``provider_errors``
    (e.g. ``{"granicus": 2, "swagit": 0}``) by ``_record_run_history`` in ``run.py``.
    Signals provider drift before a deploy goes red.
    """
    if not run_history:
        return []
    recent = run_history[-look_back:]
    runs_with_errors: dict[str, int] = {}
    for row in recent:
        for provider, count in (row.get("provider_errors") or {}).items():
            if count > 0:
                runs_with_errors[provider] = runs_with_errors.get(provider, 0) + 1
    findings = []
    for provider, n in sorted(runs_with_errors.items()):
        if n >= threshold:
            findings.append(
                Finding(
                    "(all)",
                    f"provider-errors:{provider}",
                    WARN,
                    f"{provider} had source-fetch failures in {n} of the last "
                    f"{len(recent)} run(s); provider endpoint may be drifting.",
                )
            )
    return findings


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
        # Withheld media (silent/quarantined or unreachable) is excluded from the feed, so a stale
        # hosted URL left on the record must not file a `dead-enclosure` finding (GH#795).
        if e.media_availability is not None and e.media_availability.is_withheld():
            continue
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

# Structural timeline checks (ordering, coverage, chapter range) use _FRAME_TOLERANCE: a single
# sample boundary. The rendered-vs-EDL duration comparison needs a looser floor, because a clean
# re-encode legitimately differs from the EDL sum by AAC encoder priming/padding plus per-cut sample
# rounding (~0.1-0.4s observed) without any cue-integrity problem. Classifying that band as an ERROR
# produced a long tail of sub-finding `rendered-duration-mismatch` diagnostics that were never filed
# (findings gate at finding_min_delta, default 1.0s) — pure artifact noise. Real divergences in the
# GH#702 cohort are ≥1s, so a 0.5s floor cleanly separates padding noise from genuine drift while
# leaving the 1s finding/repair thresholds untouched (GH#702 de-noise).
_RENDERED_DURATION_TOLERANCE = 0.5

# Tolerance for comparing the header-only probe against a full-download reconciliation probe
# (this task). Both reads should derive the same numbers from the same `moov` bytes, so this is
# just float/string round-trip slack — not the structural/encoder-padding slack the constants
# above cover. Anything beyond this is a real disagreement between the two probe methods.
_PROBE_RECONCILE_TOLERANCE = 0.02


def _probe_reconcile_delta(header: AudioDurationProbe, full: AudioDurationProbe) -> float | None:
    """Largest disagreement between the header-only and full-download probes' duration fields,
    or ``None`` if there's no comparable pair (e.g. one side lacks a value)."""
    deltas: list[float] = []
    if header.container_duration is not None and full.container_duration is not None:
        deltas.append(abs(header.container_duration - full.container_duration))
    if header.stream_sample_duration is not None and full.stream_sample_duration is not None:
        deltas.append(abs(header.stream_sample_duration - full.stream_sample_duration))
    return max(deltas) if deltas else None


def _timeline_duration(ep: Episode) -> float | None:
    return edl_duration(ep.timeline)


def _source_duration_delta(ep: Episode) -> float | None:
    if not ep.sources or len(ep.sources) <= 1:
        return None
    src_by_id = {s.id: s for s in ep.sources if s.duration is not None}
    deltas: list[float] = []
    for seg in ep.timeline.segments if ep.timeline else ():
        if seg.kind != "source" or seg.source_id not in src_by_id:
            continue
        if seg.source_start is None or seg.source_end is None:
            continue
        expected = max(0.0, seg.source_end - seg.source_start)
        actual = float(src_by_id[seg.source_id].duration or 0)
        deltas.append(actual - expected)
    if not deltas:
        return None
    return sum(deltas)


def _source_media_telemetry(ep: Episode) -> dict:
    sources = list(ep.sources or [])
    source_count = len(sources)
    if source_count > 1:
        source_mode = "multi-part"
    elif source_count == 1:
        source_mode = "single-file"
    else:
        source_mode = "unregistered"

    source_media = [
        {
            "id": src.id,
            "duration": src.duration,
            "duration_basis": src.duration_basis,
            "media_kind": src.media_kind,
        }
        for src in sources
    ]
    known_source_durations = [float(src.duration) for src in sources if src.duration is not None]
    source_duration_total = (
        sum(known_source_durations)
        if source_count and len(known_source_durations) == source_count
        else None
    )
    segment_source_span_total = None
    if ep.timeline is not None and ep.timeline.segments:
        spans = [
            float(seg.source_end - seg.source_start)
            for seg in ep.timeline.segments
            if seg.kind == "source" and seg.source_start is not None and seg.source_end is not None
        ]
        if spans:
            segment_source_span_total = sum(spans)

    return {
        "timeline_version": ep.timeline.version if ep.timeline is not None else None,
        "source_mode": source_mode,
        "source_count": source_count,
        "source_media": source_media,
        "source_duration_bases": [src.duration_basis for src in sources],
        "source_duration_total": source_duration_total,
        "segment_source_span_total": segment_source_span_total,
    }


def _classify_timeline_audio_duration(
    ep: Episode,
    probe: AudioDurationProbe | None,
) -> tuple[str, str, list[str]]:
    # Withheld media (silent/quarantined or unreachable) is terminal for timeline-audio repair:
    # the stale hosted object no longer represents anything we will serve, so a stream-vs-EDL
    # mismatch is expected and must not drive a repair finding (GH#795). The availability lane owns
    # the episode's lifecycle from here; the audit only records the observation.
    if ep.media_availability is not None and ep.media_availability.is_withheld():
        return "media-withheld", WARN, []

    # Confirmed-partial (genuinely short/incomplete source, GH#851) is also terminal for repair:
    # the episode is real and published (unlike withheld), its EDL is built from the same decoded
    # length used to confirm it, and re-selecting timeline-replan/audio-rematerialize against a
    # source we already know is short would just churn the same repair loop forever. The
    # availability lane's flat recheck (stages.py) owns picking it back up if the city later
    # posts a complete file.
    if ep.media_availability is not None and ep.media_availability.is_confirmed_partial():
        return "media-partial", WARN, []
    timeline_duration = _timeline_duration(ep)
    if timeline_duration is None:
        return "duration-probe-inconclusive", WARN, []
    if probe is None:
        return "duration-probe-inconclusive", WARN, []

    stream = probe.stream_sample_duration
    container = probe.container_duration
    if stream is None:
        return "duration-probe-inconclusive", WARN, []

    stream_delta = abs(stream - timeline_duration)
    container_delta = abs(container - timeline_duration) if container is not None else None
    if stream_delta > _RENDERED_DURATION_TOLERANCE:
        source_delta = _source_duration_delta(ep)
        if source_delta is not None and abs(source_delta) > _FRAME_TOLERANCE:
            return (
                "timeline-source-duration-mismatch",
                ERROR,
                [
                    REPAIR_TIMELINE_REPLAN,
                    REPAIR_AUDIO_REMATERIALIZE,
                    REPAIR_TRANSCRIPT_REGENERATE,
                ],
            )
        return (
            "rendered-duration-mismatch",
            ERROR,
            [
                REPAIR_TIMELINE_REPLAN,
                REPAIR_AUDIO_REMATERIALIZE,
                REPAIR_TRANSCRIPT_REGENERATE,
            ],
        )
    if container_delta is not None and container_delta > _RENDERED_DURATION_TOLERANCE:
        return "container-duration-drift", WARN, []
    return "ok", WARN, []


def _timeline_audio_diagnostic(
    slug: str,
    ep: Episode,
    probe: AudioDurationProbe | None,
    status: str,
    severity: str,
    repair: list[str],
    *,
    max_kbps: int,
) -> dict:
    from citypods.records import audio_spec_hash, transcript_media_hash

    timeline_duration = _timeline_duration(ep)
    container = probe.container_duration if probe else None
    stream = probe.stream_sample_duration if probe else None
    probe_error = probe.probe_error if probe else "probe-not-run"
    container_delta_signed = (
        container - timeline_duration
        if container is not None and timeline_duration is not None
        else None
    )
    stream_delta_signed = (
        stream - timeline_duration if stream is not None and timeline_duration is not None else None
    )
    media_availability = ep.media_availability
    availability_state = (
        media_availability.effective_state() if media_availability is not None else None
    )
    return {
        "slug": slug,
        "uid": ep.uid or ep.guid,
        "check": status,
        "severity": severity,
        "timeline_duration": timeline_duration,
        "audio_duration_served": ep.audio_duration_served,
        "container_duration": container,
        "stream_sample_duration": stream,
        "container_delta": (
            abs(container_delta_signed) if container_delta_signed is not None else None
        ),
        "stream_delta": (abs(stream_delta_signed) if stream_delta_signed is not None else None),
        "container_delta_signed": container_delta_signed,
        "stream_delta_signed": stream_delta_signed,
        "duration_drift_kind": (
            "container-only"
            if status == "container-duration-drift"
            else ("stream" if status == "rendered-duration-mismatch" else None)
        ),
        "source_duration_delta": _source_duration_delta(ep),
        "repair": repair,
        "audio_key": ep.audio_key,
        "audio_spec_hash": audio_spec_hash(ep, max_kbps=max_kbps),
        "transcript_key": ep.transcript_key,
        "transcript_media_hash": transcript_media_hash(ep),
        "timeline_digest": timeline_digest(ep.timeline, ep.sources) if ep.timeline else "",
        "prior_integrity": timeline_audio_integrity(ep) or None,
        "probe_error": probe_error,
        "media_availability_state": availability_state,
        "media_withheld": bool(media_availability and media_availability.is_withheld()),
        **_source_media_telemetry(ep),
    }


def _diagnostic_delta(diag: dict) -> float | None:
    delta = diag.get("stream_delta")
    if not isinstance(delta, (int, float)):
        return None
    return abs(float(delta))


def _select_timeline_audio_repair(
    diag: dict,
    repair: list[str],
    *,
    min_delta: float | None,
) -> bool:
    if not repair:
        return False
    if min_delta is None:
        return True
    delta = _diagnostic_delta(diag)
    return delta is not None and delta >= min_delta


def _emit_timeline_audio_finding(
    status: str,
    diag: dict,
    *,
    min_delta: float,
) -> bool:
    if status in {
        "ok",
        "container-duration-drift",
        "duration-probe-inconclusive",
        "media-withheld",
        "media-partial",
    }:
        return False
    if status == "rendered-duration-mismatch":
        delta = _diagnostic_delta(diag)
        return delta is not None and delta >= min_delta
    return True


def check_timeline_integrity(
    slug: str,
    episodes: list[Episode],
    *,
    probe_audio: Callable[[Episode], AudioDurationProbe | None] | None = None,
    probe_audio_full: Callable[[Episode], AudioDurationProbe | None] | None = None,
    diagnostics: list[dict] | None = None,
    mutate_integrity: bool = False,
    max_kbps: int = 96,
    repair_min_delta: float | None = None,
    repair_cohort: str | None = None,
    finding_min_delta: float = 1.0,
) -> list[Finding]:
    """Validate the Edit Decision Lists stored on episodes that have non-identity timelines.

    ``probe_audio`` is the cheap header-only (range-read) duration probe. ``probe_audio_full``,
    when given, is only ever invoked for an episode ``probe_audio`` already flagged as non-"ok"
    (inconclusive, drifted, or mismatched) — it re-measures the same file with a full download
    and (a) supersedes the header probe as the authoritative reading for that episode's finding/
    repair decision, and (b) files a distinct ``timeline-audio-probe-divergence`` alert if the two
    disagree beyond noise, since the header-only probe's core assumption (moov alone matches a
    full read) would then be broken for a file class we should know about immediately. This never
    fires for episodes the header probe already found "ok", so the expensive path stays scoped to
    exactly the files worth double-checking.

    Checks (per episode with a non-identity Timeline):
      1. **Segment ordering**: segments are monotonically ordered and non-overlapping.
      2. **Coverage start**: first segment starts at 0.
      3. **Duration match / end coverage**: stored and probed served duration data agree with the
         EDL within the applicable tolerance. The stored-field fallback defaults to
         ``max(_FRAME_TOLERANCE, finding_min_delta)``; live probe reconciliation uses the
         stream/container tolerances documented above.
      4. **Source span bounds**: source segment ``[source_start, source_end]`` lies within
         ``SourceMedia.duration`` (when the source duration is known).
      5. **Chapter alignment**: served-time chapters (``chapters_basis == "served"``) fall
         within ``[0, audio_duration_served]``.

    Pure (no network) — safe to run offline.
    """
    findings: list[Finding] = []
    for ep in episodes:
        if ep.timeline is None or timeline_digest(ep.timeline, ep.sources) == "":
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

        # Resolve the live probe up front — including reconciliation against a full download
        # when the header-only probe already flags trouble (see the docstring on
        # ``probe_audio_full``) — *before* §3 decides whether to suppress the cheap stored-field
        # fallback. §3 defers to §3b only when the probe actually yields a usable stream clock;
        # that decision must be made from the *final* (post-reconciliation) probe, not the raw
        # header-only one — otherwise a header probe that looked usable but got overridden by an
        # inconclusive full-probe reconciliation would suppress §3 without §3b having a usable
        # clock either, and a real stored-duration mismatch could vanish from both checks.
        # Withheld media is terminal for timeline-audio repair (GH#795): the cheap header probe
        # still runs for the diagnostic record, but classification is forced to `media-withheld`
        # (no finding, no repair), the expensive full-download reconciliation is skipped (the stale
        # object will be ignored either way), and the §3 stored-field checks below are suppressed so
        # they cannot re-file the same EDL-vs-hosted-file mismatch under a different key.
        # Confirmed-partial media (GH#851) gets the same reconciliation/§3 suppression — it is
        # published, not stale, but re-selecting repair against a source already known to be short
        # would just churn the same loop forever; see _classify_timeline_audio_duration.
        withheld = bool(ep.media_availability and ep.media_availability.is_withheld())
        confirmed_partial = bool(
            ep.media_availability and ep.media_availability.is_confirmed_partial()
        )
        probe = probe_audio(ep) if probe_audio is not None else None
        status = severity = repair = None
        probe_divergence: float | None = None
        if probe_audio is not None:
            status, severity, repair = _classify_timeline_audio_duration(ep, probe)

            # Reconciliation: the header-only probe just flagged this episode as non-"ok", so
            # re-measure it with a full download and let that (ground-truth) reading decide the
            # actual finding/repair — and separately alert if the two methods disagree, since
            # that means the header-only fast path's core assumption broke for this file.
            if (
                status != "ok"
                and not withheld
                and not confirmed_partial
                and probe_audio_full is not None
            ):
                full_probe = probe_audio_full(ep)
                if full_probe is not None:
                    if probe is not None:
                        probe_divergence = _probe_reconcile_delta(probe, full_probe)
                    probe = full_probe
                    status, severity, repair = _classify_timeline_audio_duration(ep, probe)
                    if (
                        probe_divergence is not None
                        and probe_divergence > _PROBE_RECONCILE_TOLERANCE
                    ):
                        findings.append(
                            Finding(
                                slug,
                                "timeline-audio-probe-divergence",
                                ERROR,
                                f"{uid}: header-only probe disagrees with full-download probe "
                                f"by {probe_divergence:.3f}s — the moov-only fast-path "
                                "assumption has broken for this file; do not trust header-only "
                                "probes until this is root-caused",
                            )
                        )
        probe_has_stream = probe is not None and probe.stream_sample_duration is not None

        # 2b. Self-heal a stale audio_duration_served (issue #849): when this run actually probed
        # the hosted object, the probe is ground truth for what's served — whatever its
        # relationship to the EDL, which is §3b's concern, not this one. Nothing else reliably
        # rewrites this field once an object is reused rather than freshly re-encoded (the encode
        # and ASR paths only set it on their own write), so a pre-repair value can fossilize
        # indefinitely and the no-probe §3 check below re-files a mismatch against reality forever.
        # Gated on mutate_integrity so this only ever writes during an explicit
        # ``--persist-timeline-integrity`` pass, mirroring how repair blocks are persisted; skipped
        # for withheld media, whose served duration is moot (GH#795 keeps that lifecycle
        # audit-only).
        if (
            mutate_integrity
            and not withheld
            and probe is not None
            and probe.stream_sample_duration is not None
            and (
                ep.audio_duration_served is None
                or abs(ep.audio_duration_served - probe.stream_sample_duration) > _FRAME_TOLERANCE
            )
        ):
            ep.audio_duration_served = probe.stream_sample_duration

        # 3. Cheap record-only duration match + end coverage, from stored fields alone.
        # audio_duration_served is now the *probed hosted-stream* duration (review/20 / GH#702), so
        # a mismatch against the EDL means the real file diverged from the cue clock — the same
        # condition §3b classifies as `rendered-duration-mismatch`, but far more precisely (it
        # separates container-only drift from real stream drift). To avoid double-filing the same
        # slug, this stored-field check is suppressed only when §3b has a usable live stream clock;
        # if the probe is absent OR inconclusive (no stream_sample_duration), the cheap check still
        # runs so a real mismatch is not silently dropped.
        served_dur = ep.audio_duration_served
        stored_integrity_stamped = False
        if (
            served_dur is not None
            and not probe_has_stream
            and not withheld
            and not confirmed_partial
        ):
            seg_total = sum(s.served_end - s.served_start for s in segs)
            delta = abs(seg_total - served_dur)
            end_delta = abs(segs[-1].served_end - served_dur)
            stored_duration_delta = max(delta, end_delta)
            stored_duration_threshold = max(_FRAME_TOLERANCE, finding_min_delta)
            if delta >= stored_duration_threshold:
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
            if end_delta >= stored_duration_threshold:
                findings.append(
                    Finding(
                        slug,
                        "timeline-short-coverage",
                        ERROR,
                        f"{uid}: last segment ends at {segs[-1].served_end:.3f}s != "
                        f"audio_duration_served {served_dur:.3f}s",
                    )
                )
            if mutate_integrity and stored_duration_delta >= stored_duration_threshold:
                stored_repair = [
                    REPAIR_TIMELINE_REPLAN,
                    REPAIR_AUDIO_REMATERIALIZE,
                    REPAIR_TRANSCRIPT_REGENERATE,
                ]
                stored_status = (
                    "timeline-duration-mismatch"
                    if delta >= stored_duration_threshold
                    else "timeline-short-coverage"
                )
                stored_block = build_timeline_audio_integrity(
                    status=stored_status,
                    timeline_duration=seg_total,
                    container_duration=None,
                    stream_sample_duration=None,
                    repair=stored_repair,
                    source_duration_delta=_source_duration_delta(ep),
                )
                if repair_min_delta is not None:
                    stored_block["repair_min_delta"] = repair_min_delta
                if repair_cohort:
                    stored_block["repair_cohort"] = repair_cohort
                if _select_timeline_audio_repair(
                    {"stream_delta": stored_duration_delta},
                    stored_repair,
                    min_delta=repair_min_delta,
                ):
                    set_timeline_audio_integrity(ep, stored_block)
                    stored_integrity_stamped = True

        # 3b. Rendered audio duration diagnostics. This compares the EDL to the stream sample
        # clock when the caller provides a probe; container-only drift is reported separately so
        # it does not masquerade as cue drift. Classification/reconciliation already happened
        # above (before §3's suppression decision); this just emits the diagnostic/finding.
        if probe_audio is not None:
            diag = _timeline_audio_diagnostic(
                slug,
                ep,
                probe,
                status,
                severity,
                repair,
                max_kbps=max_kbps,
            )
            if probe_divergence is not None:
                diag["probe_reconciled"] = True
                diag["probe_divergence_delta"] = probe_divergence
            repair_selected = _select_timeline_audio_repair(
                diag,
                repair,
                min_delta=repair_min_delta,
            )
            diag["repair_selected"] = repair_selected
            if repair_min_delta is not None:
                diag["repair_min_delta"] = repair_min_delta
            if repair_cohort:
                diag["repair_cohort"] = repair_cohort
            if diagnostics is not None:
                diagnostics.append(diag)
            timeline_duration = diag.get("timeline_duration")
            block = build_timeline_audio_integrity(
                status=status,
                timeline_duration=(
                    float(timeline_duration)
                    if isinstance(timeline_duration, (int, float))
                    else None
                ),
                container_duration=diag.get("container_duration"),
                stream_sample_duration=diag.get("stream_sample_duration"),
                repair=repair,
                source_duration_delta=diag.get("source_duration_delta"),
            )
            if repair_min_delta is not None:
                block["repair_min_delta"] = repair_min_delta
            if repair_cohort:
                block["repair_cohort"] = repair_cohort
            if status == "ok":
                if mutate_integrity:
                    clear_resolved_timeline_audio_integrity(ep, status)
            elif mutate_integrity and repair_selected and not stored_integrity_stamped:
                set_timeline_audio_integrity(ep, block)
            if _emit_timeline_audio_finding(
                status,
                diag,
                min_delta=finding_min_delta,
            ):
                duration_label = (
                    f"{timeline_duration:.3f}s"
                    if isinstance(timeline_duration, (int, float))
                    else "unknown"
                )
                findings.append(
                    Finding(
                        slug,
                        status,
                        severity,
                        f"{uid}: timeline={duration_label} "
                        f"stream={diag.get('stream_sample_duration')} "
                        f"container={diag.get('container_duration')} "
                        f"probe_error={diag.get('probe_error')} repair={repair or []}",
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
    run_history: list[dict] | None = None,
    probe_timeline_audio: Callable[[Episode], AudioDurationProbe | None] | None = None,
    probe_timeline_audio_full: Callable[[Episode], AudioDurationProbe | None] | None = None,
    timeline_diagnostics: list[dict] | None = None,
    mutate_timeline_integrity: bool = False,
    max_kbps: int = 96,
    timeline_repair_min_delta: float | None = None,
    timeline_repair_cohort: str | None = None,
    timeline_finding_min_delta: float = 1.0,
) -> list[Finding]:
    """Fetch a city once and run every applicable check, returning all findings.

    ``records`` is the full append-only archive (issue #109): it drives the three-way triage
    (backlog / dropped / regression) and the archive_newest staleness correction.
    ``resolve`` is the provider's media-URL re-resolver for dead-enclosure self-healing.
    """
    from citypods.records import assign_uids, merge_persisted
    from citypods.seeds import merge_seed_episodes

    try:
        episodes = merge_seed_episodes(city, provider.fetch_episodes(city.source))
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
        from citypods.bodies import matches

        dates = []
        for rec in records.values():
            if body and not matches(rec.get("body"), body):
                continue
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
            backlog = check_rehost_backlog(city.slug, episodes, run_history=run_history)
            if backlog:
                findings.append(backlog)
        if head is not None:
            findings.extend(check_enclosures(city.slug, episodes, head, resolve=resolve))
        # Timeline integrity: offline, always runs when records are present.
        if records is not None:
            findings.extend(
                check_timeline_integrity(
                    city.slug,
                    episodes,
                    probe_audio=probe_timeline_audio,
                    probe_audio_full=probe_timeline_audio_full,
                    diagnostics=timeline_diagnostics,
                    mutate_integrity=mutate_timeline_integrity,
                    max_kbps=max_kbps,
                    repair_min_delta=timeline_repair_min_delta,
                    repair_cohort=timeline_repair_cohort,
                    finding_min_delta=timeline_finding_min_delta,
                )
            )
            if mutate_timeline_integrity:
                sync_timeline_integrity_mutations(episodes, records)
    return findings


def sync_timeline_integrity_mutations(episodes: list[Episode], records: dict) -> None:
    """Copy back the two fields ``check_timeline_integrity`` may mutate in place on an
    ``Episode`` when ``mutate_integrity`` is set, into the raw ``records`` dict that actually
    gets persisted.

    ``episodes`` here is a fresh provider fetch overlaid with ``merge_persisted``, not a
    round-trip-safe copy of ``records`` — so a mutation left only on the ``Episode`` object
    (the ``integrity`` block, and issue #849's ``audio_duration_served`` self-heal) is silently
    lost the moment ``audit_city`` returns unless it is copied back here first.
    """
    for ep in episodes:
        uid = ep.uid or ""
        if not uid or uid not in records:
            continue
        if ep.integrity:
            records[uid] = {**records[uid], "integrity": ep.integrity}
        elif records[uid].get("integrity"):
            records[uid] = {**records[uid], "integrity": None}
        stored_audio = records[uid].get("audio") or {}
        if stored_audio.get("duration_served") != ep.audio_duration_served:
            records[uid] = {
                **records[uid],
                "audio": {**stored_audio, "duration_served": ep.audio_duration_served},
            }


def _load_run_history(state_dir: Path, *, limit: int = _STALL_LOOK_BACK) -> list[dict]:
    """Return the last *limit* entries from ``run_history.jsonl``, newest-last."""
    import json

    path = Path(state_dir) / "run_history.jsonl"
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows[-limit:]


def _net_probe() -> Callable[[str], tuple[int, str]]:
    """HEAD probe that returns ``(status_code, final_url)`` after following redirects."""
    from citypods.http import DEFAULT_TIMEOUT, make_session

    session = make_session()
    session.headers.update(
        {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Upgrade-Insecure-Requests": "1",
        }
    )

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


# Audio-lane-owned fields that a shared/coalesced artifact (GH#421's audio_artifact_cache) can
# leave inconsistent across two source-key stores of one uid (issue #850). Deliberately excludes
# ``attempts``/``last_attempt``/``error``/``error_spec_hash`` (per-source materialize backoff
# state, not a property of the shared artifact) and ``rebuild`` (a per-source repair nonce).
_CROSS_SOURCE_AUDIO_FIELDS = ("key", "url", "spec_hash", "bytes", "encode_time", "duration_served")

# Planning-lane fields (records.PLANNING_FIELDS, minus ``sources``) that should converge to one
# canonical copy alongside the audio fields above (issue #854). TimelineStage/ChaptersStage plan
# once per source-key store and never recompute ("chapters don't change once set"), so two stores
# sharing one physical uid can derive genuinely different chapters/timeline independently, with
# nothing to notice — this is also what produces #850-style audio_spec_hash divergence in the
# first place, since the spec hash is derived from these same fields.
_CROSS_SOURCE_PLANNING_FIELDS = ("chapters", "chapters_basis", "timeline")


def reconcile_cross_source_audio(
    records_by_source: dict[str, dict],
    entity_of_source: dict[str, str | None],
    status_by_source_uid: dict[tuple[str, str], str],
    *,
    mutate: bool,
) -> tuple[list[Finding], set[str]]:
    """Detect (and, when ``mutate``, correct) a uid whose audio-lane-owned or planning-lane-owned
    fields disagree across two or more source-key stores that share one ``city_entity``
    (issues #850 and #854).

    A combined feed and its per-board siblings are meant to share one record store
    (``source_key()`` deliberately ignores ``body``), but a combined feed's ``feed_urls`` can
    still legitimately or accidentally differ from its siblings', landing the same uid in two
    separate ``sources/<key>/episodes.json`` files. ``AudioArtifactCache.canonical_source``
    (GH#421) only synchronizes the audio-key *namespace* at the moment both sources need a
    fresh encode/credit in the very same run; it does not durably reconcile the two stores on
    every later run, so once each store looks internally consistent on its own, they can drift
    apart forever with nothing to notice. The same gap applies one layer upstream to
    ``chapters``/``chapters_basis``/``timeline`` (issue #854): each store's ``TimelineStage``/
    ``ChaptersStage`` plans once and never recomputes, so two stores can independently derive
    different chapters/timeline for what is the same physical meeting, which is what actually
    produces a different ``audio_spec_hash`` (and thus a different independently-encoded
    ``audio_key``) on each side in the first place.

    Canonical selection, per a uid's present-in sources: prefer whichever source this run's
    live probe classified ``"ok"`` (ground truth for what's actually served); if none or more
    than one source qualifies, fall back to the source with the newest ``audio_encode_time``;
    if that is also tied or unavailable, leave the uid unresolved (a finding is still emitted,
    no record is mutated) rather than guess.

    Returns ``(findings, touched_source_keys)`` — ``touched_source_keys`` is empty unless
    ``mutate`` is set, letting the caller persist only the sources actually corrected.
    """
    findings: list[Finding] = []
    touched: set[str] = set()

    by_entity: dict[str, list[str]] = {}
    for src_key, entity in entity_of_source.items():
        if not entity:
            continue
        by_entity.setdefault(entity, []).append(src_key)

    for entity, src_keys in by_entity.items():
        if len(src_keys) < 2:
            continue
        uid_sources: dict[str, list[str]] = {}
        for src_key in src_keys:
            for uid in records_by_source.get(src_key, {}):
                uid_sources.setdefault(uid, []).append(src_key)

        for uid, present_in in uid_sources.items():
            if len(present_in) < 2:
                continue
            audio_copies = {
                sk: (records_by_source[sk][uid].get("audio") or {}) for sk in present_in
            }
            integrity_copies = {
                sk: records_by_source[sk][uid].get("integrity") for sk in present_in
            }
            signatures = [
                (
                    tuple(audio_copies[sk].get(f) for f in _CROSS_SOURCE_AUDIO_FIELDS),
                    integrity_copies[sk],
                    tuple(records_by_source[sk][uid].get(f) for f in _CROSS_SOURCE_PLANNING_FIELDS),
                )
                for sk in present_in
            ]
            # Plain equality, not a set: integrity_copies/chapters/timeline values are dicts/lists
            # (unhashable).
            if all(sig == signatures[0] for sig in signatures):
                continue  # already converged

            ok_sources = [sk for sk in present_in if status_by_source_uid.get((sk, uid)) == "ok"]
            canonical: str | None = ok_sources[0] if len(ok_sources) == 1 else None
            if canonical is None:
                candidates = ok_sources if ok_sources else present_in
                dated = sorted(
                    (audio_copies[sk].get("encode_time"), sk)
                    for sk in candidates
                    if audio_copies[sk].get("encode_time")
                )
                if dated:
                    newest_time = dated[-1][0]
                    newest = [sk for t, sk in dated if t == newest_time]
                    if len(newest) == 1:
                        canonical = newest[0]

            slugs = ", ".join(sorted(present_in))
            if canonical is None:
                findings.append(
                    Finding(
                        entity,
                        "cross-source-audio-divergence",
                        WARN,
                        f"{uid}: audio and/or planning fields disagree across sources sharing "
                        f"city_entity {entity!r} ({slugs}) and no live-probe/encode-time tiebreak "
                        "resolved a canonical copy; left unresolved.",
                    )
                )
                continue

            findings.append(
                Finding(
                    entity,
                    "cross-source-audio-divergence",
                    WARN if mutate else ERROR,
                    f"{uid}: audio and/or planning fields disagree across sources sharing "
                    f"city_entity {entity!r} ({slugs}); canonical source is {canonical!r}"
                    + ("" if mutate else " (dry run: rerun with repair persistence to fix)"),
                )
            )
            if not mutate:
                continue

            canonical_audio = audio_copies[canonical]
            canonical_integrity = integrity_copies[canonical]
            canonical_rec = records_by_source[canonical][uid]
            for sk in present_in:
                if sk == canonical:
                    continue
                rec = records_by_source[sk][uid]
                new_audio = dict(rec.get("audio") or {})
                for f in _CROSS_SOURCE_AUDIO_FIELDS:
                    new_audio[f] = canonical_audio.get(f)
                # A credited-from-canonical follower's own materialize backoff/error history no
                # longer applies to the object it now points at (mirrors media._apply_artifact's
                # reset semantics for the same "adopt a shared artifact" situation).
                new_audio["attempts"] = 0
                new_audio["last_attempt"] = None
                new_audio["error"] = None
                new_audio["error_spec_hash"] = None
                records_by_source[sk][uid] = {
                    **rec,
                    "audio": new_audio,
                    "integrity": canonical_integrity,
                    **{f: canonical_rec.get(f) for f in _CROSS_SOURCE_PLANNING_FIELDS},
                }
                touched.add(sk)

    return findings, touched


def audit_all(
    cities: list[City],
    *,
    site_config: dict,
    output_dir: str | Path = "docs",
    check_enclosures_net: bool = False,
    check_meetings_urls_net: bool = False,
    now: datetime | None = None,
    timeline_diagnostics: list[dict] | None = None,
    persist_timeline_integrity: bool = False,
    timeline_repair_min_delta: float | None = None,
    timeline_repair_cohort: str | None = None,
    timeline_finding_min_delta: float = 1.0,
) -> list[Finding]:
    """Run every check across all cities. One fetch per city; ``view_counts`` and the
    per-source record store are gathered so the provider-specific checks apply."""
    import tempfile

    from citypods.media import _probe_audio_duration_details, _probe_audio_duration_header
    from citypods.providers import get_provider
    from citypods.records import load_records, save_records, source_key
    from citypods.state import resolve_state_dir
    from citypods.storage import make_storage

    now = now or datetime.now(UTC)
    state_dir = resolve_state_dir(site_config, Path(output_dir))
    defaults = site_config.get("defaults", {})
    min_meetings = int(defaults.get("min_meetings_per_body", 3))
    max_kbps = int(defaults.get("audio_max_kbps", 96))
    dead_threshold = int(defaults.get("dead_audio_alert_threshold", DEAD_AUDIO_ALERT_THRESHOLD))
    head = _net_head() if check_enclosures_net else None
    run_history = _load_run_history(state_dir)
    timeline_storage = None
    if timeline_diagnostics is not None:
        try:
            timeline_storage = make_storage(
                site_config, site_config.get("base_url", ""), output_dir
            )
        except Exception as exc:  # noqa: BLE001 - diagnostics must not take down the audit
            print(f"timeline diagnostics: storage unavailable ({exc})")

    def _probe_timeline_audio_full(ep: Episode) -> AudioDurationProbe | None:
        """Full-download probe: ground truth, and the only path when the header-only probe
        can't be used. Also invoked by ``check_timeline_integrity`` to reconcile against the
        header-only probe for episodes it already flagged as non-"ok"."""
        if timeline_storage is None:
            return AudioDurationProbe(probe_error="storage-unavailable")
        if not ep.audio_key:
            return AudioDurationProbe(probe_error="missing-audio-key")
        if not hasattr(timeline_storage, "get_file"):
            return AudioDurationProbe(probe_error="storage-read-unsupported")
        with tempfile.TemporaryDirectory() as t:
            dest = Path(t) / "audio.m4a"
            try:
                if not timeline_storage.get_file(ep.audio_key, dest):
                    return AudioDurationProbe(probe_error="download-failed")
                return _probe_audio_duration_details(dest)
            except Exception:  # noqa: BLE001 - one bad object becomes inconclusive diagnostics
                return AudioDurationProbe(probe_error="download-exception")

    def _probe_timeline_audio_header(ep: Episode) -> AudioDurationProbe | None:
        """Cheap default probe: range-read just the MP4 header (ftyp+moov) instead of
        downloading the whole hosted file — see ``media._probe_audio_duration_header``.
        Returns ``None`` (not an error probe) whenever that isn't possible — missing
        storage/key, a backend without ``get_range``, or the box walk giving up — so
        ``check_timeline_integrity``'s reconciliation step falls back to
        ``_probe_timeline_audio_full`` for that one episode instead of reporting a false
        result."""
        if timeline_storage is None or not ep.audio_key:
            return None
        if not hasattr(timeline_storage, "get_range"):
            return None

        def _get_range(start: int, end: int) -> bytes | None:
            try:
                return timeline_storage.get_range(ep.audio_key, start, end)
            except Exception:  # noqa: BLE001 - a read hiccup just falls back to full download
                return None

        try:
            return _probe_audio_duration_header(_get_range)
        except Exception:  # noqa: BLE001 - the fast path must never take down the audit
            return None

    findings: list[Finding] = []
    # Audio-failure tally is per *source* (per-body feeds share one record store), so accumulate
    # over unique source keys to avoid double-counting (issue #120). A representative slug per
    # source labels the prevalence examples.
    failures_by_source: dict[str, tuple[int, int]] = {}
    slug_by_source: dict[str, str] = {}
    media_too_large_sources: set[str] = set()
    view_cap_contexts: dict[str, tuple[str, str]] = {}
    # issue #850: cross-source audio-field reconciliation needs every source's final records
    # dict, which city_entity groups them, and (when diagnostics ran) each source's per-uid
    # live-probe verdict as the canonical-copy tiebreak.
    records_by_source: dict[str, dict] = {}
    entity_of_source: dict[str, str | None] = {}
    status_by_source_uid: dict[tuple[str, str], str] = {}
    for city in cities:
        provider = get_provider(city.provider)
        view_cap_contexts[city.slug] = (city.provider, city.city_entity or city.slug)

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
        entity_of_source[src_key] = city.city_entity
        failures_by_source.setdefault(src_key, count_audio_failures(records))
        slug_by_source.setdefault(src_key, city.slug)
        # Per-body feeds share one record store (issue #120's dedup shape); only scan a given
        # source once so a shared media-too-large rejection doesn't file once per body.
        if src_key not in media_too_large_sources:
            media_too_large_sources.add(src_key)
            findings.extend(check_media_too_large(city.slug, records))
        _diag_before = len(timeline_diagnostics) if timeline_diagnostics is not None else 0
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
                run_history=run_history,
                probe_timeline_audio=(
                    _probe_timeline_audio_header if timeline_diagnostics is not None else None
                ),
                probe_timeline_audio_full=(
                    _probe_timeline_audio_full if timeline_diagnostics is not None else None
                ),
                timeline_diagnostics=timeline_diagnostics,
                mutate_timeline_integrity=persist_timeline_integrity,
                max_kbps=max_kbps,
                timeline_repair_min_delta=timeline_repair_min_delta,
                timeline_repair_cohort=timeline_repair_cohort,
                timeline_finding_min_delta=timeline_finding_min_delta,
            )
        )
        if timeline_diagnostics is not None:
            for diag in timeline_diagnostics[_diag_before:]:
                status_by_source_uid[(src_key, diag["uid"])] = diag["check"]
        if persist_timeline_integrity and records:
            save_records(state_dir, src_key, records)
        # Last write for a src_key wins: multiple cities sharing one source_key each load+save
        # it independently in this same sequential loop, so the last one reflects every prior
        # sibling's self-heal by the time we get here.
        records_by_source[src_key] = records

    cross_findings, cross_touched = reconcile_cross_source_audio(
        records_by_source,
        entity_of_source,
        status_by_source_uid,
        mutate=persist_timeline_integrity,
    )
    findings.extend(cross_findings)
    for touched_key in cross_touched:
        save_records(state_dir, touched_key, records_by_source[touched_key])

    findings = aggregate_view_cap_findings(findings, view_cap_contexts)

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

    findings.extend(check_provider_error_rates(run_history))

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
