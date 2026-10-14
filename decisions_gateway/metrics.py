"""In-memory per-minute aggregates, Prometheus rendering and the /v1/stats summary.

Privacy rule: a Record holds only labels (config allow-lists or closed enums) and numbers. No text chosen by
a caller, from a header or a body, ever becomes a label.
"""

from __future__ import annotations

import re
import threading
import time
from bisect import bisect_left
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field

from . import metric_names as names

LATENCY_MS = (1, 2.5, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000, 60000)  # then +Inf
BYTES = (256, 1024, 4096, 16384, 65536, 262144, 1048576)
COUNTS = (1, 2, 4, 8, 16, 32, 64)
# histogram key -> (metric name, bucket bounds, scale from stored unit to exported unit)
HISTOGRAMS = {
    "duration": (names.DURATION, LATENCY_MS, 1e-3),
    "queue": (names.QUEUE, LATENCY_MS, 1e-3),
    "inference": (names.INFERENCE, LATENCY_MS, 1e-3),
    "overhead": (names.OVERHEAD, LATENCY_MS, 1e-3),
    "request_bytes": (names.REQUEST_BYTES, BYTES, 1),
    "questions": (names.QUESTIONS_PER_REQUEST, COUNTS, 1),
}
TYPES = ("noul", "choice", "score", "other")
WINDOWS_MIN = {"5m": 5, "1h": 60, "24h": 1440}
_LABEL_DROP = re.compile(r"[^a-z0-9._-]")


def sanitize(value: str | None, max_len: int = 40) -> str:
    cleaned = _LABEL_DROP.sub("", (value or "").lower())[:max_len]
    return cleaned or "unknown"


class LabelSet:
    """Maps raw text to a label: an allow-listed value, "unknown" when absent, "other" for anything else."""

    def __init__(self, allowed: Iterable[str]):
        self.allowed = frozenset(sanitize(a) for a in allowed)

    def get(self, raw: str | None) -> str:
        if not raw:
            return "unknown"
        value = sanitize(raw)
        return value if value in self.allowed else "other"


def bucket_index(value: float, bounds: tuple = LATENCY_MS) -> int:
    """Index of the first bucket whose upper bound is >= value (Prometheus `le` semantics)."""
    return bisect_left(bounds, value)


def parse_server_timing(header: str | None) -> dict[str, float]:
    """`queue;dur=789.17, inference;dur=75.7` -> {"queue": 789.17, "inference": 75.7}."""
    out: dict[str, float] = {}
    for entry in (header or "").split(","):
        parts = [p.strip() for p in entry.split(";")]
        for param in parts[1:] if parts[0] else ():
            key, _, value = param.partition("=")
            if key.strip().lower() == "dur":
                try:
                    out[parts[0].lower()] = float(value.strip().strip('"'))
                except ValueError:
                    pass
    return out


@dataclass
class Record:
    minute: int
    client: str
    backend: str
    model: str = "unknown"
    outcome: str = "ok"
    reason: str | None = None  # rejection reason, from names.REJECTION_REASONS
    body_read: bool = False
    n_questions: int = 0
    n_options_total: int = 0
    types: dict[str, int] = field(default_factory=dict)
    request_bytes: int = 0
    response_bytes: int = 0
    total_ms: float = 0.0
    queue_ms: float | None = None
    inference_ms: float | None = None
    overhead_ms: float | None = None

    def observations(self) -> dict[str, float | None]:
        out = {
            "duration": self.total_ms,
            "queue": self.queue_ms,
            "inference": self.inference_ms,
            "overhead": self.overhead_ms,
        }
        if self.body_read:
            out.update(request_bytes=self.request_bytes, questions=self.n_questions)
        return out


