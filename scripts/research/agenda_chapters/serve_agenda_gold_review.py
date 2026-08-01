#!/usr/bin/env python
"""Serve a localhost-only agenda-gold labeling UI for GH#1078."""

from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class GoldReviewHandler(BaseHTTPRequestHandler):
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

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/api/review":
            self._send_json(self.review_data)
            return
        if self.path == "/api/decisions":
            stored = (
                json.loads(self.decisions_path.read_text()) if self.decisions_path.exists() else {}
            )
            self._send_json(stored)
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
        if self.path != "/api/decisions":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if payload["decision"] not in {"keep", "remove", "unsure", "added", "feedback"}:
                raise ValueError("invalid decision")
            if not isinstance(payload["decision_id"], str) or len(payload["decision_id"]) > 200:
                raise ValueError("invalid decision ID")
            item = payload.get("item")
            if payload["decision"] == "added" and (
                not isinstance(item, dict)
                or not isinstance(item.get("title"), str)
                or not item["title"].strip()
            ):
                raise ValueError("added item requires a title")
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        stored = json.loads(self.decisions_path.read_text()) if self.decisions_path.exists() else {}
        stored[payload["decision_id"]] = {
            "decision": payload["decision"],
            "item": payload.get("item"),
            "note": str(payload.get("note", ""))[:2_000],
        }
        self.decisions_path.write_text(json.dumps(stored, indent=2, sort_keys=True) + "\n")
        self._send_json({"saved": True})

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-data", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args(argv)
    GoldReviewHandler.review_data = json.loads(args.review_data.read_text())
    GoldReviewHandler.decisions_path = args.decisions
    GoldReviewHandler.page_path = Path(__file__).with_name("agenda_gold_review.html")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), GoldReviewHandler)
    print(f"Gold review UI: http://127.0.0.1:{args.port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
