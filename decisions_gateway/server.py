"""HTTP front: Host/auth/content checks, caps, admission, proxy to a backend, privacy-safe observability.

Nothing from a request or response body, nor any request path, is ever logged or stored here.
"""

from __future__ import annotations

import argparse
import hmac
import http.client
import json
import logging
import re
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from . import __version__
from .admission import Admission
from .caps import CapError, Shape, inspect
from .config import DEFAULT_CONFIG_PATH, KEY_FILE_ENV, Backend, Config, ConfigError, load_config, read_key
from .metrics import Metrics, Record, parse_server_timing
from .store import Store

log = logging.getLogger("decisions_gateway")

PROXY_PATH = re.compile(r"^/v1/[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*$")
PROXY_GET_PATHS = {"/v1/models"}
GATEWAY_PATHS = {"/health", "/metrics", "/v1/stats"}
HEALTH_FIELDS = ("status", "model", "fingerprint")
SHED = {
    "concurrency_cap": (429, "client concurrency limit reached"),
    "overloaded": (503, "backend at capacity"),
    "deadline": (503, "deadline exceeded waiting for a backend slot"),
}


class Reject(Exception):
    """A gateway-generated error. `message` is a fixed string (plus numbers), never input."""

    def __init__(self, status: int, message: str, reason: str | None = None, retry: bool = False):
        super().__init__(message)
        self.status, self.message, self.reason, self.retry = status, message, reason, retry


@dataclass
class Gateway:
    cfg: Config
    client_key: str
    backend_keys: dict[str, str]
    metrics: Metrics
    store: Store
    admission: Admission
    allowed_hosts: set[str] = field(default_factory=set)
    backend_up: dict[str, int] = field(default_factory=dict)

    def pick_backend(self, shape: Shape) -> Backend:
        """Routing seam: today every request goes to the default backend."""
        return self.cfg.backends[self.cfg.default_backend]

    def forward(
        self,
        backend: Backend,
        method: str,
        target: str,
        body: bytes | None,
        read_timeout: float | None = None,
        auth: bool = True,
    ) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection(backend.host, backend.port, timeout=self.cfg.connect_timeout_s)
        try:
            try:
                conn.connect()
            except OSError:
                self.fail(backend, "connect")
                raise Reject(502, "backend unreachable", reason="backend_down", retry=True) from None
            conn.sock.settimeout(read_timeout or self.cfg.read_timeout_s)
            headers = {"Accept": "application/json"}
            if auth:
                headers["Authorization"] = "Bearer " + self.backend_keys[backend.name]
            if body is not None:
                headers["Content-Type"] = "application/json"
            try:
                conn.request(method, target, body=body, headers=headers)
                resp = conn.getresponse()
                data = resp.read()
            except TimeoutError:
                self.fail(backend, "timeout")
                raise Reject(504, "backend timeout") from None
            except (OSError, http.client.HTTPException):
                self.fail(backend, "protocol")
                raise Reject(502, "backend connection failed") from None
            self.backend_up[backend.name] = 1
            keep = {k: v for k in ("Content-Type", "Server-Timing") if (v := resp.getheader(k)) is not None}
            return resp.status, keep, data
        finally:
            conn.close()

    def fail(self, backend: Backend, reason: str) -> None:
        self.metrics.backend_error(backend.name, reason)
        if reason == "connect":
            self.backend_up[backend.name] = 0

    def health(self) -> dict:
        backends = {}
        for name, backend in self.cfg.backends.items():
            entry: dict = {"reachable": False}
            try:
                status, _, data = self.forward(backend, "GET", "/health", None, read_timeout=5.0, auth=False)
                doc = json.loads(data)
                entry["reachable"] = status == 200
                if isinstance(doc, dict):
                    entry.update({k: str(doc[k])[:128] for k in HEALTH_FIELDS if k in doc})
            except (Reject, ValueError):
                pass
            backends[name] = entry
        ok = backends.get(self.cfg.default_backend, {}).get("reachable", False)
        return {
            "status": "ok" if ok else "degraded",
            "service": "decisions-gateway",
            "version": __version__,
            "default_backend": self.cfg.default_backend,
            "backends": backends,
        }

    def flush(self) -> None:
        rows = self.metrics.take_dirty()
        try:
            self.store.write(rows)
        except Exception as exc:  # keep serving; retry these minutes next time
            self.metrics.requeue(rows)
            log.error("metrics flush failed: %s", type(exc).__name__)


def envelope(status: int, message: str) -> bytes:
    return json.dumps({"error": {"code": status, "message": message}}).encode()


