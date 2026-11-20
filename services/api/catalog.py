"""Local catalog, project membership and scoped bearer credentials."""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


DEV_USERS = (
    ("alex", "Alex", "student"), ("blair", "Blair", "student"),
    ("casey", "Casey", "student"), ("teacher", "Teacher", "admin"),
)
ENVIRONMENTS = [
    {"id": "python-cpu-v1", "name": "Python · CPU", "architectures": ["amd64", "arm64"],
     "description": "Curated Python environment", "profiles": ["cpu", "simulated"]},
]


class ApiError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


SCHEMA = """
CREATE TABLE IF NOT EXISTS api_schema_versions (version INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY, alias TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','student')),
    disabled INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS auth_identities (
    provider TEXT NOT NULL, subject TEXT NOT NULL, user_id TEXT NOT NULL REFERENCES users(id),
    PRIMARY KEY(provider,subject)
);
CREATE TABLE IF NOT EXISTS auth_tokens (
    digest TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), scopes TEXT NOT NULL,
    expires_at REAL NOT NULL, provider TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, owner_id TEXT NOT NULL REFERENCES users(id), created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS project_members (
    project_id TEXT NOT NULL REFERENCES projects(id), user_id TEXT NOT NULL REFERENCES users(id),
    PRIMARY KEY(project_id,user_id)
);
CREATE TABLE IF NOT EXISTS revisions (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), manifest TEXT NOT NULL,
    source_hash TEXT NOT NULL, size_bytes INTEGER NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS datasets (
    id TEXT PRIMARY KEY, owner_id TEXT REFERENCES users(id), public INTEGER NOT NULL,
    name TEXT NOT NULL, version TEXT NOT NULL, storage_ref TEXT NOT NULL, size_bytes INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS dataset_acl (
    dataset_id TEXT NOT NULL REFERENCES datasets(id), user_id TEXT NOT NULL REFERENCES users(id),
    PRIMARY KEY(dataset_id,user_id)
);
INSERT OR IGNORE INTO api_schema_versions VALUES (1);
"""


