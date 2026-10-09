import contextlib
import json
import os
import sqlite3
from pathlib import Path


class Busy(Exception):
    pass


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "history.sqlite3"
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS results (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE, started REAL, body TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS server_cursors (name TEXT PRIMARY KEY, next_index INTEGER NOT NULL)")

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextlib.contextmanager
    def lock(self):
        with open(self.directory / "run.lock", "a+b") as f:
            f.seek(0)
            if os.fstat(f.fileno()).st_size == 0:
                f.write(b"0")
                f.flush()
            f.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as e:
                raise Busy("A test is already running") from e
            try:
                yield
            finally:
                f.seek(0)
                if os.name == "nt":
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(f, fcntl.LOCK_UN)

    def latest(self):
        with self.connect() as db:
            row = db.execute("SELECT body FROM results ORDER BY seq DESC LIMIT 1").fetchone()
        return json.loads(row[0]) if row else None

    def sequence(self):
        with self.connect() as db:
            return db.execute("SELECT coalesce(max(seq),0) FROM results").fetchone()[0]

    def next_server_index(self, group, count):
        """Advance a group's round-robin cursor; caller holds the cycle lock."""
        with self.connect() as db:
            row = db.execute("SELECT next_index FROM server_cursors WHERE name=?", (group,)).fetchone()
            index = (row[0] if row else 0) % count
            db.execute("INSERT INTO server_cursors(name,next_index) VALUES (?,?) ON CONFLICT(name) DO UPDATE SET next_index=excluded.next_index", (group, (index + 1) % count))
        return index

    def save(self, result, limit):
        with self.connect() as db:
            db.execute("INSERT INTO results(id,started,body) VALUES (?,?,?)", (result["id"], result["started_epoch"], json.dumps(result, allow_nan=False)))
            db.execute("DELETE FROM results WHERE seq NOT IN (SELECT seq FROM results ORDER BY seq DESC LIMIT ?)", (limit,))

    def history(self, limit=100):
        with self.connect() as db:
            rows = db.execute("SELECT body FROM results ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(row[0]) for row in rows]
