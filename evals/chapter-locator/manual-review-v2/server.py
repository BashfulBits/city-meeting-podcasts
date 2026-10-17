"""Serve the local manual review UI and save labels to scores.json."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        paths = {
            "/": (ROOT / "index.html", "text/html; charset=utf-8"),
            "/api/packet": (ROOT / "packet.json", "application/json"),
            "/api/scores": (ROOT / "scores.json", "application/json"),
        }
        resource = paths.get(self.path)
        if resource is None:
            self.send_error(404)
            return
        path, content_type = resource
        data = path.read_bytes() if path.exists() else b"{}"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        if self.path != "/api/scores":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            scores = json.loads(self.rfile.read(length))
            if not isinstance(scores, dict):
                raise ValueError("scores must be an object")
            (ROOT / "scores.json").write_text(
                json.dumps(scores, indent=2, ensure_ascii=False) + "\n"
            )
        except (ValueError, json.JSONDecodeError) as exc:
            payload = json.dumps({"error": str(exc)}).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        payload = b'{"saved":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args: object) -> None:
        return


if __name__ == "__main__":
    address = ("127.0.0.1", 8768)
    print(f"Chapter-locator review: http://{address[0]}:{address[1]}", flush=True)
    ThreadingHTTPServer(address, Handler).serve_forever()
