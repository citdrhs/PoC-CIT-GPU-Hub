"""Operator command to expire published outputs and completed attempt scratch."""
import argparse
import json
import shutil
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from .manifest import identifier
from .storage import Storage


def prune(storage, control_db, *, days=30, apply=False):
    if type(days) is not int or days < 1:
        raise ValueError("Retention must be at least one day.")
    cutoff = time.time() - days * 86400
    summary = {"apply": apply, "eligible_attempts": [], "artifacts": 0, "artifact_bytes": 0}
    db_path = Path(control_db).resolve(strict=True)
    with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as control:
        for (attempt,) in control.execute("""SELECT a.id FROM attempts a
              LEFT JOIN reservations r ON a.id=r.attempt_id
              WHERE a.finished_at < ? AND a.cleanup_confirmed=1 AND r.attempt_id IS NULL""", (cutoff,)):
            with storage._db(write=apply) as db:
                if not db.execute("SELECT 1 FROM collections WHERE attempt=?", (attempt,)).fetchone():
                    continue
                rows = db.execute("SELECT id,size FROM items WHERE attempt=? AND kind='artifact'", (attempt,)).fetchall()
                scratch_root = (storage.root/"attempts").resolve()
                scratch = scratch_root / identifier(attempt)
                if scratch.resolve().parent != scratch_root:
                    raise ValueError("Scratch path escaped its configured root.")
                if not rows and not scratch.exists():
                    continue
                summary["eligible_attempts"].append(attempt)
                summary["artifacts"] += len(rows)
                summary["artifact_bytes"] += sum(row["size"] for row in rows)
                if apply:
                    # Crashes can leave an expired file missing; retry completes the same retirement.
                    for row in rows:
                        (storage.root/"objects"/identifier(row["id"])).unlink(missing_ok=True)
                    db.execute("DELETE FROM items WHERE attempt=? AND kind='artifact'", (attempt,))
                    if scratch.exists():
                        shutil.rmtree(scratch)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storage", required=True)
    parser.add_argument("--db", required=True, help="Control-plane SQLite database")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--apply", action="store_true", help="Perform the reviewed retirement")
    args = parser.parse_args()
    print(json.dumps(prune(Storage(args.storage), args.db, days=args.days, apply=args.apply), indent=2))


if __name__ == "__main__":
    main()
