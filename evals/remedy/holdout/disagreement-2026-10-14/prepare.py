"""Deterministically expand the eight original Sol/GLM disagreements; never read gold."""

import hashlib
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[4]
BODY = ROOT / "evals/remedy/results/body-selection-2026-10-13"
sys.path.insert(0, str(BODY))
from body_selection import SelectionInput, load_manifest, write_exclusive  # noqa: E402

from citypods.remedy_evaluation import EvidenceRef, canonical_hash  # noqa: E402
from citypods.security import validate_source_url  # noqa: E402

OUT = Path(__file__).resolve().parent
BASE = ROOT / "evals/remedy/results/prompt-comparison-2026-10-14"
HOSTS = {
    "www.citymeetings.fyi",
    "audio.citymeetings.fyi",
    "addisontx.new.swagit.com",
    "dallastx.new.swagit.com",
    "fortworthgov.granicus.com",
    "arlingtontx.granicus.com",
    "agendas.addisontx.gov",
    "dallascityhall.com",
    "www.dallascityhall.com",
}


def pattern(title):
    return re.sub(r"\s+[–—-]\s+[A-Z][a-z]{2,8}\s+\d{1,2},?\s+\d{4}$", "", title)


def terms(value):
    return set(re.findall(r"[a-z0-9]+", pattern(value).lower()))


def similarity(a, b):
    return len(a & b) / max(1, len(a | b))


def disagreements(cases, rows):
    chosen = {}
    for row in sorted(rows, key=lambda r: r.get("finished_at", "")):
        if row["status"] == "completed" and row["mode"] == "prompt_comparison/original":
            chosen.setdefault((row["candidate"], row["case_id"]), row)
    result = []
    for case in sorted(cases, key=lambda c: c.case_id):
        pair = [chosen[(m, case.case_id)] for m in ("sol", "glm_flash")]
        signatures = [
            (r["answer"]["publication_action"], sorted(r["answer"]["feed_slugs"])) for r in pair
        ]
        if signatures[0] != signatures[1]:
            if int(canonical_hash(case.case_id)[0], 16) % 2:
                pair.reverse()
            result.append((case, pair))
    if len(result) != 8:
        raise ValueError("Expected exactly eight original disagreements")
    return result


def selected_feeds(case, pair):
    seeds = terms(case.recordings[0].title)
    suggested = set()
    for row in pair:
        suggested.update(row["answer"]["feed_slugs"])
        for body in row["answer"]["bodies"]:
            seeds |= terms(body["name"])
    others = [f for f in case.existing_feeds if f["slug"] not in suggested]
    others.sort(key=lambda f: (-similarity(seeds, terms(f["title"])), f["slug"]))
    slugs = suggested | {f["slug"] for f in others[:3]}
    return [f for f in case.existing_feeds if f["slug"] in slugs]


def retrieve(url):
    key = hashlib.sha256(url.encode()).hexdigest()
    meta_path = OUT / "retrieved" / (key + ".json")
    if meta_path.exists():
        return json.loads(meta_path.read_text())
    row = {"url": url, "retrieved_at": datetime.now(UTC).isoformat()}
    try:
        with httpx.Client(timeout=40, follow_redirects=False) as client:
            target = url
            for _ in range(5):
                validate_source_url(target, allowed_hosts=sorted(HOSTS))
                with client.stream("GET", target) as response:
                    row["http_status"] = response.status_code
                    if response.is_redirect:
                        target = urljoin(target, response.headers["location"])
                        continue
                    response.raise_for_status()
                    chunks, count = [], 0
                    for chunk in response.iter_bytes():
                        count += len(chunk)
                        if count > 5 * 1024 * 1024:
                            raise ValueError("Evidence exceeds 5MB cap")
                        chunks.append(chunk)
                    data = b"".join(chunks)
                    break
            else:
                raise ValueError("Redirect limit")
        row.update(final_url=target, sha256=hashlib.sha256(data).hexdigest())
        raw_path = OUT / "retrieved" / (key + ".raw")
        raw_path.write_bytes(data)
        if data.startswith(b"%PDF"):
            value = subprocess.run(
                ["pdftotext", "-layout", str(raw_path), "-"],
                check=True,
                capture_output=True,
                timeout=40,
            ).stdout.decode("utf-8", errors="replace")
        elif "audio_feed.xml" in url:
            value = data.decode("utf-8", errors="replace")
        else:
            soup = BeautifulSoup(data, "html.parser")
            for element in soup(["script", "style", "noscript"]):
                element.decompose()
            value = soup.get_text(" ", strip=True)
        row.update(text=value, text_hash=canonical_hash(value), error=None)
    except Exception as exc:
        row.update(error=f"{type(exc).__name__}: {str(exc)[:300]}", text="")
    write_exclusive(meta_path, row)
    return row


