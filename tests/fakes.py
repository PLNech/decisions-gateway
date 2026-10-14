"""An in-process stand-in for a typed-decision backend (stdlib only), used by the tests."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TIMING = (
    "parse;dur=0.01, validate;dur=0.02, tokenize;dur=0.25, queue;dur={queue}, "
    "inference;dur=75.7, respond;dur=0.01, total;dur={total}"
)


class FakeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def reply(self, status: int, doc: dict, timing: str | None = None) -> None:
        body = json.dumps(doc).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if timing:
            self.send_header("Server-Timing", timing)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self.reply(
                200,
                {"status": "ready", "model": "fake-v1", "fingerprint": "f00d", "served": 1234, "batched_requests": 99},
            )
        if self.headers.get("Authorization") != "Bearer " + self.server.key:
            return self.reply(401, {"error": "unauthorized"})
        self.reply(200, {"data": [{"id": "model-a"}]})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.headers.get("Authorization") != "Bearer " + self.server.key:
            return self.reply(401, {"error": "unauthorized"})
        fake = self.server
        with fake.lock:
            fake.arrived += 1
        fake.gate.wait(10)
        try:
            doc = json.loads(raw)
            questions = doc["questions"]
            assert isinstance(questions, dict) and doc.get("state") != "force-422"
        except Exception:
            # like the real server: the 422 body echoes the input
            return self.reply(422, {"detail": "invalid request", "input": raw.decode("utf-8", "replace")})
        answers = {name: {"type": q.get("type"), "noul": 0.5} for name, q in questions.items()}
        self.reply(200, {"model": "fake-v1", "answers": answers}, TIMING.format(queue=12.5, total=90.0))


class FakeBackend(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, key: str):
        super().__init__(("127.0.0.1", 0), FakeHandler)
        self.key = key
        self.lock = threading.Lock()
        self.arrived = 0
        self.gate = threading.Event()  # cleared = requests block until released
        self.gate.set()
        self.thread = threading.Thread(target=self.serve_forever, args=(0.05,), daemon=True)
        self.thread.start()

    @property
    def port(self) -> int:
        return self.server_address[1]

    def stop(self) -> None:
        self.gate.set()
        self.shutdown()
        self.server_close()
