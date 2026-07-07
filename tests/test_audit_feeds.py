"""Tests for scripts/audit_feeds.py reconcile logic (no real GitHub calls)."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest.mock as mock
from datetime import UTC, datetime
from pathlib import Path

from citypods.audit import ERROR, WARN, Finding

# Load audit_feeds from scripts/ without making it a package. Registering it in sys.modules
# before exec_module is required for the frozen dataclass in audit_feeds.py -- with `from
# __future__ import annotations`, dataclass field-type resolution looks up
# sys.modules[cls.__module__], which errors on an unregistered module.
_script = Path(__file__).parent.parent / "scripts" / "audit_feeds.py"
_spec = importlib.util.spec_from_file_location("audit_feeds", _script)
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mod
_spec.loader.exec_module(_mod)

reconcile = _mod.reconcile
_title = _mod._title
_body = _mod._body
_state_comment = _mod._state_comment
_issue_key = _mod._issue_key
_key_marker = _mod._key_marker
_grouped_title = _mod._grouped_title
_group_by_check = _mod._group_by_check
_merge_first_seen = _mod._merge_first_seen
_since_label = _mod._since_label
_render_grouped_body = _mod._render_grouped_body
_parse_state = _mod._parse_state
_parse_state_rows = _mod._parse_state_rows
_parse_prior_rows = _mod._parse_prior_rows
_FeedRow = _mod._FeedRow

_NOW = datetime(2026, 6, 30, tzinfo=UTC)


def _finding(slug="testcity", check="drift", sev=WARN, msg="something is wrong"):
    return Finding(slug, check, sev, msg)


def _grouped_issue(
    check: str,
    *,
    number: int = 1,
    first_seen: dict[str, str] | None = None,
    rows: dict[str, _FeedRow] | None = None,
    now: datetime = _NOW,
    labels: list[str] | None = None,
    city_of: dict[str, str] | None = None,
):
    """Build an "existing" issue dict for the consolidated model, round-tripped through the
    real renderer so tests exercise the actual body/title format rather than a hand-rolled one."""
    first_seen = dict(first_seen or {})
    if rows is None:
        rows = {
            slug: _FeedRow(slug=slug, count=1, severity=WARN, example="example")
            for slug in first_seen
        }
    city_of = city_of or {}
    severity = ERROR if any(r.severity == ERROR for r in rows.values()) else WARN
    body = _render_grouped_body(
        check, rows=rows, first_seen=first_seen, city_of=city_of, severity=severity, now=now
    )
    title = _grouped_title(
        check, n_feeds=len(rows), n_cities=len({city_of.get(s, s) for s in rows})
    )
    return {
        _issue_key("", check): {
            "number": number,
            "title": title,
            "body": body,
            "labels": [{"name": label} for label in (labels or [])],
        }
    }


def _per_slug_issue(
    finding: Finding,
    *,
    number: int = 1,
    body_override: str | None = None,
    labels: list[str] | None = None,
):
    f = finding
    title = _title(f.slug, f.check)
    body = (
        body_override
        if body_override is not None
        else _body(f.message, f.severity, f.check, f.slug)
    )
    return {
        _issue_key(f.slug, f.check): {
            "number": number,
            "title": title,
            "body": body,
            "labels": [{"name": label} for label in (labels or [])],
        }
    }


def _run_reconcile(findings, existing_issues, **kwargs):
    """Run reconcile with _gh and _open_issues both mocked; return list of gh call arg tuples."""
    calls: list[tuple] = []
    with mock.patch.object(_mod, "_open_issues", return_value=existing_issues):
        with mock.patch.object(_mod, "_gh", side_effect=lambda *a, **kw: calls.append(a) or ""):
            reconcile(findings, dry_run=False, **kwargs)
    return calls


# ---------------------------------------------------------------------------
# _issue_key / _key_marker
# ---------------------------------------------------------------------------


def test_issue_key_consolidated_check_is_check_name_only():
    assert _issue_key("cityA", "drift") == "drift"


def test_issue_key_per_slug_check_uses_double_colon():
    assert _issue_key("cityA", "meetings-url-dead") == "cityA::meetings-url-dead"


def test_key_marker_format():
    assert _key_marker("drift") == "<!-- citypods:feed-health:key=drift -->"


# ---------------------------------------------------------------------------
# _group_by_check
# ---------------------------------------------------------------------------


def test_group_by_check_groups_multiple_findings_per_slug_and_maxes_severity():
    findings = [
        _finding(slug="cityA", check="drift", sev=WARN, msg="first"),
        _finding(slug="cityA", check="drift", sev=ERROR, msg="second"),
        _finding(slug="cityB", check="drift", sev=WARN, msg="other"),
    ]
    grouped = _group_by_check(findings)
    assert set(grouped) == {"drift"}
    assert grouped["drift"]["cityA"].count == 2
    assert grouped["drift"]["cityA"].severity == ERROR
    assert grouped["drift"]["cityA"].example == "first"
    assert grouped["drift"]["cityB"].count == 1


def test_group_by_check_excludes_per_slug_checks():
    findings = [_finding(slug="cityA", check="meetings-url-dead")]
    assert _group_by_check(findings) == {}


def test_group_by_check_truncates_long_example():
    findings = [_finding(slug="cityA", check="drift", msg="x" * 500)]
    example = _group_by_check(findings)["drift"]["cityA"].example
    assert len(example) == _mod._MAX_EXAMPLE_CHARS
    assert example.endswith("…")


# ---------------------------------------------------------------------------
# _merge_first_seen
# ---------------------------------------------------------------------------


def test_merge_first_seen_unscoped_stamps_new_slug_now():
    keep, first_seen = _merge_first_seen({}, set(), {"cityA"}, audited_slugs=None, now=_NOW)
    assert keep == {"cityA"}
    assert first_seen == {"cityA": _NOW.isoformat()}


def test_merge_first_seen_unscoped_preserves_prior_timestamp():
    prior = {"cityA": "2026-06-01T00:00:00+00:00"}
    keep, first_seen = _merge_first_seen(prior, {"cityA"}, {"cityA"}, audited_slugs=None, now=_NOW)
    assert keep == {"cityA"}
    assert first_seen == prior


def test_merge_first_seen_unscoped_drops_cleared_slug():
    prior = {"cityA": "2026-06-01T00:00:00+00:00"}
    keep, first_seen = _merge_first_seen(prior, {"cityA"}, set(), audited_slugs=None, now=_NOW)
    assert keep == set()
    assert first_seen == {}


def test_merge_first_seen_scoped_preserves_untouched_slug():
    # A --city run scoped to cityB must not appear to "clear" cityA, which it never looked at.
    prior = {"cityA": "2026-06-01T00:00:00+00:00"}
    keep, first_seen = _merge_first_seen(prior, {"cityA"}, set(), audited_slugs={"cityB"}, now=_NOW)
    assert keep == {"cityA"}
    assert first_seen == prior


def test_merge_first_seen_scoped_adds_newly_affected_in_scope_slug():
    keep, first_seen = _merge_first_seen({}, set(), {"cityB"}, audited_slugs={"cityB"}, now=_NOW)
    assert keep == {"cityB"}
    assert first_seen == {"cityB": _NOW.isoformat()}


def test_merge_first_seen_scoped_clears_in_scope_resolved_slug():
    prior = {"cityB": "2026-06-01T00:00:00+00:00"}
    keep, first_seen = _merge_first_seen(prior, {"cityB"}, set(), audited_slugs={"cityB"}, now=_NOW)
    assert keep == set()
    assert first_seen == {}


# ---------------------------------------------------------------------------
# _since_label
# ---------------------------------------------------------------------------


def test_since_label_today():
    assert _since_label(_NOW.isoformat(), now=_NOW) == "2026-06-30 (today)"


def test_since_label_days_ago():
    earlier = datetime(2026, 6, 15, tzinfo=UTC)
    assert _since_label(earlier.isoformat(), now=_NOW) == "2026-06-15 (15d)"


def test_since_label_unknown_on_bad_iso():
    assert _since_label("not-a-date", now=_NOW) == "unknown"


# ---------------------------------------------------------------------------
# _render_grouped_body / _parse_state / _parse_prior_rows
# ---------------------------------------------------------------------------


def test_render_grouped_body_contains_marker_key_and_severity():
    rows = {"cityA": _FeedRow(slug="cityA", count=1, severity=WARN, example="ex")}
    first_seen = {"cityA": _NOW.isoformat()}
    body = _render_grouped_body(
        "drift", rows=rows, first_seen=first_seen, city_of={}, severity=WARN, now=_NOW
    )
    assert _mod.MARKER in body
    assert _key_marker("drift") in body
    assert "**Severity:** warn" in body
    assert "`cityA`" in body


def test_render_grouped_body_timeline_repair_checks_link_to_workflow_dispatch():
    rows = {"cityA": _FeedRow(slug="cityA", count=1, severity=ERROR, example="ex")}
    first_seen = {"cityA": _NOW.isoformat()}
    body = _render_grouped_body(
        "timeline-duration-mismatch",
        rows=rows,
        first_seen=first_seen,
        city_of={},
        severity=ERROR,
        now=_NOW,
    )
    assert "actions/workflows/audit.yml" in body
    assert "timeline_repair=true" in body
    assert "timeline_repair_cohort" in body
    assert "auto-closes after a later feed-health audit" in body


def test_repairable_timeline_guidance_links_to_workflow_dispatch():
    for check in (
        "timeline-duration-mismatch",
        "timeline-short-coverage",
        "rendered-duration-mismatch",
        "timeline-source-duration-mismatch",
        "timeline-source-underrun",
        "timeline-source-overrun",
    ):
        guidance = _mod._guidance_for(check)
        assert "actions/workflows/audit.yml" in guidance
        assert "timeline_repair=true" in guidance


def test_render_grouped_body_truncates_past_max_rows(monkeypatch):
    monkeypatch.setattr(_mod, "_MAX_ROWS", 2)
    rows = {
        f"city{i}": _FeedRow(slug=f"city{i}", count=1, severity=WARN, example="ex")
        for i in range(3)
    }
    first_seen = {f"city{i}": _NOW.isoformat() for i in range(3)}
    body = _render_grouped_body(
        "drift", rows=rows, first_seen=first_seen, city_of={}, severity=WARN, now=_NOW
    )
    assert "...and 1 more" in body


def test_render_grouped_body_parse_state_round_trip():
    rows = {"cityA": _FeedRow(slug="cityA", count=1, severity=WARN, example="ex")}
    first_seen = {"cityA": "2026-06-01T00:00:00+00:00"}
    body = _render_grouped_body(
        "drift", rows=rows, first_seen=first_seen, city_of={}, severity=WARN, now=_NOW
    )
    state = _parse_state(body)
    assert state["check"] == "drift"
    assert state["first_seen"] == first_seen


def test_render_grouped_body_state_rows_survive_display_cap(monkeypatch):
    # The visible table truncates at _MAX_ROWS, but the hidden state block must still carry full
    # row detail for every affected feed, so a carried-forward feed beyond the display cap
    # doesn't fall back to fabricated severity/count/example.
    monkeypatch.setattr(_mod, "_MAX_ROWS", 1)
    rows = {
        "cityA": _FeedRow(slug="cityA", count=1, severity=WARN, example="ex-a"),
        "cityB": _FeedRow(slug="cityB", count=5, severity=ERROR, example="ex-b"),
    }
    first_seen = {"cityA": _NOW.isoformat(), "cityB": _NOW.isoformat()}
    body = _render_grouped_body(
        "drift", rows=rows, first_seen=first_seen, city_of={}, severity=ERROR, now=_NOW
    )
    assert "`cityB`" not in body  # beyond the display cap
    recovered = _parse_state_rows(_parse_state(body))
    assert recovered["cityB"] == _FeedRow(slug="cityB", count=5, severity=ERROR, example="ex-b")


def test_parse_state_returns_empty_on_missing_block():
    assert _parse_state("no state block here") == {}


def test_parse_state_returns_empty_when_first_seen_is_not_a_dict():
    # A hand-edited or corrupted state block (e.g. first_seen as a string) must not silently
    # propagate into _merge_first_seen, which assumes first_seen is a dict.
    body = 'x\n<!-- citypods:feed-health:state\n{"check": "drift", "first_seen": "garbage"}\n-->'
    assert _parse_state(body) == {}


def test_parse_state_drops_non_string_first_seen_entries():
    # A hand-edited state block with e.g. a numeric timestamp for one slug must not propagate
    # that malformed entry -- only the invalid entry is dropped, not the whole state.
    body = (
        "x\n<!-- citypods:feed-health:state\n"
        '{"check": "drift", "first_seen": {"cityA": "2026-06-01T00:00:00+00:00", "cityB": 123}}'
        "\n-->"
    )
    state = _parse_state(body)
    assert state["first_seen"] == {"cityA": "2026-06-01T00:00:00+00:00"}


def test_parse_state_keeps_first_seen_when_rows_is_malformed():
    # A malformed `rows` value must not also discard an otherwise-valid first_seen -- only
    # _parse_state_rows() (which reads `rows` alone) should be affected.
    body = (
        "x\n<!-- citypods:feed-health:state\n"
        '{"check": "drift", "first_seen": {"cityA": "2026-06-01T00:00:00+00:00"}, '
        '"rows": "not-a-dict"}'
        "\n-->"
    )
    state = _parse_state(body)
    assert state["first_seen"] == {"cityA": "2026-06-01T00:00:00+00:00"}
    assert _parse_state_rows(state) == {}


def test_render_state_block_escapes_html_comment_terminator_in_example():
    # A feed-derived example (e.g. a scraped provider error message) containing a literal "-->"
    # must not be able to prematurely terminate the hidden HTML comment and corrupt the state
    # block -- the round trip through render -> parse must still recover the original text.
    rows = {"cityA": _FeedRow(slug="cityA", count=1, severity=WARN, example="broke here --> oops")}
    first_seen = {"cityA": _NOW.isoformat()}
    body = _render_grouped_body(
        "drift", rows=rows, first_seen=first_seen, city_of={}, severity=WARN, now=_NOW
    )
    # The raw payload embedded in the HTML comment must not contain an unescaped "-->".
    match = _mod._STATE_RE.search(body)
    assert "-->" not in match.group(1)
    state = _parse_state(body)
    assert state["first_seen"] == first_seen  # comment wasn't truncated early
    recovered = _parse_state_rows(state)
    assert recovered["cityA"].example == "broke here --> oops"


def test_parse_prior_rows_recovers_slug_count_and_example():
    rows = {"cityA": _FeedRow(slug="cityA", count=3, severity=WARN, example="some example")}
    first_seen = {"cityA": _NOW.isoformat()}
    body = _render_grouped_body(
        "drift",
        rows=rows,
        first_seen=first_seen,
        city_of={"cityA": "CityA"},
        severity=WARN,
        now=_NOW,
    )
    recovered = _parse_prior_rows(body)
    assert recovered["cityA"].count == 3
    assert recovered["cityA"].example == "some example"


def test_parse_prior_rows_recovers_error_severity():
    # A carried-forward row must not silently downgrade an ERROR-severity feed to WARN just
    # because it wasn't re-evaluated this run -- that would let a real error mismatch the
    # issue's overall severity label.
    rows = {"cityA": _FeedRow(slug="cityA", count=1, severity=ERROR, example="boom")}
    first_seen = {"cityA": _NOW.isoformat()}
    body = _render_grouped_body(
        "drift", rows=rows, first_seen=first_seen, city_of={}, severity=ERROR, now=_NOW
    )
    recovered = _parse_prior_rows(body)
    assert recovered["cityA"].severity == ERROR


# ---------------------------------------------------------------------------
# _grouped_title
# ---------------------------------------------------------------------------


def test_grouped_title_singular_feed_no_city_clause():
    assert _grouped_title("drift", n_feeds=1, n_cities=1) == "[feed-health] drift: 1 feed"


def test_grouped_title_omits_city_clause_when_equal_counts():
    assert _grouped_title("drift", n_feeds=3, n_cities=3) == "[feed-health] drift: 3 feeds"


def test_grouped_title_includes_city_clause_when_fewer_cities():
    assert (
        _grouped_title("drift", n_feeds=3, n_cities=2)
        == "[feed-health] drift: 3 feeds across 2 cities"
    )


def test_grouped_title_singular_city_clause():
    assert (
        _grouped_title("drift", n_feeds=3, n_cities=1)
        == "[feed-health] drift: 3 feeds across 1 city"
    )


# ---------------------------------------------------------------------------
# _state_comment
# ---------------------------------------------------------------------------


def test_state_comment_contains_message_and_severity():
    comment = _state_comment("drift", "newly affected: cityB", severity=WARN)
    assert "warn" in comment
    assert "newly affected: cityB" in comment
    assert "Audit update" in comment


# ---------------------------------------------------------------------------
# reconcile — dry_run (no GitHub calls, uses empty existing dict)
# ---------------------------------------------------------------------------


def test_reconcile_dry_run_create_printed(capsys):
    f = _finding(slug="cityA", check="drift", msg="zero episodes")
    with mock.patch.object(_mod, "_open_issues", return_value={}):
        reconcile([f], dry_run=True, now=_NOW)
    out = capsys.readouterr().out
    assert "CREATE" in out
    assert "drift" in out
    assert "1 feed" in out


def test_reconcile_dry_run_no_issues_prints_summary(capsys):
    with mock.patch.object(_mod, "_open_issues", return_value={}):
        reconcile([], dry_run=True, now=_NOW)
    out = capsys.readouterr().out
    assert "0 created, 0 updated, 0 closed." in out


# ---------------------------------------------------------------------------
# reconcile — consolidated (one-issue-per-check) model
# ---------------------------------------------------------------------------


def test_reconcile_grouped_creates_new_issue_with_count_in_title():
    f = _finding(slug="cityA", check="drift", msg="zero episodes")
    calls = _run_reconcile([f], existing_issues={}, now=_NOW)
    create_calls = [a for a in calls if len(a) >= 2 and a[1] == "create"]
    assert create_calls
    assert "[feed-health] drift: 1 feed" in create_calls[0]


def test_reconcile_grouped_no_action_when_unchanged():
    f = _finding(slug="cityA", check="drift", msg="zero episodes")
    rows = _group_by_check([f])["drift"]
    first_seen = {"cityA": _NOW.isoformat()}
    existing = _grouped_issue(
        "drift",
        number=10,
        first_seen=first_seen,
        rows=rows,
        now=_NOW,
        labels=["signal:feed-health", "type:operations", "severity:warn"],
    )
    calls = _run_reconcile([f], existing, now=_NOW)
    edit_calls = [a for a in calls if len(a) >= 2 and a[1] == "edit"]
    comment_calls = [a for a in calls if len(a) >= 2 and a[1] == "comment"]
    assert not edit_calls, "body, title and labels all unchanged -> no edit"
    assert not comment_calls


def test_reconcile_grouped_syncs_stale_label_without_comment():
    f = _finding(slug="cityA", check="drift", msg="zero episodes")
    rows = _group_by_check([f])["drift"]
    first_seen = {"cityA": _NOW.isoformat()}
    existing = _grouped_issue(
        "drift", number=40, first_seen=first_seen, rows=rows, now=_NOW, labels=[]
    )
    calls = _run_reconcile([f], existing, now=_NOW)
    label_edit_calls = [a for a in calls if len(a) >= 2 and a[1] == "edit" and "--add-label" in a]
    comment_calls = [a for a in calls if len(a) >= 2 and a[1] == "comment"]
    assert label_edit_calls, "missing labels should be added even when body is unchanged"
    assert "--body" not in label_edit_calls[0], "label sync should not also re-edit the body"
    assert not comment_calls


def test_reconcile_grouped_no_comment_on_pure_day_count_change():
    # A "since Nd ago" display refreshing daily must not itself trigger a chatty comment --
    # only a real change in the affected-feed set should. Labels are pre-matched here so the
    # edit_calls assertion below isolates the body-change path (a label-sync edit would also
    # make edit_calls truthy, confounding the check this test exists for).
    f = _finding(slug="cityA", check="drift", msg="zero episodes")
    rows = _group_by_check([f])["drift"]
    old_now = datetime(2026, 6, 1, tzinfo=UTC)
    first_seen = {"cityA": old_now.isoformat()}
    existing = _grouped_issue(
        "drift",
        number=10,
        first_seen=first_seen,
        rows=rows,
        now=old_now,
        labels=["signal:feed-health", "type:operations", "severity:warn"],
    )
    later = datetime(2026, 6, 20, tzinfo=UTC)
    calls = _run_reconcile([f], existing, now=later)
    body_edit_calls = [a for a in calls if len(a) >= 2 and a[1] == "edit" and "--body" in a]
    comment_calls = [a for a in calls if len(a) >= 2 and a[1] == "comment"]
    assert body_edit_calls, "since-label day count changed -> body edit expected"
    assert not comment_calls, "affected-feed set unchanged -> no comment"


def test_reconcile_grouped_updates_and_comments_on_newly_affected_feed():
    f_a = _finding(slug="cityA", check="drift", msg="zero episodes")
    rows_a = _group_by_check([f_a])["drift"]
    first_seen = {"cityA": _NOW.isoformat()}
    existing = _grouped_issue("drift", number=10, first_seen=first_seen, rows=rows_a, now=_NOW)
    f_b = _finding(slug="cityB", check="drift", msg="zero episodes")
    calls = _run_reconcile([f_a, f_b], existing, now=_NOW)
    edit_calls = [a for a in calls if len(a) >= 2 and a[1] == "edit"]
    comment_calls = [a for a in calls if len(a) >= 2 and a[1] == "comment"]
    assert edit_calls
    assert comment_calls
    assert any("cityB" in " ".join(c) for c in comment_calls)


def test_reconcile_grouped_closes_when_no_feeds_remain():
    rows = {"cityA": _FeedRow(slug="cityA", count=1, severity=WARN, example="x")}
    first_seen = {"cityA": _NOW.isoformat()}
    existing = _grouped_issue("drift", number=20, first_seen=first_seen, rows=rows, now=_NOW)
    calls = _run_reconcile([], existing, now=_NOW)
    close_calls = [a for a in calls if len(a) >= 2 and a[1] == "close"]
    assert close_calls
    assert "20" in close_calls[0]


def test_reconcile_grouped_does_not_close_when_only_some_feeds_clear():
    rows = {
        "cityA": _FeedRow(slug="cityA", count=1, severity=WARN, example="x"),
        "cityB": _FeedRow(slug="cityB", count=1, severity=WARN, example="y"),
    }
    first_seen = {"cityA": _NOW.isoformat(), "cityB": _NOW.isoformat()}
    existing = _grouped_issue("drift", number=21, first_seen=first_seen, rows=rows, now=_NOW)
    f_a = _finding(slug="cityA", check="drift", msg="zero episodes")
    calls = _run_reconcile([f_a], existing, now=_NOW)
    close_calls = [a for a in calls if len(a) >= 2 and a[1] == "close"]
    comment_calls = [a for a in calls if len(a) >= 2 and a[1] == "comment"]
    assert not close_calls
    assert any("cleared" in " ".join(c) and "cityB" in " ".join(c) for c in comment_calls)


def test_reconcile_grouped_scoped_run_preserves_out_of_scope_feed():
    rows = {"cityA": _FeedRow(slug="cityA", count=1, severity=WARN, example="x")}
    first_seen = {"cityA": _NOW.isoformat()}
    existing = _grouped_issue(
        "drift",
        number=30,
        first_seen=first_seen,
        rows=rows,
        now=_NOW,
        labels=["signal:feed-health", "type:operations", "severity:warn"],
    )
    # This --city run is scoped to cityB; it never re-evaluated cityA, so cityA's row must be
    # carried forward untouched (not silently cleared just because it's absent this run).
    calls = _run_reconcile([], existing, now=_NOW, audited_slugs={"cityB"}, city_of={})
    assert not calls, "an issue entirely out of a scoped run's audited slugs must not be touched"


def test_reconcile_grouped_scoped_run_preserves_error_severity_of_out_of_scope_feed():
    # If the carry-forward path downgraded cityA's recovered severity to WARN, the issue's
    # overall severity would flip from error to warn and mismatch the existing severity:error
    # label -> spurious label-edit call, even though nothing about cityA actually changed.
    rows = {"cityA": _FeedRow(slug="cityA", count=1, severity=ERROR, example="boom")}
    first_seen = {"cityA": _NOW.isoformat()}
    existing = _grouped_issue(
        "drift",
        number=32,
        first_seen=first_seen,
        rows=rows,
        now=_NOW,
        labels=["signal:feed-health", "type:operations", "severity:error"],
    )
    calls = _run_reconcile([], existing, now=_NOW, audited_slugs={"cityB"}, city_of={})
    assert not calls, "out-of-scope ERROR severity must survive the carry-forward unchanged"


def test_reconcile_grouped_scoped_run_preserves_severity_beyond_display_cap(monkeypatch):
    # Same as above, but for a feed that falls past _MAX_ROWS in the *visible* table -- the
    # carry-forward path must read the hidden state block's full row map, not just what
    # _parse_prior_rows can recover from the (truncated) rendered table.
    monkeypatch.setattr(_mod, "_MAX_ROWS", 1)
    # cityA's example matches what this run's finding will produce, so cityA's row doesn't also
    # look "changed" this run -- isolating the test to cityB's out-of-scope carry-forward.
    rows = {
        "cityA": _FeedRow(slug="cityA", count=1, severity=WARN, example="zero episodes"),
        "cityB": _FeedRow(slug="cityB", count=1, severity=ERROR, example="boom"),
    }
    first_seen = {"cityA": _NOW.isoformat(), "cityB": _NOW.isoformat()}
    existing = _grouped_issue(
        "drift",
        number=33,
        first_seen=first_seen,
        rows=rows,
        now=_NOW,
        labels=["signal:feed-health", "type:operations", "severity:error"],
    )
    # cityB (ERROR, past the display cap) is out of scope; cityA (WARN, shown) is re-affirmed.
    f_a = _finding(slug="cityA", check="drift", msg="zero episodes")
    calls = _run_reconcile([f_a], existing, now=_NOW, audited_slugs={"cityA"})
    assert not calls, "cityB's ERROR severity/detail must survive the carry-forward unchanged"


def test_reconcile_grouped_scoped_run_clears_in_scope_feed():
    rows = {
        "cityA": _FeedRow(slug="cityA", count=1, severity=WARN, example="x"),
        "cityB": _FeedRow(slug="cityB", count=1, severity=WARN, example="y"),
    }
    first_seen = {"cityA": _NOW.isoformat(), "cityB": _NOW.isoformat()}
    existing = _grouped_issue(
        "drift",
        number=31,
        first_seen=first_seen,
        rows=rows,
        now=_NOW,
        labels=["signal:feed-health", "type:operations", "severity:warn"],
    )
    f_b = _finding(slug="cityB", check="drift", msg="zero episodes")
    calls = _run_reconcile([f_b], existing, now=_NOW, audited_slugs={"cityA", "cityB"})
    close_calls = [a for a in calls if len(a) >= 2 and a[1] == "close"]
    comment_calls = [a for a in calls if len(a) >= 2 and a[1] == "comment"]
    assert not close_calls
    assert any("cleared" in " ".join(c) and "cityA" in " ".join(c) for c in comment_calls)


# ---------------------------------------------------------------------------
# reconcile — per-slug (one-issue-per-(slug, check)) model, meetings-url-* only
# ---------------------------------------------------------------------------


def test_reconcile_per_slug_creates_new_issue_with_human_verification_label():
    f = _finding(
        slug="cityA",
        check="meetings-url-dead",
        msg="meetings_url returned HTTP 404: https://x.gov",
    )
    calls = _run_reconcile([f], existing_issues={}, now=_NOW)
    create_calls = [a for a in calls if len(a) >= 2 and a[1] == "create"]
    assert create_calls
    assert "needs:human-verification" in create_calls[0]


def test_reconcile_per_slug_no_action_when_unchanged():
    f = _finding(
        slug="cityA",
        check="meetings-url-dead",
        msg="meetings_url returned HTTP 404: https://x.gov",
    )
    existing = _per_slug_issue(
        f,
        number=50,
        labels=[
            "signal:feed-health",
            "type:operations",
            "severity:warn",
            "needs:human-verification",
        ],
    )
    calls = _run_reconcile([f], existing, now=_NOW)
    edit_calls = [a for a in calls if len(a) >= 2 and a[1] == "edit"]
    comment_calls = [a for a in calls if len(a) >= 2 and a[1] == "comment"]
    assert not edit_calls
    assert not comment_calls


def test_reconcile_per_slug_update_edits_body_and_posts_comment():
    f_old = _finding(
        slug="cityA",
        check="meetings-url-dead",
        msg="meetings_url returned HTTP 404: https://x.gov/old",
    )
    f_new = _finding(
        slug="cityA",
        check="meetings-url-dead",
        msg="meetings_url returned HTTP 404: https://x.gov/new",
    )
    existing = _per_slug_issue(f_old, number=51, labels=["needs:human-verification"])
    calls = _run_reconcile([f_new], existing, now=_NOW)
    edit_calls = [a for a in calls if len(a) >= 2 and a[1] == "edit"]
    comment_calls = [a for a in calls if len(a) >= 2 and a[1] == "comment"]
    assert edit_calls, "state changed -> body should be updated"
    assert comment_calls, "state changed -> comment should be posted"
    assert "51" in comment_calls[0]


def test_reconcile_per_slug_does_not_autoclose_with_needs_human_verification():
    existing = {
        _issue_key("cityA", "meetings-url-dead"): {
            "number": 77,
            "title": _title("cityA", "meetings-url-dead"),
            "body": "x",
            "labels": [{"name": "needs:human-verification"}],
        }
    }
    calls = _run_reconcile([], existing, now=_NOW)
    close_calls = [a for a in calls if len(a) >= 2 and a[1] == "close"]
    assert not close_calls, "meetings-url issues awaiting human verification must not auto-close"


def test_reconcile_per_slug_closes_after_human_verification_label_removed():
    existing = {
        _issue_key("cityA", "meetings-url-dead"): {
            "number": 77,
            "title": _title("cityA", "meetings-url-dead"),
            "body": "x",
            "labels": [],
        }
    }
    calls = _run_reconcile([], existing, now=_NOW)
    close_calls = [a for a in calls if len(a) >= 2 and a[1] == "close"]
    assert close_calls, "verified resolved meetings-url issues should close"


def test_reconcile_per_slug_closes_obsolete_inconclusive_issue():
    existing = {
        _issue_key("cityA", "meetings-url-dead"): {
            "number": 77,
            "title": _title("cityA", "meetings-url-dead"),
            "body": "meetings_url returned HTTP 403: https://x.gov/meetings",
            "labels": [{"name": "needs:human-verification"}],
        }
    }
    calls = _run_reconcile([], existing, now=_NOW)
    close_calls = [a for a in calls if len(a) >= 2 and a[1] == "close"]
    assert close_calls, "legacy 403 meetings-url issues should close under the current policy"


def test_reconcile_per_slug_scoped_run_does_not_touch_other_city_issue():
    existing = {
        _issue_key("othercity", "meetings-url-dead"): {
            "number": 5,
            "title": _title("othercity", "meetings-url-dead"),
            "body": "x",
            "labels": [],
        }
    }
    calls = _run_reconcile([], existing, now=_NOW, audited_slugs={"thiscity"})
    close_calls = [a for a in calls if len(a) >= 2 and a[1] == "close"]
    assert not close_calls, "issue for a city outside the audited scope must not be closed"


# ---------------------------------------------------------------------------
# main() plumbing
# ---------------------------------------------------------------------------


def test_main_pulls_canonical_state_before_auditing(monkeypatch):
    """``main()`` must pull the bucket's canonical state before running checks (not rely solely
    on whatever ``actions/cache/restore`` happened to land on) — otherwise the timeline-integrity
    check can compare an EDL and a served-duration captured at two different points in the
    pipeline's history and file a false-positive finding (see audit.yml's removed cache step)."""
    calls: list[str] = []

    monkeypatch.setattr(_mod, "load_site_config", lambda *_a, **_k: {"defaults": {}})
    monkeypatch.setattr(_mod, "load_city_configs", lambda *_a, **_k: [])
    monkeypatch.setattr(
        _mod, "pull_canonical_state", lambda *_a, **_k: calls.append("pull_canonical_state")
    )
    monkeypatch.setattr(_mod, "audit_all", lambda *_a, **_k: (calls.append("audit_all"), [])[1])
    monkeypatch.setattr(_mod, "_ensure_labels", lambda: None)
    monkeypatch.setattr(_mod, "reconcile", lambda *_a, **_k: 0)

    _mod.main(["--dry-run"])

    assert calls.index("pull_canonical_state") < calls.index("audit_all")


def test_main_builds_city_of_from_city_entity(monkeypatch, tmp_path):
    cities = [
        types.SimpleNamespace(slug="feedA", city_entity="cityA"),
        types.SimpleNamespace(slug="feedB", city_entity=None),
    ]
    captured: dict = {}

    monkeypatch.setattr(_mod, "load_site_config", lambda *_a, **_k: {"defaults": {}})
    monkeypatch.setattr(_mod, "load_city_configs", lambda *_a, **_k: cities)
    monkeypatch.setattr(_mod, "pull_canonical_state", lambda *_a, **_k: tmp_path)
    monkeypatch.setattr(_mod, "audit_all", lambda *_a, **_k: [])
    monkeypatch.setattr(_mod, "_ensure_labels", lambda: None)

    def _fake_reconcile(_findings, **kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(_mod, "reconcile", _fake_reconcile)

    _mod.main(["--dry-run"])

    assert captured["city_of"] == {"feedA": "cityA", "feedB": "feedB"}


def test_pull_canonical_state_degrades_gracefully_when_bucket_unavailable(monkeypatch, tmp_path):
    """If B2 secrets are absent/invalid or the bucket is unreachable, the audit must keep running
    against whatever state is already on disk rather than crashing — the old actions/cache step
    degraded silently on a cache miss, and the bucket pull must preserve that, not regress to a
    hard failure that takes down unrelated checks (e.g. GH issue reconciliation) too."""
    from citypods import state as state_mod

    monkeypatch.setattr(state_mod, "resolve_state_dir", lambda *_a, **_k: tmp_path)

    def _boom(*_a, **_k):
        raise ValueError("Invalid endpoint: ")

    monkeypatch.setattr(state_mod, "make_storage", _boom)

    result = state_mod.pull_canonical_state({}, "docs")

    assert result == tmp_path
