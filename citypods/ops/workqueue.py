"""Backlog work queue: the ``WorkItem`` schema, comparator registry, and ordering policy.

This is the H5 **ordering engine** (PR1). It decides, given the configured
``backlog_priority`` policy, the order in which pending work is processed within the
wall-clock enrich window. The design is locked in
[`review/12` §H5](../../review/12-hardening-and-efficiency.md).

Scope boundaries (see the "Decisions locked" block in review/12 §H5):

* **Hybrid manifest.** The per-source records stay canonical; the pending set is *derived*,
  not persisted. So this module is pure in-memory ordering — the durable sidecar
  (leases / backoff / observed timings) and cross-source dispatch land in **PR2**.
* **Behavior-preserving default.** With no policy configured, :func:`order` is the identity
  (returns its input unchanged), so wiring it into ``_materialize_set`` cannot change output.
* **Diarization-forward schema, reserved now.** ``WorkItem`` is keyed by *output artifact*
  (``work_class``) and carries ``stage_version`` + ``input_hashes`` for surgical invalidation
  and groupable ``lease_*`` fields, so a future ``diarization`` / ``transcript-merge`` lane (and
  whisperX-style fused execution on a GPU backend) slots in with no manifest migration. None of
  those fields are populated in H5 — they're reserved.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from citypods.bodies import body_key, canonical_body, rank_by_body
from citypods.durations import episode_duration_hours, record_duration_hours
from citypods.models import City, Episode
from citypods.records import load_records
from citypods.records import source_key as record_source_key

if TYPE_CHECKING:
    from citypods.transcript_quality import TranscriptQualityRoute

MANIFEST_NAME = "work.json"
MANIFEST_VERSION = 1

# Work classes are keyed by OUTPUT ARTIFACT (diarization-forward; review/12 §H5).
WORK_CLASSES = (
    "audio",
    "transcript-asr",
    "transcript-align",
    "provider-transcript-align",
    "provider-transcript-diarize",
)
# Reserved — recognized but not emitted yet (reserve-now, no migration later).
# ``transcript-diarize`` is the future diarize-only queue for episodes whose transcript was
# produced by the GitHub ASR lane; external GPU workers will claim it once diarization is enabled.
RESERVED_WORK_CLASSES = ("transcript-merge", "transcript-diarize")

# Work classes the ``long_first`` comparator (H13/H14) is allowed to reorder: every
# transcript-producing lane, since those are the ones a capped *external* GPU free tier is the
# only path for once an episode exceeds ``asr_local_max_duration_hours`` (the in-process backend
# refuses it outright — ``stages._asr_local_duration_eligible``). ``audio`` is deliberately
# excluded: it is not capacity-gated by duration the same way (a long encode gets a bigger memory
# reservation, never a refusal), so reordering it would just delay publishing the common case of
# short meetings with no corresponding benefit. Reserved transcript lanes are included for forward
# compatibility even though H5 does not yet emit them.
DURATION_AWARE_WORK_CLASSES = (frozenset(WORK_CLASSES) - {"audio"}) | frozenset(
    RESERVED_WORK_CLASSES
)

# Priority buckets.  The full-artifact archive tier is active: it is materialized after the
# feed-visible cohort, under the existing wall-clock budget.  Deep archive is metadata-only.
BUCKET_FEED_VISIBLE = "feed_visible"
BUCKET_RECENT_ARCHIVE = "recent_archive"
BUCKET_DEEP_ARCHIVE = "deep_archive"

# Comparator keys that are named/recognized but not yet implemented. Referencing one in a
# policy is a clear error (not a silent no-op), so the config stays honest.
RESERVED_KEYS = frozenset({"requested_first", "strong_towns_first", "population"})


@dataclass
class WorkItem:
    """One unit of backlog work — an (episode, output-artifact) pair.

    Only the ordering inputs (``published`` / ``city_slug`` / ``body`` / ``priority_bucket`` /
    ``duration_hours``) are exercised in H5. The remaining fields are the reserved
    persistence/diarization schema (PR2 + future); they carry inert defaults today.
    """

    # identity
    source_key: str
    episode_uid: str
    work_class: str
    # ordering inputs
    published: datetime | None = None
    city_slug: str = ""
    body: str = ""
    priority_bucket: str = BUCKET_FEED_VISIBLE
    # The episode's served (preferred) or source duration in hours, 0.0 when unknown. Feeds the
    # ``long_first`` comparator (H13/H14); not exercised by any other H5 comparator.
    duration_hours: float = 0.0
    # --- reserved (PR2 sidecar + diarization-forward schema); not populated in H5 ---
    stage_version: str = ""
    input_hashes: tuple[str, ...] = ()
    state: str = "queued"  # queued | running | done | backoff | dead
    est_seconds: float = 0.0
    observed_seconds: float = 0.0
    last_error: str = ""
    next_retry: datetime | None = None
    lease_owner: str = ""  # groupable: one owner may hold several items of one episode
    lease_expires: datetime | None = None


def workitem_from_episode(
    ep: Episode,
    *,
    source_key: str = "",
    city_slug: str = "",
    work_class: str = "audio",
    priority_bucket: str = BUCKET_FEED_VISIBLE,
) -> WorkItem:
    """Build a (transient) ordering view of an episode. Reused by ``_materialize_set`` (PR1)
    and the cross-source pending-set build (PR2)."""
    return WorkItem(
        source_key=source_key,
        episode_uid=ep.uid or ep.guid,
        work_class=work_class,
        published=ep.published,
        city_slug=city_slug,
        body=ep.body or "",
        priority_bucket=priority_bucket,
        duration_hours=episode_duration_hours(ep)[0],
    )


# --------------------------------------------------------------------------------------------
# Comparator registry. Each builder takes the parsed params + run-global inputs (the
# ``city_order`` list and a ``now`` reference) and returns a ``key_fn: WorkItem -> sortable``.
# ``order`` sorts ascending by the lexicographic tuple of the policy's key_fns, so a key_fn
# yields a *smaller* value for work that should run *earlier*.
# --------------------------------------------------------------------------------------------


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _age_days(dt: datetime | None, now: datetime) -> float:
    """Age in days; missing dates are treated as infinitely old (sort last / out of window)."""
    if dt is None:
        return math.inf
    return (now - _aware(dt)).total_seconds() / 86_400.0


def _build_recency(params, *, city_order, now):
    """`recency` — `desc` (newest first, default) / `asc` (oldest first), optional
    `within_days: N` horizon. Inside the window, sort by date; **beyond it, collapse to a
    constant** so the *next* key governs the backlog instead of the date neutering it."""
    if params is None or isinstance(params, str):
        order_, within = (params or "desc"), None
    elif isinstance(params, dict):
        order_, within = str(params.get("order", "desc")), params.get("within_days")
    else:
        raise ValueError(f"recency params must be a string or mapping, got {params!r}")
    if order_ not in ("asc", "desc"):
        raise ValueError(f"recency order must be 'asc' or 'desc', got {order_!r}")
    desc = order_ == "desc"

    if within is None:
        # Unbounded global date sort. Missing dates sort last regardless of direction.
        def key(wi: WorkItem):
            if wi.published is None:
                return math.inf
            ts = _aware(wi.published).timestamp()
            return -ts if desc else ts

        return key

    horizon = float(within)
    if horizon <= 0:
        raise ValueError(f"recency within_days must be > 0, got {within!r}")

    def key(wi: WorkItem):
        if _age_days(wi.published, now) <= horizon:
            ts = _aware(wi.published).timestamp()  # in-window ⇒ published is not None
            return (0, -ts if desc else ts)
        return (1, 0.0)  # out of window ⇒ all tie ⇒ next key governs

    return key


def _build_recent_first(params, *, city_order, now):
    """`recent_first: N` — boolean bucket: within N days beats older. Within each bucket the
    *next* key orders (unlike windowed `recency`, which also date-sorts inside the window)."""
    if params is None:
        raise ValueError("recent_first requires a day count, e.g. `recent_first: 30`")
    horizon = float(params)
    if horizon <= 0:
        raise ValueError(f"recent_first days must be > 0, got {params!r}")

    def key(wi: WorkItem):
        return 0 if _age_days(wi.published, now) <= horizon else 1

    return key


def _build_city_order(params, *, city_order, now):
    """`city_order` — explicit slug ranking. Named cities rank by index; **every unnamed city
    shares a sentinel rank** and falls through to the next key. An inline list (``city_order:
    [a, b]`` as the param) overrides the top-level ``city_order``."""
    order_list = params if isinstance(params, list) else city_order
    rank = {slug: i for i, slug in enumerate(order_list)}
    sentinel = len(order_list)

    def key(wi: WorkItem):
        return rank.get(wi.city_slug, sentinel)

    return key


def _build_body_order(params, *, city_order, now):
    """`body_order` — an explicit body ranking (inline list of body names), else a deterministic
    alphabetical tiebreak by normalized body key."""
    if isinstance(params, list):
        rank = {body_key(b): i for i, b in enumerate(params)}
        sentinel = len(params)

        def key(wi: WorkItem):
            bk = body_key(wi.body)
            return (rank.get(bk, sentinel), bk)

        return key

    def key(wi: WorkItem):
        return (0, body_key(wi.body))

    return key


def _build_feed_visible_first(params, *, city_order, now):
    """`feed_visible_first` — publishable work before the active full-artifact backfill.

    Metadata-only records are never scheduled, but retain the final rank for defensive callers.
    """

    def key(wi: WorkItem):
        return {
            BUCKET_FEED_VISIBLE: 0,
            BUCKET_RECENT_ARCHIVE: 1,
            BUCKET_DEEP_ARCHIVE: 2,
        }.get(wi.priority_bucket, 2)

    return key


def _build_long_first(params, *, city_order, now):
    """`long_first: N` — binary bucket: pending transcript-lane work over *N* hours ranks first,
    catalog-wide, ahead of every later configured key (e.g. `recency`/`city_order` then govern
    ordering *within* each band). ``from_site_config`` resolves a bare `long_first` (no params) to
    the site's configured `defaults.asr_local_max_duration_hours` before calling this builder, so
    the prioritization boundary can't silently drift from the local-backend capability boundary —
    pass an explicit value to prioritize a different cutoff.

    Exists because once an episode exceeds `asr_local_max_duration_hours` the in-process ASR
    backend refuses it outright (`stages._asr_local_duration_eligible`) — a capped *external* GPU
    free tier (H13/H14) is its only path to ever being transcribed. With no duration awareness, a
    steady stream of short episodes (which have a working local fallback) can keep consuming that
    scarce external budget in arrival order while long ones starve behind them indefinitely.

    Scoped to :data:`DURATION_AWARE_WORK_CLASSES` — an `audio` item's key is always the low-rank
    constant (falls straight through to the next key), since audio is not capacity-gated by
    duration the same way."""
    if params is None:
        raise ValueError(
            "long_first requires an hours threshold, e.g. `long_first: 4` "
            "(from_site_config defaults this to defaults.asr_local_max_duration_hours)"
        )
    threshold = float(params)
    if threshold <= 0:
        raise ValueError(f"long_first hours must be > 0, got {params!r}")

    def key(wi: WorkItem):
        if wi.work_class not in DURATION_AWARE_WORK_CLASSES:
            return 1
        return 0 if wi.duration_hours > threshold else 1

    return key


_BUILDERS: dict[str, Callable[..., Callable[[WorkItem], object]]] = {
    "recency": _build_recency,
    "recent_first": _build_recent_first,
    "city_order": _build_city_order,
    "body_order": _build_body_order,
    "feed_visible_first": _build_feed_visible_first,
    "long_first": _build_long_first,
}


@dataclass(frozen=True)
class SortKey:
    name: str
    key_fn: Callable[[WorkItem], object]


def _split_entry(entry) -> tuple[str, object]:
    """A `backlog_priority` entry is either a bare key (`city_order`) or a single-key mapping
    (`{recency: desc}` / `{recency: {order: desc, within_days: 30}}`)."""
    if isinstance(entry, str):
        return entry, None
    if isinstance(entry, dict) and len(entry) == 1:
        ((name, params),) = entry.items()
        return name, params
    raise ValueError(f"invalid backlog_priority entry: {entry!r}")


@dataclass(frozen=True)
class BacklogPolicy:
    """An ordered tuple of comparator keys. Empty ⇒ the behavior-preserving identity order."""

    keys: tuple[SortKey, ...] = ()

    @classmethod
    def from_site_config(cls, site_config: dict, *, now: datetime | None = None) -> BacklogPolicy:
        now = now or datetime.now(UTC)
        defaults = site_config.get("defaults") or {}
        raw = site_config.get("backlog_priority") or defaults.get("backlog_priority") or []
        city_order = list(site_config.get("city_order") or defaults.get("city_order") or [])
        keys: list[SortKey] = []
        for entry in raw:
            name, params = _split_entry(entry)
            if name in RESERVED_KEYS:
                raise ValueError(
                    f"backlog_priority comparator {name!r} is reserved but not yet implemented"
                )
            builder = _BUILDERS.get(name)
            if builder is None:
                raise ValueError(
                    f"unknown backlog_priority comparator {name!r}; known: {sorted(_BUILDERS)}"
                )
            if name == "long_first" and params is None:
                # Resolve the implicit default here (config normalization), not inside the
                # builder, so the comparator registry's call convention stays uniform across
                # every key — only `long_first` has a site-wide default to fall back to.
                params = defaults.get("asr_local_max_duration_hours", 4.0)
            keys.append(SortKey(name, builder(params, city_order=city_order, now=now)))
        return cls(keys=tuple(keys))


def sort_key_for(policy: BacklogPolicy) -> Callable[[WorkItem], tuple]:
    """The composite lexicographic sort key for a policy (first key primary)."""
    fns = [k.key_fn for k in policy.keys]

    def key(wi: WorkItem) -> tuple:
        return tuple(fn(wi) for fn in fns)

    return key


def order(items: Sequence[WorkItem], policy: BacklogPolicy | None) -> list[WorkItem]:
    """Return *items* ordered by *policy*. With no policy (or an empty one) this is the
    **identity** — a stable passthrough that preserves the caller's order byte-for-byte."""
    if policy is None or not policy.keys:
        return list(items)
    return sorted(items, key=sort_key_for(policy))