def _ms(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"


def server_timing(backend_header: str | None, overhead_ms: float | None, total_ms: float) -> str:
    """Keep the backend's entries (its `total` renamed `backend`), append gateway overhead and total."""
    entries = []
    for entry in (backend_header or "").split(","):
        name, sep, rest = entry.strip().partition(";")
        if name:
            entries.append(("backend" if name.lower() == "total" else name) + sep + rest)
    if overhead_ms is not None:
        entries.append(f"gateway;dur={overhead_ms:.2f}")
    entries.append(f"total;dur={total_ms:.2f}")
    return ", ".join(entries)


class Handler(BaseHTTPRequestHandler):
    server_version = "decisions-gateway/" + __version__
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 30  # client socket: bounds slow senders

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        """Silenced: the stdlib access log prints request lines and paths."""

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    @property
    def gw(self) -> Gateway:
        return self.server.gateway

    def send(self, status: int, body: bytes, headers: dict[str, str] | None = None, close: bool = False) -> None:
        headers = {"Content-Type": "application/json", **(headers or {})}
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def dispatch(self, method: str) -> None:
        t0 = time.monotonic()
        url = urlsplit(self.path)
        is_proxy = url.path not in GATEWAY_PATHS and bool(
            (method == "POST" and PROXY_PATH.match(url.path)) or (method == "GET" and url.path in PROXY_GET_PATHS)
        )
        rec = Record(minute=int(time.time() // 60), client="unauthenticated", backend=self.gw.cfg.default_backend)
        try:
            status = self.route(method, url, is_proxy, rec, t0)
        except Reject as exc:
            status = exc.status
            rec.outcome, rec.reason = "rejected" if exc.reason else "backend_error", exc.reason
            headers = {"Retry-After": str(self.gw.cfg.retry_after_s)} if exc.retry else {}
            if exc.status == 401:
                headers["WWW-Authenticate"] = "Bearer"
            # an unread request body would poison keep-alive: close after early rejections
            self.send(status, envelope(status, exc.message), headers, close=not rec.body_read and method == "POST")
        if is_proxy:
            rec.total_ms = rec.total_ms or (time.monotonic() - t0) * 1000
            self.gw.metrics.record(rec)
            log.info(
                "ts=%s client=%s backend=%s status=%d n_questions=%d total_ms=%s queue_ms=%s inference_ms=%s",
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                rec.client,
                rec.backend,
                status,
                rec.n_questions,
                _ms(rec.total_ms),
                _ms(rec.queue_ms),
                _ms(rec.inference_ms),
            )

    def route(self, method: str, url, is_proxy: bool, rec: Record, t0: float) -> int:
        """Handle one request and return the HTTP status sent, or raise Reject."""
        if self.headers.get("Host", "").lower() not in self.gw.allowed_hosts:
            raise Reject(403, "forbidden host", reason="host")
        if method == "GET" and url.path == "/health":
            self.send(200, json.dumps(self.gw.health()).encode())
            return 200
        scheme, _, token = self.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(token.strip().encode(), self.gw.client_key.encode()):
            raise Reject(401, "missing or invalid bearer token", reason="auth")
        rec.client = self.gw.metrics.clients.get(self.headers.get("X-Decision-Client"))
        if method == "GET" and url.path == "/metrics":
            text = self.gw.metrics.render_prometheus(self.gw.admission.in_flight(), self.gw.backend_up, __version__)
            self.send(200, text.encode(), headers={"Content-Type": "text/plain; version=0.0.4; charset=utf-8"})
            return 200
        if method == "GET" and url.path == "/v1/stats":
            doc = self.gw.metrics.stats()
            doc.update(in_flight=self.gw.admission.in_flight(), backend_up=self.gw.backend_up)
            self.send(200, json.dumps(doc).encode())
            return 200
        if not is_proxy:
            raise Reject(404, "not found")
        return self.proxy(method, url, rec, t0)

    def read_body(self, rec: Record) -> bytes:
        ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise Reject(415, "content type must be application/json", reason="content_type")
        raw = self.headers.get("Content-Length")
        if self.headers.get("Transfer-Encoding") or raw is None or not raw.isdigit():
            raise Reject(411, "content length required", reason="length_required")
        if int(raw) > self.gw.cfg.max_body_bytes:
            raise Reject(413, f"body too large: limit {self.gw.cfg.max_body_bytes} bytes", reason="payload_too_large")
        body = self.rfile.read(int(raw))
        rec.body_read, rec.request_bytes = True, len(body)
        return body

    def deadline_s(self) -> float | None:
        raw = self.headers.get("X-Decision-Deadline-Ms", "")
        return min(int(raw) / 1000, self.gw.cfg.read_timeout_s) if raw.isdigit() and int(raw) > 0 else None

    def proxy(self, method: str, url, rec: Record, t0: float) -> int:
        cfg = self.gw.cfg
        body, shape = None, Shape()
        if method == "POST":
            body = self.read_body(rec)
            try:
                shape = inspect(body, cfg.max_questions, cfg.max_options, cfg.max_budget)
            except CapError as exc:
                shape = exc.shape
                raise Reject(400, str(exc), reason="too_many_questions") from None
            finally:
                rec.n_questions, rec.n_options_total, rec.types = shape.n_questions, shape.n_options_total, shape.types
                rec.model = self.gw.metrics.models.get(shape.model)
        backend = self.gw.pick_backend(shape)
        rec.backend = backend.name
        shed = self.gw.admission.acquire(backend.name, rec.client, self.deadline_s())
        if shed:
            raise Reject(*SHED[shed], reason=shed, retry=True)
        target = url.path + ("?" + url.query if url.query else "")
        try:
            status, headers, data = self.gw.forward(backend, method, target, body)
        finally:
            self.gw.admission.release(backend.name, rec.client)
        timing = parse_server_timing(headers.get("Server-Timing"))
        rec.queue_ms, rec.inference_ms = timing.get("queue"), timing.get("inference")
        rec.outcome = "ok" if status < 400 else "client_error" if status < 500 else "backend_error"
        if 400 <= status < 500:  # the backend echoes input in error bodies: never pass them on
            data = envelope(status, "backend rejected the request")
            headers["Content-Type"] = "application/json"
        rec.response_bytes = len(data)
        rec.total_ms = (time.monotonic() - t0) * 1000
        if "total" in timing:
            rec.overhead_ms = max(rec.total_ms - timing["total"], 0.0)
        headers["Server-Timing"] = server_timing(headers.get("Server-Timing"), rec.overhead_ms, rec.total_ms)
        self.send(status, data, headers=headers)
        return status


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], gateway: Gateway):
        super().__init__(address, Handler)
        self.gateway = gateway
        port = self.server_address[1]
        gateway.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    def handle_error(self, request, client_address):
        """The stdlib default prints a traceback; exception text could carry request bytes."""
        log.error("request handler error: %s", sys.exc_info()[0].__name__)


def build_server(cfg: Config) -> GatewayServer:
    keys = {name: read_key(*cfg.backend_key_source(b)) for name, b in cfg.backends.items()}
    admission = Admission(cfg.backend_max_in_flight, cfg.client_max_in_flight)
    admission.backends = dict.fromkeys(cfg.backends, 0)
    gateway = Gateway(
        cfg,
        read_key(cfg.key_file, cfg.key_var),
        keys,
        Metrics(ring_minutes=cfg.ring_minutes, known_clients=cfg.known_clients, known_models=cfg.known_models),
        Store(cfg.state_dir, retention_days=cfg.retention_days, max_rows=cfg.max_rows),
        admission,
        backend_up=dict.fromkeys(cfg.backends, 0),
    )
    return GatewayServer((cfg.listen_host, cfg.listen_port), gateway)


def background_loop(gateway: Gateway, stop: threading.Event) -> None:
    """Probe backends (feeds backend_up) and flush metrics, every flush interval."""
    gateway.health()
    while not stop.wait(gateway.cfg.flush_interval_s):
        gateway.health()
        gateway.flush()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="decisions-gateway", description=__doc__, epilog=f"{KEY_FILE_ENV} overrides key_file from the config."
    )
    ap.add_argument("--config", help=f"TOML config path (default {DEFAULT_CONFIG_PATH})")
    ap.add_argument("--version", action="version", version="%(prog)s " + __version__)
    ap.add_argument("--host", help="override listen_host")
    ap.add_argument("--port", type=int, help="override listen_port")
    args = ap.parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        cfg = load_config(args.config)
        cfg.listen_host = args.host or cfg.listen_host
        cfg.listen_port = args.port if args.port is not None else cfg.listen_port
        logging.getLogger().setLevel(cfg.log_level.upper())
        server = build_server(cfg)
    except (ConfigError, OSError, ValueError) as exc:
        log.error("startup failed: %s", exc)
        return 2
    stop = threading.Event()
    threading.Thread(target=background_loop, args=(server.gateway, stop), daemon=True).start()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    b = cfg.backends[cfg.default_backend]
    log.info(
        "listening on %s:%d, default backend %s at %s:%d",
        cfg.listen_host,
        server.server_address[1],
        b.name,
        b.host,
        b.port,
    )
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.gateway.flush()
        server.server_close()
        log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
