#!/usr/bin/env python
"""Run the feed-health audit and reconcile findings to GitHub issues (idempotently).

**Consolidated checks** (most checks — see ``_PER_SLUG_CHECKS`` for the exception): one issue
per *check*, covering every affected feed — title ``[feed-health] <check>: N feed(s) [across M
cit(ies)]``. The body lists each affected feed with when it was first observed failing (tracked
in a hidden JSON block in the issue body itself — no external state file) and a representative
example, plus check-specific guidance on causes and resolution. This replaces the old one-issue-
per-``(slug, check)`` model, which could file dozens of near-duplicate issues for one systemic
problem (e.g. a single code regression affecting every feed's timeline check).

**Per-slug checks** (``meetings-url-dead`` / ``meetings-url-changed``): kept on the original
one-issue-per-``(slug, check)`` model. Each broken ``meetings_url`` is typically a genuinely
distinct problem needing a specific human to verify a specific city's page and update that
city's YAML — consolidating them would make the ``needs:human-verification`` label ambiguous
when some but not all affected cities are fixed.

Issue matching uses a hidden ``<!-- citypods:feed-health:key=... -->`` marker in the body, not
the title — so the title can show a live, changing affected-count without breaking run-to-run
matching. On each run:

  * a check with newly-affected feeds and no matching open issue -> create it;
  * a check whose issue exists and whose affected-feed set or detail changed -> update it;
  * an open feed-health issue whose check now affects zero feeds -> close it.

Designed for the daily ``audit.yml`` cron (GITHUB_TOKEN -> ``gh``), but runs locally with
``--dry-run`` to preview without touching GitHub.

Usage:
    PYTHONPATH=. python scripts/audit_feeds.py [--dry-run] [--enclosures] [--city SLUG]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from citypods.audit import ERROR, WARN, Finding, audit_all
from citypods.config import load_city_configs, load_site_config
from citypods.records import source_key
from citypods.state import pull_canonical_state
from citypods.statesync import push_state
from citypods.storage import make_storage

LABELS = {
    "signal:feed-health": ("0E8A16", "Automated feed-health finding"),
    "type:operations": ("5319E7", "Operational work, not a feature or bug"),
    "severity:error": ("B60205", "A feed is broken (no/dead episodes)"),
    "severity:warn": ("FBCA04", "A feed may be degraded or incomplete"),
    "needs:human-verification": ("C5DEF5", "Requires manual investigation before auto-closing"),
}
TITLE_PREFIX = "[feed-health]"
MARKER = "<!-- citypods:feed-health -->"
_KEY_RE = re.compile(r"<!-- citypods:feed-health:key=(\S+) -->")
_STATE_RE = re.compile(r"<!-- citypods:feed-health:state\n(.*?)\n-->", re.DOTALL)

# Checks that stay on the legacy one-issue-per-(slug, check) model. Every other check is
# consolidated into one issue per check, covering every affected feed. See the module docstring.
_PER_SLUG_CHECKS = frozenset({"meetings-url-dead", "meetings-url-changed"})
_DEAD_MEETINGS_URL_STATUSES = frozenset({404, 410, 451})

# How many affected feeds get a full row before the table is truncated with a "+N more" note.
# Keeps a systemic, catalog-wide regression from producing an unreadably long single issue body —
# exactly the "chatty" outcome this redesign exists to avoid, just moved from many issues to one
# overlong issue instead. The hidden state block (below) still tracks every affected feed's
# first-seen date regardless of the display cap, so nothing is lost across runs.
_MAX_ROWS = 60
_MAX_EXAMPLE_CHARS = 180

# Per-check guidance: what usually causes it, and how an operator should investigate/resolve it.
# Shown in every issue body (consolidated or per-slug) beneath the severity/summary line.
_GUIDANCE: dict[str, str] = {
    "unreachable": (
        "**What this means:** the provider's source feed/API itself could not be fetched or "
        "parsed this run.\n\n"
        "**Common causes:** the provider's website is down or changed its page/API structure, a "
        "transient network error, rate limiting, or a malformed response.\n\n"
        "**Resolution:** run `citypods doctor --city <slug>` locally to reproduce. If the "
        "provider changed its markup/API, the adapter in `citypods/providers/<provider>.py` "
        "needs an update. Transient failures usually clear on their own by the next run."
    ),
    "drift": (
        "**What this means:** the feed returned zero episodes this run.\n\n"
        "**Common causes:** a parser regression, a misconfigured source URL/`view_id`, or the "
        "provider genuinely changed/removed the source.\n\n"
        "**Resolution:** verify the configured source resolves in a browser and still lists "
        "meetings. Compare against the archive-diff summary in each feed's detail row below — "
        "if the archive still has materialized episodes, this may be a transient window shift "
        "rather than real data loss."
    ),
    "empty": (
        "**What this means:** the feed has fewer episodes than `min_meetings_per_body` in its "
        "current window.\n\n"
        "**Common causes:** a genuinely low-frequency board, a provider window that doesn't "
        "reach back far enough, or a `body:` filter that's too narrow or misspelled.\n\n"
        "**Resolution:** confirm the `body:` filter matches the provider's actual "
        "committee/body naming. If the provider's window is too short, consider a Legistar "
        "calendar provider or Swagit multi-part coverage to extend it."
    ),
    "stale": (
        "**What this means:** the feed's newest episode is much older than its own typical "
        "meeting cadence.\n\n"
        "**Common causes:** the board is in recess or was dissolved, the provider feed broke "
        "silently, or a stale/incorrect `view_id`/body filter is excluding recent meetings.\n\n"
        "**Resolution:** check the city's public meeting calendar for a real gap. If meetings "
        "are happening but not appearing here, check the provider config for a stale view or "
        "filter."
    ),
    "rehost-backlog": (
        "**What this means:** the feed has HLS episodes but none have been hosted, even though "
        "the audio pipeline is actively encoding other feeds.\n\n"
        "**Common causes:** materialization is repeatedly failing for this specific source "
        "(unreachable media, a geo/DRM block, a source-specific auth/token issue), or a "
        "sharding/backlog-priority quirk is starving this source of budget.\n\n"
        "**Resolution:** check `materialize_attempts`/`materialize_error` on this source's "
        "records for a specific error signature, and check recent `audio.yml` run logs for "
        "this `source_key`."
    ),
    "dead-enclosure": (
        "**What this means:** a sample of this feed's hosted-audio URLs returned 4xx/5xx.\n\n"
        "**Common causes:** a signed/presigned URL expired, the object was deleted from storage "
        "(e.g. by orphan-GC before a re-check), or — for directly-hosted, non-rehosted audio — "
        "the provider took the file down.\n\n"
        "**Resolution:** this usually self-heals automatically (the audit attempts a re-resolve "
        "before filing). If it persists, check the storage bucket for the expected key and "
        "re-run `audio.yml` for the affected source."
    ),
    "timeline-empty": (
        "**What this means:** an episode's non-identity timeline has zero segments — this "
        "should be structurally impossible.\n\n"
        "**Common causes:** a `TimelinePlanner` implementation returned a `Timeline` with no "
        "segments instead of `None`/an identity timeline.\n\n"
        "**Resolution:** this is a code bug, not a data issue. Check `timeline_version` on the "
        "affected episode(s) to identify which planner combination produced it."
    ),
    "timeline-overlap": (
        "**What this means:** two served-time segments in an episode's EDL overlap.\n\n"
        "**Common causes:** a planner produced non-monotonic segments — most likely a bug in a "
        "concat/insert planner's ordering.\n\n"
        "**Resolution:** code bug. Inspect the affected episode's stored EDL and the planner "
        "version that produced it."
    ),
    "timeline-gap": (
        "**What this means:** there's a hole in served time between two adjacent EDL segments.\n\n"
        "**Common causes:** a planner failed to account for some served audio, often at a "
        "concat/insert boundary.\n\n"
        "**Resolution:** code bug. Inspect the affected episode's stored EDL and the planner "
        "version that produced it."
    ),
    "timeline-gap-start": (
        "**What this means:** an episode's EDL doesn't start at served time 0.\n\n"
        "**Common causes:** a planner bug — the served clock should always start at 0.\n\n"
        "**Resolution:** code bug. Inspect the affected episode's stored EDL and the planner "
        "version that produced it."
    ),
    "timeline-duration-mismatch": (
        "**What this means:** the stored EDL's segment total doesn't match the recorded "
        "`audio_duration_served` (the cheap record-field check, used when no live audio probe "
        "was available this run).\n\n"
        "**Common causes:** `audio_duration_served` and the EDL fell out of sync — typically a "
        "stale record from before a repair, or a materialize step that didn't refresh the "
        "served-duration field.\n\n"
        "**Resolution:** force a re-materialize for the affected episode(s) (a "
        "`timeline-replan`/`audio-rematerialize` repair flag). If a live audio probe becomes "
        "available on a later run, the more precise `rendered-duration-mismatch` check "
        "supersedes this one for the same episode."
    ),
    "timeline-short-coverage": (
        "**What this means:** an episode's last EDL segment doesn't reach "
        "`audio_duration_served`.\n\n"
        "**Common causes:** same as `timeline-duration-mismatch` — the EDL and the recorded "
        "served duration have fallen out of sync.\n\n"
        "**Resolution:** force a re-materialize for the affected episode(s)."
    ),
    "rendered-duration-mismatch": (
        "**What this means:** the *live-probed* rendered audio duration disagrees with the "
        "stored EDL beyond tolerance (GH#702).\n\n"
        "**Common causes:** the silence planner's source-duration basis (container vs. decoded "
        "vs. stream-sample) overstating the true playable audio, a PTS discontinuity in the "
        "source stream, or an unresolved render-filtergraph bug. The `is_degenerate_served_"
        "duration` safety net can also leave a stale, uncorrected EDL in place when a "
        "legitimately-short corrected EDL trips it.\n\n"
        "**Resolution:** see the GH#702 runbook in `review/20`. Typically requires re-stamping "
        "`timeline-replan` + `audio-rematerialize` repair flags via a manual `timeline_repair` "
        "dispatch of the feed-health workflow, then letting the audio lane drain and "
        "re-auditing."
    ),
    "timeline-source-duration-mismatch": (
        "**What this means:** for a multi-source (concat) episode, a source segment's actual "
        "duration disagrees with its registered `SourceMedia.duration`.\n\n"
        "**Common causes:** the concat planner measured a source's duration from a stale probe, "
        "or (for legacy multi-part meetings) a part was re-uploaded/replaced upstream after "
        "being measured.\n\n"
        "**Resolution:** re-plan the episode's timeline to force a fresh source-duration probe, "
        "then re-materialize."
    ),
    "timeline-source-underrun": (
        "**What this means:** an EDL segment's source-time span starts before 0.\n\n"
        "**Common causes:** a stale/incorrect `SourceMedia.duration`, or a planner bug computing "
        "segment bounds.\n\n"
        "**Resolution:** re-plan the episode's timeline to refresh the source-duration "
        "measurement."
    ),
    "timeline-source-overrun": (
        "**What this means:** an EDL segment's source-time span extends past "
        "`SourceMedia.duration`.\n\n"
        "**Common causes:** a stale/incorrect `SourceMedia.duration` recorded before the actual "
        "source changed, or a planner bug computing segment bounds.\n\n"
        "**Resolution:** re-plan the episode's timeline to refresh the source-duration "
        "measurement."
    ),
    "timeline-chapter-out-of-range": (
        "**What this means:** a served-time chapter marker falls outside "
        "`[0, served_duration]`.\n\n"
        "**Common causes:** chapters were remapped against an EDL that has since changed (a "
        "silence re-plan moved the served clock without a fresh chapter remap), or the "
        "provider-supplied chapter timestamps are simply wrong.\n\n"
        "**Resolution:** usually self-heals once `RemapStage` re-runs against the current EDL. "
        "If it persists, check `chapters_basis` on the affected record."
    ),
    "view-cap": (
        "**What this means:** a provider's RSS view is returning exactly the item cap — the "
        "window is likely truncated.\n\n"
        "**Common causes:** the provider's feed/view has more items than its page size allows, "
        "silently dropping older or less-frequent bodies from the window.\n\n"
        "**Resolution:** split the source into multiple `feed_urls` (per-view), or migrate "
        "low-frequency bodies to a provider/method with full history (a Legistar calendar "
        "provider, or Swagit)."
    ),
    "dead-audio": (
        "**What this means:** episodes across feeds have no materializable audio at all "
        "(`MEDIA_DEAD`).\n\n"
        "**Common causes:** keyless/legacy sources with no usable page media, or the provider "
        "permanently removed a recording.\n\n"
        "**Resolution:** mostly unrecoverable per-episode (already durably marked). Review the "
        "`/admin/status` repair backlog to distinguish a genuine new regression from known, "
        "accepted legacy gaps."
    ),
    "deferred-audio": (
        "**What this means:** episodes across feeds are in materialization backoff "
        "(`MEDIA_DEFERRED`) and will retry automatically.\n\n"
        "**Common causes:** transient fetch failures, rate limiting, or a temporarily "
        "unreachable provider CDN.\n\n"
        "**Resolution:** usually self-heals as backoff expires. Investigate only if the count "
        "keeps climbing across multiple audits instead of draining."
    ),
    "meetings-url-dead": (
        "**What this means:** the configured `meetings_url` returned a browser-visible dead "
        "status (404/410/451).\n\n"
        "**Common causes:** the city reorganized its website or removed the meeting-archive "
        "page.\n\n"
    ),
    "meetings-url-changed": (
        "**What this means:** the configured `meetings_url` redirected to a much shorter "
        "path — likely the site root, not a real meeting-archive page.\n\n"
        "**Common causes:** the city reorganized its website's page structure.\n\n"
    ),
}


def _provider_errors_guidance(provider: str) -> str:
    return (
        f"**What this means:** `{provider}` has had source-fetch failures in multiple recent "
        "runs.\n\n"
        "**Common causes:** a provider-side outage, rate-limiting/blocking (e.g. a WAF), or an "
        "API/markup contract change.\n\n"
        "**Resolution:** check `run_history.jsonl` for the failure pattern. If persistent, the "
        f"`{provider}` provider adapter likely needs a fix (auth, headers, endpoint change)."
    )


def _guidance_for(check: str) -> str:
    if check.startswith("provider-errors:"):
        return _provider_errors_guidance(check.split(":", 1)[1])
    return _GUIDANCE.get(check, "")


def _title(slug: str, check: str) -> str:
    return f"{TITLE_PREFIX} {slug}: {check}"


def _label_note(to_add: set[str], to_remove: set[str]) -> str:
    if not (to_add or to_remove):
        return ""
    return f"  +{sorted(to_add)} -{sorted(to_remove)}"


def _grouped_title(check: str, *, n_feeds: int, n_cities: int) -> str:
    feeds = f"{n_feeds} feed{'s' if n_feeds != 1 else ''}"
    if n_cities and n_cities != n_feeds:
        cities = f"{n_cities} cit{'ies' if n_cities != 1 else 'y'}"
        return f"{TITLE_PREFIX} {check}: {feeds} across {cities}"
    return f"{TITLE_PREFIX} {check}: {feeds}"


def _issue_key(slug: str, check: str) -> str:
    """The stable identifier embedded in an issue body for run-to-run matching.

    Consolidated checks key on the check name alone (one issue covers every feed); per-slug
    checks key on ``slug::check`` (double colon — check names may themselves contain a single
    colon, e.g. ``provider-errors:granicus``, so this can't collide)."""
    if check in _PER_SLUG_CHECKS:
        return f"{slug}::{check}"
    return check


def _key_marker(key: str) -> str:
    return f"<!-- citypods:feed-health:key={key} -->"


def _state_comment(check: str, message: str, *, severity: str = "") -> str:
    """A comment posted when a check's substantive state changes (a feed newly affected,
    cleared, or a severity change) — not on every cosmetic body refresh (e.g. a day-count
    ticking over), which would just recreate the "chatty" problem as noisy edits instead of
    noisy issues."""
    date = datetime.now(UTC).strftime("%Y-%m-%d")
    sev = f"`{severity}` — " if severity else ""
    return (
        f"**Audit update {date}:** {sev}{message}\n\n"
        "_Added automatically when the affected-feed set changed. "
        "The issue body has been updated with the current state._"
    )


def _footer(check: str) -> str:
    if check in _PER_SLUG_CHECKS:
        return (
            "**Action required:** verify the city's current meeting archive page and update "
            "`meetings_url` in the city YAML. This issue will NOT auto-close while it has "
            "the `needs:human-verification` label — remove the label once the YAML has "
            "been updated and verified."
        )
    return (
        "_Filed automatically by the feed-health audit. It auto-closes once no feed fails this "
        "check. See `citypods doctor` to reproduce locally._"
    )


def _body(message: str, severity: str, check: str, slug: str) -> str:
    """Body for a per-slug-model issue (currently only meetings-url-*)."""
    guidance = _guidance_for(check)
    parts = [MARKER, _key_marker(_issue_key(slug, check)), "", f"**Severity:** {severity}", ""]
    if guidance:
        parts.append(guidance)
    parts.append(message)
    parts.append("")
    parts.append(_footer(check))
    return "\n".join(parts)


def _gh(*args: str, check: bool = True) -> str:
    proc = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if check and proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def _ensure_labels() -> None:
    for name, (color, desc) in LABELS.items():
        _gh(
            "label", "create", name, "--color", color, "--description", desc, "--force", check=False
        )


def _open_issues() -> dict[str, dict]:
    """Open feed-health issues keyed by their embedded ``key=`` marker (not title — the title of
    a consolidated issue changes every run as its affected-feed count changes)."""
    out = _gh(
        "issue",
        "list",
        "--label",
        "signal:feed-health",
        "--state",
        "open",
        "--json",
        "number,title,body,labels",
        "--limit",
        "1000",
    )
    issues = json.loads(out or "[]")
    result: dict[str, dict] = {}
    for issue in issues:
        if not issue["title"].startswith(TITLE_PREFIX):
            continue
        match = _KEY_RE.search(issue.get("body") or "")
        if match:
            result[match.group(1)] = issue
    return result


def _label_names(issue: dict) -> set[str]:
    names: set[str] = set()
    for label in issue.get("labels") or []:
        if isinstance(label, dict):
            name = label.get("name")
        else:
            name = str(label)
        if name:
            names.add(name)
    return names


def _is_obsolete_meetings_url_issue(issue: dict, check_name: str) -> bool:
    if check_name != "meetings-url-dead":
        return False
    match = re.search(r"meetings_url returned HTTP (\d{3}):", issue.get("body") or "")
    if not match:
        return False
    return int(match.group(1)) not in _DEAD_MEETINGS_URL_STATUSES


# ---------------------------------------------------------------------------
# Consolidated (one-issue-per-check) model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FeedRow:
    """One affected feed's detail within a consolidated check's issue, for this run."""

    slug: str
    count: int  # number of underlying findings (e.g. episodes) this run
    severity: str
    example: str


def _group_by_check(findings: list[Finding]) -> dict[str, dict[str, _FeedRow]]:
    """Group consolidated-model findings into ``{check: {slug: _FeedRow}}``."""
    by_check: dict[str, dict[str, list[Finding]]] = {}
    for f in findings:
        if f.check in _PER_SLUG_CHECKS:
            continue
        by_check.setdefault(f.check, {}).setdefault(f.slug, []).append(f)
    result: dict[str, dict[str, _FeedRow]] = {}
    for check, by_slug in by_check.items():
        rows: dict[str, _FeedRow] = {}
        for slug, fs in by_slug.items():
            severity = ERROR if any(f.severity == ERROR for f in fs) else fs[0].severity
            example = fs[0].message
            if len(example) > _MAX_EXAMPLE_CHARS:
                example = example[: _MAX_EXAMPLE_CHARS - 1] + "…"
            rows[slug] = _FeedRow(slug=slug, count=len(fs), severity=severity, example=example)
        result[check] = rows
    return result


def _parse_state(body: str) -> dict:
    match = _STATE_RE.search(body or "")
    if not match:
        return {}
    try:
        data = json.loads(match.group(1))
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _render_state_block(check: str, first_seen: dict[str, str]) -> str:
    payload = json.dumps(
        {"check": check, "first_seen": dict(sorted(first_seen.items()))}, sort_keys=True
    )
    return f"<!-- citypods:feed-health:state\n{payload}\n-->"


def _merge_first_seen(
    prior_first_seen: dict[str, str],
    prior_slugs: set[str],
    this_run_slugs: set[str],
    *,
    audited_slugs: set[str] | None,
    now: datetime,
) -> tuple[set[str], dict[str, str]]:
    """Resolve the affected-feed set and first-seen timestamps for one check this run.

    ``audited_slugs`` is the set of feed slugs this run actually re-evaluated (``None`` means
    every feed was evaluated, e.g. a full unscoped run). A slug outside that scope must be left
    exactly as the prior issue had it — a ``--city``-scoped run must never appear to "clear" or
    silently drop feeds it never looked at (mirrors the existing per-slug ``audited_slugs``
    guard, generalized to a set of rows within one issue instead of a set of issues)."""
    if audited_slugs is None:
        keep_slugs = this_run_slugs
    else:
        out_of_scope = prior_slugs - audited_slugs
        keep_slugs = (this_run_slugs & audited_slugs) | out_of_scope

    first_seen: dict[str, str] = {}
    for slug in keep_slugs:
        if audited_slugs is not None and slug not in audited_slugs:
            # Out of scope this run: carry the prior timestamp forward unchanged.
            if slug in prior_first_seen:
                first_seen[slug] = prior_first_seen[slug]
            continue
        first_seen[slug] = prior_first_seen.get(slug, now.isoformat())
    return keep_slugs, first_seen


def _since_label(iso: str, *, now: datetime) -> str:
    try:
        seen = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return "unknown"
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=UTC)
    days = max(0, (now - seen).days)
    date_label = seen.strftime("%Y-%m-%d")
    return f"{date_label} ({days}d)" if days > 0 else f"{date_label} (today)"