# --------------------------------------------------------------------------------------------
# Manifest derivation (PR2). The pending set is DERIVED from the canonical records each run
# (hybrid model) — this is not authoritative state. It backs the status surface and is the
# substrate the durable sidecar + H6b leasing build on.
# --------------------------------------------------------------------------------------------


def _published_of(rec: dict) -> datetime | None:
    raw = rec.get("published")
    if not raw:
        return None
    try:
        return _aware(datetime.fromisoformat(raw))
    except (TypeError, ValueError):
        return None


def _episode_buckets(
    recs: dict, feed_visible_per_body: int, full_artifact_per_body: int
) -> dict[str, str]:
    """Classify retained records into feed, full-artifact, and metadata-only cohorts."""
    buckets: dict[str, str] = {}
    for items in rank_by_body(
        recs.items(),
        body_of=lambda kv: kv[1].get("body"),
        published_of=lambda kv: _published_of(kv[1]),
    ):
        for i, (uid, _rec) in enumerate(items):
            buckets[uid] = (
                BUCKET_FEED_VISIBLE
                if i < feed_visible_per_body
                else BUCKET_RECENT_ARCHIVE
                if i < full_artifact_per_body
                else BUCKET_DEEP_ARCHIVE
            )
    return buckets


def _provider_transcript_entry(registry: object) -> dict | None:
    if not isinstance(registry, dict):
        return None
    for slot in ("candidate", "known_good"):
        entry = registry.get(slot)
        if (
            isinstance(entry, dict)
            and entry.get("key")
            and entry.get("format") in {"txt", "vtt", "srt"}
        ):
            return entry
    return None


