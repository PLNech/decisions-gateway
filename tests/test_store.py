import os
import sqlite3
import stat

from decisions_gateway.metrics import Metrics, Record
from decisions_gateway.store import Store


def rec(minute, client="bench"):
    return Record(
        minute=minute,
        client=client,
        backend="default",
        model="model-a",
        outcome="ok",
        n_questions=1,
        n_options_total=2,
        types={"noul": 1},
        total_ms=12.0,
    )


def test_flush_modes_and_upsert(tmp_path):
    store = Store(str(tmp_path / "state"), retention_days=30, max_rows=1000)
    assert stat.S_IMODE(os.stat(tmp_path / "state").st_mode) == 0o700
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    m = Metrics()
    now_min = 30 * 24 * 60 + 100
    m.record(rec(now_min))
    store.write(m.take_dirty(), now=now_min * 60)
    m.record(rec(now_min))
    store.write(m.take_dirty(), now=now_min * 60)
    db = sqlite3.connect(store.path)
    rows = db.execute("SELECT minute, client, data FROM minute_agg").fetchall()
    assert len(rows) == 1 and rows[0][0] == now_min and '"requests":2' in rows[0][2]


def test_retention_drops_old_minutes(tmp_path):
    store = Store(str(tmp_path), retention_days=1, max_rows=1000)
    m = Metrics()
    now_min = 10 * 24 * 60
    m.record(rec(now_min - 2 * 24 * 60))  # two days old
    m.record(rec(now_min - 60))
    m.record(rec(now_min))
    store.write(m.take_dirty(), now=now_min * 60)
    minutes = [r[0] for r in sqlite3.connect(store.path).execute("SELECT minute FROM minute_agg ORDER BY 1")]
    assert minutes == [now_min - 60, now_min]


def test_hard_row_cap_keeps_newest(tmp_path):
    store = Store(str(tmp_path), retention_days=365, max_rows=5)
    m = Metrics()
    now_min = 10_000
    for i in range(12):
        m.record(rec(now_min - i))
    store.write(m.take_dirty(), now=now_min * 60)
    assert store.count() == 5
    minutes = [r[0] for r in sqlite3.connect(store.path).execute("SELECT minute FROM minute_agg ORDER BY 1")]
    assert minutes == list(range(now_min - 4, now_min + 1))
