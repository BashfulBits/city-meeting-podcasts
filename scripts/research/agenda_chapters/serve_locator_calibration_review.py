#!/usr/bin/env python
"""Serve the localhost-only calibrated locator adjudication packet."""

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

EVIDENCE_STATUS = {"supported", "no_evidence", "ambiguous"}
ITEM_CORRECTNESS = {"correct", "incorrect"}
BOUNDARY_VALIDITY = {"valid", "invalid", "no_boundary"}


class CalibrationReviewHandler(BaseHTTPRequestHandler):
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
        elif self.path == "/api/decisions":
            self._send_json(self._load_decisions())
        elif self.path == "/":
            body = self.page_path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
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
            evidence_status = payload.get("evidence_status")
            item_correctness = payload.get("item_correctness")
            boundary_validity = payload.get("boundary_validity")
            note = payload.get("note", "")
            if evidence_status not in EVIDENCE_STATUS:
                raise ValueError("choose evidence status")
            if evidence_status == "supported":
                if item_correctness not in ITEM_CORRECTNESS:
                    raise ValueError("supported evidence requires correct or incorrect item")
                if boundary_validity not in BOUNDARY_VALIDITY:
                    raise ValueError("supported evidence requires boundary validity")
            elif item_correctness not in (None, "") or boundary_validity not in (None, ""):
                raise ValueError("item/boundary fields must be blank for no evidence or ambiguous")
            if not isinstance(note, str):
                raise ValueError("note must be text")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        decisions[case_id] = {
            "evidence_status": evidence_status,
            "item_correctness": item_correctness or None,
            "boundary_validity": boundary_validity or None,
            "note": note[:4_000],
            "updated_at": datetime.now(UTC).isoformat(),
        }
        with self.decisions_lock:
            self._write(decisions)
        self._send_json({"saved": True, "case_id": case_id, "decision": decisions[case_id]})

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
    parser.add_argument("--port", type=int, default=8767)
    args = parser.parse_args(argv)
    CalibrationReviewHandler.packet = json.loads(args.packet.read_text(encoding="utf-8"))
    CalibrationReviewHandler.cases = {
        str(case["case_id"]): case for case in CalibrationReviewHandler.packet.get("cases", [])
    }
    CalibrationReviewHandler.decisions_path = args.decisions
    CalibrationReviewHandler.page_path = Path(__file__).with_name("locator_calibration_review.html")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), CalibrationReviewHandler)
    print(f"Locator calibration review UI: http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