def _quality_route_for(
    source_key: str,
    rec: dict,
    routes: Mapping[tuple[str, str], TranscriptQualityRoute] | None,
) -> TranscriptQualityRoute | None:
    if not routes:
        return None
    from citypods.transcript_quality import quality_body_key

    body = canonical_body(rec.get("body") or "") or rec.get("body") or ""
    return routes.get((source_key, quality_body_key(body)))


def _transcript_class(rec: dict, *, route: TranscriptQualityRoute | None = None) -> str:
    """Classify the next transcript artifact this episode can produce.

    Every provider source document is the provider-derived lane. Only a provider VTT that already
    carries word timing can bypass our aligner; cue-only VTT/SRT and raw TXT still need computed
    word boundaries. A missing provider source uses fresh ASR.
    """
    if route is not None and route.prefers_fresh_asr:
        return "transcript-asr"
    if _provider_transcript_entry(rec.get("provider_transcript")) is not None:
        return "provider-transcript-align"
    has_source_text = bool((rec.get("links") or {}).get("transcript"))
    return "provider-transcript-align" if has_source_text else "transcript-asr"


def _episode_work_items(
    source_key: str,
    city: City,
    uid: str,
    rec: dict,
    bucket: str,
    *,
    transcript_quality_routes: Mapping[tuple[str, str], TranscriptQualityRoute] | None = None,
) -> list[WorkItem]:
    """The work items one episode contributes: an ``audio`` item when it should be hosted, and a
    transcript item (asr/align, done/queued/alignment-disabled) when hosted audio exists."""
    base = dict(
        source_key=source_key,
        episode_uid=uid,
        published=_published_of(rec),
        city_slug=city.slug,
        body=rec.get("body") or "",
        priority_bucket=bucket,
        duration_hours=record_duration_hours(rec)[0],
    )
    items: list[WorkItem] = []
    audio_done = bool((rec.get("audio") or {}).get("url"))
    if rec.get("media_kind") == "hls" or bool(city.extract_audio) or audio_done:
        items.append(WorkItem(work_class="audio", state="done" if audio_done else "queued", **base))

    if audio_done and city.asr_enabled:
        route = _quality_route_for(source_key, rec, transcript_quality_routes)
        work_class = _transcript_class(rec, route=route)
        transcript = rec.get("transcript") or {}
        provider = _provider_transcript_entry(rec.get("provider_transcript"))
        if work_class == "provider-transcript-align":
            # CR2-CP-23: an old ASR-produced transcript.key (no "-provider-align-" marker) is
            # not proof this class is done — it must be the provider-align artifact itself, at
            # the provider's current align_spec_hash, or this episode still needs that work.
            native_done = (
                provider is not None
                and bool(provider.get("word_timed"))
                and transcript.get("key") == provider.get("key")
                and transcript.get("selection") == "provider-native"
                and bool(transcript.get("words_key"))
            )
            aligned_done = (
                bool(transcript.get("key"))
                and "-provider-align-" in str(transcript.get("key"))
                and provider is not None
                and transcript.get("spec_hash") == provider.get("align_spec_hash")
                and bool(transcript.get("words_key"))
            )
            transcript_done = native_done or aligned_done
        else:
            # A source/body fresh-ASR route must not mistake an older provider artifact for a
            # completed ASR result. The route is intentionally able to replace the served source.
            transcript_key = str(transcript.get("key") or "")
            provider_source_exists = provider is not None or bool(
                (rec.get("links") or {}).get("transcript")
            )
            transcript_done = bool(transcript_key) and (
                not provider_source_exists
                or transcript.get("selection") == "asr"
                or f"{uid}-asr-" in transcript_key.rsplit("/", 1)[-1]
            )
        align_lane_unblocked = route is not None and route.prefers_provider_align
        if transcript_done:
            items.append(WorkItem(work_class=work_class, state="done", **base))
        elif (
            work_class == "transcript-align"
            and not city.asr_alignment_enabled
            and not align_lane_unblocked
        ):
            items.append(
                WorkItem(work_class="transcript-align", state="alignment-disabled", **base)
            )
        else:
            items.append(WorkItem(work_class=work_class, state="queued", **base))
        speakers = rec.get("speakers") or {}
        active_provider_align = (
            provider is not None
            and transcript.get("key")
            and "-provider-align-" in str(transcript.get("key"))
            and transcript.get("spec_hash") == provider.get("align_spec_hash")
        )
        if active_provider_align:
            state = (
                "done"
                if speakers.get("key")
                and speakers.get("spec_hash") == provider.get("diarize_spec_hash")
                else "queued"
            )
            items.append(WorkItem(work_class="provider-transcript-diarize", state=state, **base))
    return items


