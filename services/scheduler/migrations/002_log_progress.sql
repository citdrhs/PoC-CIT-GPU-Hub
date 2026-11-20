CREATE TABLE IF NOT EXISTS log_progress (
    attempt_id TEXT PRIMARY KEY REFERENCES attempts(id), last_seq INTEGER NOT NULL DEFAULT 0,
    bytes INTEGER NOT NULL DEFAULT 0, truncated INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO log_progress(attempt_id,bytes)
SELECT attempt_id, SUM(length(CAST(text AS BLOB))) FROM job_logs GROUP BY attempt_id;
INSERT OR IGNORE INTO control_migrations VALUES (2, CAST(strftime('%s','now') AS REAL));