def _render_grouped_body(
    check: str,
    *,
    rows: dict[str, _FeedRow],
    first_seen: dict[str, str],
    city_of: Mapping[str, str],
    severity: str,
    now: datetime,
) -> str:
    n_feeds = len(rows)
    n_cities = len({city_of.get(slug, slug) for slug in rows})
    key = _issue_key("", check)
    guidance = _guidance_for(check)

    feeds_word = "feed" if n_feeds == 1 else "feeds"
    summary = f"**{n_feeds} {feeds_word}"
    if n_cities and n_cities != n_feeds:
        cities_word = "city" if n_cities == 1 else "cities"
        summary += f" across {n_cities} {cities_word}"
    summary += "** currently fail this check."

    parts = [MARKER, _key_marker(key), "", f"**Severity:** {severity}", "", summary]
    if guidance:
        parts += ["", guidance]

    ordered_slugs = sorted(rows, key=lambda s: first_seen.get(s, ""))
    parts += ["", "### Affected feeds", ""]
    parts.append("| Feed | City | Since | Count | Example |")
    parts.append("|---|---|---|---|---|")
    shown = ordered_slugs[:_MAX_ROWS]
    for slug in shown:
        row = rows[slug]
        since = _since_label(first_seen.get(slug, now.isoformat()), now=now)
        city = city_of.get(slug, slug)
        example = row.example.replace("|", "\\|").replace("\n", " ")
        parts.append(f"| `{slug}` | {city} | {since} | {row.count} | {example} |")
    if len(ordered_slugs) > _MAX_ROWS:
        parts.append(f"| _...and {len(ordered_slugs) - _MAX_ROWS} more_ | | | | |")

    parts += ["", _footer(check)]
    parts += ["", _render_state_block(check, first_seen)]
    return "\n".join(parts)


