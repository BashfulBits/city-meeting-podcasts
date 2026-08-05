"""Edit Decision List (EDL) model for the served audio timeline.

A meeting's served enclosure is an ordered list of segments cut from one or more source
media files (plus optional synthetic inserts). This module is the single authority on the
mapping between *served* time (what a listener hears) and *source* time (what the city's
video platform shows) so that every downstream artifact — chapters, transcripts, soundbites,
deep-links — works off the same translation, regardless of which audio-manipulation
features are active for a given episode.

Pure and dependency-free (like ``projection.py``): no I/O, no pipeline wiring.

Time-basis convention (enforced here, referenced everywhere)
-----------------------------------------------------------
Every timestamped artifact carries a basis tag:

  * ``"served"`` — seconds into the hosted enclosure; what players scrub.
  * ``"source:<id>"`` — seconds into a specific source (e.g. ``"source:s0"``).

The render layer always emits **served**-time offsets. Provider chapters/SRT arrive in
source time and are converted via :func:`remap`. ASR output is born in served time
(transcribed from the served file) and needs no conversion.

The identity timeline (backward-compatibility keystone)
------------------------------------------------------
An episode with no audio manipulation has exactly one source segment:
``served=[0, D]`` mapping to ``source_s0=[0, D]``. For that episode:

  * :func:`served_to_source` / :func:`source_to_served` are the identity map.
  * :func:`timeline_digest` returns ``""`` — so ``audio_spec_hash`` is unchanged, and
    the episode never re-encodes when this model ships.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class SourceMedia:
    """One original media source contributing to an episode's served audio."""

    id: str
    provider: str
    ref: str  # stable handle: watch-page URL, clip id, or dfile — never a tokenized URL
    media_kind: str  # "direct" | "hls"
    duration: float | None  # source-clip seconds (None when unknown before probing)
    watch_url: str | None  # human watch page (canonical_video)
    backup_key: str | None = None  # future: our archived copy of this source media (§7)
    duration_basis: str | None = None  # e.g. "container", "stream-sample", "decoded:48000"


@dataclass(frozen=True)
class Segment:
    """One span of the served audio, either lifted from a source or synthetically inserted."""

    served_start: float
    served_end: float
    kind: str  # "source" | "insert"

    # kind == "source" fields
    source_id: str | None = None
    source_start: float | None = None
    source_end: float | None = None

    # kind == "insert" fields
    insert: str | None = None  # "intro" | "outro" | "silence" | "gap"
    asset_id: str | None = None
    asset_version: str | None = None

    def __post_init__(self) -> None:
        """Reject a kind/field combination inconsistent with itself (CR2-CP-13) — a malformed
        mixed segment would otherwise only surface as a confusing bug several calls downstream
        in served_to_source/source_to_served, far from where it was actually constructed."""
        if self.kind == "source":
            # source_end may legitimately be None — a non-final "open-ended" span whose true
            # end isn't known yet (media.py's generic filter-complex path, GH#702) — but
            # source_id/source_start are never optional.
            if self.source_id is None or self.source_start is None:
                raise ValueError(
                    f"Segment(kind='source') requires source_id/source_start: {self!r}"
                )
            if (
                self.insert is not None
                or self.asset_id is not None
                or self.asset_version is not None
            ):
                raise ValueError(
                    f"Segment(kind='source') must not set insert/asset_id/asset_version: {self!r}"
                )
        elif self.kind == "insert":
            if self.insert is None:
                raise ValueError(f"Segment(kind='insert') requires insert: {self!r}")
            if (
                self.source_id is not None
                or self.source_start is not None
                or self.source_end is not None
            ):
                raise ValueError(
                    "Segment(kind='insert') must not set "
                    f"source_id/source_start/source_end: {self!r}"
                )
        else:
            raise ValueError(f"Segment.kind must be 'source' or 'insert', got {self.kind!r}")


@dataclass(frozen=True)
class Timeline:
    """An Edit Decision List: the complete mapping from served time to source time(s).

    ``segments`` must be non-overlapping, monotonically ordered by ``served_start``, and
    together cover ``[0, served_duration]``.  :func:`identity_timeline` produces the
    canonical no-op form; :func:`timeline_digest` returns ``""`` for that case so
    existing ``audio_spec_hash`` values are unchanged when this model ships.
    """

    version: str
    segments: tuple[Segment, ...]
    basis: str = "served"  # served-time is always canonical


# ---------------------------------------------------------------------------
# Constructors
# ---------------------------------------------------------------------------