def build_manifest(
    sources: Sequence[tuple[str, City, dict]],
    *,
    policy: BacklogPolicy | None = None,
    now: datetime | None = None,
    transcript_quality_routes: Mapping[tuple[str, str], TranscriptQualityRoute] | None = None,
) -> list[WorkItem]:
    """Derive the work manifest from per-source records.

    *sources* is a list of ``(source_key, representative_city, records)`` — one entry per unique
    source (board feeds sharing a source must be deduplicated by the caller, else episodes
    double-count). Emits an ``audio`` item for every episode that should be hosted and a
    ``transcript-asr`` / ``transcript-align`` item for every hosted, ASR-enabled episode, each
    tagged ``done`` / ``queued`` / ``alignment-disabled`` and bucketed feed_visible vs deep_archive.
    """
    items: list[WorkItem] = []
    for source_key, city, recs in sources:
        buckets = _episode_buckets(recs, city.max_episodes, city.full_artifact_episodes)
        for uid, rec in recs.items():
            items.extend(
                _episode_work_items(
                    source_key,
                    city,
                    uid,
                    rec,
                    buckets.get(uid, BUCKET_FEED_VISIBLE),
                    transcript_quality_routes=transcript_quality_routes,
                )
            )

    if policy is not None and policy.keys:
        items = order(items, policy)
    return items


