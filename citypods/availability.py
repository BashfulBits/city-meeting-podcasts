"""Durable media-availability classification (H16 PR3, GH#353).

A Swagit/Granicus meeting whose source media is missing or (near-)totally silent used to produce
*repeated ambiguous failures*: the silence planner's ``is_degenerate_served_duration`` guard logged
and dropped the timeline, ``TruncatedAudioError`` backed off, and the episode was re-attempted every
run with no durable verdict — so a bad/empty enclosure could be published and the same work redone.

This module is the explicit, **versioned** projection that replaces those ambiguous retries. It is a
pure state machine over an :class:`Episode`'s observed media signal, persisted as the
``media_availability`` record block (see :mod:`citypods.records`). Key invariants, matching
``review/12`` §"Three-PR closure sequence" PR3:

* **Transport failures cannot confirm silence.** A 403/429/timeout/truncation is *transport*, not
  evidence about the recording's content, so it never flips a known-good episode to empty and never
  advances a silence confirmation.
* **Automatic confirmation requires two independent successful fetches.** Each scheduled run that
  successfully fetches+decodes the source and finds it (near-)totally silent advances
  ``silent_confirmations``; only at two does the verdict harden from ``suspected_empty`` to
  ``confirmed_empty``.
* **Re-evaluable without an audio pipeline-version bump.** A dedicated :data:`DETECTOR_VERSION`, a
  source fingerprint, the detector ``profile``, periodic re-checks, and operator overrides each
  re-open a classification. None of them touch ``AUDIO_PIPELINE_VERSION``, so re-classifying never
  triggers a catalog re-encode.

Withheld states are kept out of audio/video feeds and the media-affecting stages; metadata stages
continue so agenda/minutes/chapters/resources still reach the Phase-R meeting pages (review/13).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from urllib.parse import urlsplit

# Bumped only when the *classification logic or fingerprint inputs* change in a way that should
# re-open every stored verdict. Deliberately independent of ``records.AUDIO_PIPELINE_VERSION`` —
# re-classifying a meeting must never re-encode the catalog.
DETECTOR_VERSION = "1"

# --- states -------------------------------------------------------------------------------------
AVAILABLE = "available"
SUSPECTED_EMPTY = "suspected_empty"
CONFIRMED_EMPTY = "confirmed_empty"
MISSING = "missing"
INVALID = "invalid"
RECOVERED = "recovered"
# GH#851: a real, playable recording whose decoded length falls well short of what the source
# itself advertises (container/manifest header, pre-silence-trim) — a genuinely incomplete/
# truncated upload, not a silence-trimmed one. Deliberately NOT in WITHHELD_STATES: unlike
# empty/dead media this is real content worth publishing, just annotated rather than excluded.
SUSPECTED_PARTIAL = "suspected_partial"
CONFIRMED_PARTIAL = "confirmed_partial"

# States whose media must be kept out of feeds and media-affecting stages. ``available`` and
# ``recovered`` are playable; the rest are withheld until a fresh successful fetch recovers them.
# suspected_partial/confirmed_partial are deliberately excluded (GH#851) — see above.
WITHHELD_STATES: frozenset[str] = frozenset({SUSPECTED_EMPTY, CONFIRMED_EMPTY, MISSING, INVALID})

# The durable "confirmed dead" subset of the withheld states: media positively established as empty
# (two silent confirmations) or unreachable (``DEAD``/``ABSENT`` are confirmed on first sight).
# These get a flat long recheck cadence (GH#795); ``suspected_empty`` is deliberately excluded so it
# stays on the exponential backoff and can reach its second silent confirmation quickly.
CONFIRMED_DEAD_STATES: frozenset[str] = frozenset({CONFIRMED_EMPTY, MISSING, INVALID})

# All states an operator override may legitimately force. ``available`` lets an operator clear a
# false positive; the withheld states let one force-withhold a known-bad recording; the partial
# states let one force-annotate/clear a short-source verdict.
OVERRIDE_STATES: frozenset[str] = WITHHELD_STATES | frozenset(
    {AVAILABLE, RECOVERED, SUSPECTED_PARTIAL, CONFIRMED_PARTIAL}
)

# Two independent successful silent fetches harden suspicion into a confirmed verdict. Reused
# as-is (GH#851) for the partial-source confirmation gate: a transient truncated fetch rarely
# reproduces identically on an independent retry, a genuinely incomplete source does — the same
# discriminator, so the same threshold.
CONFIRM_THRESHOLD = 2

# Observation kinds the classifier accepts.
SILENT = "silent"  # successful fetch+decode, judged (near-)totally silent / degenerate
PLAYABLE = "playable"  # successful fetch+decode with real audio
PARTIAL = "partial"  # successful fetch+decode, real audio, but far shorter than source-advertised
TRANSPORT_FAILED = "transport_failed"  # 403/429/timeout/truncation — says nothing about content
DEAD = "dead"  # provider reports no usable media exists (MediaUnavailable, dead container)
ABSENT = "missing"  # the source URL resolved to nothing / an empty page


def _now_iso(now: datetime | str | None) -> str:
    if isinstance(now, str):
        return now
    return (now or datetime.now(UTC)).isoformat()


@dataclass(frozen=True)
class MediaAvailability:
    """The durable availability verdict persisted on an episode.

    ``operator_override`` (when set) supersedes the auto-detected ``state`` for every gate, so an
    operator can clear a false positive or force-withhold a known-bad recording without the detector
    overwriting their call on the next run.
    """

    state: str
    reason: str = ""
    detector_version: str = DETECTOR_VERSION
    source_fingerprint: str | None = None
    profile: str = ""
    last_check: str | None = None
    silent_confirmations: int = 0
    # GH#851: independent-fetch confirmation count for the partial/short-source verdict, tracked
    # separately from silent_confirmations since the two are evidence for different verdicts and
    # must not be conflated by a single shared counter.
    partial_confirmations: int = 0
    # Pointer to the bounded diagnostic evidence bundle (PR3b). None until evidence is emitted.
    evidence_ref: str | None = None
    # An operator-forced state that supersedes ``state`` for gating, plus when/why it was set.
    operator_override: str | None = None
    operator_reason: str = ""
    recovered_at: str | None = None

    def effective_state(self) -> str:
        return self.operator_override or self.state

    def is_withheld(self) -> bool:
        return self.effective_state() in WITHHELD_STATES

    def is_confirmed_dead(self) -> bool:
        """True for the durable withheld states (confirmed-empty / missing / invalid) that get a
        flat long recheck cadence. ``suspected_empty`` is withheld but not yet confirmed dead."""
        return self.effective_state() in CONFIRMED_DEAD_STATES

    def is_confirmed_partial(self) -> bool:
        """True once a short/incomplete source has reproduced on two independent fetches
        (GH#851). Not withheld — the episode still publishes, with a disclaimer — but terminal
        for timeline-audio repair and eligible for the same flat recheck cadence as confirmed-dead
        media (:func:`citypods.records.confirmed_dead_recheck_due` is state-agnostic; callers gate
        on this accessor first, exactly as they do for ``is_confirmed_dead``)."""
        return self.effective_state() == CONFIRMED_PARTIAL

    def is_suspected_partial(self) -> bool:
        """True for a single, not-yet-confirmed short-source observation — still on the normal
        exponential backoff/retry cadence, not yet eligible for the flat recheck or publish-with-
        disclaimer treatment."""
        return self.effective_state() == SUSPECTED_PARTIAL


@dataclass(frozen=True)
class Observation:
    """One run's observation of an episode's media, fed to :func:`classify`.

    ``fingerprint`` identifies the source bytes the observation is about; a change from the stored
    verdict's fingerprint means the city swapped the underlying media, so prior silence
    confirmations no longer apply.
    """

    kind: str
    fingerprint: str | None = None
    profile: str = ""
    reason: str = ""
    now: datetime | str | None = None


def source_fingerprint(ep, profile: str = "") -> str:
    """A stable identity for the source media an availability verdict is about.

    Built from the *query-stripped* source URL (so a rotated signed ``?X-Amz-...`` query does not
    look like new media), the declared duration, the media kind, and the detector ``profile``. It
    changes when the city actually swaps the recording (path/duration change) or when the detection
    profile changes — both of which must re-open the verdict.
    """
    raw_url = ep.resolved_audio_url() or ""
    parts = urlsplit(raw_url)
    stable_url = f"{parts.scheme}://{parts.netloc}{parts.path}" if parts.netloc else raw_url
    basis = "\n".join(
        [
            stable_url,
            str(ep.duration if ep.duration is not None else ""),
            ep.media_kind or "",
            profile or "",
            DETECTOR_VERSION,
        ]
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def content_seconds(silences: list[tuple[float, float]], source_duration: float) -> float:
    """Non-silent (spoken-content) seconds = duration minus the detected silence spans.

    Spans are clamped to ``[0, source_duration]`` so a detector span that runs past the probed
    duration cannot drive the result negative.
    """
    if source_duration <= 0:
        return 0.0
    silent = 0.0
    for start, end in silences:
        lo = max(0.0, min(start, source_duration))
        hi = max(0.0, min(end, source_duration))
        if hi > lo:
            silent += hi - lo
    return max(0.0, source_duration - silent)


def is_effectively_silent(
    silences: list[tuple[float, float]],
    source_duration: float,
    *,
    min_seconds: float = 5.0,
    min_fraction: float = 0.02,
) -> bool:
    """True when a *successfully decoded* source has implausibly little non-silent content.

    Mirrors the floor in :func:`citypods.silence.is_degenerate_served_duration` (the larger of an
    absolute minimum and a fraction of the source) but is computed straight from the silence spans,
    so it also catches a fully-silent source that ``build_silence_timeline`` collapses to ``None``.
    Only call this on a real decode — a missing/garbage probe duration must be treated as transport
    failure, never as silence (the caller owns that distinction).
    """
    if source_duration <= 0:
        return False
    floor = max(min_seconds, source_duration * min_fraction)
    return content_seconds(silences, source_duration) < floor


def _fresh_source(prior: MediaAvailability | None, obs: Observation) -> bool:
    """True when the observation is about different source bytes than the stored verdict."""
    return (
        prior is not None
        and obs.fingerprint is not None
        and prior.source_fingerprint is not None
        and obs.fingerprint != prior.source_fingerprint
    )


def classify(prior: MediaAvailability | None, obs: Observation) -> MediaAvailability | None:
    """Fold one :class:`Observation` into the stored verdict and return the next one.

    Returns ``None`` (no verdict) only when a transport failure arrives with no prior verdict — the
    episode then stays in normal processing and the existing backoff/circuit machinery owns retries.
    """
    now = _now_iso(obs.now)

    # A transport failure is not evidence about the recording: never confirm silence, never flip a
    # known-good (or any) episode to withheld. Just refresh ``last_check`` on an existing verdict.
    if obs.kind == TRANSPORT_FAILED:
        if prior is None:
            return None
        return replace(prior, last_check=now)

    # A change of underlying media bytes retires prior silence confirmations.
    base = prior
    if _fresh_source(prior, obs):
        base = None

    fingerprint = obs.fingerprint or (prior.source_fingerprint if prior else None)
    profile = obs.profile or (prior.profile if prior else "")
    override = prior.operator_override if prior else None
    override_reason = prior.operator_reason if prior else ""
    evidence = base.evidence_ref if base else None

    def build(
        state: str,
        *,
        silent_confirmations: int,
        partial_confirmations: int,
        reason: str,
        recovered_at: str | None,
    ) -> MediaAvailability:
        return MediaAvailability(
            state=state,
            reason=reason,
            detector_version=DETECTOR_VERSION,
            source_fingerprint=fingerprint,
            profile=profile,
            last_check=now,
            silent_confirmations=silent_confirmations,
            partial_confirmations=partial_confirmations,
            evidence_ref=evidence,
            operator_override=override,
            operator_reason=override_reason,
            recovered_at=recovered_at,
        )

    if obs.kind == DEAD:
        return build(
            INVALID,
            silent_confirmations=0,
            partial_confirmations=0,
            reason=obs.reason or "provider reports no usable media",
            recovered_at=base.recovered_at if base else None,
        )

    if obs.kind == ABSENT:
        return build(
            MISSING,
            silent_confirmations=0,
            partial_confirmations=0,
            reason=obs.reason or "source resolved to no media",
            recovered_at=base.recovered_at if base else None,
        )

    if obs.kind == SILENT:
        confirmations = (base.silent_confirmations if base else 0) + 1
        state = CONFIRMED_EMPTY if confirmations >= CONFIRM_THRESHOLD else SUSPECTED_EMPTY
        reason = obs.reason or (
            f"near-total silence confirmed on {confirmations} independent fetches"
            if state == CONFIRMED_EMPTY
            else "near-total silence on a successful fetch (awaiting confirmation)"
        )
        return build(
            state,
            silent_confirmations=confirmations,
            partial_confirmations=0,  # silence is not partial-source evidence
            reason=reason,
            recovered_at=base.recovered_at if base else None,
        )

    if obs.kind == PARTIAL:
        confirmations = (base.partial_confirmations if base else 0) + 1
        state = CONFIRMED_PARTIAL if confirmations >= CONFIRM_THRESHOLD else SUSPECTED_PARTIAL
        reason = obs.reason or (
            f"short source confirmed on {confirmations} independent fetches"
            if state == CONFIRMED_PARTIAL
            else "decoded audio far shorter than source-advertised on a successful fetch "
            "(awaiting confirmation)"
        )
        return build(
            state,
            silent_confirmations=0,  # not silence evidence
            partial_confirmations=confirmations,
            reason=reason,
            recovered_at=base.recovered_at if base else None,
        )

    if obs.kind == PLAYABLE:
        prior_withheld = base is not None and base.effective_state() in WITHHELD_STATES
        if prior_withheld:
            return build(
                RECOVERED,
                silent_confirmations=0,
                partial_confirmations=0,
                reason=obs.reason or "playable audio fetched after a prior withheld verdict",
                recovered_at=now,
            )
        return build(
            AVAILABLE,
            silent_confirmations=0,
            partial_confirmations=0,
            reason=obs.reason or "playable audio",
            recovered_at=base.recovered_at if base else None,
        )

    raise ValueError(f"unknown observation kind: {obs.kind!r}")


def with_operator_override(
    prior: MediaAvailability | None,
    state: str | None,
    reason: str = "",
    *,
    now: datetime | str | None = None,
) -> MediaAvailability:
    """Set (or clear, with ``state=None``) an operator override on the stored verdict.

    Creating a verdict from nothing is allowed so an operator can withhold/clear an episode the
    detector has not yet examined. The override supersedes the auto-detected ``state`` in every
    gate.
    """
    if state is not None and state not in OVERRIDE_STATES:
        raise ValueError(f"invalid operator override state: {state!r}")
    if prior is None:
        prior = MediaAvailability(state=AVAILABLE, reason="created by operator override")
    return replace(
        prior,
        operator_override=state,
        operator_reason=reason if state is not None else "",
        last_check=_now_iso(now),
    )
