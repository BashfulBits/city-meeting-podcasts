#!/usr/bin/env python3
"""Weekly empty-recording review digest (H16 PR3b, GH#353).

Scans the persisted records for episodes PR3a classified as suspected/confirmed empty, samples a
small deterministic set of *new or changed* candidates, renders bounded low-bitrate review proxies
(untrimmed + silence-trimmed) plus an evidence JSON for each, zips the bundle, and prints an issue
body the workflow posts to a single rolling digest issue. Encoding is best-effort: a candidate whose
source can't be fetched/encoded is still surfaced (with a note) so it is never silently dropped.

The deterministic selection / evidence / issue-rendering logic lives in
:mod:`citypods.availability_digest`; this script owns the ffmpeg/network/filesystem side.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import zipfile
from pathlib import Path

from citypods.availability_digest import (
    DIGEST_STATE_NAME,
    Candidate,
    ProxyResult,
    build_evidence,
    digest_state_path,
    iter_candidates,
    load_digest_state,
    proxy_ffmpeg_cmd,
    render_issue_body,
    safe_stem,
    select_for_digest,
    sha256_file,
    updated_digest_state,
)
from citypods.config import load_city_configs, load_site_config
from citypods.records import record_to_episode, source_key
from citypods.security import SecurityError, allowed_hosts_for_city, validate_source_url
from citypods.silence import detect_silences
from citypods.state import resolve_state_dir


def _cities_by_source(config_dir: str, site_config: dict) -> dict:
    out: dict = {}
    for city in load_city_configs(config_dir, site_config.get("defaults", {})):
        out.setdefault(source_key(city), city)
    return out


def _resolve_source_url(candidate: Candidate, cities_by_source: dict) -> str | None:
    """Best-effort resolution of the candidate's playable source URL via its provider."""
    from citypods.providers import get_provider
    from citypods.providers.base import ProviderError

    city = cities_by_source.get(candidate.source_key)
    if city is None:
        return None
    try:
        provider = get_provider(city.provider)
    except Exception:  # noqa: BLE001 - an unknown provider just means no proxy this week
        return None
    # Reconstruct a minimal Episode from the record so the provider can resolve fresh media.
    ep = record_to_episode(
        {"provider_guid": candidate.uid, "video_url": candidate.video_url, "uid": candidate.uid}
    )
    try:
        url = provider.resolve_media_url(ep, city.source)
    except (ProviderError, Exception):  # noqa: BLE001 - best effort; record a note instead
        return None
    if not url:
        return None
    # ffmpeg fetches this URL itself, bypassing the Python SSRF guard, so gate it here before it
    # reaches detect_silences / the proxy encode. A blocked URL falls back to metadata-only.
    try:
        validate_source_url(
            url, allowed_hosts=allowed_hosts_for_city(city.provider, city.city_website)
        )
    except SecurityError:
        return None
    return url


def _encode_proxy(
    src_url: str, dest: Path, *, trim: bool, max_kbps: int, ffmpeg_binary: str, timeout: float
) -> ProxyResult | None:
    cmd = proxy_ffmpeg_cmd(src_url, dest, ffmpeg_binary=ffmpeg_binary, max_kbps=max_kbps, trim=trim)
    try:
        subprocess.run(cmd, capture_output=True, timeout=timeout, check=True)
    except (subprocess.SubprocessError, OSError):
        dest.unlink(missing_ok=True)
        return None
    if not dest.exists() or dest.stat().st_size == 0:
        dest.unlink(missing_ok=True)
        return None
    from citypods.media import _probe_duration_secs

    return ProxyResult(
        path=dest.name,
        size_bytes=dest.stat().st_size,
        sha256=sha256_file(dest),
        duration_seconds=_probe_duration_secs(dest, ffmpeg_binary),
    )


