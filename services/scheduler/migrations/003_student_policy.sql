CREATE TABLE IF NOT EXISTS student_runtime_limits (
    user_id TEXT PRIMARY KEY REFERENCES users(id),
    runtime_seconds INTEGER NOT NULL CHECK(runtime_seconds > 0)
);
INSERT OR IGNORE INTO control_migrations VALUES (3, CAST(strftime('%s','now') AS REAL));
