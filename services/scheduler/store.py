"""Durable control-plane transactions for a single host using local SQLite WAL."""
import hashlib
import json
import math
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from packages.resource_policy import fixed_half


PROFILES = ("cpu", "simulated", "dell-mig", "spark-fixed-half")
TERMINAL = frozenset(("SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT", "PAUSED"))
CAPABILITIES = (
    "cpu_hard_quota", "host_memory_hard_limit", "gpu_compute_aggregate_limit",
    "gpu_memory_aggregate_limit", "descendant_containment", "routing_bypass_prevention",
    "runtime_deadline_enforcement", "verified_runtime_configuration",
)
BLOCKED = "BLOCKED_UNVERIFIED_ENFORCEMENT"
_OMITTED = object()
ATTEMPT_SELECT = """
    SELECT a.*, j.user_id,j.manifest,j.revision_id,j.profile,j.desired_state,j.name,
           n.name AS node_name,n.backend AS node_backend
    FROM attempts a JOIN jobs j ON j.id=a.job_id JOIN nodes n ON n.id=a.node_id
"""


class StoreError(Exception):
    def __init__(self, code, message, status=409):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def record(row, *json_fields):
    result = dict(row)
    for field in json_fields:
        if result.get(field) is not None:
            result[field] = json.loads(result[field])
    return result


def require(row, label):
    if row is None:
        raise StoreError("NOT_FOUND", f"{label} does not exist", 404)
    return row


def event(db, job_id, attempt_id, kind, message, at, data=None):
    db.execute("INSERT INTO events(job_id,attempt_id,kind,message,at,data) VALUES(?,?,?,?,?,?)",
               (job_id, attempt_id, kind, message, at, encode(data or {})))


