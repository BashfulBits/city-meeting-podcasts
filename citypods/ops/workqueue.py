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

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from citypods.bodies import body_key
from citypods.models import Episode

# Work classes are keyed by OUTPUT ARTIFACT (diarization-forward; review/12 §H5).
WORK_CLASSES = ("audio", "transcript-asr", "transcript-align")
# Reserved — recognized but not emitted in H5 (reserve-now, no migration later).
RESERVED_WORK_CLASSES = ("diarization", "transcript-merge")

# Priority buckets. feed-visible ≡ materialized today, so the archive buckets are
# reserved-but-inert until the opt-in archive-backfill feature populates them (review/12 §H5).
BUCKET_FEED_VISIBLE = "feed_visible"
BUCKET_RECENT_ARCHIVE = "recent_archive"
BUCKET_DEEP_ARCHIVE = "deep_archive"

# Comparator keys that are named/recognized but not yet implemented. Referencing one in a
# policy is a clear error (not a silent no-op), so the config stays honest.
RESERVED_KEYS = frozenset({"requested_first", "strong_towns_first", "population"})


@dataclass
class WorkItem:
    """One unit of backlog work — an (episode, output-artifact) pair.

    Only the ordering inputs (``published`` / ``city_slug`` / ``body`` / ``priority_bucket``)
    are exercised in H5. The remaining fields are the reserved persistence/diarization schema
    (PR2 + future); they carry inert defaults today.
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
    """`feed_visible_first` — rendered episodes before deep archive. **Inert today**: every
    pending item is `feed_visible` until the opt-in archive-backfill feature lands."""

    def key(wi: WorkItem):
        return 0 if wi.priority_bucket == BUCKET_FEED_VISIBLE else 1

    return key


_BUILDERS: dict[str, Callable[..., Callable[[WorkItem], object]]] = {
    "recency": _build_recency,
    "recent_first": _build_recent_first,
    "city_order": _build_city_order,
    "body_order": _build_body_order,
    "feed_visible_first": _build_feed_visible_first,
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