def manifest_counts(items: Sequence[WorkItem]) -> dict:
    """Aggregate actionable work — split by tier so the name matches the content — and the
    metadata-only archive total.

    ``feed_visible_pending`` is genuinely feed-visible (rank <= ``max_episodes`` per body);
    ``archive_backfill_pending`` is the active but never-published 501–2,000 cohort. Keeping
    these separate avoids the earlier drift where ``feed_visible_pending`` silently grew to
    include backfill work once the archive-backfill tier went active (review/39).
    """
    by_work_class: dict[str, dict[str, int]] = {}
    deep_archive = 0
    alignment_disabled = 0
    feed_visible_pending = 0
    archive_backfill_pending = 0
    for it in items:
        if it.priority_bucket == BUCKET_DEEP_ARCHIVE:
            deep_archive += 1
            continue
        states = by_work_class.setdefault(it.work_class, {})
        states[it.state] = states.get(it.state, 0) + 1
        if it.state == "alignment-disabled":
            alignment_disabled += 1
        if it.state == "queued":
            if it.priority_bucket == BUCKET_RECENT_ARCHIVE:
                archive_backfill_pending += 1
            else:
                feed_visible_pending += 1
    return {
        "by_work_class": by_work_class,
        "feed_visible_pending": feed_visible_pending,
        "archive_backfill_pending": archive_backfill_pending,
        "alignment_disabled": alignment_disabled,
        "deep_archive_items": deep_archive,
    }