class Catalog:
    def __init__(self, db: str | Path, root: Path, dev_auth: bool):
        self.path = str(db)
        self.dev_auth = dev_auth
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        Path(db).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        Path(db).touch(mode=0o600, exist_ok=True)
        sample = root / "datasets" / "numbers-v1" / "sample.csv"
        sample.parent.mkdir(parents=True, exist_ok=True)
        try:
            with sample.open("x", encoding="utf-8") as stream:
                stream.write("x,y\n1,2\n2,4\n3,6\n4,8\n")
            # Only this fixture directory is bind-mounted into a non-root container.
            # The enclosing data root remains private to the platform.
            sample.chmod(0o644)
            sample.parent.chmod(0o755)
        except FileExistsError:
            pass
        with self.connect() as dbh:
            dbh.executescript(SCHEMA)
            dbh.execute("INSERT OR IGNORE INTO datasets VALUES (?,NULL,1,?,?,?,?)",
                        ("numbers-v1", "Sample numbers", "1", "numbers-v1", sample.stat().st_size))
            if dev_auth:
                for uid, alias, role in DEV_USERS:
                    self._identity(dbh, uid, alias, role, "dev")

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _identity(db, uid, alias, role, provider):
        # Scheme-specific subjects cannot be bound to an existing account implicitly.
        binding = db.execute("SELECT provider FROM auth_identities WHERE user_id=?", (uid,)).fetchone()
        if binding and binding["provider"] != provider:
            raise ApiError("IDENTITY_CONFLICT", "Choose a distinct identity for this provider.", 409)
        db.execute("INSERT OR IGNORE INTO users(id,alias,role) VALUES (?,?,?)", (uid, alias, role))
        db.execute("INSERT OR IGNORE INTO auth_identities VALUES (?,?,?)", (provider, uid, uid))

    def issue(self, uid, alias, role, provider, lifetime, *, read_only=False):
        if provider == "dev" and (not self.dev_auth or (uid, alias, role) not in DEV_USERS):
            raise ApiError("AUTH_REQUIRED", "Development identity unavailable.", 403)
        now = time.time()
        token = secrets.token_urlsafe(36)
        scopes = ["read"] if read_only else ["read", "write"]
        if role == "admin":
            scopes.append("admin")
        with self.connect() as db:
            self._identity(db, uid, alias, role, provider)
            row = db.execute("SELECT id,alias,role,disabled FROM users WHERE id=?", (uid,)).fetchone()
            if row["disabled"] or row["role"] != role:
                raise ApiError("IDENTITY_CONFLICT", "Identity disabled or role conflicts with registration.", 409)
            db.execute("DELETE FROM auth_tokens WHERE expires_at<=?", (now,))
            db.execute("INSERT INTO auth_tokens VALUES (?,?,?,?,?,?)",
                       (self.digest(token), uid, json.dumps(scopes), now + lifetime, provider, now))
        return {"token": token, "user": {k: row[k] for k in ("id","alias","role")},
                "expires_at": now + lifetime, "scopes": scopes}

    @staticmethod
    def digest(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def authenticate(self, token):
        with self.connect() as db:
            row = db.execute("""SELECT u.id,u.alias,u.role,t.scopes,t.provider FROM auth_tokens t
                JOIN users u ON u.id=t.user_id WHERE t.digest=? AND t.expires_at>? AND u.disabled=0""",
                             (self.digest(token), time.time())).fetchone()
        if not row or (row["provider"] == "dev" and not self.dev_auth):
            raise ApiError("AUTH_REQUIRED", "Sign in with a valid access token.", 401)
        return {**{k:row[k] for k in ("id","alias","role")}, "scopes":json.loads(row["scopes"])}

    def create_project(self, name, user):
        record = dict(id="prj_" + uuid.uuid4().hex, name=name, owner_id=user["id"], created_at=time.time())
        with self.connect() as db:
            db.execute("INSERT INTO projects VALUES (:id,:name,:owner_id,:created_at)", record)
            db.execute("INSERT INTO project_members VALUES (?,?)", (record["id"],user["id"]))
        return record

    def projects(self, user):
        with self.connect() as db:
            rows = db.execute("""SELECT p.* FROM projects p WHERE ?='admin' OR EXISTS (
                SELECT 1 FROM project_members m WHERE m.project_id=p.id AND m.user_id=?)
                ORDER BY created_at DESC""", (user["role"],user["id"]))
            return [dict(row) for row in rows]

    def project(self, pid, user):
        with self.connect() as db:
            row = db.execute("""SELECT p.* FROM projects p WHERE p.id=? AND (?='admin' OR EXISTS (
                SELECT 1 FROM project_members m WHERE m.project_id=p.id AND m.user_id=?))""",
                             (pid,user["role"],user["id"])).fetchone()
        if not row:
            raise ApiError("NOT_FOUND", "Project unavailable.", 404)
        return dict(row)

    def record_revision(self, rev):
        with self.connect() as db:
            db.execute("""INSERT OR IGNORE INTO revisions VALUES (?,?,?,?,?,?)""",
                       (rev["id"],rev["project_id"],json.dumps(rev["manifest"]),rev["source_hash"],
                        rev["size_bytes"],time.time()))

    def revisions(self, project_id):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM revisions WHERE project_id=? ORDER BY created_at DESC",
                              (project_id,))
            return [{**dict(r),"manifest":json.loads(r["manifest"])} for r in rows]

    def revision(self, rid, user):
        with self.connect() as db:
            row = db.execute("SELECT * FROM revisions WHERE id=?", (rid,)).fetchone()
        if not row:
            raise ApiError("NOT_FOUND", "Revision unavailable.", 404)
        self.project(row["project_id"], user)
        return {**dict(row),"manifest":json.loads(row["manifest"])}

    def datasets(self, user):
        with self.connect() as db:
            rows = db.execute("""SELECT d.id,d.name,d.version,d.size_bytes FROM datasets d
                WHERE d.public OR d.owner_id=? OR ?='admin' OR EXISTS
                (SELECT 1 FROM dataset_acl a WHERE a.dataset_id=d.id AND a.user_id=?)
                ORDER BY d.id""", (user["id"],user["role"],user["id"]))
            return [dict(row) for row in rows]

    def aliases(self):
        with self.connect() as db:
            return {r["id"]:r["alias"] for r in db.execute("SELECT id,alias FROM users")}