@dataclass
class Agg:
    requests: int = 0
    options: int = 0
    response_bytes: int = 0
    types: dict[str, int] = field(default_factory=lambda: dict.fromkeys(TYPES, 0))
    rejections: dict[str, int] = field(default_factory=dict)
    hists: dict[str, list[int]] = field(
        default_factory=lambda: {k: [0] * (len(b) + 1) for k, (_, b, _) in HISTOGRAMS.items()}
    )
    sums: dict[str, float] = field(default_factory=lambda: dict.fromkeys(HISTOGRAMS, 0.0))

    def add(self, r: Record) -> None:
        self.requests += 1
        self.options += r.n_options_total
        self.response_bytes += r.response_bytes
        for t, n in r.types.items():
            self.types[t if t in TYPES else "other"] += n
        if r.reason:
            self.rejections[r.reason] = self.rejections.get(r.reason, 0) + 1
        for k, value in r.observations().items():
            if value is not None:
                self.hists[k][bucket_index(value, HISTOGRAMS[k][1])] += 1
                self.sums[k] += value

    def merge(self, o: Agg) -> None:
        self.requests += o.requests
        self.options += o.options
        self.response_bytes += o.response_bytes
        for t in TYPES:
            self.types[t] += o.types.get(t, 0)
        for reason, n in o.rejections.items():
            self.rejections[reason] = self.rejections.get(reason, 0) + n
        for k in HISTOGRAMS:
            self.sums[k] += o.sums[k]
            self.hists[k] = [a + b for a, b in zip(self.hists[k], o.hists[k], strict=True)]


Key = tuple[str, str, str, str]  # client, backend, model, outcome


def quantile(hist: list[int], q: float, bounds: tuple = LATENCY_MS) -> float | None:
    """Upper bound of the bucket holding quantile q; None when empty or above the top bucket."""
    total, seen = sum(hist), 0
    for i, n in enumerate(hist):
        seen += n
        if total and seen >= q * total:
            return float(bounds[i]) if i < len(bounds) else None
    return None


def merged(rows: Iterable[tuple[Key, Agg]], keyfn) -> dict:
    out: dict = {}
    for k, a in rows:
        out.setdefault(keyfn(k), Agg()).merge(a)
    return out