def order_cities_by_policy(
    cities: Sequence[City],
    records_by_slug: dict[str, dict],
    policy: BacklogPolicy | None,
    *,
    now: datetime | None = None,
) -> list[City]:
    """Order cities for pool submission by each city's highest-priority pending work (proxied by
    its newest episode). Coarse cross-source prioritization — a started city still drains its own
    (within-source-ordered) backlog. Identity when no policy. True global interleaving is PR3."""
    if policy is None or not policy.keys:
        return list(cities)
    key = sort_key_for(policy)

    def city_key(city: City):
        recs = records_by_slug.get(city.slug) or {}
        newest: datetime | None = None
        for rec in recs.values():
            p = _published_of(rec)
            if p is not None and (newest is None or p > newest):
                newest = p
        proxy = WorkItem(
            source_key=city.slug,
            episode_uid="",
            work_class="audio",
            published=newest,
            city_slug=city.slug,
        )
        return key(proxy)

    return sorted(cities, key=city_key)


# --------------------------------------------------------------------------------------------
# Durable sidecar (PR2). The manifest snapshot is persisted to ``state/work.json`` (auto-synced
# by statesync). Leases are the H6b substrate: the API is built + tested here, but nothing
# competitively acquires a lease until concurrent ASR workflows arrive (H6b).
# --------------------------------------------------------------------------------------------


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _parse_dt(raw) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None


