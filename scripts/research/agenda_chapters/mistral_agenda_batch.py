#!/usr/bin/env python
"""Prepare, submit, and inspect a Mistral Batch API agenda-title shadow run.

This deliberately keeps batch submission separate from retrieval.  The input manifest maps
Mistral's ``custom_id`` back to the immutable GH#1078 frozen sample, allowing a later ingestion
step to validate and persist results without re-fetching or re-submitting agendas.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import requests
from audit_chapters import collect_benchmark_cohort

from citypods.chapter_titles import (
    build_agenda_item_extraction_request,
    ensure_agenda_item_extractor_contract,
)
from citypods.config import load_city_configs, load_site_config
from citypods.http import make_session

API_ROOT = "https://api.mistral.ai/v1"
DEFAULT_MODEL = "mistral-large-2512"


def _samples(sample_path: Path, state_dir: Path) -> list[tuple[dict, object]]:
    selected = json.loads(sample_path.read_text())
    selected_keys = {(row["slug"], row["uid"]) for row in selected}
    site = load_site_config("config/site_config.yml")
    cities = load_city_configs("config", site.get("defaults", {}))
    cohort = collect_benchmark_cohort(cities, state_dir, sample_size=999_999)
    candidates = {
        (sample.slug, sample.uid): sample
        for provider in cohort.values()
        for sample in provider.candidates
    }
    missing = selected_keys - candidates.keys()
    if missing:
        raise RuntimeError(f"frozen sample rows missing from restored state: {sorted(missing)!r}")
    return [(row, candidates[(row["slug"], row["uid"])]) for row in selected]


def _source_text(*, row: dict, sample, session, storage, cache_dir: Path) -> str:
    """Read a public agenda sidecar, falling back to its immutable B2 object."""
    cached = cache_dir / f"{row['slug']}--{sample.uid}.agenda.txt"
    if cached.exists():
        return cached.read_text(encoding="utf-8")
    try:
        response = session.get(sample.agenda_text_url, timeout=30)
        response.raise_for_status()
        text = response.content.decode("utf-8", errors="replace")
    except Exception as public_error:
        if storage is None or not storage.get_file(sample.agenda_text_key, cached):
            raise RuntimeError(
                f"public agenda artifact unavailable and B2 key missing: {public_error}"
            ) from public_error
        return cached.read_text(encoding="utf-8")
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_text(text, encoding="utf-8")
    return text


def _request_body(*, agenda_text: str) -> dict:
    request = build_agenda_item_extraction_request(agenda_text=agenda_text)
    response_model = ensure_agenda_item_extractor_contract()
    return {
        "messages": list(request.messages),
        "stream": False,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": response_model.__name__,
                "schema": response_model.model_json_schema(),
            },
        },
    }


def prepare(args: argparse.Namespace) -> None:
    """Write a reproducible, credential-free inline Batch API payload and manifest."""
    samples = _samples(args.sample, args.state_dir)
    session = make_session()
    # Most sidecars are public; defer B2 construction until an actual fallback is needed.
    # This keeps preparing a public-only sample independent of unrelated local B2 credentials.
    storage = None
    args.work_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = args.work_dir / "agenda-cache"
    requests_payload: list[dict] = []
    manifest_rows: list[dict] = []
    for index, (row, sample) in enumerate(samples):
        source_text = _source_text(
            row=row, sample=sample, session=session, storage=storage, cache_dir=cache_dir
        )
        custom_id = f"agenda-{index:03d}-{row['slug']}-{row['uid']}"
        requests_payload.append(
            {"custom_id": custom_id, "body": _request_body(agenda_text=source_text)}
        )
        manifest_rows.append(
            {
                "custom_id": custom_id,
                "episode": row,
                "source_artifact": sample.agenda_text_key,
                "source_line_count": len(source_text.splitlines()),
            }
        )
    payload = {
        "requests": requests_payload,
        "model": args.model,
        "endpoint": "/v1/chat/completions",
        "metadata": {"purpose": "citypods-gh1078-agenda-shadow", "sample_size": str(len(samples))},
    }
    (args.work_dir / "batch-request.json").write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    (args.work_dir / "manifest.json").write_text(
        json.dumps(manifest_rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    request_bytes = (args.work_dir / "batch-request.json").stat().st_size
    print(f"prepared {len(samples)} requests ({request_bytes:,} bytes)")


def _headers() -> dict[str, str]:
    key = os.environ.get("MISTRAL_API_KEY")
    if not key:
        raise RuntimeError("MISTRAL_API_KEY is required; use /usr/local/bin/citypods-env")
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def submit(args: argparse.Namespace) -> None:
    """Submit exactly one prepared batch job and record its server response locally."""
    destination = args.work_dir / "job.json"
    if destination.exists():
        raise RuntimeError(f"refusing to submit twice; existing job record: {destination}")
    payload_path = args.work_dir / "batch-request.json"
    if not payload_path.exists():
        raise RuntimeError("run prepare before submit")
    response = requests.post(
        f"{API_ROOT}/batch/jobs", headers=_headers(), data=payload_path.read_bytes(), timeout=60
    )
    response.raise_for_status()
    destination.write_text(json.dumps(response.json(), indent=2) + "\n", encoding="utf-8")
    print(f"submitted batch job {response.json()['id']}")


def status(args: argparse.Namespace) -> None:
    """Fetch and preserve the latest status for an already-submitted batch job."""
    job = json.loads((args.work_dir / "job.json").read_text())
    response = requests.get(f"{API_ROOT}/batch/jobs/{job['id']}", headers=_headers(), timeout=30)
    response.raise_for_status()
    payload = response.json()
    (args.work_dir / "status.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"{payload['id']}: {payload['status']} ({payload.get('succeeded_requests', 0)} succeeded, "
        f"{payload.get('failed_requests', 0)} failed)"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "submit", "status"))
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--sample", type=Path)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args(argv)
    if args.command == "prepare":
        if not args.sample or not args.state_dir:
            parser.error("prepare requires --sample and --state-dir")
        prepare(args)
    elif args.command == "submit":
        submit(args)
    else:
        status(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
