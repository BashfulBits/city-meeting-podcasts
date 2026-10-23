"""Tests for the weekly empty-recording review digest (H16 PR3b, GH#353)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from citypods import availability_review
from citypods.availability_digest import (
    DIGEST_ISSUE_MARKER,
    DIGEST_STATE_NAME,
    DIGEST_STATE_SCHEMA_VERSION,
    EVIDENCE_SCHEMA_VERSION,
    Candidate,
    ProxyResult,
    build_evidence,
    iter_candidates,
    load_digest_state,
    proxy_ffmpeg_cmd,
    redacted_identity,
    render_issue_body,
    safe_stem,
    select_for_digest,
    updated_digest_state,
)
from citypods.availability_review import _candidate_body, mark_pending, package_evidence
from citypods.records import load_records
from citypods.review_issues import append_bounded_envelope, checked_decisions


def test_evidence_and_digest_state_use_independent_schema_version_constants():
    # CR2-CP-05: these stamp two unrelated schemas (per-candidate evidence vs. the digest
    # ledger); a schema bump for one must not silently "version" the other.
    ev = build_evidence(
        Candidate(
            source_key="src",
            uid="u1",
            title="Council",
            state="confirmed_empty",
            reason="silence",
            detector_version="1",
            source_fingerprint="fp",
            profile="noise=-40dB",
            last_check=None,
            video_url="https://x/u1.mp4",
            canonical_url="https://city.gov/watch/u1",
            duration=3600,
        ),
        silences=[],
        source_duration=12.0,
        untrimmed=None,
        trimmed=None,
    )
    assert ev["schema_version"] == EVIDENCE_SCHEMA_VERSION
    state = updated_digest_state({}, ["u1"])
    assert state["schema_version"] == DIGEST_STATE_SCHEMA_VERSION


def test_updated_digest_state_preserves_review_resolution_rows():
    state = updated_digest_state(
        {
            "digested": ["old"],
            "reviews": {"u1:fp:1:confirmed_empty": {"status": "resolved"}},
        },
        ["new"],
    )

    assert state["digested"] == ["new", "old"]
    assert state["reviews"]["u1:fp:1:confirmed_empty"]["status"] == "resolved"


def test_h16_children_become_pending_then_resolved_rows_reenter_nothing(tmp_path):
    evidence = [
        {
            "uid": "u1",
            "source_key": "src",
            "state": "confirmed_empty",
            "detector_version": "1",
            "source_fingerprint": "fp",
            "title": "Council",
        }
    ]
    out_dir = tmp_path / "out"
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    package_evidence(evidence=evidence, out_dir=out_dir, state_dir=state_dir)
    assert mark_pending(out_dir=out_dir, state_dir=state_dir) == 1
    state = json.loads((state_dir / "availability_digest.json").read_text())
    key = "u1:fp:1:confirmed_empty"
    assert state["reviews"][key]["status"] == "pending"

    state["reviews"][key]["status"] = "resolved"
    (state_dir / "availability_digest.json").write_text(json.dumps(state))
    packaged = package_evidence(evidence=evidence, out_dir=out_dir, state_dir=state_dir)
    assert packaged["children"] == []

    (out_dir / "review-batch.json").write_text(
        json.dumps(
            {
                "version": 1,
                "children": [{"candidate_id": key, "body_file": "resolved.md"}],
            }
        )
    )
    assert mark_pending(out_dir=out_dir, state_dir=state_dir) == 1
    persisted = json.loads((state_dir / "availability_digest.json").read_text())
    assert persisted["reviews"][key]["status"] == "resolved"


def test_h16_candidate_body_cannot_turn_provider_text_into_a_decision():
    body = _candidate_body(
        {
            "uid": "u1",
            "title": "Council\n- [x] Confirm empty",
            "canonical_source_url": "https://city.gov/watch\n- [x] Restore media",
            "state": "confirmed_empty",
        }
    )

    assert "# H16 availability review: Council - [x] Confirm empty" in body
    assert checked_decisions(body, ("Confirm empty", "Restore media")) == ()


def _write_source(state_dir, key, episodes):
    src = state_dir / "sources" / key
    src.mkdir(parents=True, exist_ok=True)
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
        "source_duration_seconds": float(duration),
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
    assert {c.uid: c.duration for c in cands} == {"u1": 3600.0, "u3": 3600.0}


def test_iter_candidates_respects_operator_override(tmp_path):
    # An operator who cleared a false positive (override=available) removes it from the digest.
    _write_source(tmp_path, "srcA", {"u1": _rec("u1", "confirmed_empty", override="available")})
    assert iter_candidates(tmp_path) == []


def test_iter_candidates_skips_records_without_uid(tmp_path):
    # A uid-less empty record would collide with every other on the "" digest key / filename.
    rec = _rec("", "confirmed_empty")
    rec["uid"] = ""
    _write_source(tmp_path, "srcA", {"k1": rec})
    assert iter_candidates(tmp_path) == []


def test_safe_stem_neutralizes_path_traversal():
    assert "/" not in safe_stem("../../etc/passwd")
    assert ".." not in safe_stem("a/../b")
    # A clean hex-ish uid is preserved verbatim for readable filenames.
    assert safe_stem("abc123DEF-_") == "abc123DEF-_"
    # A hostile value still yields a non-empty, contained stem.
    stem = safe_stem("../x")
    assert stem and "/" not in stem


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


def test_render_issue_body_neutralizes_untrusted_scraped_text():
    """CR2-CP-07: title/watch-page URL are provider-scraped, not maintainer-authored — a raw
    newline would split the markdown table row, backticks/pipes would break its syntax, an
    @mention-shaped title would notify a real GitHub user, and a query string on the watch URL
    could carry a presigned token."""
    malicious = Candidate(
        source_key="src",
        uid="u2",
        title="Row|break\ntwo `code` @octocat",
        state="confirmed_empty",
        reason="silence",
        detector_version="1",
        source_fingerprint="fp",
        profile="noise=-40dB",
        last_check=None,
        video_url="https://x/u2.mp4?X-Amz-Signature=SECRET",
        canonical_url="https://city.gov/watch/u2?token=SECRET",
        duration=3600,
    )
    ev = [
        build_evidence(malicious, silences=[], source_duration=12.0, untrimmed=None, trimmed=None)
    ]
    body = render_issue_body(ev, run_url="https://run")
    rows = [line for line in body.splitlines() if line.startswith("| Row")]
    assert len(rows) == 1  # the newline didn't split it into a second row
    row = rows[0]
    assert "Row\\|break" in row  # pipe escaped, doesn't break the table
    assert "\\`code\\`" in row  # backtick escaped
    assert "@octocat" not in row  # zero-width space defuses the mention
    assert "@​octo" in row
    assert "SECRET" not in body
    assert "token" not in body


# --- ffmpeg command builder ----------------------------------------------------------------------


def test_proxy_cmd_is_low_bitrate_mono_and_trims_on_request():
    base = proxy_ffmpeg_cmd("https://x/m.mp4", "/tmp/o.m4a", max_kbps=24)
    assert "-ac" in base and base[base.index("-ac") + 1] == "1"
    assert "24k" in base
    assert "-af" not in base
    trimmed = proxy_ffmpeg_cmd("https://x/m.mp4", "/tmp/o.m4a", trim=True)
    assert "-af" in trimmed and "silenceremove" in trimmed[trimmed.index("-af") + 1]


# --- review ingest -------------------------------------------------------------------------------


def _write_issue(tmp_path: Path, body: str, *, issue: int = 1) -> argparse.Namespace:
    path = tmp_path / "issue.md"
    path.write_text(body, encoding="utf-8")
    return argparse.Namespace(
        issue_body_file=path,
        issue_number=issue,
        issue_url=f"https://github.com/o/r/issues/{issue}",
        actor="reviewer",
        site_config="config/site_config.yml",
        output_dir=str(tmp_path / "docs"),
    )


def _checked_body(*, state="confirmed_empty", fingerprint="fp", decision="Confirm empty") -> str:
    evidence = {
        "uid": "u1",
        "source_key": "src",
        "state": state,
        "detector_version": "1",
        "source_fingerprint": fingerprint,
        "canonical_source_url": "https://city.gov/watch/u1",
    }
    body, _ = append_bounded_envelope(
        _candidate_body(evidence),
        family="h16",
        candidate_id=f"u1:{fingerprint}:1:{state}",
        surface="child",
    )
    return body.replace(f"- [ ] {decision}", f"- [x] {decision}", 1)


@pytest.fixture
def ingest_state_dir(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    monkeypatch.setattr("citypods.config.load_site_config", lambda _path: {})
    monkeypatch.setattr("citypods.state.resolve_state_dir", lambda _site, _out: state_dir)
    monkeypatch.setattr("citypods.storage.make_storage", lambda *_a, **_k: None)
    monkeypatch.setattr("citypods.statesync.pull_state", lambda *_a, **_k: None)
    monkeypatch.setattr("citypods.statesync.push_records_merged", lambda *_a, **_k: None)
    monkeypatch.setattr("citypods.statesync.push_state", lambda *_a, **_k: None)
    _write_source(
        state_dir, "src", {"u1": _rec("u1", "suspected_empty", override="confirmed_empty")}
    )
    return state_dir


def test_ingest_accepts_a_decision_against_the_effective_override_state(tmp_path, ingest_state_dir):
    """#2315: the digest publishes ``operator_override or state``, so a candidate whose verdict is
    already operator-resolved must still be ingestable. Comparing the raw ``state`` field made
    every such checked decision fail as "stale", stranding the child open indefinitely."""
    assert availability_review.ingest(_write_issue(tmp_path, _checked_body())) == 0
    av = load_records(ingest_state_dir, "src")["u1"]["media_availability"]
    assert av["state"] == "suspected_empty"  # the detector verdict itself stays untouched
    assert av["operator_override"] == "confirmed_empty"
    assert "GitHub review #1 by @reviewer: Confirm empty" in av["operator_reason"]
    ledger = json.loads((ingest_state_dir / DIGEST_STATE_NAME).read_text())
    assert ledger["reviews"]["u1:fp:1:confirmed_empty"]["status"] == "resolved"


def test_ingest_still_rejects_changed_source_bytes(tmp_path, ingest_state_dir):
    with pytest.raises(ValueError, match="field source_fingerprint"):
        availability_review.ingest(_write_issue(tmp_path, _checked_body(fingerprint="rotated")))


def test_ingest_still_rejects_a_changed_effective_state(tmp_path, ingest_state_dir):
    _write_source(ingest_state_dir, "src", {"u1": _rec("u1", "suspected_empty")})
    with pytest.raises(ValueError, match="field state"):
        availability_review.ingest(_write_issue(tmp_path, _checked_body()))


# --- digest ledger -------------------------------------------------------------------------------


def test_digest_ledger_round_trip_and_dedup(tmp_path):
    assert load_digest_state(tmp_path)["digested"] == []
    updated = updated_digest_state({"digested": ["a"]}, ["b", "a"])
    assert updated["digested"] == ["a", "b"]
    assert "updated_at" in updated