def identity_timeline(source: SourceMedia, duration: float) -> Timeline:
    """The no-op EDL for an un-manipulated episode: one full-length source segment."""
    return Timeline(
        version="identity",
        segments=(
            Segment(
                served_start=0.0,
                served_end=duration,
                kind="source",
                source_id=source.id,
                source_start=0.0,
                source_end=duration,
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Digest
# ---------------------------------------------------------------------------


def _is_identity(tl: Timeline, sources: tuple[SourceMedia, ...] = ()) -> bool:
    """True when the timeline is a single full-pass source segment (no offset, no trim).

    The comparison is *exact* (``served_start == source_start`` etc.), so a planner that
    no-ops must return the canonical :func:`identity_timeline` (or leave ``ep.timeline``
    as ``None``). A segment that is identity only up to floating-point error is treated as
    a real manipulation → non-empty digest → a (needless) re-encode. This is deliberate:
    we never want to *accidentally* collapse a real edit to identity, and an exact no-op is
    cheap to produce.
    """
    if len(tl.segments) != 1:
        return False
    s = tl.segments[0]
    if not (
        s.kind == "source"
        and s.source_id is not None
        and s.served_start == 0
        and s.source_start == 0
        and s.served_start == s.source_start
        and s.served_end == s.source_end
    ):
        return False
    if sources:
        src = next((src for src in sources if src.id == s.source_id), None)
        if src is None:
            return False
        if src.duration is not None and s.source_end != src.duration:
            return False
    return True


def timeline_digest(tl: Timeline, sources: tuple[SourceMedia, ...] | list[SourceMedia] = ()) -> str:
    """Canonical hash of the EDL, or ``""`` for an identity timeline.

    The empty-string sentinel is load-bearing: it lets ``audio_spec_hash`` produce the
    same bytes for identity episodes as it did before this model existed, so no re-encode
    storm occurs when the model is first deployed.
    """
    if _is_identity(tl, tuple(sources)):
        return ""
    blob = json.dumps(dataclasses.asdict(tl), separators=(",", ":"), sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def edl_duration(tl: Timeline | None) -> float | None:
    """The canonical EDL (cue) clock: the summed served span of the timeline.

    This is the served-time length a listener scrubs and the basis for chapter/cue range
    checks. It is one of three duration facts that review/20 requires to be kept distinct and
    never silently reconciled:

      * **source** — ``SourceMedia.duration`` (the original recording length);
      * **served/hosted** — ``Episode.audio_duration_served`` (the *probed* duration of the
        actual rendered enclosure);
      * **EDL/cue** — this function (the planned served length).

    For a correct episode all three agree within frame tolerance; the audit treats any
    divergence as a finding. Every place that needs the EDL clock derives it here so the three
    facts cannot drift apart through subtly-different local re-implementations. Returns ``None``
    for an empty timeline or a non-positive total.
    """
    if tl is None or not tl.segments:
        return None
    total = sum(s.served_end - s.served_start for s in tl.segments)
    return total if total > 0 else None


# ---------------------------------------------------------------------------
# Time maps
# ---------------------------------------------------------------------------


def served_to_source(tl: Timeline, t: float) -> tuple[str, float] | None:
    """Map a served-time instant to ``(source_id, source_t)``.

    Returns ``None`` when ``t`` falls inside a synthetic insert (no source backing).

    Segments use half-open intervals ``[served_start, served_end)`` except the final
    segment, which is closed ``[served_start, served_end]``.  This means a boundary
    point belongs to the segment that *starts* there, not the one that ends there — so
    the first frame of a source segment is never ambiguously claimed by a preceding insert.
    """
    segs = tl.segments
    for i, s in enumerate(segs):
        last = i == len(segs) - 1
        in_span = (
            (s.served_start <= t < s.served_end)
            if not last
            else (s.served_start <= t <= s.served_end)
        )
        if in_span:
            if s.kind == "insert":
                return None
            offset = t - s.served_start
            return (s.source_id, s.source_start + offset)  # type: ignore[return-value]
    return None


def source_to_served(tl: Timeline, source_id: str, t: float) -> float | None:
    """Map a source-time instant to served time.

    Returns ``None`` when ``t`` was cut from the served audio (falls in a gap between
    the source spans that were kept) or belongs to a different source entirely.

    Uses the same half-open convention as :func:`served_to_source`: the right endpoint
    of each non-last source segment is excluded so boundary points uniquely belong to
    the segment that begins there.

    Boundary caveat (matters only for concat, #122): "last" here means *last segment for
    this source_id*, whereas :func:`served_to_source` treats only the *globally* last
    segment as closed. At a concat seam where source ``A`` hands off to source ``B``, the
    shared instant is therefore claimed by ``A`` going one way and by ``B`` going the other,
    so the two maps are not exact inverses *at that single instant* (they agree everywhere
    else). The discrepancy is sub-frame and only affects an item whose timestamp lands
    exactly on a seam; it is pinned by the concat-seam tests in ``test_timeline.py``.
    """
    source_segs = [s for s in tl.segments if s.kind == "source" and s.source_id == source_id]
    for i, s in enumerate(source_segs):
        last = i == len(source_segs) - 1
        in_span = (  # type: ignore[operator]
            (s.source_start <= t < s.source_end)
            if not last
            else (s.source_start <= t <= s.source_end)
        )
        if in_span:
            offset = t - s.source_start  # type: ignore[operator]
            return s.served_start + offset
    return None


def _nearest_cut_boundary(tl: Timeline, source_id: str, t: float) -> float | None:
    """Served-time of the boundary the cut span containing source-time *t* hands off to.

    Used when an item's end falls inside a removed span: the item should end at the
    served-time edge of the *last kept segment before the cut*, not at the episode's
    overall served duration — clamping to the global duration would wrongly overlap any
    later chapters/cues that follow the cut.
    """
    source_segs = sorted(
        (s for s in tl.segments if s.kind == "source" and s.source_id == source_id),
        key=lambda s: s.source_start,  # type: ignore[arg-type]
    )
    boundary: float | None = None
    for s in source_segs:
        if s.source_start <= t:  # type: ignore[operator]
            boundary = s.served_end
        else:
            break
    return boundary


def _next_kept_boundary(tl: Timeline, source_id: str, t: float) -> float | None:
    """Return the served start of the next kept source span after source time *t*.

    This is intentionally separate from :func:`_nearest_cut_boundary`: the latter finds the
    preceding served edge for an item whose *end* enters a cut, while chapter starts that fall in
    a removed span should optionally snap forward to the first audio that remains.
    """
    source_segs = sorted(
        (s for s in tl.segments if s.kind == "source" and s.source_id == source_id),
        key=lambda s: s.source_start,  # type: ignore[arg-type]
    )
    for segment in source_segs:
        if segment.source_start is not None and segment.source_start > t:
            return segment.served_start
    return None


def remap(
    tl: Timeline,
    items: list[dict],
    *,
    source_id: str | None = None,
    clamp_to: float | None = None,
    snap_cut_starts: bool = False,
) -> list[dict]:
    """Remap timestamped items (chapters, transcript cues) from source-time to served-time.

    Items whose ``start`` falls in a cut span are dropped entirely unless ``snap_cut_starts`` is
    true, in which case the start is moved to the next kept served boundary. ``end`` is remapped
    when present; if the end was cut it becomes ``None`` — *unless* ``clamp_to`` is given,
    in which case it is first snapped to the served-time edge of the nearest kept segment
    before the cut (so it never overlaps a later chapter/cue), falling back to ``clamp_to``
    itself only when no such boundary exists. Passing ``clamp_to`` centralizes the "an item
    that runs into a removed span ends at the boundary" rule so no downstream consumer has
    to special-case ``None`` (e.g. the transcript and permalink renderers).

    ``source_id`` names which source's clock the items are in. When ``None``, defaults to
    the first source segment's id (correct for all single-source episodes).
    """
    if source_id is None:
        for s in tl.segments:
            if s.kind == "source" and s.source_id is not None:
                source_id = s.source_id
                break

    result = []
    for item in items:
        new_start = source_to_served(tl, source_id, item["start"])  # type: ignore[arg-type]
        if new_start is None and snap_cut_starts:
            new_start = _next_kept_boundary(tl, source_id, item["start"])  # type: ignore[arg-type]
        if new_start is None:
            continue  # start is in a cut span with no requested snap — drop
        new_item = dict(item)
        new_item["start"] = new_start
        if "end" in item and item["end"] is not None:
            new_end = source_to_served(tl, source_id, item["end"])  # type: ignore[arg-type]
            if new_end is None and clamp_to is not None:
                # End fell in a cut span — clamp to the nearest segment boundary, not the
                # episode's overall served duration (which would overlap a later chapter).
                new_end = _nearest_cut_boundary(tl, source_id, item["end"])  # type: ignore[arg-type]
                if new_end is None:
                    new_end = clamp_to
            new_item["end"] = new_end
        result.append(new_item)
    return result