def _reconcile_grouped(
    findings: list[Finding],
    *,
    dry_run: bool,
    audited_slugs: set[str] | None,
    city_of: Mapping[str, str],
    existing: dict[str, dict],
    now: datetime,
) -> tuple[int, int, int]:
    """Reconcile the consolidated (one-issue-per-check) model.

    Returns ``(created, updated, closed)``."""
    wanted = _group_by_check(findings)
    created = updated = closed = 0

    all_checks = set(wanted) | {key for key in existing if not _is_per_slug_key(key)}
    for check in sorted(all_checks):
        key = _issue_key("", check)
        rows = wanted.get(check, {})
        issue = existing.get(key)
        prior_body = issue.get("body", "") if issue else ""
        prior_state = _parse_state(prior_body)
        prior_first_seen: dict[str, str] = prior_state.get("first_seen") or {}
        prior_slugs = set(prior_first_seen)

        keep_slugs, first_seen = _merge_first_seen(
            prior_first_seen,
            prior_slugs,
            set(rows),
            audited_slugs=audited_slugs,
            now=now,
        )
        # Rows for out-of-scope slugs that are being carried forward have no fresh Finding this
        # run (their source feed wasn't evaluated) — reuse the prior example/count/severity from
        # the issue body if we can recover it, else fall back to a placeholder that still shows
        # the feed is affected without fabricating detail we don't have this run.
        prior_rows_by_slug = _parse_prior_rows(prior_body)
        merged_rows: dict[str, _FeedRow] = {}
        for slug in keep_slugs:
            if slug in rows:
                merged_rows[slug] = rows[slug]
            else:
                merged_rows[slug] = prior_rows_by_slug.get(
                    slug,
                    _FeedRow(
                        slug=slug, count=0, severity=WARN, example="(not re-evaluated this run)"
                    ),
                )

        if not merged_rows:
            if issue is not None:
                if dry_run:
                    print(f"CLOSE   {_grouped_title(check, n_feeds=0, n_cities=0)}")
                else:
                    _gh(
                        "issue",
                        "close",
                        str(issue["number"]),
                        "--comment",
                        "✅ Resolved — no feed currently fails this check.",
                    )
                closed += 1
            continue

        n_feeds = len(merged_rows)
        n_cities = len({city_of.get(s, s) for s in merged_rows})
        title = _grouped_title(check, n_feeds=n_feeds, n_cities=n_cities)
        severity = ERROR if any(r.severity == ERROR for r in merged_rows.values()) else WARN
        body = _render_grouped_body(
            check,
            rows=merged_rows,
            first_seen=first_seen,
            city_of=city_of,
            severity=severity,
            now=now,
        )
        sev_label = f"severity:{severity}"

        if issue is None:
            if dry_run:
                print(f"CREATE  {title}  [{sev_label}]")
            else:
                _gh(
                    "issue",
                    "create",
                    "--title",
                    title,
                    "--body",
                    body,
                    "--label",
                    "signal:feed-health",
                    "--label",
                    "type:operations",
                    "--label",
                    sev_label,
                )
            created += 1
            continue

        current_labels = _label_names(issue)
        desired_labels = {"signal:feed-health", "type:operations", sev_label}
        to_remove = {
            lbl for lbl in current_labels if lbl.startswith("severity:") and lbl != sev_label
        }
        to_add = desired_labels - current_labels
        # A visible activity-log comment is posted only when the AFFECTED-FEED SET changes (a
        # feed newly failing or clearing) — not on every cosmetic body refresh (e.g. a "since"
        # day-count ticking over daily), which would just recreate the "chatty" problem as noisy
        # edits/comments instead of noisy issues.
        stable_changed = set(prior_slugs) != set(keep_slugs)
        title_changed = issue.get("title", "") != title
        body_changed = prior_body.strip() != body.strip()

        if not (body_changed or title_changed or to_add or to_remove):
            continue

        if dry_run:
            print(f"UPDATE  {title}{_label_note(to_add, to_remove)}")
        else:
            num = str(issue["number"])
            if body_changed or title_changed:
                _gh("issue", "edit", num, "--title", title, "--body", body)
                if stable_changed:
                    newly = sorted(set(keep_slugs) - set(prior_slugs))
                    cleared = sorted(set(prior_slugs) - set(keep_slugs))
                    msg_parts = []
                    if newly:
                        msg_parts.append(f"newly affected: {', '.join(newly)}")
                    if cleared:
                        msg_parts.append(f"cleared: {', '.join(cleared)}")
                    _gh(
                        "issue",
                        "comment",
                        num,
                        "--body",
                        _state_comment(check, "; ".join(msg_parts) or "state changed"),
                    )
            if to_add or to_remove:
                label_args = []
                for lbl in sorted(to_add):
                    label_args += ["--add-label", lbl]
                for lbl in sorted(to_remove):
                    label_args += ["--remove-label", lbl]
                _gh("issue", "edit", num, *label_args)
        updated += 1

    return created, updated, closed