class Store:
    def __init__(self, db_path, *, clock=time.time, heartbeat_timeout=None):
        self.db_path = str(db_path)
        self.clock = clock
        self.heartbeat_timeout = float(os.getenv("HUB_HEARTBEAT_TIMEOUT", "45")
                                       if heartbeat_timeout is None else heartbeat_timeout)
        if not math.isfinite(self.heartbeat_timeout) or self.heartbeat_timeout <= 0:
            raise ValueError("Invalid heartbeat timeout")
        Path(self.db_path).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            fd = os.open(self.db_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        migrations = sorted((Path(__file__).parent / "migrations").glob("*.sql"))
        if not migrations:
            raise RuntimeError("Scheduler SQL migrations are missing from this installation")
        db = sqlite3.connect(self.db_path, timeout=20, isolation_level=None)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            installed = set()
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='control_migrations'").fetchone():
                installed = {r[0] for r in db.execute("SELECT version FROM control_migrations")}
            for migration in migrations:
                if int(migration.name.split("_", 1)[0]) in installed:
                    continue
                db.executescript("BEGIN IMMEDIATE;\n" + migration.read_text(encoding="utf-8") + "\nCOMMIT;")
        finally:
            db.close()

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(self.db_path, timeout=20, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _attempt(self, db, attempt_id):
        row = require(db.execute(ATTEMPT_SELECT + " WHERE a.id=?", (attempt_id,)).fetchone(), "Attempt")
        return record(row, "manifest", "envelope", "inventory", "metadata", "metrics")

    def attempt(self, attempt_id):
        with self.connection() as db:
            return self._attempt(db, attempt_id)

    def _job(self, db, job_id):
        row = require(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone(), "Job")
        result = record(row, "manifest")
        result["attempts"] = [
            record(a, "manifest", "envelope", "inventory", "metadata", "metrics")
            for a in db.execute(ATTEMPT_SELECT + " WHERE a.job_id=? ORDER BY a.generation", (job_id,))
        ]
        return result

    def job(self, job_id):
        with self.connection() as db:
            return self._job(db, job_id)

    def jobs(self, user_id=None):
        where, args = (" WHERE user_id=?", (user_id,)) if user_id is not None else ("", ())
        with self.connection() as db:
            rows = db.execute("SELECT id FROM jobs" + where + " ORDER BY created_at,id", args)
            return [self._job(db, r["id"]) for r in rows]

    def _check_key(self, db, user_id, operation, key, request):
        if not isinstance(key, str) or not (1 <= len(key) <= 200):
            raise StoreError("INVALID_IDEMPOTENCY_KEY", "Use a key of 1–200 characters", 422)
        previous = db.execute(
            "SELECT request_hash,job_id FROM control_idempotency WHERE user_id=? AND operation=? AND key=?",
            (user_id, operation, key)).fetchone()
        if previous and previous["request_hash"] != digest(request):
            raise StoreError("IDEMPOTENCY_CONFLICT", "Key already used for a different request")
        return previous["job_id"] if previous else None

    def _student_policy(self, db, user_id):
        row = require(db.execute("""SELECT u.alias,u.role,u.disabled,l.runtime_seconds FROM users u
                                    LEFT JOIN student_runtime_limits l ON l.user_id=u.id WHERE u.id=?""",
                                 (user_id,)).fetchone(), "User")
        maximum = int(os.getenv("HUB_MAX_RUNTIME_SECONDS", "7200"))
        cap = row["runtime_seconds"]
        return {"user_id": user_id, "alias": row["alias"], "role": row["role"],
                "disabled": bool(row["disabled"]), "runtime_override_seconds": cap,
                "effective_runtime_max_seconds": min(cap or maximum, maximum)}

    def student_policy(self, user_id):
        with self.connection() as db:
            return self._student_policy(db, user_id)

    def student_policies(self):
        with self.connection() as db:
            users = db.execute("SELECT id FROM users WHERE role='student' ORDER BY alias,id")
            return [self._student_policy(db, row["id"]) for row in users]

    def set_student_policy(self, user_id, actor, *, disabled=None, runtime_override_seconds=_OMITTED):
        with self.connection(True) as db:
            admin = self._student_policy(db, actor)
            if admin["role"] != "admin" or admin["disabled"]:
                raise StoreError("FORBIDDEN", "An enabled administrator is required", 403)
            before = self._student_policy(db, user_id)
            if before["role"] != "student":
                raise StoreError("INVALID_TARGET", "Select a student account", 422)
            if disabled is not None:
                if type(disabled) is not bool:
                    raise StoreError("INVALID_POLICY", "disabled must be a boolean", 422)
                db.execute("UPDATE users SET disabled=? WHERE id=?", (int(disabled), user_id))
                if disabled and not before["disabled"]:
                    db.execute("DELETE FROM auth_tokens WHERE user_id=?", (user_id,))
            if runtime_override_seconds is not _OMITTED:
                if runtime_override_seconds is None:
                    db.execute("DELETE FROM student_runtime_limits WHERE user_id=?", (user_id,))
                else:
                    maximum = int(os.getenv("HUB_MAX_RUNTIME_SECONDS", "7200"))
                    if type(runtime_override_seconds) is not int or not 1 <= runtime_override_seconds <= maximum:
                        raise StoreError("INVALID_POLICY", f"Runtime override must be between 1 and {maximum} seconds", 422)
                    db.execute("""INSERT INTO student_runtime_limits VALUES(?,?) ON CONFLICT(user_id)
                                  DO UPDATE SET runtime_seconds=excluded.runtime_seconds""", (user_id, runtime_override_seconds))
            after = self._student_policy(db, user_id)
            if before != after:
                event(db, None, None, "STUDENT_POLICY_CHANGED", "Student access or runtime limit updated", self.clock(),
                      {"actor": actor, "user_id": user_id, "before": before, "after": after})
            return after

    def _authorize_runtime(self, db, owner, manifest):
        maximum = int(os.getenv("HUB_MAX_RUNTIME_SECONDS", "7200"))
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='api_schema_versions'").fetchone():
            policy = self._student_policy(db, owner)
            if policy["disabled"]:
                raise StoreError("ACCOUNT_DISABLED", "Student access is disabled", 403)
            maximum = policy["effective_runtime_max_seconds"]
        runtime = manifest.get("runtime_seconds", 3600)
        if type(runtime) is not int or not 1 <= runtime <= maximum:
            raise StoreError("RUNTIME_POLICY_EXCEEDED", f"Requested runtime exceeds the {maximum}-second limit", 422)

    def submit(self, user_id, revision_id, manifest, key, *, profile="simulated",
               checkpoint_id=None, operation="submit"):
        if profile not in PROFILES:
            raise StoreError("INVALID_MANIFEST", "Unknown execution profile", 422)
        request = [revision_id, manifest, profile, checkpoint_id]
        with self.connection(True) as db:
            prior = self._check_key(db, user_id, operation, key, request)
            if prior:
                return self._job(db, prior)
            self._authorize_runtime(db, user_id, manifest)
            job_id, now = "job-" + uuid.uuid4().hex, self.clock()
            db.execute("""INSERT INTO jobs VALUES(?,?,?,?,?,?,?,'QUEUED','RUN',?,?,?,?)""",
                       (job_id, user_id, revision_id, manifest["name"], encode(manifest), profile,
                        checkpoint_id, "Waiting for the scheduler", now, now, now))
            db.execute("INSERT INTO control_idempotency VALUES(?,?,?,?,?)",
                       (user_id, operation, key, digest(request), job_id))
            event(db, job_id, None, "QUEUED", "Job submitted", now)
            return self._job(db, job_id)

    def resume(self, job_id, user_id, checkpoint_id, key):
        operation, request = "resume", [job_id, checkpoint_id]
        with self.connection(True) as db:
            prior = self._check_key(db, user_id, operation, key, request)
            if prior:
                return self._job(db, prior)
            job = self._job(db, job_id)
            if job["state"] not in TERMINAL:
                raise StoreError("JOB_ACTIVE", "Waiting for confirmed termination before resume")
            if not checkpoint_id:
                raise StoreError("CHECKPOINT_REQUIRED", "Select a completed checkpoint", 422)
            self._authorize_runtime(db, job["user_id"], job["manifest"])
            now = self.clock()
            db.execute("""UPDATE jobs SET state='QUEUED',desired_state='RUN',checkpoint_id=?,
                          waiting_reason='Waiting for the scheduler',updated_at=?,queued_at=? WHERE id=?""",
                       (checkpoint_id, now, now, job_id))
            db.execute("INSERT INTO control_idempotency VALUES(?,?,?,?,?)",
                       (user_id, operation, key, digest(request), job_id))
            event(db, job_id, None, "RESUMED", "Resume queued", now, {"checkpoint_id": checkpoint_id})
            return self._job(db, job_id)

    def cancel(self, job_id):
        with self.connection(True) as db:
            job = self._job(db, job_id)
            if job["desired_state"] == "STOP" or job["state"] in TERMINAL:
                return job
            state = "CANCELLED" if job["state"] == "QUEUED" else job["state"]
            now = self.clock()
            db.execute("UPDATE jobs SET state=?,desired_state='STOP',waiting_reason=NULL,updated_at=? WHERE id=?",
                       (state, now, job_id))
            event(db, job_id, None, "CANCEL_REQUESTED", "User requested cancellation", now)
            return self._job(db, job_id)

    def plan_node(self, node_id, name, backend):
        if backend not in PROFILES:
            raise ValueError("Unknown node backend")
        with self.connection(True) as db:
            db.execute("""INSERT OR IGNORE INTO nodes(id,name,backend,inventory,qualification_status,
                          environments,dataset_ids) VALUES(?,?,?,'{}',?,'[]','[]')""",
                       (node_id, name, backend, BLOCKED))
            self._slots(db, node_id)
            return self._node(db, node_id)

    @staticmethod
    def _slots(db, node_id):
        db.executemany("INSERT OR IGNORE INTO slots(node_id,id) VALUES(?,?)", [(node_id, "A"), (node_id, "B")])

    def register_node(self, node_id, name, backend, inventory, reserve_bytes, reserved_cpus=0,
                      evidence=None, dataset_ids=(), environments=("python-cpu-v1",)):
        if backend not in PROFILES:
            raise ValueError("Unknown node backend")
        env = fixed_half(inventory["memory_bytes"], reserve_bytes, inventory["logical_cpus"],
                         reserved_cpus, gpu=backend != "cpu").as_dict()
        evidence = evidence or {}
        fingerprint = inventory.get("fingerprint") or digest(inventory)
        devices = evidence.get("devices", {})
        if backend in ("simulated", "cpu"):
            qual = "SIMULATED" if backend == "simulated" else "CPU_ONLY"
        else:
            qual = BLOCKED
            caps = evidence.get("capabilities", {})
            sizes = evidence.get("device_memory_bytes", {})
            if (backend == "dell-mig" and inventory.get("fingerprint")
                    and evidence.get("fingerprint") == fingerprint
                    and isinstance(evidence.get("observed_at"), (int, float))
                    and all(caps.get(k) == "VERIFIED" for k in CAPABILITIES)
                    and all(isinstance(devices.get(s), str) and devices[s].startswith("MIG-")
                            and type(sizes.get(s)) is int and sizes[s] > 0 for s in ("A", "B"))
                    and devices["A"] != devices["B"]):
                qual = "VERIFIED"
        now = self.clock()
        config = (name, backend, encode(inventory), reserve_bytes, reserved_cpus, encode(env),
                  fingerprint, qual, encode(sorted(environments)), encode(sorted(dataset_ids)))
        with self.connection(True) as db:
            old = db.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
            busy = db.execute("SELECT 1 FROM reservations WHERE node_id=?", (node_id,)).fetchone()
            if old and old["heartbeat_at"] is not None and now-old["heartbeat_at"] > self.heartbeat_timeout:
                self._unknown(db, node_id, now, "NODE_UNREACHABLE")
            if old and busy and (old["fingerprint"] != fingerprint or old["envelope"] != encode(env)
                                 or old["backend"] != backend or old["qualification_status"] != qual):
                # Retain the active claim. All future admission needs operator reconciliation.
                db.execute("UPDATE nodes SET draining=1,qualification_status=? WHERE id=?", (BLOCKED, node_id))
                self._unknown(db, node_id, now, "Worker configuration changed during an active reservation")
                event(db, None, None, "DRIFT", "Node quarantined after configuration drift", now, {"node_id": node_id})
            else:
                db.execute("""INSERT INTO nodes(id,name,backend,inventory,reserve_bytes,reserved_cpus,envelope,
                              fingerprint,qualification_status,environments,dataset_ids,heartbeat_at)
                              VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                              name=excluded.name, backend=excluded.backend,inventory=excluded.inventory,
                              reserve_bytes=excluded.reserve_bytes,reserved_cpus=excluded.reserved_cpus,
                              envelope=excluded.envelope,fingerprint=excluded.fingerprint,
                              qualification_status=excluded.qualification_status,environments=excluded.environments,
                              dataset_ids=excluded.dataset_ids,heartbeat_at=excluded.heartbeat_at""",
                           (node_id, *config, now))
                self._slots(db, node_id)
                for slot in ("A", "B"):
                    db.execute("UPDATE slots SET device=? WHERE node_id=? AND id=?",
                               (devices.get(slot) if qual == "VERIFIED" else None, node_id, slot))
                db.execute("INSERT INTO capability_snapshots(node_id,fingerprint,at,evidence) VALUES(?,?,?,?)",
                           (node_id, fingerprint, now, encode(evidence)))
            return self._node(db, node_id)

    def heartbeat(self, node_id, *, pressure=False, metrics=None):
        with self.connection(True) as db:
            now = self.clock()
            old = db.execute("SELECT heartbeat_at FROM nodes WHERE id=?", (node_id,)).fetchone()
            if old and old[0] is not None and now-old[0] > self.heartbeat_timeout:
                self._unknown(db, node_id, now, "NODE_UNREACHABLE")
            updated = db.execute("""UPDATE nodes SET heartbeat_at=?,pressure=?,metrics=COALESCE(?,metrics)
                                    WHERE id=? AND envelope IS NOT NULL""",
                                 (now, int(pressure), encode(metrics) if metrics is not None else None, node_id))
            if not updated.rowcount:
                raise StoreError("NODE_UNREGISTERED", "Register this worker before sending heartbeats", 404)

    def drain(self, node_id, enabled=True, actor="operator"):
        with self.connection(True) as db:
            require(db.execute("SELECT id FROM nodes WHERE id=?", (node_id,)).fetchone(), "Node")
            db.execute("UPDATE nodes SET draining=? WHERE id=?", (int(bool(enabled)), node_id))
            event(db, None, None, "DRAIN" if enabled else "UNDRAIN", "Administrative admission change",
                  self.clock(), {"node_id": node_id, "actor": actor})
            return self._node(db, node_id)

    def qualification(self, node_id):
        with self.connection() as db:
            node = self._node(db, node_id)
            snapshot = db.execute("SELECT * FROM capability_snapshots WHERE node_id=? ORDER BY id DESC LIMIT 1",
                                  (node_id,)).fetchone()
            evidence = record(snapshot, "evidence") if snapshot else None
            return {"node_id": node_id, "status": node["qualification_status"], "fingerprint": node["fingerprint"],
                    "snapshot": evidence, "capabilities": (evidence["evidence"] if evidence else {}).get("capabilities", {})}

    def _status(self, node, now):
        if node["heartbeat_at"] is None:
            return "PLANNED"
        if now - node["heartbeat_at"] > self.heartbeat_timeout:
            return "UNREACHABLE"
        if node["qualification_status"] == BLOCKED:
            return BLOCKED
        if node["draining"]:
            return "DRAINING"
        if node["pressure"]:
            return "RESOURCE_PRESSURE"
        return "ONLINE"

    def _admission(self, db, node, now):
        status = self._status(node, now)
        if status == "ONLINE" and db.execute(
            """SELECT 1 FROM attempts a JOIN reservations r ON r.attempt_id=a.id
               WHERE r.node_id=? AND a.state='UNKNOWN'""", (node["id"],)).fetchone():
            return "QUARANTINED"
        return status

    def _node(self, db, node_id):
        row = require(db.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone(), "Node")
        node = record(row, "inventory", "envelope", "metrics", "environments", "dataset_ids")
        node["draining"] = bool(node["draining"])
        node["status"] = self._admission(db, node, self.clock())
        node["slots"] = []
        for slot in db.execute("""
            SELECT s.id,r.attempt_id,r.user_id,a.job_id,a.kind,a.state AS attempt_state,j.name AS job_name
              FROM slots s LEFT JOIN reservations r ON r.node_id=s.node_id AND r.slot=s.id
              LEFT JOIN attempts a ON a.id=r.attempt_id LEFT JOIN jobs j ON j.id=a.job_id
             WHERE s.node_id=? ORDER BY s.id""", (node_id,)):
            slot = dict(slot)
            if slot["attempt_id"]:
                quarantined = slot["attempt_state"] == "UNKNOWN" or node["status"] == "UNREACHABLE"
                slot["state"] = "QUARANTINED" if quarantined else "OCCUPIED"
            else:
                slot["state"] = "AVAILABLE" if node["status"] == "ONLINE" else "BLOCKED"
            node["slots"].append(slot)
        return node

    def nodes(self):
        with self.connection() as db:
            return [self._node(db, row["id"]) for row in db.execute("SELECT id FROM nodes ORDER BY name,id")]

    def commands(self, node_id):
        with self.connection() as db:
            return [self._attempt(db, row["attempt_id"]) for row in db.execute(
                "SELECT attempt_id FROM reservations WHERE node_id=? ORDER BY slot", (node_id,))]

    def _unknown(self, db, node_id, now, reason):
        rows = db.execute("""SELECT a.id,a.job_id FROM reservations r JOIN attempts a ON a.id=r.attempt_id
                             WHERE r.node_id=? AND a.state!='UNKNOWN'""", (node_id,)).fetchall()
        for row in rows:
            db.execute("UPDATE attempts SET state='UNKNOWN',reason=? WHERE id=?", (reason, row["id"]))
            db.execute("UPDATE jobs SET state='UNKNOWN',updated_at=? WHERE id=?", (now, row["job_id"]))
            event(db, row["job_id"], row["id"], "UNKNOWN", reason, now)

    def _reserve(self, db, job, node, slot, now):
        generation = db.execute("SELECT COALESCE(MAX(generation),0)+1 FROM attempts WHERE job_id=?",
                                (job["id"],)).fetchone()[0]
        attempt_id = "attempt-" + uuid.uuid4().hex
        inventory = json.loads(node["inventory"])
        manifest = job["manifest"]
        key, cached = None, None
        if manifest.get("dependencies_lock"):
            key = digest([job["revision_id"], manifest["environment"], manifest["dependencies_lock"],
                          node["id"], node["fingerprint"], inventory.get("image_digest"),
                          inventory.get("architecture"), inventory.get("build_recipe")])
            cached = db.execute("SELECT * FROM environment_builds WHERE build_key=?", (key,)).fetchone()
        kind = "BUILD" if key and not cached else "RUN"
        envelope = json.loads(node["envelope"])
        if kind == "BUILD" or job["profile"] == "cpu":
            envelope["gpu_fraction"] = None
        elif node["backend"] == "dell-mig":
            envelope["device_id"] = db.execute("SELECT device FROM slots WHERE node_id=? AND id=?",
                                                (node["id"], slot)).fetchone()[0]
            proof = db.execute("SELECT evidence FROM capability_snapshots WHERE node_id=? ORDER BY id DESC LIMIT 1",
                               (node["id"],)).fetchone()
            envelope["gpu_memory_bytes"] = json.loads(proof[0])["device_memory_bytes"][slot]
        db.execute("""INSERT INTO attempts(id,job_id,node_id,slot,generation,kind,state,envelope,inventory,
                      build_key,image_digest,cache_path,checkpoint_id,created_at) VALUES(?,?,?,?,?,?,'STARTING',?,?,?,?,?,?,?)""",
                   (attempt_id, job["id"], node["id"], slot, generation, kind, encode(envelope), node["inventory"],
                    key, cached["image_digest"] if cached else inventory.get("image_digest"),
                    cached["cache_path"] if cached else None, job["checkpoint_id"], now))
        db.execute("INSERT INTO reservations VALUES(?,?,?,?)", (attempt_id, job["user_id"], node["id"], slot))
        db.execute("UPDATE jobs SET state=?,waiting_reason=NULL,updated_at=? WHERE id=?",
                   ("PREPARING" if kind == "BUILD" else "STARTING", now, job["id"]))
        event(db, job["id"], attempt_id, "RESERVED", f"{kind.title()} reservation acquired", now,
              {"node_id": node["id"], "slot": slot, "user_id": job["user_id"], "generation": generation})
        return attempt_id

    @staticmethod
    def _access_reason(db, job):
        principal = db.execute("SELECT role,disabled FROM users WHERE id=?", (job["user_id"],)).fetchone()
        if not principal or principal["disabled"]:
            return "Owner unavailable or disabled"
        if principal["role"] != "admin":
            membership = db.execute("""SELECT 1 FROM revisions v JOIN project_members m ON m.project_id=v.project_id
                                       WHERE v.id=? AND m.user_id=?""", (job["revision_id"], job["user_id"])).fetchone()
            if not membership:
                return "Project access unavailable"
        for dataset_id in job["manifest"].get("dataset_ids", []):
            access = db.execute("""SELECT 1 FROM datasets d WHERE d.id=? AND (d.public OR ?='admin' OR
                                   d.owner_id=? OR EXISTS (SELECT 1 FROM dataset_acl a WHERE a.dataset_id=d.id
                                   AND a.user_id=?))""", (dataset_id, principal["role"], job["user_id"], job["user_id"])).fetchone()
            if not access:
                return "Dataset access unavailable"
        return None

    def tick(self):
        now = self.clock()
        with self.connection(True) as db:
            queue_cutoff = now - int(os.getenv("HUB_QUEUE_TIMEOUT_SECONDS", "604800"))
            expired = db.execute("SELECT id FROM jobs WHERE state='QUEUED' AND queued_at<=?", (queue_cutoff,)).fetchall()
            for job in expired:
                db.execute("""UPDATE jobs SET state='TIMED_OUT',desired_state='STOP',updated_at=?,
                              waiting_reason=NULL WHERE id=?""", (now, job["id"]))
                event(db, job["id"], None, "QUEUE_TIMEOUT", "Maximum queue wait elapsed", now)
            launch_cutoff = now - int(os.getenv("HUB_LAUNCH_TIMEOUT_SECONDS", "180"))
            launches = db.execute("""SELECT a.id,a.job_id FROM attempts a JOIN reservations r ON r.attempt_id=a.id
                                     JOIN jobs j ON j.id=a.job_id WHERE a.state='STARTING' AND a.created_at<=?
                                     AND j.desired_state='RUN'""", (launch_cutoff,)).fetchall()
            for attempt in launches:
                db.execute("UPDATE jobs SET desired_state='STOP',updated_at=? WHERE id=?", (now, attempt["job_id"]))
                event(db, attempt["job_id"], attempt["id"], "LAUNCH_TIMEOUT",
                      "Launch acknowledgement overdue; requested stop while retaining reservation", now)
            nodes = list(db.execute("SELECT * FROM nodes ORDER BY id"))
            for node in nodes:
                if self._status(node, now) == "UNREACHABLE":
                    self._unknown(db, node["id"], now, "NODE_UNREACHABLE")
            active = {r[0] for r in db.execute("SELECT user_id FROM reservations")}
            queued = [record(j, "manifest") for j in db.execute(
                "SELECT * FROM jobs WHERE state='QUEUED' AND desired_state='RUN' ORDER BY queued_at,id")]
            launched = []
            remaining = {job["id"] for job in queued}
            reasons = {job["id"]: "Compatible worker unavailable" for job in queued}
            # The API owns catalog migrations. Standalone policy tests can use the
            # control store without a catalog. Check live ACLs again at allocation.
            denied = {}
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='api_schema_versions'").fetchone():
                denied = {j["id"]: why for j in queued if (why := self._access_reason(db, j))}
            candidates = []
            for node in nodes:
                node_status = self._admission(db, node, now)
                eligible = []
                for job in queued:
                    if job["id"] not in remaining or job["id"] in denied:
                        continue
                    compatible = job["profile"] == node["backend"] or (
                        job["profile"] == "cpu" and node["backend"] == "simulated"
                        and json.loads(node["inventory"]).get("execution_mode") == "docker")
                    if not compatible:
                        continue
                    if node_status != "ONLINE":
                        if reasons[job["id"]] == "Compatible worker unavailable":
                            reasons[job["id"]] = ("GPU enforcement is unverified" if node["qualification_status"] == BLOCKED
                                                  else f"Worker is {node_status.lower().replace('_',' ')}")
                        continue
                    if job["manifest"]["environment"] not in json.loads(node["environments"]):
                        reasons[job["id"]] = "Environment unavailable on this worker"
                        continue
                    if set(job["manifest"].get("dataset_ids", ())) - set(json.loads(node["dataset_ids"])):
                        reasons[job["id"]] = "Dataset unavailable on this worker"
                        continue
                    reasons[job["id"]] = "Waiting for a free half-capacity slot"
                    eligible.append(job)
                if not eligible:
                    continue
                # Cap the accounting penalty at five minutes. Queue age then dominates
                # even a continuously busy user. Usage is local to this hardware pool.
                usage = dict(db.execute("""
                    SELECT j.user_id,SUM(a.finished_at-MAX(COALESCE(a.started_at,a.created_at),?))
                      FROM attempts a JOIN jobs j ON j.id=a.job_id JOIN nodes n ON n.id=a.node_id
                     WHERE n.backend=? AND a.finished_at>? GROUP BY j.user_id""",
                    (now-86400, node["backend"], now-86400)))
                free = db.execute("""SELECT s.id FROM slots s LEFT JOIN reservations r
                                    ON r.node_id=s.node_id AND r.slot=s.id WHERE s.node_id=?
                                    AND r.attempt_id IS NULL ORDER BY s.id""", (node["id"],))
                candidates.append((node, {j["id"] for j in eligible}, [slot[0] for slot in free], usage))
            while True:
                # Pick a user's earliest runnable request across the fleet before
                # applying the chosen pool's usage ordering to competing users.
                runnable = {jid for _, ids, slots, _ in candidates if slots for jid in ids}
                heads = {}
                for job in queued:
                    if job["id"] in runnable and job["user_id"] not in active:
                        heads.setdefault(job["user_id"], job)
                for node, ids, slots, usage in candidates:
                    contenders = [j for j in heads.values() if j["id"] in ids]
                    if slots and contenders:
                        job = min(contenders, key=lambda j: (j["queued_at"] + min(300, max(0, usage.get(j["user_id"], 0))),
                                                            j["queued_at"], j["id"]))
                        launched.append(self._reserve(db, job, node, slots.pop(0), now))
                        remaining.remove(job["id"])
                        active.add(job["user_id"])
                        break
                else:
                    break
            for job in queued:
                if job["id"] in remaining:
                    why = denied.get(job["id"]) or ("User already has an active allocation"
                                                    if job["user_id"] in active else reasons[job["id"]])
                    if why != job["waiting_reason"]:
                        db.execute("UPDATE jobs SET waiting_reason=?,updated_at=? WHERE id=?", (why, now, job["id"]))
            return [self._attempt(db, attempt_id) for attempt_id in launched]

    def observe(self, attempt_id, generation, state, *, started_at=None, deadline=None,
                container_id=None, reason=None, cleanup_confirmed=False, metrics=None, metadata=None):
        allowed = {"STARTING", "RUNNING", "STOPPING", "UNKNOWN"} | TERMINAL
        if state not in allowed:
            raise StoreError("INVALID_STATE", f"Unknown attempt state {state}", 422)
        with self.connection(True) as db:
            attempt = self._attempt(db, attempt_id)
            if generation != attempt["generation"]:
                raise StoreError("STALE_GENERATION", "Generation differs from the durable launch")
            if attempt["state"] in TERMINAL:
                # Replayed observations cannot modify a released generation.
                return attempt
            before = attempt["state"]
            if before == "RUNNING" and state == "STARTING" or before == "STOPPING" and state in ("RUNNING", "STARTING"):
                return attempt
            now = self.clock()
            started = attempt["started_at"] if attempt["started_at"] is not None else started_at
            cutoff = attempt["deadline"] if attempt["deadline"] is not None else deadline
            if started is not None:
                if not isinstance(started, (int, float)) or not math.isfinite(started):
                    raise StoreError("INVALID_START", "Invalid process start", 422)
                duration = (int(os.getenv("HUB_BUILD_TIMEOUT_SECONDS", "900")) if attempt["kind"] == "BUILD"
                            else attempt["manifest"].get("runtime_seconds", 3600))
                cutoff = min(cutoff if cutoff is not None else started + duration, started + duration)
                if not math.isfinite(cutoff) or cutoff < started:
                    raise StoreError("INVALID_DEADLINE", "Invalid absolute deadline", 422)
            if state == "RUNNING" and (started is None or cutoff is None):
                raise StoreError("UNCONFIRMED_START", "Report the confirmed process start time", 422)
            if container_id and attempt["container_id"] and container_id != attempt["container_id"]:
                state, reason, cleanup_confirmed = "UNKNOWN", "CONTAINER_IDENTITY_CHANGED", False
                container_id = attempt["container_id"]
            details = {**attempt["metadata"], **(metadata or {})}
            if state in TERMINAL and not cleanup_confirmed:
                details["reported_outcome"] = state
                state, reason = "UNKNOWN", reason or "CLEANUP_UNVERIFIED"
            # Store stop/finish evidence independently from desired state and outcome.
            terminal = state in TERMINAL
            if terminal and attempt["kind"] == "BUILD" and state == "SUCCEEDED":
                if not details.get("image_digest"):
                    raise StoreError("BUILD_DIGEST_REQUIRED", "A completed build needs a digest", 422)
                db.execute("INSERT OR REPLACE INTO environment_builds VALUES(?,?,?,?,?,?)",
                           (attempt["build_key"], attempt["node_id"], attempt_id, details["image_digest"],
                            details.get("cache_path"), now))
            db.execute("""UPDATE attempts SET state=?,started_at=?,deadline=?,container_id=COALESCE(?,container_id),
                          finished_at=?,cleanup_confirmed=?,reason=?,metrics=COALESCE(?,metrics),metadata=?
                          WHERE id=?""",
                       (state, started, cutoff, container_id, now if terminal else None, int(terminal),
                        reason if reason is not None or state != before else attempt["reason"],
                        encode(metrics) if metrics is not None else None, encode(details), attempt_id))
            job_state = state
            if attempt["kind"] == "BUILD":
                if state in ("STARTING", "RUNNING"):
                    job_state = "PREPARING"
                if state == "SUCCEEDED":
                    job_state = "CANCELLED" if attempt["desired_state"] == "STOP" else "QUEUED"
            if terminal:
                db.execute("DELETE FROM reservations WHERE attempt_id=?", (attempt_id,))
            db.execute("UPDATE jobs SET state=?,waiting_reason=?,updated_at=? WHERE id=?",
                       (job_state, "Environment ready; waiting for execution" if job_state == "QUEUED" else None,
                        now, attempt["job_id"]))
            if state != before:
                event(db, attempt["job_id"], attempt_id, state, reason or f"Worker confirmed {state.lower()}",
                      now, {"node_id": attempt["node_id"], "slot": attempt["slot"],
                            "user_id": attempt["user_id"], "cleanup_confirmed": terminal})
            return self._attempt(db, attempt_id)

    def append_log(self, attempt_id, text, seq=None):
        if not text:
            return
        with self.connection(True) as db:
            attempt = self._attempt(db, attempt_id)
            cap = max(0, int(os.getenv("HUB_LOG_BYTES", str(4 << 20))))
            db.execute("INSERT OR IGNORE INTO log_progress(attempt_id) VALUES(?)", (attempt_id,))
            progress = db.execute("SELECT * FROM log_progress WHERE attempt_id=?", (attempt_id,)).fetchone()
            if seq is not None and seq <= progress["last_seq"]:
                return
            if seq is not None and seq != progress["last_seq"]+1:
                raise StoreError("LOG_SEQUENCE_GAP", "Replay the missing worker log sequence")
            size = progress["bytes"]
            raw = text.encode("utf-8")
            chunk = raw[:max(0, cap-size)].decode("utf-8", "ignore")
            now = self.clock()
            if chunk:
                db.execute("INSERT INTO job_logs(attempt_id,job_id,text,at) VALUES(?,?,?,?)",
                           (attempt_id, attempt["job_id"], chunk, now))
            truncated = int(progress["truncated"] or len(raw) > cap-size)
            if truncated and not progress["truncated"]:
                event(db, attempt["job_id"], attempt_id, "LOG_TRUNCATED", "Attempt log limit reached", now)
            db.execute("UPDATE log_progress SET bytes=?,last_seq=?,truncated=? WHERE attempt_id=?",
                       (size+len(chunk.encode("utf-8")), progress["last_seq"] if seq is None else seq,
                        truncated, attempt_id))

    add_log = append_log

    def logs(self, job_id, after=0, limit=201):
        with self.connection() as db:
            return [dict(r) for r in db.execute("SELECT * FROM job_logs WHERE job_id=? AND id>? ORDER BY id LIMIT ?",
                                                (job_id, after, min(2000, max(1, limit))))]

    def events(self, job_id, after=0, limit=201):
        with self.connection() as db:
            return [record(r, "data") for r in db.execute("SELECT * FROM events WHERE job_id=? AND id>? ORDER BY id LIMIT ?",
                                                          (job_id, after, min(2000, max(1, limit))))]
