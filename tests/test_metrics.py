import pytest

from decisions_gateway import metric_names as names
from decisions_gateway.metrics import (
    BYTES,
    LATENCY_MS,
    LabelSet,
    Metrics,
    Record,
    bucket_index,
    parse_server_timing,
    quantile,
    sanitize,
)

BACKEND_TIMING = (
    "parse;dur=0.01, validate;dur=0.02, tokenize;dur=0.25, queue;dur=789.17, "
    "inference;dur=75.7, respond;dur=0.01, total;dur=865.3"
)


def test_parse_server_timing():
    t = parse_server_timing(BACKEND_TIMING)
    assert t["queue"] == pytest.approx(789.17) and t["inference"] == pytest.approx(75.7)
    assert t["total"] == pytest.approx(865.3)
    assert parse_server_timing(None) == {}
    assert parse_server_timing("cache;desc=hit, queue;dur=abc, x") == {}
    assert parse_server_timing('Queue; desc="q"; dur="12.5"') == {"queue": 12.5}


@pytest.mark.parametrize(
    "raw, want",
    [
        ("bench", "bench"),
        ("Bench_Script.Labeller", "bench_script.labeller"),
        ('a b/c"d{e}\n', "abcde"),
        ("x" * 100, "x" * 40),
        ("", "unknown"),
        (None, "unknown"),
        ("!!!", "unknown"),
    ],
)
def test_sanitize(raw, want):
    assert sanitize(raw) == want


def test_labels_are_allow_listed():
    labels = LabelSet(["bench-1", "Batch_Labeller"])
    assert labels.get("Bench-1") == "bench-1" and labels.get("batch_labeller") == "batch_labeller"
    assert labels.get(None) == "unknown" and labels.get("") == "unknown"
    assert {labels.get(f"junk{i}") for i in range(100)} == {"other"}
    assert labels.get("unauthenticated") == "other"  # reserved labels cannot be claimed from a header
    m = Metrics(known_models=["model-a"])
    assert m.clients.get("anything") == "other"
    assert m.models.get("model-a") == "model-a" and m.models.get("model-b") == "other"
    # with the default empty allow-list every model is "other", and a missing one is "unknown"
    assert Metrics().models.get("model-a") == "other" and Metrics().models.get(None) == "unknown"


def test_bucket_index_edges():
    assert bucket_index(0) == 0
    assert bucket_index(1) == 0  # le=1 ms includes 1
    assert bucket_index(1.01) == 1
    assert bucket_index(2.5) == 1
    assert bucket_index(60000) == len(LATENCY_MS) - 1
    assert bucket_index(60000.1) == len(LATENCY_MS)  # +Inf
    assert bucket_index(300, BYTES) == 1


def test_quantiles_from_histogram():
    hist = [0] * (len(LATENCY_MS) + 1)
    hist[bucket_index(40)] = 90
    hist[bucket_index(900)] = 10
    assert quantile(hist, 0.5) == 50
    assert quantile(hist, 0.95) == 1000
    assert quantile([0] * len(hist), 0.5) is None


def rec(minute, client="bench", outcome="ok", reason=None, total=30.0, queue=20.0, infer=8.0):
    return Record(
        minute=minute,
        client=client,
        backend="default",
        model="model-a",
        outcome=outcome,
        reason=reason,
        body_read=True,
        n_questions=2,
        n_options_total=5,
        types={"noul": 1, "choice": 1},
        request_bytes=300,
        response_bytes=50,
        total_ms=total,
        queue_ms=queue,
        inference_ms=infer,
        overhead_ms=2.0,
    )


def test_ring_is_bounded():
    m = Metrics(ring_minutes=10)
    for minute in range(100):
        m.record(rec(minute))
    assert len(m.ring) == 10
    assert list(m.ring) == list(range(90, 100))
    assert m.totals[("bench", "default", "model-a", "ok")].requests == 100


def test_take_dirty_returns_touched_minutes_once():
    m = Metrics()
    m.record(rec(5))
    m.record(rec(5))
    m.record(rec(6))
    rows = m.take_dirty()
    assert sorted({r[0] for r in rows}) == [5, 6]
    assert next(r for r in rows if r[0] == 5)[2]["requests"] == 2
    assert m.take_dirty() == []
    m.requeue(rows)
    assert len(m.take_dirty()) == 2


def test_stats_windows_and_shares():
    m = Metrics()
    now_min = 1000
    m.record(rec(now_min, total=40, queue=30, infer=10))
    m.record(rec(now_min, outcome="rejected", reason="overloaded", total=3, queue=None, infer=None))
    m.record(rec(now_min - 30, client="labeller", total=900))
    s = m.stats(now=now_min * 60 + 5)["windows"]
    bench = s["5m"]["clients"]["bench"]
    assert bench["count"] == 2 and bench["error_rate"] == 0.5
    assert bench["queue_share"] == 0.75 and bench["rejections"] == {"overloaded": 1}
    assert "labeller" not in s["5m"]["clients"]
    assert s["1h"]["clients"]["labeller"]["p50_ms"] == 1000
    assert s["24h"]["backends"]["default"]["count"] == 3


def test_prometheus_text():
    m = Metrics()
    m.record(rec(1, total=30))
    m.record(rec(1, total=70000))
    m.record(Record(minute=1, client="unauthenticated", backend="default", outcome="rejected", reason="auth"))
    m.backend_error("default", "connect")
    text = m.render_prometheus({"default": 0}, {"default": 1}, "0.1.0")
    lbl = 'backend="default",client="bench"'
    assert f'{names.BUILD_INFO}{{version="0.1.0"}} 1' in text
    assert f'{names.REQUESTS}{{backend="default",model="model-a",client="bench",outcome="ok"}} 2' in text
    assert f'{names.REJECTIONS}{{reason="auth",client="unauthenticated"}} 1' in text
    assert f'{names.DURATION}_bucket{{{lbl},le="0.001"}} 0' in text
    assert f'{names.DURATION}_bucket{{{lbl},le="0.0025"}} 0' in text
    assert f'{names.DURATION}_bucket{{{lbl},le="0.05"}} 1' in text
    assert f'{names.DURATION}_bucket{{{lbl},le="+Inf"}} 2' in text
    assert f"{names.DURATION}_count{{{lbl}}} 2" in text
    assert f'{names.REQUEST_BYTES}_bucket{{{lbl},le="1024"}} 2' in text
    assert f'{names.QUESTIONS_PER_REQUEST}_bucket{{{lbl},le="2"}} 2' in text
    assert f"{names.OVERHEAD}_count{{{lbl}}} 2" in text
    # a rejected request with no body is not observed in the size histograms
    assert f'{names.REQUEST_BYTES}_count{{backend="default",client="unauthenticated"}} 0' in text
    assert f'{names.IN_FLIGHT}{{backend="default"}} 0' in text
    assert f'{names.BACKEND_UP}{{backend="default"}} 1' in text
    assert f'{names.BACKEND_ERRORS}{{backend="default",reason="connect"}} 1' in text
    assert f'{names.QUESTIONS}{{type="choice"}} 2' in text
    assert all(n.startswith("decisions_gateway_") for n in names.HELP)
