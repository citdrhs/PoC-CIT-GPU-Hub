"""A private node-local journal independent of the controller database."""
import json
import os
import sqlite3
from pathlib import Path


class Journal:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Hold for the life of this process. Never unlock by deleting the inode.
        self.lock = self.path.with_suffix(".lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.lock.seek(0)
                msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.lock.close()
            raise ValueError("Another worker owns this node journal") from exc
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS journal (id TEXT PRIMARY KEY, stage TEXT, payload TEXT)")
        self.db.commit()

    def save(self, row):
        with self.db:
            self.db.execute("INSERT INTO journal VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET "
                            "stage=excluded.stage,payload=excluded.payload",
                            (row["a"]["id"], row["stage"], json.dumps(row, default=str)))

    def row(self, key):
        found = self.db.execute("SELECT payload FROM journal WHERE id=?", (key,)).fetchone()
        return json.loads(found[0]) if found else None

    def active(self):
        return [json.loads(r[0]) for r in self.db.execute("SELECT payload FROM journal WHERE stage!='REPORTED'")]

    def close(self):
        self.db.close()
        self.lock.close()
