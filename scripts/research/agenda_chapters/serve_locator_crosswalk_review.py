#!/usr/bin/env python
"""Serve the localhost-only locator crosswalk review packet (GH#1078)."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import threading
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

LABELS = {
    "matched_candidate",
    "consent_composite",
    "section_or_procedural",
    "missing_generated_candidate",
    "source_or_extraction_problem",
    "unsure",
}
REASONS = {
    "agenda_item_not_present",
    "extraction_missed_item",
    "provider_only_section",
    "skipped_or_withdrawn",
    "other",
}


class LocatorReviewHandler(BaseHTTPRequestHandler):
    packet: dict[str, Any]
    cases: dict[str, dict[str, Any]]
    decisions_path: Path
    page_path: Path
    decisions_lock = threading.RLock()

    def _send_json(self, value: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _load_decisions(self) -> dict[str, Any]:
        if not self.decisions_path.exists():
            return {}
        try:
            value = json.loads(self.decisions_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"unable to read decisions: {exc}") from exc
        if not isinstance(value, dict):
            raise RuntimeError("decisions file must contain an object")
        return value

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/api/review":
            self._send_json(self.packet)
            return
        if self.path == "/api/decisions":
            self._send_json(self._load_decisions())
            return
        if self.path == "/":
            body = self.page_path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        with self.decisions_lock:
            self._do_POST()

    def _do_POST(self) -> None:
        if self.path != "/api/decisions":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            case_id = payload["case_id"]
            if not isinstance(case_id, str) or case_id not in self.cases:
                raise ValueError("unknown case_id")
            with self.decisions_lock:
                decisions = self._load_decisions()
                if payload.get("clear") is True:
                    decisions.pop(case_id, None)
                    self._write(decisions)
                    self._send_json({"saved": True, "cleared": True, "case_id": case_id})
                    return
            labels = payload.get("labels", [])
            candidate_ids = payload.get("candidate_ids", [])
            reason = payload.get("reason", "")
            note = payload.get("note", "")
            if not isinstance(labels, list) or not labels or not set(labels) <= LABELS:
                raise ValueError("choose one review label")
            if len(labels) != 1:
                raise ValueError("choose exactly one review label")
            if not isinstance(candidate_ids, list) or not all(
                isinstance(value, str) for value in candidate_ids
            ):
                raise ValueError("candidate_ids must be a list of strings")
            valid_candidates = {
                str(item["candidate_id"]) for item in self.cases[case_id].get("candidates", [])
            }
            if not set(candidate_ids) <= valid_candidates:
                raise ValueError("unknown candidate_id")
            if labels[0] in {"matched_candidate", "consent_composite"} and not candidate_ids:
                raise ValueError("this label requires at least one candidate")
            if labels[0] == "consent_composite" and len(candidate_ids) < 2:
                raise ValueError("consent_composite requires at least two candidates")
            if not isinstance(reason, str) or reason not in REASONS | {""}:
                raise ValueError("invalid reason")
            if labels[0] == "missing_generated_candidate" and not reason:
                raise ValueError("missing candidate requires a reason")
            if not isinstance(note, str):
                raise ValueError("note must be text")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        decisions[case_id] = {
            "labels": labels,
            "candidate_ids": candidate_ids,
            "reason": reason,
            "note": note[:4_000],
            "updated_at": datetime.now(UTC).isoformat(),
        }
        with self.decisions_lock:
            self._write(decisions)
        self._send_json({"saved": True, "case_id": case_id})

    def _write(self, decisions: dict[str, Any]) -> None:
        self.decisions_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.decisions_path.parent, delete=False
        ) as handle:
            json.dump(decisions, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            temporary = handle.name
        os.replace(temporary, self.decisions_path)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args(argv)
    LocatorReviewHandler.packet = json.loads(args.packet.read_text(encoding="utf-8"))
    LocatorReviewHandler.cases = {
        str(case["case_id"]): case for case in LocatorReviewHandler.packet.get("cases", [])
    }
    LocatorReviewHandler.decisions_path = args.decisions
    LocatorReviewHandler.page_path = Path(__file__).with_name("locator_crosswalk_review.html")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), LocatorReviewHandler)
    print(f"Locator crosswalk review UI: http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
