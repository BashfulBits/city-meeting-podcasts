#!/usr/bin/env python
"""Survey durable agenda_text_artifact documents for raw, undecoded PDF bytes.

Root cause: ``citypods/agenda_text.py``'s ``_extract_pdf`` fell back, when ``pypdf`` was not
importable, to decoding the PDF's own raw bytes as UTF-8 and returning that as if it were
extracted text. Real PDF container syntax and garbled compressed-stream bytes decode into
plausible, keyword-bearing noise rather than raising, so that corruption slipped past
``assess_agenda_document``'s quality gate and was persisted as a genuine ``agenda_text_artifact``.
Both the extraction fallback and the quality-gate gap are fixed (the ``%PDF-`` guard in
``_is_placeholder_text``), but any artifact produced *before* that fix is still corrupted on disk
and needs separate remediation.

Read-only: this fetches each episode's already-published ``agenda_text_artifact`` over HTTP (the
same public URL served to users/LLMs) and flags any that still start with the raw PDF file
signature. It does not re-extract, OCR, or mutate any record -- see GH#1092's existing
placeholder-detection/OCR retry path for how flagged episodes should be remediated once found
(their `agenda_text_quality` should be reset the way ``scripts/reset_agenda_chapter_state.py``
resets other bad-agenda-state records, so AgendaTextStage re-derives them with the fixed
extractor).

Requires a populated local state mirror (``--pull-state`` refreshes it from the canonical
object-store snapshot, same as ``scripts/research/agenda_chapters/audit_chapters.py``).

Usage:
    PYTHONPATH=. python scripts/audit_raw_pdf_agenda_artifacts.py --pull-state
    PYTHONPATH=. python scripts/audit_raw_pdf_agenda_artifacts.py --state-dir /path/to/state --json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

from citypods.config import load_city_configs, load_site_config
from citypods.http import make_session
from citypods.records import load_records, source_key
from citypods.state import pull_canonical_state, resolve_state_dir

# Bounded: the raw-bytes bug's signature always sits at byte 0 of the persisted artifact, so a
# small prefix is enough -- no need to download every artifact's full (up to 50k-char) body.
_PROBE_BYTES = 64


@dataclass(frozen=True)
class RawPdfHit:
    slug: str
    uid: str
    url: str
    prefix: str


def _looks_like_raw_pdf(prefix: bytes) -> bool:
    """Cheap, structural check mirroring ``citypods.agenda_text._RAW_PDF_HEADER_RE``: real
    extracted text never starts with the PDF file signature -- only raw, undecoded container
    bytes do."""
    return prefix.decode("utf-8", errors="ignore").lstrip().startswith("%PDF-")


def find_raw_pdf_artifacts(cities, state_dir: Path, *, session) -> list[RawPdfHit]:
    hits: list[RawPdfHit] = []
    for city in cities:
        records = load_records(state_dir, source_key(city))
        for uid, record in records.items():
            if not isinstance(record, Mapping):
                continue
            url = (record.get("links") or {}).get("agenda_text_artifact")
            if not url:
                continue
            try:
                # Range-bounded so a 50k-char artifact doesn't need a full download just to read
                # its first few bytes; a server that ignores Range simply returns the whole body,
                # which is still correct here since only the prefix is inspected.
                resp = session.get(
                    url, timeout=15, headers={"Range": f"bytes=0-{_PROBE_BYTES - 1}"}
                )
                resp.raise_for_status()
            except Exception as exc:  # noqa: BLE001 - one bad fetch must not stop the survey
                print(f"[skip] {city.slug} {uid}: fetch failed ({exc})", file=sys.stderr)
                continue
            prefix = resp.content[:_PROBE_BYTES]
            if _looks_like_raw_pdf(prefix):
                hits.append(
                    RawPdfHit(
                        slug=city.slug,
                        uid=str(uid),
                        url=url,
                        prefix=prefix.decode("utf-8", errors="replace"),
                    )
                )
    return hits


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--site-config", default="config/site_config.yml")
    parser.add_argument("--output-dir", default="docs")
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument(
        "--pull-state",
        action="store_true",
        help="Refresh the local state mirror from the canonical object-store snapshot first.",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    args = parser.parse_args()

    site_config = load_site_config(args.site_config)
    cities = load_city_configs(args.config_dir, site_config.get("defaults", {}))
    if args.pull_state:
        state_dir = pull_canonical_state(site_config, Path(args.output_dir))
    else:
        state_dir = args.state_dir or resolve_state_dir(site_config, Path(args.output_dir))

    session = make_session()
    hits = find_raw_pdf_artifacts(cities, state_dir, session=session)

    if args.json:
        print(json.dumps([asdict(hit) for hit in hits], indent=2, sort_keys=True))
    else:
        if not hits:
            print("No raw-PDF-bytes agenda_text_artifact documents found.")
        for hit in hits:
            print(f"{hit.slug} {hit.uid}: {hit.url}\n    {hit.prefix!r}")
        print(f"\n{len(hits)} affected episode(s) found.")
    return 1 if hits else 0


if __name__ == "__main__":
    raise SystemExit(main())