def title_examples(feed, retrieval, excluded_uids, excluded_titles, seeds):
    row = {
        "slug": feed["slug"],
        "url": retrieval["url"],
        "retrieval_error": retrieval.get("error"),
        "examples": [],
    }
    if retrieval.get("error"):
        return row
    try:
        tree = ET.fromstring(retrieval["text"])
        patterns = {}
        for item in tree.findall("./channel/item"):
            title, guid = item.findtext("title") or "", item.findtext("guid") or ""
            if guid in excluded_uids or title in excluded_titles:
                continue
            own = pattern(title)
            patterns.setdefault(own, {"title": title, "guid": guid})
        ordered = sorted(
            patterns.values(),
            key=lambda r: (-similarity(seeds, terms(r["title"])), r["title"], r["guid"]),
        )
        row.update(
            examples=ordered[:3],
            distinct_patterns=len(patterns),
            interpretation="Existing memberships may be mistaken; not official ownership proof.",
        )
    except (ET.ParseError, ValueError) as exc:
        row["retrieval_error"] = str(exc)
    return row


def main():
    if (OUT / "packet.json").exists():
        raise SystemExit("Frozen packet exists; do not overwrite")
    (OUT / "retrieved").mkdir(exist_ok=True)
    manifest, cases = load_manifest()
    rows = [json.loads(p.read_text()) for p in (BASE / "batches").glob("*.json")]
    pairs = disagreements(cases, rows)
    urls = {e.url for case, _ in pairs for e in case.evidence_refs}
    urls |= {
        f"https://www.citymeetings.fyi/{f['slug']}/audio_feed.xml"
        for case, pair in pairs
        for f in selected_feeds(case, pair)
    }
    with ThreadPoolExecutor(max_workers=4) as pool:
        retrieved = dict(zip(sorted(urls), pool.map(retrieve, sorted(urls)), strict=True))
    excluded_uids = {c.recordings[0].uid for c in cases}
    excluded_titles = {c.recordings[0].title for c in cases}
    output, provenance = [], []
    for case, pair in pairs:
        expanded = SelectionInput.model_validate(case.model_dump())
        retrieval_status = []
        for original in case.evidence_refs:
            item = retrieved[original.url]
            text = item["text"][:16000]
            retrieval_status.append(
                {
                    "url": original.url,
                    "error": item.get("error"),
                    "text_characters": len(item["text"]),
                    "sent_characters": len(text),
                    "sha256": item.get("sha256"),
                }
            )
            for index in range(0, len(text), 4000):
                span = text[index : index + 4000]
                expanded.evidence_refs.append(
                    EvidenceRef(
                        id=f"expanded-{original.id}-{index // 4000}",
                        url=original.url,
                        recording_id=original.recording_id,
                        retrieved_at=item["retrieved_at"],
                        source_span=span,
                        content_hash=canonical_hash(span),
                    )
                )
        seeds = terms(case.recordings[0].title)
        for response in pair:
            for body in response["answer"]["bodies"]:
                seeds |= terms(body["name"])
        histories = [
            title_examples(
                f,
                retrieved[f"https://www.citymeetings.fyi/{f['slug']}/audio_feed.xml"],
                excluded_uids,
                excluded_titles,
                seeds,
            )
            for f in selected_feeds(case, pair)
        ]
        entry = {
            "case": expanded.model_dump(mode="json"),
            "proposals": {letter: row["answer"] for letter, row in zip("AB", pair, strict=True)},
            "feed_history": histories,
            "retrieval_status": retrieval_status,
        }
        output.append(entry)
        provenance.append(
            {
                "case_id": case.case_id,
                "judges": {
                    letter: {k: row.get(k) for k in ("candidate", "attempt_id", "prompt_hash")}
                    for letter, row in zip("AB", pair, strict=True)
                },
            }
        )
    write_exclusive(
        OUT / "packet.json",
        {
            "version": 1,
            "cases": output,
            "source_manifest_hash": canonical_hash(manifest),
            "qualified": False,
        },
    )
    write_exclusive(
        OUT / "provenance.json",
        {
            "cases": provenance,
            "prepared_at": datetime.now(UTC).isoformat(),
            "retrieval_urls": sorted(urls),
        },
    )
    print(
        json.dumps(
            {
                "packets": len(output),
                "urls": len(urls),
                "retrieval_errors": sum(bool(r.get("error")) for r in retrieved.values()),
            }
        )
    )


if __name__ == "__main__":
    main()
