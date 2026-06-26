"""Record-level H16 identity checks for Granicus transport processing."""

from __future__ import annotations

import re
import threading
from collections import Counter
from dataclasses import dataclass

from citypods.models import City, Episode
from citypods.records import audio_object_key, audio_spec_hash, source_key
from citypods.storage.base import StorageBackend


@dataclass(frozen=True)
class _Snapshot:
    uid: str | None
    provider_guid: str
    video_url: str
    source_url: str
    canonical_video_url: str | None
    source_duration: int | None
    audio_key: str | None
    audio_url: str | None
    audio_spec_hash: str | None
    audio_duration_served: float | None
    expected_spec_hash: str
    expected_key: str
    expected_url: str


class H16IdentityTracker:
    """Capture pre-media Granicus identity and verify the post-media record."""

    def __init__(
        self,
        *,
        storage: StorageBackend | None,
        max_kbps: int,
        loudness_profile: str,
        processing_profile: str,
        enabled: bool,
    ) -> None:
        self._storage = storage
        self._max_kbps = max_kbps
        self._loudness_profile = loudness_profile
        self._processing_profile = processing_profile
        self._enabled = enabled and storage is not None
        self._snapshots: dict[tuple[str, str], _Snapshot] = {}
        self._object_keys: dict[int, tuple[str, str]] = {}
        self._cities: dict[str, City] = {}
        self._results: dict[tuple[str, str], tuple[bool, frozenset[str], bool]] = {}
        self._lock = threading.Lock()

    def _expected(self, city: City, ep: Episode) -> tuple[str, str, str]:
        assert self._storage is not None
        spec = audio_spec_hash(
            ep,
            max_kbps=self._max_kbps,
            loudness_profile=self._loudness_profile,
            processing_profile=self._processing_profile,
        )
        key = audio_object_key(city, ep, spec)
        return spec, key, self._storage.public_url(key)

    def _artifact_matches_recipe(
        self,
        city: City,
        uid: str,
        spec: str,
        key: str | None,
        url: str | None,
    ) -> bool:
        """Accept the record's own prefix or a GH#421 coalesced sibling-source prefix."""
        if self._storage is None or not key or not url:
            return False
        pattern = (
            rf"^{re.escape(city.provider)}/[0-9a-f]{{12}}/{re.escape(uid)}-{re.escape(spec)}\.m4a$"
        )
        return bool(re.fullmatch(pattern, key)) and url == self._storage.public_url(key)

    def capture(self, city: City, episodes: list[Episode]) -> None:
        if not self._enabled or city.provider != "granicus":
            return
        source = source_key(city)
        with self._lock:
            self._cities[source] = city
        for ep in episodes:
            if not ep.uid:
                continue
            spec, key, url = self._expected(city, ep)
            snapshot = _Snapshot(
                uid=ep.uid,
                provider_guid=ep.guid,
                video_url=ep.video_url,
                source_url=ep.resolved_audio_url(),
                canonical_video_url=ep.links.get("canonical_video"),
                source_duration=ep.duration,
                audio_key=ep.audio_key,
                audio_url=ep.hosted_audio_url,
                audio_spec_hash=ep.audio_spec_hash,
                audio_duration_served=ep.audio_duration_served,
                expected_spec_hash=spec,
                expected_key=key,
                expected_url=url,
            )
            with self._lock:
                identity = (source, ep.uid)
                self._snapshots[identity] = snapshot
                self._object_keys[id(ep)] = identity

    def verify(self, source: str, episodes: list[Episode]) -> None:
        with self._lock:
            city = self._cities.get(source)
        if city is None:
            return
        seen: set[tuple[str, str]] = set()
        for ep in episodes:
            if not ep.uid:
                continue
            with self._lock:
                # id(ep) can be reused after GC, so only trust a cached identity
                # that exactly matches this record; otherwise fall back to (source, uid).
                identity = self._object_keys.get(id(ep))
                if identity is None or identity != (source, ep.uid):
                    identity = (source, ep.uid)
                before = self._snapshots.get(identity)
            if before is None:
                continue
            seen.add(identity)
            categories: set[str] = set()
            if ep.uid != before.uid:
                categories.add("uid")
            if ep.guid != before.provider_guid:
                categories.add("provider_guid")
            if ep.video_url != before.video_url:
                categories.add("official_url")
            if ep.resolved_audio_url() != before.source_url:
                categories.add("source_url")
            if ep.links.get("canonical_video") != before.canonical_video_url:
                categories.add("canonical_video_url")
            if ep.duration != before.source_duration:
                categories.add("source_duration")

            expected_spec, expected_key, expected_url = self._expected(city, ep)
            initial_complete = bool(
                before.audio_key and before.audio_url and before.audio_spec_hash
            )
            initial_current = (
                initial_complete
                and before.audio_spec_hash == before.expected_spec_hash
                and self._artifact_matches_recipe(
                    city,
                    before.uid or ep.uid,
                    before.expected_spec_hash,
                    before.audio_key,
                    before.audio_url,
                )
                and before.audio_duration_served is not None
                and before.audio_duration_served > 0
            )
            post_complete = bool(ep.audio_key and ep.hosted_audio_url and ep.audio_spec_hash)
            # The content-addressed key/spec/url comparison is only meaningful for an artifact this
            # run actually (re)materialized. When the persisted pointer is byte-identical to what
            # was captured before the media chain, this run did not write a new artifact — the
            # record legitimately retains its prior, valid object — so a divergence from the freshly
            # recomputed ``_expected()`` is a *pending* re-encode, not corruption. This covers the
            # transients that all leave the old pointer in place: a recipe change whose re-encode
            # was deferred by the run budget, a reused migrated ``legacy`` artifact, and a re-encode
            # whose upload failed transiently (GH#353, Audio #54/#56: a B2 ServiceUnavailable left
            # the new probed duration on the record while the pointer stayed at the prior object,
            # tripping the duration-change branch below). A freshly *written* artifact is still
            # validated, so real content-addressing drift is still caught.
            artifact_identity_unchanged = (
                ep.audio_key == before.audio_key
                and ep.audio_spec_hash == before.audio_spec_hash
                and ep.hosted_audio_url == before.audio_url
            )
            artifact_checked = False

            if any((ep.audio_key, ep.hosted_audio_url, ep.audio_spec_hash)) and not post_complete:
                categories.add("artifact_partial")
                artifact_checked = True
            elif post_complete and (
                not initial_complete
                or (
                    ep.audio_key,
                    ep.hosted_audio_url,
                    ep.audio_spec_hash,
                    ep.audio_duration_served,
                )
                != (
                    before.audio_key,
                    before.audio_url,
                    before.audio_spec_hash,
                    before.audio_duration_served,
                )
                or (initial_current and expected_spec == before.expected_spec_hash)
            ):
                artifact_checked = True
                if not artifact_identity_unchanged:
                    if ep.audio_spec_hash != expected_spec:
                        categories.add("audio_spec_hash")
                    artifact_matches_recipe = self._artifact_matches_recipe(
                        city, ep.uid, expected_spec, ep.audio_key, ep.hosted_audio_url
                    )
                    if not artifact_matches_recipe:
                        categories.add("audio_key")
                        categories.add("audio_url")
                if not ep.audio_duration_served or ep.audio_duration_served <= 0:
                    categories.add("served_duration")
                if (
                    initial_current
                    and expected_spec == before.expected_spec_hash
                    and not self._artifact_matches_recipe(
                        city, ep.uid, expected_spec, ep.audio_key, ep.hosted_audio_url
                    )
                ):
                    # A current-at-capture artifact, same recipe, that no longer resolves to a
                    # valid content-addressed object for that recipe was genuinely replaced this
                    # run. A served-duration delta alone is NOT that — the audio is identical across
                    # a recipe, so a re-probe or a GH#421 coalesced-sibling adoption (record now
                    # points at the canonical shared object) is metadata-only, not a new artifact.
                    categories.add("current_artifact_changed")

            if categories:
                # GH#353 diagnostic (review/11 H16): pinpoint which (source, uid) mismatched and
                # why, so a mismatch can be correlated against the circuit-recovery retry log
                # lines (which re-run chapters→audio for the same uid) and the provider-lease
                # stale-reap log lines (a possible concurrent-refetch race) — see h16_report.py.
                print(
                    f"[h16-identity] mismatch source={source} uid={ep.uid} "
                    f"categories={sorted(categories)} "
                    f"before(key={before.audio_key!r} spec={before.audio_spec_hash!r} "
                    f"url={before.audio_url!r}) "
                    f"actual(key={ep.audio_key!r} spec={ep.audio_spec_hash!r} "
                    f"url={ep.hosted_audio_url!r}) "
                    f"expected(key={expected_key!r} spec={expected_spec!r} url={expected_url!r})",
                    flush=True,
                )
            with self._lock:
                self._results[identity] = (not categories, frozenset(categories), artifact_checked)

        # A captured Granicus record must survive the media chain and persistence boundary.
        with self._lock:
            for identity in self._snapshots:
                if identity[0] == source and identity not in seen:
                    self._results[identity] = (False, frozenset({"record_missing"}), False)

    def summary(self) -> dict:
        with self._lock:
            results = list(self._results.values())
        categories = Counter(category for _ok, rows, _artifact in results for category in rows)
        return {
            "checked": len(results),
            "mismatches": sum(not ok for ok, _rows, _artifact in results),
            "artifact_checked": sum(artifact for _ok, _rows, artifact in results),
            "mismatch_categories": dict(sorted(categories.items())),
        }