def _is_per_slug_key(key: str) -> bool:
    return "::" in key


_ROW_RE = re.compile(r"^\| `([^`]+)` \| [^|]* \| [^|]* \| (\d+) \| (.*) \|$", re.MULTILINE)


def _parse_prior_rows(body: str) -> dict[str, _FeedRow]:
    """Best-effort recovery of a prior run's per-feed detail from the rendered table, used only
    to avoid fabricating an example/count for an out-of-scope feed being carried forward."""
    rows: dict[str, _FeedRow] = {}
    for match in _ROW_RE.finditer(body or ""):
        slug, count, example = match.group(1), match.group(2), match.group(3)
        rows[slug] = _FeedRow(slug=slug, count=int(count), severity=WARN, example=example)
    return rows


# ---------------------------------------------------------------------------
# Per-slug (one-issue-per-(slug, check)) model — meetings-url-* only
# ---------------------------------------------------------------------------


def _reconcile_per_slug(
    findings: list[Finding],
    *,
    dry_run: bool,
    audited_slugs: set[str] | None,
    existing: dict[str, dict],
) -> tuple[int, int, int]:
    relevant = [f for f in findings if f.check in _PER_SLUG_CHECKS]
    wanted = {
        _issue_key(f.slug, f.check): (f, _body(f.message, f.severity, f.check, f.slug))
        for f in relevant
    }
    scoped_existing = {
        key: issue
        for key, issue in existing.items()
        if "::" in key and (audited_slugs is None or key.split("::", 1)[0] in audited_slugs)
    }

    created = updated = closed = 0
    for key, (finding, body) in wanted.items():
        title = _title(finding.slug, finding.check)
        sev_label = f"severity:{finding.severity}"
        if key in scoped_existing:
            issue = scoped_existing[key]
            desired_labels = {
                "signal:feed-health",
                "type:operations",
                sev_label,
                "needs:human-verification",
            }
            current_labels = _label_names(issue)
            to_remove = {
                lbl for lbl in current_labels if lbl.startswith("severity:") and lbl != sev_label
            }
            to_add = desired_labels - current_labels
            body_changed = issue.get("body", "").strip() != body.strip()
            if body_changed or to_add or to_remove:
                if dry_run:
                    print(f"UPDATE  {title}{_label_note(to_add, to_remove)}")
                else:
                    num = str(issue["number"])
                    if body_changed:
                        _gh("issue", "edit", num, "--body", body)
                        comment = _state_comment(
                            finding.check, finding.message, severity=finding.severity
                        )
                        _gh("issue", "comment", num, "--body", comment)
                    if to_add or to_remove:
                        label_args = []
                        for lbl in sorted(to_add):
                            label_args += ["--add-label", lbl]
                        for lbl in sorted(to_remove):
                            label_args += ["--remove-label", lbl]
                        _gh("issue", "edit", num, *label_args)
                updated += 1
        else:
            if dry_run:
                print(f"CREATE  {title}  [{sev_label}] [needs:human-verification]")
            else:
                _gh(
                    "issue",
                    "create",
                    "--title",
                    title,
                    "--body",
                    body,
                    "--label",
                    "signal:feed-health",
                    "--label",
                    "type:operations",
                    "--label",
                    sev_label,
                    "--label",
                    "needs:human-verification",
                )
            created += 1

    for key, issue in scoped_existing.items():
        if key in wanted:
            continue
        check_name = key.split("::", 1)[1] if "::" in key else ""
        title = issue.get("title", key)
        if (
            check_name in _PER_SLUG_CHECKS
            and "needs:human-verification" in _label_names(issue)
            and not _is_obsolete_meetings_url_issue(issue, check_name)
        ):
            if dry_run:
                print(f"SKIP-CLOSE (needs:human-verification)  {title}")
            continue
        if dry_run:
            print(f"CLOSE   {title}")
        else:
            _gh(
                "issue",
                "close",
                str(issue["number"]),
                "--comment",
                "✅ Resolved — the feed-health check now passes.",
            )
        closed += 1

    return created, updated, closed


