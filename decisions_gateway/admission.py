"""Admission control: an in-flight cap per backend and a concurrency cap per client."""

from __future__ import annotations

import threading
import time


class Admission:
    def __init__(self, backend_cap: int, client_cap: int):
        self.backend_cap = backend_cap
        self.client_cap = client_cap
        self.cond = threading.Condition()
        self.backends: dict[str, int] = {}
        self.clients: dict[str, int] = {}

    def acquire(self, backend: str, client: str, wait_s: float | None) -> str | None:
        """Take a slot, or return the rejection reason.

        A client holding `client_cap` requests (waiting or running) gets "concurrency_cap". A full backend
        sheds at once with "overloaded", unless the caller gave a deadline: then it waits up to `wait_s`
        for a slot and sheds with "deadline".
        """
        with self.cond:
            if self.clients.get(client, 0) >= self.client_cap:
                return "concurrency_cap"
            self.clients[client] = self.clients.get(client, 0) + 1
            until = time.monotonic() + wait_s if wait_s else None
            while self.backends.get(backend, 0) >= self.backend_cap:
                remaining = until - time.monotonic() if until else 0
                if remaining <= 0:
                    self.clients[client] -= 1
                    return "deadline" if until else "overloaded"
                self.cond.wait(remaining)
            self.backends[backend] = self.backends.get(backend, 0) + 1
            return None

    def release(self, backend: str, client: str) -> None:
        with self.cond:
            self.backends[backend] -= 1
            self.clients[client] -= 1
            self.cond.notify_all()

    def in_flight(self) -> dict[str, int]:
        with self.cond:
            return dict(self.backends)
