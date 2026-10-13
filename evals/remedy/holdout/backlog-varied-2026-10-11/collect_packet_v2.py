"""Retain exact source records and available documents without model calls or state writes."""

import argparse
import concurrent.futures
import hashlib
import json
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from citypods.security import validate_source_url

ROOT = Path(__file__).resolve().parents[4]
OUT = Path(__file__).resolve().parent / "packet-v2"
FIELDS = (
    "uid",
    "provider_guid",
    "body",
    "title",
    "published",
    "video_url",
    "links",
    "source_chapters",
    "sources",
    "media_availability",
    "media_kind",
    "duration",
)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def retrieve(item):
    identifier, original = item
    url = original
    opener = urllib.request.build_opener(NoRedirect)
    result = {
        "id": identifier,
        "original_url": original,
        "retrieved_at": datetime.now(UTC).isoformat(),
    }
    try:
        for _ in range(6):
            validate_source_url(url)
            request = urllib.request.Request(url, headers={"User-Agent": "Citypods-evidence/1"})
            try:
                with opener.open(request, timeout=30) as response:
                    data = response.read(8 * 1024 * 1024 + 1)
                    if len(data) > 8 * 1024 * 1024:
                        raise ValueError("Evidence exceeds 8 MiB cap")
                    result.update(final_url=url, http_status=response.status)
                    break
            except urllib.error.HTTPError as exc:
                if exc.code not in {301, 302, 303, 307, 308}:
                    raise
                location = exc.headers.get("Location")
                if not location:
                    raise ValueError("Redirect has no location") from exc
                url = urllib.parse.urljoin(url, location)
        else:
            raise ValueError("Too many redirects")
        stem = OUT / "documents" / identifier
        raw = stem.with_suffix(".pdf" if data.startswith(b"%PDF") else ".bin")
        with raw.open("xb") as handle:
            handle.write(data)
        if data.startswith(b"%PDF"):
            subprocess.run(
                ["pdftotext", "-layout", str(raw), str(stem.with_suffix(".txt"))],
                check=True,
                capture_output=True,
            )
            text = stem.with_suffix(".txt").read_bytes().decode("utf-8", errors="replace")
        else:
            text = data.decode("utf-8", errors="replace")
            with stem.with_suffix(".txt").open("x") as handle:
                handle.write(text)
        result.update(
            raw_file=str(raw.relative_to(ROOT)),
            sha256=hashlib.sha256(data).hexdigest(),
            text_file=str(stem.with_suffix(".txt").relative_to(ROOT)),
            chars=len(text),
            text_sha256=hashlib.sha256(text.encode()).hexdigest(),
            status="retrieved-content-not-yet-adjudicated",
        )
    except Exception as exc:
        result.update(status="unavailable", error_class=type(exc).__name__, error=str(exc)[:500])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path, required=True)
    args = parser.parse_args()
    if OUT.exists():
        raise SystemExit("Refusing existing evidence packet")
    (OUT / "documents").mkdir(parents=True)
    cases = json.loads((OUT.parent / "candidate-review.json").read_text())["cases"]
    sources = {}
    source_hashes = {}
    records = []
    requests = {}
    for case in cases:
        source = case["source_key"]
        if source not in sources:
            path = args.state_root / "sources" / source / "episodes.json"
            data = path.read_bytes()
            source_hashes[source] = hashlib.sha256(data).hexdigest()
            sources[source] = json.loads(data)["episodes"]
        record = sources[source].get(case["uid"])
        if record is None or str(record.get("provider_guid")) != case["provider_guid"]:
            raise ValueError(f"Source identity mismatch: {case['id']}")
        retained = {key: record.get(key) for key in FIELDS}
        retained.update(city=case["city"], source_key=source, case_id=case["id"])
        links = record.get("links") or {}
        refs = []
        for key in ("agenda_text_artifact", "agenda_backup_artifact", "agenda"):
            url = links.get(key)
            if url:
                identifier = case["uid"] + "-" + key
                requests[identifier] = url
                refs.append(identifier)
        retained["document_ids"] = refs
        records.append(retained)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        documents = list(pool.map(retrieve, requests.items()))
    with (OUT / "source-records.json").open("x") as handle:
        json.dump({"source_snapshot_hashes": source_hashes, "records": records}, handle, indent=2)
    with (OUT / "document-retrieval.json").open("x") as handle:
        json.dump({"documents": documents}, handle, indent=2)
    print(
        json.dumps(
            {
                "records": len(records),
                "requests": len(documents),
                "retrieved": sum(d["status"] != "unavailable" for d in documents),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