def _workitem_to_dict(wi: WorkItem) -> dict:
    return {
        "source_key": wi.source_key,
        "episode_uid": wi.episode_uid,
        "work_class": wi.work_class,
        "published": _iso(wi.published),
        "city_slug": wi.city_slug,
        "body": wi.body,
        "priority_bucket": wi.priority_bucket,
        # ``duration_hours`` is a computed ordering input (from ``audio.duration_served``), not an
        # inert reserved field: ``long_first`` (H13/H14) and the report duration-band read it back
        # off the persisted manifest, so it must round-trip. Omitting it silently reset every
        # reloaded item to 0.0h — which read as "unknown duration," stalling ``long_first`` and
        # making 100% of the feed-visible transcript-asr backlog look length-unknown even though
        # the records carried a real served duration.
        "duration_hours": wi.duration_hours,
        "state": wi.state,
        "stage_version": wi.stage_version,
        "input_hashes": list(wi.input_hashes),
        "est_seconds": wi.est_seconds,
        "observed_seconds": wi.observed_seconds,
        "last_error": wi.last_error,
        "next_retry": _iso(wi.next_retry),
        "lease_owner": wi.lease_owner,
        "lease_expires": _iso(wi.lease_expires),
    }


def _workitem_from_dict(d: dict) -> WorkItem:
    return WorkItem(
        source_key=d.get("source_key", ""),
        episode_uid=d.get("episode_uid", ""),
        work_class=d.get("work_class", ""),
        published=_parse_dt(d.get("published")),
        city_slug=d.get("city_slug", ""),
        body=d.get("body", ""),
        priority_bucket=d.get("priority_bucket", BUCKET_FEED_VISIBLE),
        duration_hours=float(d.get("duration_hours", 0.0)),
        state=d.get("state", "queued"),
        stage_version=d.get("stage_version", ""),
        input_hashes=tuple(d.get("input_hashes") or ()),
        est_seconds=float(d.get("est_seconds", 0.0)),
        observed_seconds=float(d.get("observed_seconds", 0.0)),
        last_error=d.get("last_error", ""),
        next_retry=_parse_dt(d.get("next_retry")),
        lease_owner=d.get("lease_owner", ""),
        lease_expires=_parse_dt(d.get("lease_expires")),
    )


def save_manifest(state_dir: str | Path, items: Sequence[WorkItem]) -> Path:
    path = Path(state_dir) / MANIFEST_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": MANIFEST_VERSION, "items": [_workitem_to_dict(i) for i in items]}
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return path


def load_manifest(state_dir: str | Path) -> list[WorkItem]:
    path = Path(state_dir) / MANIFEST_NAME
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    return [_workitem_from_dict(d) for d in data.get("items", [])]


def lease(wi: WorkItem, owner: str, *, ttl_seconds: float, now: datetime | None = None) -> WorkItem:
    """Claim *wi* for *owner* until ``now + ttl_seconds``. Groupable: a caller may lease several
    items of one episode under the same owner (e.g. fused ASR + diarization). H6b substrate."""
    now = now or datetime.now(UTC)
    wi.lease_owner = owner
    wi.lease_expires = now + timedelta(seconds=ttl_seconds)
    wi.state = "running"
    return wi


