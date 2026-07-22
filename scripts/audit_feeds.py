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
import os
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
from citypods.security import iter_source_urls
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
_STALE_COHORT_MARKER = "<!-- citypods:stale-cohort:v1 -->"
_STALE_INCIDENT_RE = re.compile(
    r"<!-- citypods:stale-incident:v1 check=(\S+) slug=(\S+) incident=(\S+) parent=(\d+) -->"
)
_STALE_STATE_RE = re.compile(r"<!-- citypods:stale-state\n(.*?)\n-->", re.DOTALL)
_GENERATED_START = "<!-- citypods:generated:start -->"
_GENERATED_END = "<!-- citypods:generated:end -->"

# Checks that stay on the legacy one-issue-per-(slug, check) model. Every other check is
# consolidated into one issue per check, covering every affected feed. See the module docstring.
_PER_SLUG_CHECKS = frozenset({"meetings-url-dead", "meetings-url-changed"})
_LIFECYCLE_INCIDENT_CHECKS = frozenset({"stale", "dormant-resumed"})
_STALE_COHORT_CAP = 50
_DEAD_MEETINGS_URL_STATUSES = frozenset({404, 410, 451})

# How many affected feeds get a full row before the table is truncated with a "+N more" note.
# Keeps a systemic, catalog-wide regression from producing an unreadably long single issue body —
# exactly the "chatty" outcome this redesign exists to avoid, just moved from many issues to one
# overlong issue instead. The hidden state block (below) still tracks every affected feed's
# first-seen date regardless of the display cap, so nothing is lost across runs.
_MAX_ROWS = 60
_MAX_EXAMPLE_CHARS = 180
_NON_ISSUE_CHECKS = frozenset({"empty"})
_AUDIT_WORKFLOW_URL = (
    "https://github.com/BashfulBits/city-meeting-podcasts/actions/workflows/audit.yml"
)


