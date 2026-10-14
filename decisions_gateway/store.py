"""SQLite persistence for per-minute aggregates: labels and numbers only, with retention and a row cap."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS minute_agg (
    minute INTEGER NOT NULL,
    client TEXT NOT NULL,
    backend TEXT NOT NULL,
    model TEXT NOT NULL,
    outcome TEXT NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (minute, client, backend, model, outcome)
)
"""


class Store:
    def __init__(self, state_dir: str, retention_days: int = 30, max_rows: int = 500_000):
        self.dir = Path(os.path.expanduser(state_dir))
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        self.path = self.dir / "metrics.db"
        os.close(os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600))
        os.chmod(self.path, 0o600)
        self.retention_days = retention_days
        self.max_rows = max_rows
        with self._connect() as db:
            db.execute(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)

    def write(self, rows: list[tuple[int, tuple, dict]], now: float | None = None) -> None:
        """Upsert whole-minute rows, then enforce retention and the hard row cap."""
        cutoff = int((now if now is not None else time.time()) // 60) - self.retention_days * 24 * 60
        db = self._connect()
        try:
            with db:
                db.executemany(
                    "INSERT OR REPLACE INTO minute_agg VALUES (?, ?, ?, ?, ?, ?)",
                    [(m, k[0], k[1], k[2], k[3], json.dumps(d, separators=(",", ":"))) for m, k, d in rows],
                )
                db.execute("DELETE FROM minute_agg WHERE minute < ?", (cutoff,))
                (count,) = db.execute("SELECT COUNT(*) FROM minute_agg").fetchone()
                if count > self.max_rows:
                    db.execute(
                        "DELETE FROM minute_agg WHERE rowid IN "
                        "(SELECT rowid FROM minute_agg ORDER BY minute ASC LIMIT ?)",
                        (count - self.max_rows,),
                    )
        finally:
            db.close()

    def count(self) -> int:
        db = self._connect()
        try:
            return db.execute("SELECT COUNT(*) FROM minute_agg").fetchone()[0]
        finally:
            db.close()