class Metrics:
    def __init__(
        self, ring_minutes: int = 7 * 24 * 60, known_clients: Iterable[str] = (), known_models: Iterable[str] = ()
    ):
        self.ring_minutes = ring_minutes
        self.clients = LabelSet(known_clients)
        self.models = LabelSet(known_models)
        self.lock = threading.Lock()
        self.ring: OrderedDict[int, dict[Key, Agg]] = OrderedDict()
        self.totals: dict[Key, Agg] = {}
        self.dirty: set[int] = set()
        self.backend_errors: dict[tuple[str, str], int] = {}

    def record(self, r: Record) -> None:
        key: Key = (r.client, r.backend, r.model, r.outcome)
        with self.lock:
            minute = self.ring.get(r.minute)
            if minute is None:
                minute = self.ring[r.minute] = {}
                while len(self.ring) > self.ring_minutes:
                    self.ring.popitem(last=False)
            minute.setdefault(key, Agg()).add(r)
            self.totals.setdefault(key, Agg()).add(r)
            self.dirty.add(r.minute)

    def backend_error(self, backend: str, reason: str) -> None:
        with self.lock:
            self.backend_errors[(backend, reason)] = self.backend_errors.get((backend, reason), 0) + 1

    def take_dirty(self) -> list[tuple[int, Key, dict]]:
        """Rows for every minute touched since the last call (whole minutes, for upsert)."""
        with self.lock:
            rows = [(m, k, asdict(a)) for m in sorted(self.dirty) if m in self.ring for k, a in self.ring[m].items()]
            self.dirty.clear()
        return rows

    def requeue(self, rows: list[tuple[int, Key, dict]]) -> None:
        with self.lock:
            self.dirty.update(m for m, _, _ in rows)

    def render_prometheus(self, in_flight: dict[str, int], backend_up: dict[str, int], version: str) -> str:
        with self.lock:
            totals = list(merged(self.totals.items(), lambda k: k).items())
            errors = sorted(self.backend_errors.items())
        out: list[str] = []
        _family(out, names.BUILD_INFO, [((("version", version),), 1)])
        _family(
            out,
            names.REQUESTS,
            sorted(
                ((("backend", k[1]), ("model", k[2]), ("client", k[0]), ("outcome", k[3])), a.requests)
                for k, a in totals
            ),
        )
        rejections: dict[tuple, int] = {}
        questions: dict[tuple, int] = {}
        for k, a in totals:
            for reason, n in a.rejections.items():
                labels = (("reason", reason), ("client", k[0]))
                rejections[labels] = rejections.get(labels, 0) + n
            for t, n in a.types.items():
                questions[(("type", t),)] = questions.get((("type", t),), 0) + n
        _family(out, names.REJECTIONS, sorted(rejections.items()))
        _family(out, names.QUESTIONS, sorted(questions.items()))
        _family(out, names.BACKEND_ERRORS, [((("backend", b), ("reason", r)), n) for (b, r), n in errors])
        _family(out, names.IN_FLIGHT, [((("backend", b),), n) for b, n in sorted(in_flight.items())])
        _family(out, names.BACKEND_UP, [((("backend", b),), n) for b, n in sorted(backend_up.items())])
        by_bc = sorted(merged(totals, lambda k: (("backend", k[1]), ("client", k[0]))).items())
        for key, (name, bounds, scale) in HISTOGRAMS.items():
            _family(out, name, [])
            for labels, a in by_bc:
                cumulative = 0
                for i, n in enumerate(a.hists[key]):
                    cumulative += n
                    le = f"{bounds[i] * scale:g}" if i < len(bounds) else "+Inf"
                    out.append(f"{name}_bucket{_labels(labels + (('le', le),))} {cumulative}")
                out.append(f"{name}_sum{_labels(labels)} {a.sums[key] * scale:.6g}")
                out.append(f"{name}_count{_labels(labels)} {cumulative}")
        return "\n".join(out) + "\n"

    def stats(self, now: float | None = None) -> dict:
        current = int((now if now is not None else time.time()) // 60)
        with self.lock:
            windows = {
                label: [(k, a) for m, minute in self.ring.items() if m > current - width for k, a in minute.items()]
                for label, width in WINDOWS_MIN.items()
            }
            windows = {label: list(merged(rows, lambda k: k).items()) for label, rows in windows.items()}
        return {
            "latency_bucket_upper_bounds_ms": list(LATENCY_MS),
            "windows": {
                label: {"clients": _summaries(rows, 0), "backends": _summaries(rows, 1)}
                for label, rows in windows.items()
            },
        }


def _summaries(rows: list[tuple[Key, Agg]], pos: int) -> dict[str, dict]:
    groups = merged(rows, lambda k: k[pos])
    ok = merged(((k, a) for k, a in rows if k[3] == "ok"), lambda k: k[pos])
    out = {}
    for g, a in sorted(groups.items()):
        backend_ms = a.sums["queue"] + a.sums["inference"]
        out[g] = {
            "count": a.requests,
            "error_rate": round(1 - (ok[g].requests if g in ok else 0) / a.requests, 4),
            "rejections": dict(sorted(a.rejections.items())),
            "p50_ms": quantile(a.hists["duration"], 0.50),
            "p95_ms": quantile(a.hists["duration"], 0.95),
            "queue_share": round(a.sums["queue"] / backend_ms, 4) if backend_ms else None,
        }
    return out


def _labels(pairs: tuple) -> str:
    return "{" + ",".join(f'{k}="{v}"' for k, v in pairs) + "}" if pairs else ""


def _family(out: list[str], name: str, samples: Iterable) -> None:
    kind, help_text = names.HELP[name]
    out.append(f"# HELP {name} {help_text}")
    out.append(f"# TYPE {name} {kind}")
    out.extend(f"{name}{_labels(labels)} {value}" for labels, value in samples)