def _timeline_repair_workflow_guidance(*, default_min_delta: str = "1.0") -> str:
    return (
        f"\n\n**Repair action:** open the "
        f"[Feed health audit workflow]({_AUDIT_WORKFLOW_URL}), choose **Run workflow** on "
        "`main`, and set `timeline_repair=true`. Leave "
        f"`timeline_repair_min_delta={default_min_delta}` unless you intentionally want a "
        "different repair threshold, and set `timeline_repair_cohort` to a short label such as "
        "`issue-<number>-YYYYMMDD`. That dispatch stamps targeted repair flags; the normal "
        "build/audio stages then re-plan, re-materialize, and re-generate dependent transcripts. "
        "This issue auto-closes after a later feed-health audit sees no remaining failures."
    )


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
    "dormant-resumed": (
        "**What this means:** a feed committed as `dormant` has published recently.\n\n"
        "**Resolution:** verify whether regular meetings have resumed. If so, open a YAML PR "
        "that returns the feed lifecycle to `active`; otherwise record the evidence in this "
        "incident and leave the durable lifecycle unchanged."
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
    "media-too-large": (
        "**What this means:** the source-media size guard (issue #497) rejected this episode "
        "before ffmpeg started — the source honestly advertised (via HEAD or a ranged GET) a "
        "size over the configured `source_media_max_bytes` ceiling.\n\n"
        "**Common causes:** a genuinely oversized/misencoded source (e.g. an accidental "
        "high-bitrate or unusually long recording), a provider serving the wrong/full-length "
        "asset for this episode, or the cap simply needs raising for a legitimately long "
        "meeting.\n\n"
        "**Resolution:** verify the meeting via the link in this finding (or the recorded `uid` if "
        "no public link is on record). If it's a real, legitimate meeting, raise "
        "`source_media_max_bytes` in `config/site_config.yml` (see the comment there for how the "
        "current value was derived) and re-run. If it's a broken/mislabeled source, fix or "
        "exclude it upstream — this finding will clear automatically once the episode is no "
        "longer rejected."
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
        "supersedes this one for the same episode." + _timeline_repair_workflow_guidance()
    ),
    "timeline-short-coverage": (
        "**What this means:** an episode's last EDL segment doesn't reach "
        "`audio_duration_served`.\n\n"
        "**Common causes:** same as `timeline-duration-mismatch` — the EDL and the recorded "
        "served duration have fallen out of sync.\n\n"
        "**Resolution:** force a re-materialize for the affected episode(s)."
        + _timeline_repair_workflow_guidance()
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
        "re-auditing." + _timeline_repair_workflow_guidance()
    ),
    "timeline-audio-probe-divergence": (
        "**What this means:** the cheap header-only duration probe (range-reads just the "
        "MP4 `moov` box instead of downloading the whole hosted file) disagreed with a "
        "full-download probe of the same file beyond floating-point noise.\n\n"
        "**Common causes:** this is a code bug, not a data issue — the header-only fast path "
        "assumes the hosted `.m4a` is a single, non-fragmented, `moov`-before-`mdat` "
        "(faststart) file, so `format.duration`/stream `duration_ts`/`time_base` are fully "
        "contained in `moov` and identical to what a full download would report. A divergence "
        "means that assumption broke for this file — e.g. a new encode path stopped writing "
        "`-movflags +faststart`, a fragmented/multi-moov MP4 slipped through, or the `moov`-"
        "location box walk mis-parsed a malformed/unusual object.\n\n"
        "**Resolution:** code bug. Check `audio_spec_hash`/the encode path that produced this "
        "episode's `audio_key` for a missing `+faststart`, and inspect the object's box layout "
        "directly (e.g. `ffprobe -show_entries format` vs. a manual `moov` box scan) to see "
        "where the header-only read diverged."
    ),
    "cross-source-audio-divergence": (
        "**What this means:** the same uid lives in more than one per-source record store "
        "under one `city_entity` (e.g. a combined feed and a per-board feed), and their "
        "`audio_key`/`audio_spec_hash`/`audio_duration_served`/integrity fields disagree "
        "(GH#850), and/or their `chapters`/`chapters_basis`/`timeline` fields disagree "
        "(GH#854).\n\n"
        "**Common causes:** a combined feed's `feed_urls` doesn't exactly match its per-board "
        "siblings', so it hashes to a different `source_key` and gets its own independent "
        "record store instead of sharing one. `AudioArtifactCache.canonical_source` (GH#421) "
        "only synchronizes the two stores' audio fields at the moment both need a fresh "
        "encode/credit in the very same run; a later run touching only one of the sources "
        "leaves the other stale with nothing to reconcile it afterward. The same gap applies "
        "to chapters/timeline: each store's `TimelineStage`/`ChaptersStage` plans once and "
        "never recomputes, so two stores can independently derive different chapters/timeline "
        "for the same physical meeting — which is what actually produces the audio_spec_hash "
        "divergence above in the first place.\n\n"
        "**Resolution:** this self-heals automatically on the next `timeline_repair=true` "
        "feed-health dispatch (or any run with repair persistence enabled) — the canonical "
        "copy is chosen from the freshest live-probe `ok` result, falling back to the newest "
        "`audio_encode_time`. If the finding names a specific `canonical` and recurs, verify "
        "the feed configs listed actually intend to share one source; if they should, align "
        "their `feed_urls` (see `config/feeds/fort-worth-tx.yml` for the GH#850 fix)."
        + _timeline_repair_workflow_guidance()
    ),
    "timeline-source-duration-mismatch": (
        "**What this means:** for a multi-source (concat) episode, a source segment's actual "
        "duration disagrees with its registered `SourceMedia.duration`.\n\n"
        "**Common causes:** the concat planner measured a source's duration from a stale probe, "
        "or (for legacy multi-part meetings) a part was re-uploaded/replaced upstream after "
        "being measured.\n\n"
        "**Resolution:** re-plan the episode's timeline to force a fresh source-duration probe, "
        "then re-materialize." + _timeline_repair_workflow_guidance()
    ),
    "timeline-source-underrun": (
        "**What this means:** an EDL segment's source-time span starts before 0.\n\n"
        "**Common causes:** a stale/incorrect `SourceMedia.duration`, or a planner bug computing "
        "segment bounds.\n\n"
        "**Resolution:** re-plan the episode's timeline to refresh the source-duration "
        "measurement." + _timeline_repair_workflow_guidance()
    ),
    "timeline-source-overrun": (
        "**What this means:** an EDL segment's source-time span extends past "
        "`SourceMedia.duration`.\n\n"
        "**Common causes:** a stale/incorrect `SourceMedia.duration` recorded before the actual "
        "source changed, or a planner bug computing segment bounds.\n\n"
        "**Resolution:** re-plan the episode's timeline to refresh the source-duration "
        "measurement." + _timeline_repair_workflow_guidance()
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


@dataclass
class _StaleCatalog:
    """All lifecycle cohort/incident issues, including closed history for rollover links."""

    open_parents: list[dict]
    open_children: dict[tuple[str, str], dict]
    history: dict[tuple[str, str], list[dict]]
    children_by_parent: dict[int, list[dict]]


def _open_stale_catalog() -> _StaleCatalog:
    """Load lifecycle issues separately from the legacy feed-health key namespace."""
    out = _gh(
        "issue",
        "list",
        "--label",
        "signal:feed-health",
        "--state",
        "all",
        "--json",
        "number,title,body,labels,state,url",
        "--limit",
        "1000",
    )
    issues = json.loads(out or "[]")
    open_parents: list[dict] = []
    open_children: dict[tuple[str, str], dict] = {}
    history: dict[tuple[str, str], list[dict]] = {}
    children_by_parent: dict[int, list[dict]] = {}
    for issue in issues:
        body = issue.get("body") or ""
        is_open = str(issue.get("state") or "").lower() == "open"
        if _STALE_COHORT_MARKER in body:
            if is_open:
                open_parents.append(issue)
            continue
        match = _STALE_INCIDENT_RE.search(body)
        if not match:
            continue
        check, slug, incident_id, parent_raw = match.groups()
        item = {
            **issue,
            "check": check,
            "slug": slug,
            "incident_id": incident_id,
            "parent": int(parent_raw),
        }
        key = (check, slug)
        history.setdefault(key, []).append(item)
        children_by_parent.setdefault(int(parent_raw), []).append(item)
        if is_open:
            open_children[key] = item
    open_parents.sort(key=lambda issue: int(issue["number"]))
    for issues_for_key in history.values():
        issues_for_key.sort(key=lambda issue: int(issue["number"]))
    return _StaleCatalog(open_parents, open_children, history, children_by_parent)


def _parse_stale_state(body: str) -> dict:
    match = _STALE_STATE_RE.search(body or "")
    if not match:
        return {}
    try:
        state = json.loads(match.group(1))
    except (TypeError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _replace_generated(body: str, generated: str) -> str:
    """Replace only the automation-owned section, preserving maintainer notes verbatim."""
    start = body.find(_GENERATED_START)
    end = body.find(_GENERATED_END)
    if start < 0 or end < start:
        # Fail toward duplication, never data loss: a maintainer may have accidentally edited or
        # removed a marker, but their unparseable prior text must survive the next reconciliation.
        return generated + "\n\n### Maintainer notes\n\n" + body
    end += len(_GENERATED_END)
    return body[:start] + generated + body[end:]


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


@dataclass(frozen=True)
class _FeedContext:
    city: str
    feed_config_url: str | None = None
    city_config_url: str | None = None
    meetings_url: str | None = None
    source_url: str | None = None
    lifecycle_status: str = "active"
    checks_staleness: bool = True


def _group_by_check(findings: list[Finding]) -> dict[str, dict[str, _FeedRow]]:
    """Group consolidated-model findings into ``{check: {slug: _FeedRow}}``."""
    by_check: dict[str, dict[str, list[Finding]]] = {}
    for f in findings:
        if f.check in _PER_SLUG_CHECKS or f.check in _LIFECYCLE_INCIDENT_CHECKS:
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
    if not isinstance(data, dict):
        return {}
    first_seen = data.get("first_seen")
    if first_seen is not None and not isinstance(first_seen, dict):
        return {}
    if isinstance(first_seen, dict):
        cleaned = {
            slug: seen
            for slug, seen in first_seen.items()
            if isinstance(slug, str) and isinstance(seen, str)
        }
        if len(cleaned) != len(first_seen):
            data = {**data, "first_seen": cleaned}
    # A malformed `rows` value is handled by _parse_state_rows() itself (returns {} for it
    # alone) -- rejecting the whole state here would also discard an otherwise-valid first_seen.
    return data


def _parse_state_rows(state: dict) -> dict[str, _FeedRow]:
    """Recover full per-slug row detail (severity/count/example) from the hidden state block,
    independent of the visible table's ``_MAX_ROWS`` cap -- so a feed beyond the display cap in
    a prior run doesn't fall back to fabricated detail when carried forward in a scoped run."""
    raw = state.get("rows")
    if not isinstance(raw, dict):
        return {}
    rows: dict[str, _FeedRow] = {}
    for slug, detail in raw.items():
        if not isinstance(slug, str) or not isinstance(detail, dict):
            continue
        severity = detail.get("severity")
        count = detail.get("count")
        example = detail.get("example")
        if (
            not isinstance(severity, str)
            or not isinstance(count, int)
            or not isinstance(example, str)
        ):
            continue
        rows[slug] = _FeedRow(slug=slug, count=count, severity=severity, example=example)
    return rows


def _render_state_block(check: str, first_seen: dict[str, str], rows: dict[str, _FeedRow]) -> str:
    payload = json.dumps(
        {
            "check": check,
            "first_seen": dict(sorted(first_seen.items())),
            "rows": {
                slug: {"severity": r.severity, "count": r.count, "example": r.example}
                for slug, r in sorted(rows.items())
            },
        },
        sort_keys=True,
    )
    # A feed-derived example (an upstream error/response message) could contain a literal "-->",
    # which would prematurely terminate this HTML comment and corrupt the state block. "-->" can
    # only appear inside a JSON string value here (it isn't valid bare JSON syntax), so replacing
    # it with the equivalent > escape for ">" changes no parsed value on the read side.
    payload = payload.replace("-->", "--\\u003e")
    return f"<!-- citypods:feed-health:state\n{payload}\n-->"


def _blob_url(github_repo: str | None, path: str) -> str | None:
    if not github_repo:
        return None
    return f"https://github.com/{github_repo}/blob/main/{path}"


def _first_source_url(source: dict) -> str | None:
    for key in ("list_url", "feed_url"):
        value = source.get(key)
        if isinstance(value, str) and value:
            return value
    return next(iter(iter_source_urls(source)), None)


def _feed_context(city, *, github_repo: str | None, now: datetime | None = None) -> _FeedContext:
    entity = getattr(city, "city_entity", None)
    city_slug = entity or city.slug
    source = getattr(city, "source", {}) or {}
    meetings_url = getattr(city, "meetings_url", None)
    city_website = getattr(city, "city_website", None)
    lifecycle = getattr(city, "lifecycle", None)
    now = now or datetime.now(UTC)
    return _FeedContext(
        city=city_slug,
        feed_config_url=_blob_url(github_repo, f"config/feeds/{city.slug}.yml"),
        city_config_url=_blob_url(github_repo, f"config/cities/{entity}.yml") if entity else None,
        meetings_url=meetings_url or city_website,
        source_url=_first_source_url(source),
        lifecycle_status=getattr(lifecycle, "status", "active"),
        checks_staleness=(
            lifecycle.checks_staleness(now.date()) if lifecycle is not None else True
        ),
    )


def _incident_title(check: str, slug: str, incident_id: str) -> str:
    label = "stale feed" if check == "stale" else "dormant feed resumed"
    return f"{TITLE_PREFIX} {label}: {slug} ({incident_id})"


def _cohort_title(now: datetime, *, total: int, open_count: int) -> str:
    return f"{TITLE_PREFIX} stale review cohort {now:%Y-%m-%d}: {open_count} open / {total} total"


def _render_cohort_body(*, total: int, open_count: int) -> str:
    generated = "\n".join(
        [
            _GENERATED_START,
            "## Review progress",
            "",
            f"- **Open incidents:** {open_count}",
            f"- **Total incidents:** {total} / {_STALE_COHORT_CAP}",
            "",
            "Each native sub-issue represents one feed incident. Resolve it through observed "
            "recovery, a linked source repair/migration PR, or a merged lifecycle YAML PR.",
            _GENERATED_END,
        ]
    )
    return f"{_STALE_COHORT_MARKER}\n\n{generated}\n\n### Maintainer notes\n\n"


def _render_incident_body(
    *,
    check: str,
    slug: str,
    incident_id: str,
    parent: int,
    row: _FeedRow,
    context: _FeedContext | None,
    prior_state: dict,
    prior_incidents: list[dict],
    now: datetime,
) -> str:
    first_seen = prior_state.get("first_seen") or now.isoformat()
    evidence_changed = any(
        prior_state.get(key) != value
        for key, value in {
            "severity": row.severity,
            "count": row.count,
            "example": row.example,
        }.items()
    )
    last_observed = (
        now.isoformat()
        if evidence_changed or not prior_state.get("last_observed")
        else prior_state["last_observed"]
    )
    state = json.dumps(
        {
            "check": check,
            "slug": slug,
            "incident_id": incident_id,
            "first_seen": first_seen,
            "last_observed": last_observed,
            "severity": row.severity,
            "count": row.count,
            "example": row.example,
        },
        sort_keys=True,
    ).replace("-->", "--\\u003e")
    marker = (
        f"<!-- citypods:stale-incident:v1 check={check} slug={slug} "
        f"incident={incident_id} parent={parent} -->"
    )
    generated = [
        _GENERATED_START,
        "## Current audit evidence",
        "",
        f"- **Feed:** `{slug}`",
        f"- **Check:** `{check}`",
        f"- **First observed:** {first_seen[:10]}",
        f"- **Last material observation:** {last_observed[:10]}",
        f"- **Severity:** `{row.severity}`",
        f"- **Finding count:** {row.count}",
        f"- **Evidence:** {row.example}",
        "",
        _guidance_for(check),
        "",
        "## Investigation links",
        "",
    ]
    links: list[str] = []
    if context and context.feed_config_url:
        links.append(f"[applicable feed YAML]({context.feed_config_url})")
    else:
        links.append(f"`config/feeds/{slug}.yml`")
    if context and context.city_config_url:
        links.append(f"[city YAML]({context.city_config_url})")
    if context and context.meetings_url:
        links.append(f"[city meetings page]({context.meetings_url})")
    if context and context.source_url:
        links.append(f"[provider source]({context.source_url})")
    generated.append("- " + ", ".join(links))
    prior_urls = [issue.get("url") for issue in prior_incidents if issue.get("url")]
    if prior_urls:
        generated += ["", "## Prior incidents", ""]
        generated.extend(f"- {url}" for url in prior_urls)
    generated += [
        "",
        "Repair or migrate the source with a manual PR from the feed-YAML link above. "
        "Maintainers may also use `/stale pause`, `/stale dormant`, or `/stale retire`; "
        "those commands create reviewable YAML PRs and do not directly change lifecycle state.",
        "",
        f"<!-- citypods:stale-state\n{state}\n-->",
        _GENERATED_END,
    ]
    return f"{marker}\n\n" + "\n".join(generated) + "\n\n### Maintainer notes\n\n"


def _created_issue_number(output: str) -> int:
    match = re.search(r"/issues/(\d+)(?:\s*)$", output.strip())
    if not match:
        raise RuntimeError(f"could not parse created issue URL: {output!r}")
    return int(match.group(1))


def _attach_sub_issue(*, github_repo: str, parent: int, child: int) -> None:
    database_id = _gh("api", f"repos/{github_repo}/issues/{child}", "--jq", ".id").strip()
    if not database_id.isdigit():
        raise RuntimeError(f"could not resolve database id for issue #{child}")
    _gh(
        "api",
        "--method",
        "POST",
        f"repos/{github_repo}/issues/{parent}/sub_issues",
        "-F",
        f"sub_issue_id={database_id}",
    )


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
    feed_context: Mapping[str, _FeedContext] | None,
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

    # Tie-break by slug: `keep_slugs`/`rows` are built from sets, so without a deterministic
    # secondary key, feeds sharing an identical first-seen timestamp (e.g. several newly stamped
    # in the same run) could shuffle order between runs purely from set-iteration order, causing
    # a spurious body diff (and edit call) with no real state change.
    ordered_slugs = sorted(rows, key=lambda s: (first_seen.get(s, ""), s))
    parts += ["", "### Affected feeds", ""]
    parts.append("| Feed | City | Since | Severity | Count | Example |")
    parts.append("|---|---|---|---|---|---|")
    shown = ordered_slugs[:_MAX_ROWS]
    for slug in shown:
        row = rows[slug]
        since = _since_label(first_seen.get(slug, now.isoformat()), now=now)
        city = city_of.get(slug, slug)
        example = row.example.replace("|", "\\|").replace("\n", " ")
        parts.append(f"| `{slug}` | {city} | {since} | {row.severity} | {row.count} | {example} |")
    if len(ordered_slugs) > _MAX_ROWS:
        parts.append(f"| _...and {len(ordered_slugs) - _MAX_ROWS} more_ | | | | | |")

    if check == "stale":
        parts += ["", "### Audit links", ""]
        for slug in shown:
            ctx = (feed_context or {}).get(slug)
            if ctx is None:
                continue
            links = []
            if ctx.feed_config_url:
                links.append(f"[feed config]({ctx.feed_config_url})")
            if ctx.city_config_url:
                links.append(f"[city config]({ctx.city_config_url})")
            if ctx.meetings_url:
                links.append(f"[city meetings page]({ctx.meetings_url})")
            if ctx.source_url:
                links.append(f"[provider source]({ctx.source_url})")
            if links:
                parts.append(f"- `{slug}` ({ctx.city}): " + ", ".join(links))

    parts += ["", _footer(check)]
    parts += ["", _render_state_block(check, first_seen, rows)]
    return "\n".join(parts)


def _reconcile_grouped(
    findings: list[Finding],
    *,
    dry_run: bool,
    audited_slugs: set[str] | None,
    city_of: Mapping[str, str],
    feed_context: Mapping[str, _FeedContext] | None,
    existing: dict[str, dict],
    now: datetime,
) -> tuple[int, int, int]:
    """Reconcile the consolidated (one-issue-per-check) model.

    Returns ``(created, updated, closed)``."""
    wanted = {
        check: rows
        for check, rows in _group_by_check(findings).items()
        if check not in _NON_ISSUE_CHECKS
    }
    created = updated = closed = 0

    all_checks = set(wanted) | {
        key
        for key in existing
        if not _is_per_slug_key(key) and key not in _LIFECYCLE_INCIDENT_CHECKS
    }
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
        # the hidden state block (full detail, unaffected by the visible table's _MAX_ROWS cap),
        # falling back to the visible table for older bodies that predate the state-row map, else
        # a placeholder that still shows the feed is affected without fabricating detail we don't
        # have this run.
        prior_rows_by_slug = _parse_state_rows(prior_state) or _parse_prior_rows(prior_body)
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
            feed_context=feed_context,
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


def _group_lifecycle_incidents(findings: list[Finding]) -> dict[tuple[str, str], _FeedRow]:
    grouped: dict[tuple[str, str], list[Finding]] = {}
    for finding in findings:
        if finding.check in _LIFECYCLE_INCIDENT_CHECKS:
            grouped.setdefault((finding.check, finding.slug), []).append(finding)
    rows: dict[tuple[str, str], _FeedRow] = {}
    for key, group in grouped.items():
        example = group[0].message
        if len(example) > _MAX_EXAMPLE_CHARS:
            example = example[: _MAX_EXAMPLE_CHARS - 1] + "…"
        rows[key] = _FeedRow(
            slug=key[1],
            count=len(group),
            severity=ERROR if any(item.severity == ERROR for item in group) else group[0].severity,
            example=example,
        )
    return rows


def _cohort_date(issue: dict, fallback: datetime) -> datetime:
    match = re.search(r"stale review cohort (\d{4}-\d{2}-\d{2})", issue.get("title") or "")
    if not match:
        return fallback
    try:
        return datetime.fromisoformat(match.group(1)).replace(tzinfo=UTC)
    except ValueError:
        return fallback


def _reconcile_stale_incidents(
    findings: list[Finding],
    *,
    dry_run: bool,
    audited_slugs: set[str] | None,
    feed_context: Mapping[str, _FeedContext] | None,
    catalog: _StaleCatalog,
    now: datetime,
    github_repo: str | None,
    suppressed_checks: set[str] | None = None,
) -> tuple[int, int, int]:
    """Reconcile lifecycle findings as capped cohort parents with native child issues."""
    wanted = _group_lifecycle_incidents(findings)
    suppressed_checks = suppressed_checks or set()
    wanted = {key: row for key, row in wanted.items() if key[0] not in suppressed_checks}
    findings_by_slug: dict[str, list[Finding]] = {}
    for finding in findings:
        findings_by_slug.setdefault(finding.slug, []).append(finding)
    created = updated = closed = 0

    # Recovery or a committed lifecycle disposition removes the finding. Unlike meetings-url
    # issues, lifecycle children auto-close even though they begin with human-verification.
    for key, issue in list(catalog.open_children.items()):
        if key[0] in suppressed_checks:
            continue
        if audited_slugs is not None and key[1] not in audited_slugs:
            continue
        if key in wanted:
            continue
        context = (feed_context or {}).get(key[1])
        slug_findings = findings_by_slug.get(key[1], [])
        audit_inconclusive = any(
            finding.check == "unreachable"
            or (finding.check == "empty" and finding.severity == ERROR)
            for finding in slug_findings
        )
        if key[0] == "stale":
            disposition_suppresses = context is not None and not context.checks_staleness
        else:
            disposition_suppresses = context is not None and context.lifecycle_status != "dormant"
        if audit_inconclusive and not disposition_suppresses:
            continue
        if dry_run:
            print(f"CLOSE   {issue.get('title', key)}")
        else:
            _gh(
                "issue",
                "close",
                str(issue["number"]),
                "--comment",
                "✅ Resolved — the feed recovered or its committed lifecycle now suppresses "
                "this incident.",
            )
        issue["state"] = "CLOSED"
        del catalog.open_children[key]
        closed += 1

    # Refresh material evidence on existing incidents without touching maintainer notes.
    for key, row in wanted.items():
        issue = catalog.open_children.get(key)
        if issue is None:
            continue
        prior_body = issue.get("body") or ""
        prior_state = _parse_stale_state(prior_body)
        prior_incidents = [
            prior
            for prior in catalog.history.get(key, [])
            if int(prior["number"]) != int(issue["number"])
        ]
        fresh = _render_incident_body(
            check=key[0],
            slug=key[1],
            incident_id=issue["incident_id"],
            parent=int(issue["parent"]),
            row=row,
            context=(feed_context or {}).get(key[1]),
            prior_state=prior_state,
            prior_incidents=prior_incidents,
            now=now,
        )
        start = fresh.index(_GENERATED_START)
        end = fresh.index(_GENERATED_END) + len(_GENERATED_END)
        body = _replace_generated(prior_body, fresh[start:end])
        desired_labels = {
            "signal:feed-health",
            "type:operations",
            f"severity:{row.severity}",
            "needs:human-verification",
        }
        current_labels = _label_names(issue)
        to_add = desired_labels - current_labels
        to_remove = {
            label
            for label in current_labels
            if label.startswith("severity:") and label not in desired_labels
        }
        if body.strip() == prior_body.strip() and not (to_add or to_remove):
            continue
        if dry_run:
            print(f"UPDATE  {issue.get('title', key)}{_label_note(to_add, to_remove)}")
        else:
            args = ["issue", "edit", str(issue["number"])]
            if body.strip() != prior_body.strip():
                args += ["--body", body]
            for label in sorted(to_add):
                args += ["--add-label", label]
            for label in sorted(to_remove):
                args += ["--remove-label", label]
            _gh(*args)
        issue["body"] = body
        updated += 1

    def _parent_with_capacity() -> dict | None:
        for parent in reversed(catalog.open_parents):
            if len(catalog.children_by_parent.get(int(parent["number"]), [])) < _STALE_COHORT_CAP:
                return parent
        return None

    # New finding after recovery becomes a new incident; closed children remain immutable history.
    for key, row in sorted(wanted.items()):
        if key in catalog.open_children:
            continue
        parent = _parent_with_capacity()
        if parent is None:
            if dry_run:
                parent_num = -(len(catalog.open_parents) + 1)
                print(f"CREATE  {_cohort_title(now, total=0, open_count=0)}")
            else:
                if not github_repo:
                    raise RuntimeError("github_repo is required to attach native stale sub-issues")
                output = _gh(
                    "issue",
                    "create",
                    "--title",
                    _cohort_title(now, total=0, open_count=0),
                    "--body",
                    _render_cohort_body(total=0, open_count=0),
                    "--label",
                    "signal:feed-health",
                    "--label",
                    "type:operations",
                    "--label",
                    "severity:warn",
                )
                parent_num = _created_issue_number(output)
            parent = {
                "number": parent_num,
                "title": _cohort_title(now, total=0, open_count=0),
                "body": _render_cohort_body(total=0, open_count=0),
                "state": "OPEN",
            }
            catalog.open_parents.append(parent)
            catalog.children_by_parent[parent_num] = []
            created += 1

        prior_incidents = catalog.history.get(key, [])
        incident_id = f"{now:%Y%m%d}-{len(prior_incidents) + 1}"
        body = _render_incident_body(
            check=key[0],
            slug=key[1],
            incident_id=incident_id,
            parent=int(parent["number"]),
            row=row,
            context=(feed_context or {}).get(key[1]),
            prior_state={},
            prior_incidents=prior_incidents,
            now=now,
        )
        title = _incident_title(key[0], key[1], incident_id)
        if dry_run:
            child_num = -(1000 + created)
            print(f"CREATE  {title}  [native child of #{parent['number']}]")
        elif not github_repo:
            # Validate before creating anything: otherwise an existing parent with capacity can
            # leave behind a child that failed before its native relationship was attached.
            raise RuntimeError("github_repo is required to attach native stale sub-issues")
        else:
            output = _gh(
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
                f"severity:{row.severity}",
                "--label",
                "needs:human-verification",
            )
            child_num = _created_issue_number(output)
            _attach_sub_issue(
                github_repo=github_repo, parent=int(parent["number"]), child=child_num
            )
        child = {
            "number": child_num,
            "title": title,
            "body": body,
            "state": "OPEN",
            "url": f"https://github.com/{github_repo}/issues/{child_num}" if github_repo else "",
            "check": key[0],
            "slug": key[1],
            "incident_id": incident_id,
            "parent": int(parent["number"]),
            "labels": [
                {"name": "signal:feed-health"},
                {"name": "type:operations"},
                {"name": f"severity:{row.severity}"},
                {"name": "needs:human-verification"},
            ],
        }
        catalog.open_children[key] = child
        catalog.history.setdefault(key, []).append(child)
        catalog.children_by_parent[int(parent["number"])].append(child)
        created += 1

    # Parent progress derives from marker-owned children, and the parent closes only after every
    # child is closed. Closed parents remain immutable; later incidents use a fresh cohort.
    for parent in list(catalog.open_parents):
        parent_num = int(parent["number"])
        children = catalog.children_by_parent.get(parent_num, [])
        total = len(children)
        open_count = sum(str(child.get("state") or "").lower() == "open" for child in children)
        if total and open_count == 0:
            if dry_run:
                print(f"CLOSE   {parent.get('title', parent_num)}")
            else:
                _gh(
                    "issue",
                    "close",
                    str(parent_num),
                    "--comment",
                    "✅ Cohort complete — every native stale-feed sub-issue is resolved.",
                )
            parent["state"] = "CLOSED"
            catalog.open_parents.remove(parent)
            closed += 1
            continue
        title = _cohort_title(_cohort_date(parent, now), total=total, open_count=open_count)
        fresh_body = _render_cohort_body(total=total, open_count=open_count)
        start = fresh_body.index(_GENERATED_START)
        end = fresh_body.index(_GENERATED_END) + len(_GENERATED_END)
        body = _replace_generated(parent.get("body") or "", fresh_body[start:end])
        if title == parent.get("title") and body.strip() == (parent.get("body") or "").strip():
            continue
        if dry_run:
            print(f"UPDATE  {title}")
        else:
            _gh("issue", "edit", str(parent_num), "--title", title, "--body", body)
        parent["title"] = title
        parent["body"] = body
        updated += 1

    return created, updated, closed


def _is_per_slug_key(key: str) -> bool:
    return "::" in key


_ROW_RE = re.compile(r"^\| `([^`]+)` \| [^|]* \| [^|]* \| (\w+) \| (\d+) \| (.*) \|$", re.MULTILINE)


def _parse_prior_rows(body: str) -> dict[str, _FeedRow]:
    """Best-effort recovery of a prior run's per-feed detail from the rendered table, used only
    to avoid fabricating a severity/example/count for an out-of-scope feed being carried forward
    -- notably severity, so a carried-forward ERROR-severity feed can't silently downgrade an
    issue to `severity:warn` just because it wasn't re-evaluated this run."""
    rows: dict[str, _FeedRow] = {}
    for match in _ROW_RE.finditer(body or ""):
        slug, severity, count, example = match.groups()
        rows[slug] = _FeedRow(slug=slug, count=int(count), severity=severity, example=example)
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
    feed_context: Mapping[str, _FeedContext] | None = None,
    github_repo: str | None = None,
    now: datetime | None = None,
) -> int:
    """Reconcile the current findings against open GitHub issues.

    ``city_of`` maps feed slug -> owning city/entity slug (for the consolidated model's
    per-city count and display column); feeds absent from the mapping display under their own
    slug. ``audited_slugs`` restricts which feeds this run may create/update/close rows or
    issues for (``None`` = every feed was evaluated, e.g. an unscoped run)."""
    now = now or datetime.now(UTC)
    city_of = city_of or {}
    github_repo = github_repo or os.environ.get("GITHUB_REPOSITORY")
    # _open_issues() is read-only (gh issue list), so it's safe to call in dry-run too -- forcing
    # existing={} there collapsed every UPDATE/CLOSE/SKIP-CLOSE branch into CREATE, making the
    # preview useless for telling a stamping-everything-new run from a real reconcile.
    existing = _open_issues()
    stale_catalog = _open_stale_catalog()

    c0, u0, cl0 = _reconcile_stale_incidents(
        findings,
        dry_run=dry_run,
        audited_slugs=audited_slugs,
        feed_context=feed_context,
        catalog=stale_catalog,
        now=now,
        github_repo=github_repo,
        # GH#975 performs the one-time conversion of legacy GH#774 with its historical
        # first-seen values. Until that open marker-owned issue is converted/closed, do not
        # create a duplicate native stale cohort. Dormant-resumed incidents can proceed now.
        suppressed_checks={"stale"} if _issue_key("", "stale") in existing else set(),
    )
    c1, u1, cl1 = _reconcile_grouped(
        findings,
        dry_run=dry_run,
        audited_slugs=audited_slugs,
        city_of=city_of,
        feed_context=feed_context,
        existing=existing,
        now=now,
    )
    c2, u2, cl2 = _reconcile_per_slug(
        findings,
        dry_run=dry_run,
        audited_slugs=audited_slugs,
        existing=existing,
    )
    created, updated, closed = c0 + c1 + c2, u0 + u1 + u2, cl0 + cl1 + cl2
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
    now = datetime.now(UTC)
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
        now=now,
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
    github_repo = site_config.get("github_repo")
    feed_context = {c.slug: _feed_context(c, github_repo=github_repo, now=now) for c in cities}
    reconcile(
        findings,
        dry_run=args.dry_run,
        audited_slugs=audited_slugs,
        city_of=city_of,
        feed_context=feed_context,
        github_repo=github_repo,
        now=now,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
