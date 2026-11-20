CREATE TABLE IF NOT EXISTS control_migrations (
    version INTEGER PRIMARY KEY, applied_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, backend TEXT NOT NULL,
    inventory TEXT NOT NULL, reserve_bytes INTEGER, reserved_cpus INTEGER,
    envelope TEXT, fingerprint TEXT, qualification_status TEXT NOT NULL,
    environments TEXT NOT NULL, dataset_ids TEXT NOT NULL,
    heartbeat_at REAL, draining INTEGER NOT NULL DEFAULT 0,
    pressure INTEGER NOT NULL DEFAULT 0, metrics TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS capability_snapshots (
    id INTEGER PRIMARY KEY, node_id TEXT NOT NULL REFERENCES nodes(id),
    fingerprint TEXT, at REAL NOT NULL, evidence TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS slots (
    node_id TEXT NOT NULL REFERENCES nodes(id), id TEXT NOT NULL CHECK(id IN ('A','B')),
    device TEXT, PRIMARY KEY(node_id,id)
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, revision_id TEXT NOT NULL,
    name TEXT NOT NULL, manifest TEXT NOT NULL, profile TEXT NOT NULL,
    checkpoint_id TEXT, state TEXT NOT NULL, desired_state TEXT NOT NULL,
    waiting_reason TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL, queued_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
    id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id),
    node_id TEXT NOT NULL, slot TEXT NOT NULL, generation INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('BUILD','RUN')), state TEXT NOT NULL,
    envelope TEXT NOT NULL, inventory TEXT NOT NULL, build_key TEXT, image_digest TEXT,
    cache_path TEXT, checkpoint_id TEXT, container_id TEXT, created_at REAL NOT NULL,
    started_at REAL, deadline REAL, finished_at REAL, reason TEXT,
    cleanup_confirmed INTEGER NOT NULL DEFAULT 0,
    metadata TEXT NOT NULL DEFAULT '{}', metrics TEXT NOT NULL DEFAULT '{}',
    UNIQUE(job_id,generation), FOREIGN KEY(node_id,slot) REFERENCES slots(node_id,id)
);
-- A row is a live claim AND the durable dispatch record, including uncertain executions.
CREATE TABLE IF NOT EXISTS reservations (
    attempt_id TEXT PRIMARY KEY REFERENCES attempts(id), user_id TEXT NOT NULL UNIQUE,
    node_id TEXT NOT NULL, slot TEXT NOT NULL, UNIQUE(node_id,slot),
    FOREIGN KEY(node_id,slot) REFERENCES slots(node_id,id)
);
CREATE TABLE IF NOT EXISTS environment_builds (
    build_key TEXT PRIMARY KEY, node_id TEXT NOT NULL REFERENCES nodes(id), attempt_id TEXT NOT NULL,
    image_digest TEXT NOT NULL, cache_path TEXT, completed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, attempt_id TEXT, kind TEXT NOT NULL,
    message TEXT NOT NULL, at REAL NOT NULL, data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS control_event_job ON events(job_id,id);
CREATE TABLE IF NOT EXISTS job_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, attempt_id TEXT NOT NULL REFERENCES attempts(id),
    job_id TEXT NOT NULL REFERENCES jobs(id), text TEXT NOT NULL, at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS control_log_job ON job_logs(job_id,id);
CREATE INDEX IF NOT EXISTS control_log_attempt ON job_logs(attempt_id,id);
CREATE TABLE IF NOT EXISTS control_idempotency (
    user_id TEXT NOT NULL, operation TEXT NOT NULL, key TEXT NOT NULL, request_hash TEXT NOT NULL,
    job_id TEXT NOT NULL REFERENCES jobs(id), PRIMARY KEY(user_id,operation,key)
);
CREATE INDEX IF NOT EXISTS scheduler_jobs_queue ON jobs(state,queued_at,id);
CREATE INDEX IF NOT EXISTS scheduler_attempt_job ON attempts(job_id,generation);
CREATE INDEX IF NOT EXISTS scheduler_attempt_usage ON attempts(node_id,finished_at);
INSERT OR IGNORE INTO control_migrations VALUES (1, CAST(strftime('%s','now') AS REAL));