def release(wi: WorkItem) -> WorkItem:
    wi.lease_owner = ""
    wi.lease_expires = None
    return wi


def is_leased(wi: WorkItem, *, now: datetime | None = None) -> bool:
    """True if *wi* holds an unexpired lease."""
    now = now or datetime.now(UTC)
    return bool(wi.lease_owner) and wi.lease_expires is not None and wi.lease_expires > now


def overlay_leases(
    items: Sequence[WorkItem],
    leases: dict[tuple[str, str, str], tuple[str, datetime | None]],
) -> list[WorkItem]:
    """Re-apply live dispatch leases onto a freshly :func:`build_manifest`-ed list (H14a).

    ``build_manifest`` rebuilds the manifest from records each run, which would reset an in-flight
    item back to ``queued``. The dispatcher hands its live leases here — keyed by
    ``(source_key, episode_uid, work_class)`` → ``(lease_owner, lease_expires)`` — so those items
    keep ``state="running"`` with their lease across the rebuild, and the next ``compute reconcile``
    can settle or reap them. A ``done`` item (its artifact already landed) is never overlaid:
    completion wins over an in-flight lease."""
    if not leases:
        return list(items)
    for wi in items:
        if wi.state == "done":
            continue
        entry = leases.get((wi.source_key, wi.episode_uid, wi.work_class))
        if entry is None:
            continue
        wi.lease_owner, wi.lease_expires = entry
        wi.state = "running"
    return list(items)


def overlay_persisted_operational_state(
    items: Sequence[WorkItem],
    persisted: Sequence[WorkItem],
) -> list[WorkItem]:
    """Overlay non-derivable operational state from persisted ``work.json`` onto a freshly
    derived manifest."""
    persisted_by_key = {(it.source_key, it.episode_uid, it.work_class): it for it in persisted}
    out: list[WorkItem] = []
    for item in items:
        if item.state == "done":
            out.append(item)
            continue
        prev = persisted_by_key.get((item.source_key, item.episode_uid, item.work_class))
        if prev is None:
            out.append(item)
            continue
        if prev.lease_owner or prev.state in {"running", "backoff", "dead"}:
            item.state = prev.state
            item.lease_owner = prev.lease_owner
            item.lease_expires = prev.lease_expires
            item.last_error = prev.last_error
            item.next_retry = prev.next_retry
            item.observed_seconds = prev.observed_seconds
            item.est_seconds = prev.est_seconds
        out.append(item)
    return out


def rebuild_manifest_from_state(
    cities: Sequence[City],
    *,
    site_config: dict,
    state_dir: str | Path,
) -> list[WorkItem]:
    """Rebuild the discovery index from the restored record snapshot, then re-apply the live
    operational sidecar from persisted ``work.json``.

    This is the Stage-2 split in review/18 §4.1: the candidate set/order is derived from current
    records and backlog policy, while mutable lease/backoff state rides the persisted overlay.
    """
    state_dir = Path(state_dir)
    persisted = load_manifest(state_dir)
    if not cities:
        return persisted
    defaults = site_config.get("defaults") or {}
    backlog_policy = None
    if site_config.get("backlog_priority") or defaults.get("backlog_priority"):
        backlog_policy = BacklogPolicy.from_site_config(site_config)
    # build_manifest() requires one entry per unique source_key (its docstring: "board feeds
    # sharing a source must be deduplicated by the caller, else episodes double-count") — cities
    # sharing a source_key (multi-board feeds of one entity) must collapse to one representative
    # city here, same as external_worker.py's _manifest() did before it moved onto this function.
    city_by_source: dict[str, City] = {}
    for city in cities:
        city_by_source.setdefault(record_source_key(city), city)
    manifest_sources = [
        (key, city, load_records(state_dir, key)) for key, city in city_by_source.items()
    ]
    from citypods.transcript_quality import load_quality_routes

    quality_routes = load_quality_routes(site_config, state_dir)
    derived = build_manifest(
        manifest_sources,
        policy=backlog_policy,
        transcript_quality_routes=quality_routes,
    )
    return overlay_persisted_operational_state(derived, persisted)