def reconcile(
    findings: list[Finding],
    *,
    dry_run: bool,
    audited_slugs: set[str] | None = None,
    city_of: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> int:
    """Reconcile the current findings against open GitHub issues.

    ``city_of`` maps feed slug -> owning city/entity slug (for the consolidated model's
    per-city count and display column); feeds absent from the mapping display under their own
    slug. ``audited_slugs`` restricts which feeds this run may create/update/close rows or
    issues for (``None`` = every feed was evaluated, e.g. an unscoped run)."""
    now = now or datetime.now(UTC)
    city_of = city_of or {}
    # _open_issues() is read-only (gh issue list), so it's safe to call in dry-run too -- forcing
    # existing={} there collapsed every UPDATE/CLOSE/SKIP-CLOSE branch into CREATE, making the
    # preview useless for telling a stamping-everything-new run from a real reconcile.
    existing = _open_issues()

    c1, u1, cl1 = _reconcile_grouped(
        findings,
        dry_run=dry_run,
        audited_slugs=audited_slugs,
        city_of=city_of,
        existing=existing,
        now=now,
    )
    c2, u2, cl2 = _reconcile_per_slug(
        findings,
        dry_run=dry_run,
        audited_slugs=audited_slugs,
        existing=existing,
    )
    created, updated, closed = c1 + c2, u1 + u2, cl1 + cl2
    print(f"\n{created} created, {updated} updated, {closed} closed.")
    return created + updated  # nonzero exit-ish signal handled by caller if desired


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="print actions; touch nothing")
    ap.add_argument("--enclosures", action="store_true", help="also HEAD-probe enclosures")
    ap.add_argument(
        "--meetings-urls",
        action="store_true",
        help="HEAD-probe each city's meetings_url (one probe per unique URL)",
    )
    ap.add_argument("--city", help="audit only this slug")
    ap.add_argument(
        "--timeline-diagnostics",
        help="write timeline/audio duration diagnostics as JSONL for review/PR6 gating",
    )
    ap.add_argument(
        "--persist-timeline-integrity",
        action="store_true",
        help="persist confirmed timeline/audio repair flags to durable state",
    )
    ap.add_argument(
        "--timeline-repair-min-delta",
        type=float,
        help=(
            "when persisting timeline/audio repair flags, select only rows whose stream "
            "duration delta is at least this many seconds"
        ),
    )
    ap.add_argument(
        "--timeline-repair-cohort",
        help="stamp selected timeline/audio repair flags with this cohort label",
    )
    ap.add_argument(
        "--timeline-finding-min-delta",
        type=float,
        default=1.0,
        help=(
            "minimum stream duration delta, in seconds, before rendered-duration-mismatch "
            "becomes a feed-health finding"
        ),
    )
    ap.add_argument("--site-config", default="config/site_config.yml")
    ap.add_argument("--config-dir", default="config")
    args = ap.parse_args(argv)
    if args.persist_timeline_integrity and not args.timeline_diagnostics:
        ap.error("--persist-timeline-integrity requires --timeline-diagnostics")
    if args.persist_timeline_integrity and args.timeline_repair_min_delta is None:
        ap.error("--persist-timeline-integrity requires --timeline-repair-min-delta")
    if args.persist_timeline_integrity and not args.timeline_repair_cohort:
        ap.error("--persist-timeline-integrity requires --timeline-repair-cohort")
    if args.timeline_repair_min_delta is not None and args.timeline_repair_min_delta < 0:
        ap.error("--timeline-repair-min-delta must be >= 0")
    if args.timeline_finding_min_delta < 0:
        ap.error("--timeline-finding-min-delta must be >= 0")

    site_config = load_site_config(args.site_config)
    cities = load_city_configs(args.config_dir, site_config.get("defaults", {}))
    if args.city:
        cities = [c for c in cities if c.slug == args.city]

    # Pull the canonical record store from the bucket before auditing it. Without this, the
    # audit only ever saw whatever actions/cache/restore's "build-state-" prefix match happened
    # to land on — which collides with audio.yml's per-shard caches and preview.yml's PR caches,
    # so it could compare an EDL and a served-duration captured at two different points in the
    # pipeline's history and file a false-positive timeline-duration-mismatch/
    # timeline-short-coverage finding.
    output_dir = "docs"
    state_dir = pull_canonical_state(site_config, output_dir)

    timeline_diagnostics: list[dict] | None = [] if args.timeline_diagnostics else None
    findings = audit_all(
        cities,
        site_config=site_config,
        output_dir=output_dir,
        check_enclosures_net=args.enclosures,
        check_meetings_urls_net=args.meetings_urls,
        timeline_diagnostics=timeline_diagnostics,
        persist_timeline_integrity=args.persist_timeline_integrity and not args.dry_run,
        timeline_repair_min_delta=args.timeline_repair_min_delta,
        timeline_repair_cohort=args.timeline_repair_cohort,
        timeline_finding_min_delta=args.timeline_finding_min_delta,
    )
    if args.timeline_diagnostics and timeline_diagnostics is not None:
        path = Path(args.timeline_diagnostics)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as f:
            for row in timeline_diagnostics:
                f.write(json.dumps(row, sort_keys=True) + "\n")
        print(f"timeline diagnostics: wrote {len(timeline_diagnostics)} row(s) to {path}")
    if args.persist_timeline_integrity and not args.dry_run:
        storage = make_storage(site_config, site_config.get("base_url", ""), output_dir)
        prefixes = sorted({f"sources/{source_key(city)}/" for city in cities})
        pushed = push_state(storage, Path(state_dir), only_prefixes=prefixes)
        print(f"timeline integrity: pushed {pushed} state file(s)")
    for f in findings:
        print(f"  {f.severity:5} {f.slug} [{f.check}] {f.message}")

    if not args.dry_run:
        _ensure_labels()
    audited_slugs = {c.slug for c in cities} if args.city else None
    city_of = {c.slug: (c.city_entity or c.slug) for c in cities}
    reconcile(findings, dry_run=args.dry_run, audited_slugs=audited_slugs, city_of=city_of)
    return 0


if __name__ == "__main__":
    sys.exit(main())
