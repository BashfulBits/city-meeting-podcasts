#!/usr/bin/env python
"""Serve a localhost-only, blinded agenda-title review UI and persist user decisions."""

from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class ReviewHandler(BaseHTTPRequestHandler):
    review_data: dict
    decisions_path: Path
    page_path: Path

    def _send_json(self, value: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler protocol name
        if self.path == "/api/review":
            self._send_json(self.review_data)
            return
        if self.path == "/api/decisions":
            value = (
                json.loads(self.decisions_path.read_text()) if self.decisions_path.exists() else {}
            )
            self._send_json(value)
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

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler protocol name
        if self.path != "/api/decisions":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers["Content-Length"])
            payload = json.loads(self.rfile.read(length))
            decision = payload["decision"]
            if decision not in {"correct", "incorrect", "unsure"}:
                raise ValueError("invalid decision")
            decision_id = payload["decision_id"]
            if not isinstance(decision_id, str) or len(decision_id) > 200:
                raise ValueError("invalid decision ID")
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        stored = json.loads(self.decisions_path.read_text()) if self.decisions_path.exists() else {}
        stored[decision_id] = {"decision": decision, "note": str(payload.get("note", ""))[:2_000]}
        self.decisions_path.write_text(json.dumps(stored, indent=2, sort_keys=True) + "\n")
        self._send_json({"saved": True})

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - inherited protocol name
        return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-data", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    ReviewHandler.review_data = json.loads(args.review_data.read_text())
    ReviewHandler.decisions_path = args.decisions
    ReviewHandler.page_path = Path(__file__).with_name("agenda_llm_review.html")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), ReviewHandler)
    print(f"Review UI: http://127.0.0.1:{args.port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
