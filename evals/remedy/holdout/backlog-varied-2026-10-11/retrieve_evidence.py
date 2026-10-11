import concurrent.futures
import hashlib
import json
import re
import subprocess
import urllib.request
from pathlib import Path

from citypods.security import validate_source_url

root = Path(__file__).resolve().parents[4]
out = root / "evals/remedy/holdout/backlog-varied-2026-10-11"
if (out / "evidence-packets.json").exists():
    raise SystemExit("Refusing to replace existing evidence preparation")
cases = json.loads((out / "candidate-review.json").read_text())["cases"]
audit = (root / "review/evidence/addison-body-policy-audit-2026-10-04.md").read_text()
(out / "official-documents").mkdir(exist_ok=True)
urls = {}
for c in cases:
    urls[c["uid"]] = sorted(
        set(
            re.findall(
                r"https://audio\.citymeetings\.fyi/documents/[a-z0-9]+/"
                + c["uid"]
                + r"/agenda-[a-z0-9]+",
                audit,
            )
        )
    )


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Redirect requires separate validated evidence retrieval")


def fetch(item):
    uid, url = item
    try:
        validate_source_url(url, allowed_hosts=["audio.citymeetings.fyi"])
        request = urllib.request.Request(
            url, headers={"User-Agent": "Citypods-evidence-evaluation/1"}
        )
        with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
            validate_source_url(response.url, allowed_hosts=["audio.citymeetings.fyi"])
            data = response.read(5 * 1024 * 1024 + 1)
            if len(data) > 5 * 1024 * 1024:
                raise ValueError("Document exceeds evidence size cap")
        stem = out / "official-documents" / uid
        if data.startswith(b"%PDF"):
            stem.with_suffix(".pdf").write_bytes(data)
            subprocess.run(
                [
                    "pdftotext",
                    "-layout",
                    str(stem.with_suffix(".pdf")),
                    str(stem.with_suffix(".txt")),
                ],
                check=True,
                capture_output=True,
            )
            text = stem.with_suffix(".txt").read_text(errors="replace")
        else:
            text = data.decode("utf-8", errors="replace")
            stem.with_suffix(".txt").write_text(text)
        return uid, {
            "url": url,
            "sha256": hashlib.sha256(data).hexdigest(),
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "chars": len(text),
            "excerpt": text[:22000],
            "binding": (
                "retained document URL explicitly names exact source and UID; "
                "contents require adjudication"
            ),
        }
    except Exception as exc:
        return uid, {"url": url, "retrieval_error": str(exc)}


items = [(uid, u) for uid, values in urls.items() for u in values]
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
    retrieved = dict(pool.map(fetch, items))
packets = []
all_docs = list((root / "review/evidence").glob("*.md")) + [
    root / "review/51-unexpected-body-remedy-flow.md"
]
texts = {str(p.relative_to(root)): p.read_text() for p in all_docs}
for c in cases:
    refs = []
    for path, text in texts.items():
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if c["uid"] in line:
                refs.append(
                    {
                        "path": path,
                        "line": i + 1,
                        "excerpt": "\n".join(lines[max(0, i - 2) : i + 3]),
                    }
                )
    evidence = retrieved.get(c["uid"])
    packets.append(
        {
            "id": c["id"],
            "city": c["city"],
            "source_key": c["source_key"],
            "uid": c["uid"],
            "title": c["title"],
            "provider_guid": c["provider_guid"],
            "official_document": evidence,
            "review_only_research_refs": refs[:12],
            "evidence_status": "retrieved-needs-content-review"
            if evidence and "excerpt" in evidence
            else "needs-official-recording-or-content-evidence",
            "expected_decision_status": "draft-not-gold",
        }
    )
with (out / "evidence-packets.json").open("x") as f:
    json.dump({"cases": packets}, f, indent=2)
print(
    "records",
    len(packets),
    "documents retrieved",
    sum(bool(p["official_document"] and p["official_document"].get("excerpt")) for p in packets),
    "retrieval errors",
    sum(
        bool(p["official_document"] and p["official_document"].get("retrieval_error"))
        for p in packets
    ),
)
