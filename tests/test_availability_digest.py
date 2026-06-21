"""Tests for the weekly empty-recording review digest (H16 PR3b, GH#353)."""

from __future__ import annotations

import json

from citypods.availability_digest import (
    DIGEST_ISSUE_MARKER,
    Candidate,
    ProxyResult,
    build_evidence,
    iter_candidates,
    load_digest_state,
    proxy_ffmpeg_cmd,
    redacted_identity,
    render_issue_body,
    select_for_digest,
    updated_digest_state,
)


def _write_source(state_dir, key, episodes):
    src = state_dir / "sources" / key
    src.mkdir(parents=True)
    (src / "episodes.json").write_text(json.dumps({"schema_version": 2, "episodes": episodes}))


def _rec(uid, state, *, fingerprint="fp", title="Council", duration=3600, override=None):
    av = {
        "state": state,
        "reason": "near-total silence",
        "detector_version": "1",
        "source_fingerprint": fingerprint,
        "profile": "noise=-40dB",
        "last_check": "2026-06-21T00:00:00+00:00",
    }
    if override is not None:
        av["operator_override"] = override
    return {
        "uid": uid,
        "title": title,
        "video_url": f"https://city.swagit.com/play/{uid}/m.mp4?X-Amz-Signature=SECRET",
        "duration": duration,
        "links": {"canonical_video": f"https://city.gov/watch/{uid}"},
        "media_availability": av,
    }


def _candidate(uid, state="confirmed_empty", fingerprint="fp"):
    return Candidate(
        source_key="src",
        uid=uid,
        title="Council",
        state=state,
        reason="silence",
        detector_version="1",
        source_fingerprint=fingerprint,
        profile="noise=-40dB",
        last_check=None,
        video_url=f"https://x/{uid}.mp4?X-Amz-Signature=SECRET",
        canonical_url=f"https://city.gov/watch/{uid}",
        duration=3600,
    )


# --- candidate discovery -------------------------------------------------------------------------


def test_iter_candidates_picks_only_empties(tmp_path):
    _write_source(
        tmp_path,
        "srcA",
        {
            "u1": _rec("u1", "confirmed_empty"),
            "u2": _rec("u2", "available"),  # playable — not a candidate
            "u3": _rec("u3", "suspected_empty"),
            "u4": _rec("u4", "missing"),  # withheld but no audio to proxy — excluded
        },
    )
    cands = iter_candidates(tmp_path)
    states = {c.uid: c.state for c in cands}
    assert states == {"u1": "confirmed_empty", "u3": "suspected_empty"}


def test_iter_candidates_respects_operator_override(tmp_path):
    # An operator who cleared a false positive (override=available) removes it from the digest.
    _write_source(tmp_path, "srcA", {"u1": _rec("u1", "confirmed_empty", override="available")})
    assert iter_candidates(tmp_path) == []


# --- deterministic selection ---------------------------------------------------------------------


def test_select_is_deterministic_and_bounded():
    cands = [_candidate(f"u{i}") for i in range(10)]
    first = select_for_digest(cands, set(), limit=3)
    second = select_for_digest(list(reversed(cands)), set(), limit=3)
    assert [c.uid for c in first] == [c.uid for c in second]  # order-independent
    assert len(first) == 3


def test_select_skips_already_digested():
    cands = [_candidate("u1"), _candidate("u2")]
    digested = {cands[0].key()}
    selected = select_for_digest(cands, digested, limit=8)
    assert [c.uid for c in selected] == ["u2"]


def test_reclassification_resurfaces_a_changed_candidate():
    old = _candidate("u1", fingerprint="old")
    digested = {old.key()}
    # New source bytes (different fingerprint) → different key → surfaces again.
    changed = _candidate("u1", fingerprint="new")
    assert select_for_digest([changed], digested, limit=8) == [changed]


# --- evidence + redaction ------------------------------------------------------------------------


def test_redacted_identity_strips_signed_query():
    out = redacted_identity("https://city.swagit.com/play/1/m.mp4?X-Amz-Signature=SECRET&token=abc")
    assert "SECRET" not in out and "X-Amz-Signature" not in out
    assert "city.swagit.com/play/1/m.mp4" in out


def test_build_evidence_shape_and_no_raw_url():
    c = _candidate("u1")
    untrimmed = ProxyResult("u1.untrimmed.m4a", 12345, "a" * 64, 3600.0)
    trimmed = ProxyResult("u1.trimmed.m4a", 64, "b" * 64, 0.01)
    ev = build_evidence(
        c,
        silences=[(0.0, 3599.99)],
        source_duration=3600.0,
        untrimmed=untrimmed,
        trimmed=trimmed,
    )
    assert ev["uid"] == "u1"
    assert ev["silence_intervals"] == [[0.0, 3599.99]]
    assert ev["canonical_source_url"] == "https://city.gov/watch/u1"
    assert "SECRET" not in json.dumps(ev)  # raw signed URL never stored
    assert ev["untrimmed_proxy"]["sha256"] == "a" * 64
    assert ev["trimmed_proxy"]["size_bytes"] == 64


def test_build_evidence_metadata_only_when_no_proxy():
    ev = build_evidence(
        _candidate("u1"),
        silences=[],
        source_duration=None,
        untrimmed=None,
        trimmed=None,
        note="source media could not be resolved; metadata only",
    )
    assert ev["untrimmed_proxy"] is None and ev["trimmed_proxy"] is None
    assert "metadata only" in ev["note"]


# --- issue rendering -----------------------------------------------------------------------------


def test_render_issue_body_has_marker_and_rows():
    ev = [
        build_evidence(
            _candidate("u1"), silences=[], source_duration=12.0, untrimmed=None, trimmed=None
        )
    ]
    body = render_issue_body(ev, run_url="https://run")
    assert DIGEST_ISSUE_MARKER in body
    assert "Council" in body
    assert "https://city.gov/watch/u1" in body
    assert "https://run" in body


# --- ffmpeg command builder ----------------------------------------------------------------------


def test_proxy_cmd_is_low_bitrate_mono_and_trims_on_request():
    base = proxy_ffmpeg_cmd("https://x/m.mp4", "/tmp/o.m4a", max_kbps=24)
    assert "-ac" in base and base[base.index("-ac") + 1] == "1"
    assert "24k" in base
    assert "-af" not in base
    trimmed = proxy_ffmpeg_cmd("https://x/m.mp4", "/tmp/o.m4a", trim=True)
    assert "-af" in trimmed and "silenceremove" in trimmed[trimmed.index("-af") + 1]


# --- digest ledger -------------------------------------------------------------------------------


def test_digest_ledger_round_trip_and_dedup(tmp_path):
    assert load_digest_state(tmp_path)["digested"] == []
    updated = updated_digest_state({"digested": ["a"]}, ["b", "a"])
    assert updated["digested"] == ["a", "b"]
    assert "updated_at" in updated
