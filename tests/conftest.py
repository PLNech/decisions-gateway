import http.client
import io
import json
import logging
import socket
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fakes import FakeBackend  # noqa: E402

from decisions_gateway.config import Backend, Config  # noqa: E402
from decisions_gateway.server import build_server  # noqa: E402

KEY = "test-key-0123456789"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Client:
    def __init__(self, port: int):
        self.port = port

    def request(self, method, path, body=None, headers=None, auth=True, ctype="application/json"):
        h = {"Authorization": f"Bearer {KEY}"} if auth else {}
        if body is not None:
            h["Content-Type"] = ctype
            body = body if isinstance(body, bytes) else json.dumps(body).encode()
        h.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=h)
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            conn.close()

    def json(self, *args, **kw):
        status, headers, data = self.request(*args, **kw)
        return status, headers, json.loads(data)


@pytest.fixture
def make_gateway(tmp_path):
    """Start a gateway against a fake backend; returns (client, gateway, fake, log buffer)."""
    started = []
    key_file = tmp_path / "gateway.env"
    key_file.write_text(f"DECISIONS_GATEWAY_API_KEY={KEY}\n")

    def start(dead_backend=False, **overrides):
        fake = FakeBackend(KEY)
        port = free_port() if dead_backend else fake.port
        cfg = Config(
            listen_port=0,
            key_file=str(key_file),
            state_dir=str(tmp_path / "state"),
            flush_interval_s=3600,
            read_timeout_s=10,
            known_models=["model-a"],
            backends={"default": Backend("default", "127.0.0.1", port)},
        )
        for k, v in overrides.items():
            setattr(cfg, k, v)
        server = build_server(cfg)
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        logger = logging.getLogger("decisions_gateway")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
        started.append((server, fake, handler))
        return Client(server.server_address[1]), server.gateway, fake, buf

    yield start
    for server, fake, handler in started:
        logging.getLogger("decisions_gateway").removeHandler(handler)
        server.shutdown()
        server.server_close()
        fake.stop()