def _evidence_for(
    candidate: Candidate,
    work_dir: Path,
    cities_by_source: dict,
    *,
    max_kbps: int,
    ffmpeg_binary: str,
    timeout: float,
    encode: bool,
) -> dict:
    src_url = _resolve_source_url(candidate, cities_by_source) if encode else None
    silences: list[tuple[float, float]] = []
    duration: float | None = None
    untrimmed = trimmed = None
    note = ""
    if src_url is None:
        note = "source media could not be resolved; metadata only" if encode else ""
    else:
        silences, duration = detect_silences(src_url, ffmpeg_binary=ffmpeg_binary, timeout=timeout)
        stem = safe_stem(candidate.uid)
        untrimmed = _encode_proxy(
            src_url,
            work_dir / f"{stem}.untrimmed.m4a",
            trim=False,
            max_kbps=max_kbps,
            ffmpeg_binary=ffmpeg_binary,
            timeout=timeout,
        )
        trimmed = _encode_proxy(
            src_url,
            work_dir / f"{stem}.trimmed.m4a",
            trim=True,
            max_kbps=max_kbps,
            ffmpeg_binary=ffmpeg_binary,
            timeout=timeout,
        )
        if untrimmed is None and trimmed is None:
            note = "source resolved but proxy encode failed; metadata only"
    return build_evidence(
        candidate,
        silences=silences,
        source_duration=duration,
        untrimmed=untrimmed,
        trimmed=trimmed,
        note=note,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--site-config", default="config/site_config.yml")
    ap.add_argument("--config-dir", default="config")
    ap.add_argument("--output-dir", default="dist", help="state dir resolution root")
    ap.add_argument("--out", default="availability-digest", help="dir for the ZIP + issue body")
    ap.add_argument("--limit", type=int, default=8, help="max candidates per digest")
    ap.add_argument("--max-kbps", type=int, default=24)
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--no-encode", action="store_true", help="skip proxies (metadata-only)")
    ap.add_argument("--pull", action="store_true", help="pull durable bucket state before scanning")
    ap.add_argument("--push", action="store_true", help="push the updated digest ledger back")
    ap.add_argument(
        "--push-only",
        action="store_true",
        help=(
            "skip building entirely; just push the already-written digest state. Run this "
            "after the GitHub issue publish step has confirmed success, so a candidate is "
            "never marked digested without its evidence actually having been published."
        ),
    )
    args = ap.parse_args(argv)

    site_config = load_site_config(args.site_config)
    state_dir = resolve_state_dir(site_config, Path(args.output_dir))

    if args.push_only:
        from citypods.statesync import push_state
        from citypods.storage import make_storage

        state_path = digest_state_path(state_dir)
        if not state_path.exists():
            print(f"error: {state_path} not found; nothing to push (did the build step run?)")
            return 1
        storage = make_storage(site_config, site_config.get("base_url", ""), Path(args.output_dir))
        pushed = push_state(storage, state_dir, only_prefixes=[DIGEST_STATE_NAME])
        if pushed == 0:
            # push_state() also returns 0 for "no sync support" / missing state_dir -- both mean
            # nothing was actually persisted, which would silently mark candidates digested with
            # no durable record of it (the exact failure mode --push-only exists to prevent).
            print("error: push_state pushed 0 file(s); digest state was not persisted")
            return 1
        print(f"state: pushed {pushed} file(s)")
        return 0

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    storage = None
    if args.pull or args.push:
        from citypods.statesync import pull_state
        from citypods.storage import make_storage

        storage = make_storage(site_config, site_config.get("base_url", ""), Path(args.output_dir))
        if args.pull:
            pull_state(storage, state_dir)

    candidates = iter_candidates(state_dir)
    digest_state = load_digest_state(state_dir)
    digested = set(digest_state.get("digested", []))
    selected = select_for_digest(candidates, digested, limit=args.limit)

    if not selected:
        print(
            f"availability digest: no new/changed candidates ({len(candidates)} total) — no issue"
        )
        (out_dir / "has_candidates").write_text("false\n")
        return 0

    cities_by_source = _cities_by_source(args.config_dir, site_config)
    work_dir = out_dir / "evidence"
    work_dir.mkdir(parents=True, exist_ok=True)

    evidence: list[dict] = []
    for candidate in selected:
        ev = _evidence_for(
            candidate,
            work_dir,
            cities_by_source,
            max_kbps=args.max_kbps,
            ffmpeg_binary=args.ffmpeg,
            timeout=args.timeout,
            encode=not args.no_encode,
        )
        (work_dir / f"{safe_stem(candidate.uid)}.json").write_text(
            json.dumps(ev, indent=2, sort_keys=True) + "\n"
        )
        evidence.append(ev)

    zip_path = out_dir / "availability-evidence.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(work_dir.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(work_dir))

    body = render_issue_body(evidence)
    (out_dir / "issue-body.md").write_text(body)
    (out_dir / "has_candidates").write_text("true\n")

    # Persist the ledger so the same candidates are not re-digested next week (the digest workflow
    # commits/pushes state separately; here we just write the updated file under state_dir).
    new_keys = [c.key() for c in selected]
    digest_state_path(state_dir).write_text(
        json.dumps(updated_digest_state(digest_state, new_keys), indent=2) + "\n"
    )
    if args.push and storage is not None:
        from citypods.statesync import push_state

        push_state(storage, state_dir, only_prefixes=[DIGEST_STATE_NAME])

    print(
        f"availability digest: {len(selected)} candidate(s) -> {zip_path} "
        f"(ledger {DIGEST_STATE_NAME} updated)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
