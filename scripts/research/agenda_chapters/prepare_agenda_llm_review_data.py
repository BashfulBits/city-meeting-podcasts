#!/usr/bin/env python
"""Materialize source-text context for a local, blinded GH#1078 human review."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from citypods.http import make_session


def _episode_sources(key_path: Path, state_dir: Path) -> dict[str, dict]:
    """Join blinded IDs to official agenda and preserved agenda-text URLs from restored state."""
    keys = json.loads(key_path.read_text())
    by_uid = {entry["episode"][2]: entry["adjudication_id"] for entry in keys}
    found: dict[str, dict] = {}
    for path in state_dir.glob("sources/*/episodes.json"):
        for uid, episode in json.loads(path.read_text()).get("episodes", {}).items():
            adjudication_id = by_uid.get(uid)
            if adjudication_id is None:
                continue
            links = episode.get("links", {})
            found[adjudication_id] = {
                "official_agenda_url": links.get("agenda"),
                "agenda_text_artifact_url": links.get("agenda_text_artifact"),
            }
    missing = set(by_uid.values()) - set(found)
    if missing:
        raise RuntimeError(f"review episodes missing from restored state: {sorted(missing)!r}")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--write", type=Path, required=True)
    args = parser.parse_args(argv)
    sources = _episode_sources(args.key, args.state_dir)
    session = make_session()
    meetings = []
    for packet in json.loads(args.packet.read_text()):
        source = sources[packet["adjudication_id"]]
        agenda_text, retrieval_error = None, None
        url = source["agenda_text_artifact_url"]
        try:
            if not isinstance(url, str):
                raise ValueError("preserved agenda-text URL is missing")
            response = session.get(url, timeout=30)
            response.raise_for_status()
            agenda_text = response.content.decode("utf-8", errors="replace")
        except Exception as exc:
            retrieval_error = f"{type(exc).__name__}: {exc}"
        meetings.append(
            {**packet, **source, "agenda_text": agenda_text, "retrieval_error": retrieval_error}
        )
    args.write.parent.mkdir(parents=True, exist_ok=True)
    args.write.write_text(json.dumps({"meetings": meetings}, indent=2) + "\n")
    with_source_text = sum(meeting["agenda_text"] is not None for meeting in meetings)
    print(f"prepared {len(meetings)} blinded meetings; {with_source_text} with source text")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
