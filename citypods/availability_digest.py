"""Weekly review digest for empty-recording candidates (H16 PR3b, GH#353).

PR3a ([[availability]]) durably classifies a meeting's source media as suspected/confirmed empty and
withholds it from the feeds. This module turns those withheld empties into a **bounded, low-bitrate
review bundle** so a human can confirm "is there really nothing here?" without trawling logs:

* it scans the persisted records for episodes whose verdict is ``suspected_empty`` /
  ``confirmed_empty``;
* it deterministically samples a small set of *new or changed* candidates (keyed by uid + source
  fingerprint + detector version, so a re-classification re-surfaces) — bounded work, stable output;
* for each it emits an evidence record (durations, sizes, hashes, silence intervals,
  profile/detector version, canonical source-page URL, and a **redacted** source identity) plus two
  short proxies: the untrimmed source audio and the silence-trimmed candidate;
* a weekly workflow zips the bundle, uploads it as a workflow artifact, and opens/updates **one**
  digest issue — only when new/changed candidates exist.

The pure functions here (selection, evidence assembly, issue rendering, the digest-state ledger) are
I/O-free and unit-tested; ``scripts/availability_digest.py`` owns the ffmpeg/network/`gh` side.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from citypods.availability import CONFIRMED_EMPTY, SUSPECTED_EMPTY
from citypods.security import redact_subprocess_text

EVIDENCE_SCHEMA_VERSION = 1
DIGEST_STATE_NAME = "availability_digest.json"
DIGEST_ISSUE_MARKER = "<!-- h16-availability-digest -->"

# The withheld states with audio worth a human listen. ``missing``/``invalid`` have no usable media
# to proxy, so they are surfaced by the H16 report census, not by this audio review digest.
REVIEW_STATES: frozenset[str] = frozenset({SUSPECTED_EMPTY, CONFIRMED_EMPTY})


@dataclass(frozen=True)
class Candidate:
    """One empty-recording candidate worth a review proxy, derived from a persisted record."""

    source_key: str
    uid: str
    title: str
    state: str
    reason: str
    detector_version: str
    source_fingerprint: str | None
    profile: str
    last_check: str | None
    video_url: str
    canonical_url: str | None
    duration: int | None

    def key(self) -> str:
        """Stable de-dupe key. Includes the fingerprint, detector version, and state, so a
        re-classification (new media bytes, bumped detector, or suspected→confirmed) re-surfaces a
        candidate the digest has already shown."""
        return f"{self.uid}:{self.source_fingerprint}:{self.detector_version}:{self.state}"


def safe_stem(uid: str) -> str:
    """A filesystem-safe filename stem for a uid.

    A uid is normally a hex identity hash, but it is data, so a hostile/garbled value containing
    ``/`` or ``..`` must never be written into a path verbatim. Keep an alphanumeric/`-`/`_` stem
    and fall back to a content hash for anything else, so a proxy/evidence file always stays inside
    the work dir.
    """
    cleaned = "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in uid)
    if cleaned and cleaned == uid:
        return cleaned
    digest = hashlib.sha256(uid.encode("utf-8")).hexdigest()[:16]
    return f"{cleaned[:32]}-{digest}" if cleaned else digest


def iter_candidates(state_dir: Path) -> list[Candidate]:
    """Collect every empty-recording candidate from the persisted records under ``state_dir``."""
    out: list[Candidate] = []
    for path in sorted(Path(state_dir).glob("sources/*/episodes.json")):
        source_key = path.parent.name
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        for rec in (data.get("episodes") or {}).values():
            av = rec.get("media_availability") or {}
            effective = av.get("operator_override") or av.get("state")
            if effective not in REVIEW_STATES:
                continue
            uid = str(rec.get("uid") or "")
            if not uid:
                # No stable identity → its digest key and evidence filename would collide with any
                # other uid-less record. Skip it; a real meeting always has a uid (records.py).
                continue
            out.append(
                Candidate(
                    source_key=source_key,
                    uid=uid,
                    title=str(rec.get("title") or ""),
                    state=effective,
                    reason=str(av.get("reason") or ""),
                    detector_version=str(av.get("detector_version") or ""),
                    source_fingerprint=av.get("source_fingerprint"),
                    profile=str(av.get("profile") or ""),
                    last_check=av.get("last_check"),
                    video_url=str(rec.get("video_url") or ""),
                    canonical_url=(rec.get("links") or {}).get("canonical_video"),
                    duration=rec.get("duration"),
                )
            )
    return out


def select_for_digest(
    candidates: list[Candidate], digested_keys: set[str], *, limit: int
) -> list[Candidate]:
    """Deterministically choose up to ``limit`` *new or changed* candidates.

    A candidate already shown (its ``key()`` is in ``digested_keys``) is skipped; the remainder are
    ordered by key and truncated, so the same inputs always yield the same bounded sample regardless
    of record iteration order.
    """
    fresh = [c for c in candidates if c.key() not in digested_keys]
    fresh.sort(key=lambda c: c.key())
    return fresh[: max(0, limit)]


def redacted_identity(url: str) -> str:
    """The source URL with its signed query and any credential-shaped value stripped (reuses the
    PR2 subprocess redactor) — safe to store in evidence and link from a public issue."""
    return str(redact_subprocess_text(url) or "")


@dataclass(frozen=True)
class ProxyResult:
    """A rendered low-bitrate review proxy (or the metadata of one)."""

    path: str
    size_bytes: int
    sha256: str
    duration_seconds: float | None


def build_evidence(
    candidate: Candidate,
    *,
    silences: list[tuple[float, float]],
    source_duration: float | None,
    untrimmed: ProxyResult | None,
    trimmed: ProxyResult | None,
    note: str = "",
) -> dict:
    """Assemble the evidence record for one candidate. Pure: callers supply probe/encode results.

    Never stores the raw signed source URL — only the redacted identity and the canonical,
    listener-facing watch page.
    """

    def _proxy(p: ProxyResult | None) -> dict | None:
        if p is None:
            return None
        return {
            "file": p.path,
            "size_bytes": p.size_bytes,
            "sha256": p.sha256,
            "duration_seconds": p.duration_seconds,
        }

    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "uid": candidate.uid,
        "title": candidate.title,
        "source_key": candidate.source_key,
        "state": candidate.state,
        "reason": candidate.reason,
        "detector_version": candidate.detector_version,
        "source_fingerprint": candidate.source_fingerprint,
        "profile": candidate.profile,
        "last_check": candidate.last_check,
        "declared_duration_seconds": candidate.duration,
        "probed_duration_seconds": source_duration,
        "silence_intervals": [[round(s, 3), round(e, 3)] for s, e in silences],
        "canonical_source_url": candidate.canonical_url,
        "redacted_source_identity": redacted_identity(candidate.video_url),
        "untrimmed_proxy": _proxy(untrimmed),
        "trimmed_proxy": _proxy(trimmed),
        "note": note,
    }


def render_issue_body(evidence: list[dict], *, run_url: str | None = None) -> str:
    """Render the digest issue body from the per-candidate evidence records."""
    lines = [
        DIGEST_ISSUE_MARKER,
        "## H16 empty-recording review digest",
        "",
        "These meetings were withheld from the feeds because their source media decoded as "
        "(near-)totally silent. Each row links the canonical city watch page and ships an "
        "untrimmed + silence-trimmed audio proxy in the attached workflow artifact for a listen.",
        "",
        f"- **Candidates this digest:** {len(evidence)}",
    ]
    if run_url:
        lines.append(f"- **Evidence artifact:** {run_url}")
    lines += [
        "",
        "| Meeting | State | Probed / declared | Watch page |",
        "|---|---|---|---|",
    ]
    for ev in evidence:
        probed = ev.get("probed_duration_seconds")
        declared = ev.get("declared_duration_seconds")
        probed_s = f"{probed:.0f}s" if isinstance(probed, (int, float)) else "—"
        declared_s = f"{declared}s" if isinstance(declared, (int, float)) else "—"
        watch = ev.get("canonical_source_url") or "—"
        title = (ev.get("title") or ev.get("uid") or "").replace("|", "\\|")
        lines.append(f"| {title} | {ev['state']} | {probed_s} / {declared_s} | {watch} |")
    return "\n".join(lines) + "\n"


def digest_state_path(state_dir: Path) -> Path:
    return Path(state_dir) / DIGEST_STATE_NAME


def load_digest_state(state_dir: Path) -> dict:
    path = digest_state_path(state_dir)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {"schema_version": EVIDENCE_SCHEMA_VERSION, "digested": []}
    if not isinstance(data, dict):
        return {"schema_version": EVIDENCE_SCHEMA_VERSION, "digested": []}
    data.setdefault("digested", [])
    return data


def updated_digest_state(prior: dict, newly_digested: list[str]) -> dict:
    """Return the digest ledger with ``newly_digested`` keys folded in (deduped, sorted)."""
    keys = set(prior.get("digested") or []) | set(newly_digested)
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "digested": sorted(keys),
        "updated_at": datetime.now(UTC).isoformat(),
    }


def proxy_ffmpeg_cmd(
    src_url: str,
    dest: Path | str,
    *,
    ffmpeg_binary: str = "ffmpeg",
    max_kbps: int = 24,
    trim: bool = False,
    noise_db: float = -40.0,
) -> list[str]:
    """Build the ffmpeg command for a bounded, low-bitrate mono review proxy.

    ``trim=True`` drops silence with the built-in ``silenceremove`` filter (an approximation of the
    production silence EDL — good enough to answer "is there any speech here?"). The protocol
    allowlist mirrors :func:`citypods.silence.detect_silences`, and there is no ``-Range`` request,
    so a no-Range Worker stream still works.

    ``src_url`` is passed straight to ffmpeg (which does its own fetch, bypassing the Python SSRF
    guard), so the **caller must validate it with** :func:`citypods.security.validate_source_url`
    first — ``scripts/availability_digest.py`` gates the resolved URL before it reaches here.
    """
    cmd = [
        ffmpeg_binary,
        "-y",
        "-loglevel",
        "error",
        "-protocol_whitelist",
        "file,crypto,data,http,https,tcp,tls",
        "-i",
        str(src_url),
        "-vn",
        "-ac",
        "1",
        "-b:a",
        f"{max_kbps}k",
    ]
    if trim:
        cmd += [
            "-af",
            (
                "silenceremove=start_periods=1:start_duration=1:"
                f"start_threshold={noise_db}dB:"
                "stop_periods=-1:stop_duration=1:"
                f"stop_threshold={noise_db}dB"
            ),
        ]
    cmd.append(str(dest))
    return cmd


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()
