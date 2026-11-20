import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

from packages.contracts import Storage
from packages.contracts.manifest import ContractError, identifier
from packages.execution_backends.docker import AdmissionError, RuntimeUnavailable, name, timestamp
from packages.observability import memory, read
from packages.observability.storage import sample_tree
from .journal import Journal


def phase_limits(runtime):
    term = min(15, runtime // 4)
    checkpoint = min(60, runtime // 3)
    return checkpoint, term


def now_boot():
    return read("/proc/sys/kernel/random/boot_id") or "local"


def remain(row, wall, monotonic):
    left = row["deadline"] - wall
    if row["boot"] == now_boot():
        left = min(left, row["monotonic_deadline"] - monotonic)
    else:
        # A reboot cannot restore a prior monotonic deadline or award new time.
        left = min(left, 0)
    return left


class Worker:
    def __init__(self, store, root, node, backend, demo_seconds=180):
        self.store, self.node, self.backend = store, identifier(node), backend
        self.storage = Storage(root)
        journal_path = Path(root) / "workers" / self.node / "journal.db"
        first_seen = not journal_path.exists()
        self.journal = Journal(journal_path)
        self.unjournaled = {a["id"] for a in store.commands(self.node)} if first_seen else set()
        self.demo_seconds = demo_seconds
        self.orphans = set()

    def ingest(self, a):
        existing = self.journal.row(a["id"])
        if existing:
            if existing["a"]["generation"] != a["generation"]:
                raise RuntimeUnavailable("Generation mismatch for immutable attempt ID")
            if a["desired_state"] == "STOP" and not existing.get("stop_reason"):
                self.stop(existing, "USER_CANCELLED")
            if a["state"] == "UNKNOWN":
                existing["reported"] = {}
                self.journal.save(existing)
            return
        a = dict(a)
        a["budget"] = (int(os.environ.get("HUB_BUILD_TIMEOUT_SECONDS", "900")) if a["kind"] == "BUILD"
                       else a["manifest"]["runtime_seconds"])
        requested = a["desired_state"] == "STOP"
        row = dict(a=a, stage="INTENT", metadata={}, log_bytes=0, pending_logs=[],
                   observed={}, reported={}, mode="docker" if self.backend else "fixture")
        if (a["id"] in self.unjournaled or a["state"] != "STARTING" or a.get("started_at")
                or a.get("container_id") or name(a) in self.orphans):
            row["stage"] = "QUARANTINED"
            self.observe(row, "UNKNOWN", reason="MISSING_LAUNCH_JOURNAL", metadata={"cleanup_confirmed": False})
            return
        if requested:
            self.stop(row, "USER_CANCELLED")
        self.journal.save(row)

    def stop(self, row, reason):
        if not row.get("stop_reason"):
            row["stop_reason"] = reason
            row["metadata"].update(stop_reason=reason, requested_at=time.time())
            self.journal.save(row)

    def observe(self, row, state, **kwargs):
        row["observed"] = {"state": state, **kwargs}
        self.journal.save(row)

    def flush(self, row):
        a = row["a"]
        # Keep a bounded durable backlog across controller outages.
        for _ in range(min(12, len(row["pending_logs"]))):
            seq = row.get("flushed_count", 0) + 1
            self.store.append_log(a["id"], row["pending_logs"][0], seq=seq)
            row["flushed_count"] = seq
            row["pending_logs"].pop(0)
            self.journal.save(row)
        if row["stage"] == "DONE" and row["pending_logs"]:
            return
        if row["observed"] and row["observed"] != row["reported"]:
            self.store.observe(a["id"], a["generation"], **row["observed"])
            row["reported"] = row["observed"]
            if row["stage"] == "DONE":
                row["stage"] = "REPORTED"
            self.journal.save(row)

    def log(self, row, msg):
        remaining = self.storage.quotas["log_bytes"] - row["log_bytes"]
        if remaining > 0 and msg:
            encoded = msg.encode("utf-8")[:remaining]
            row["log_bytes"] += len(encoded) + 1
            row["pending_logs"].append(encoded.decode("utf8", errors="ignore"))

    def start_clock(self, row, started):
        if row.get("deadline") is None:
            row["start"] = started
            row["deadline"] = started + row["a"]["budget"]
            row["boot"] = now_boot()
            row["monotonic_deadline"] = time.monotonic() + min(row["a"]["budget"], row["deadline"]-time.time())
            row["stage"] = "ACTIVE"
            self.journal.save(row)

    def finish(self, row, state, reason):
        if row.get("build"):
            from packages.execution_backends.builds import discard_stage
            try:
                discard_stage(row["build"], self.storage.root)
            except (ValueError, OSError) as exc:
                self.log(row, f"Staging cleanup requires operator attention: {exc}")
        row["stage"] = "DONE"
        row["metadata"]["cleanup_confirmed"] = True
        row["metadata"]["progress_may_be_lost"] = state != "SUCCEEDED"
        self.observe(row, state, reason=reason, cleanup_confirmed=True, metadata=row["metadata"],
                     started_at=row.get("start"), deadline=row.get("deadline"))

    def collect(self, row):
        a = row["a"]
        if a["kind"] == "BUILD":
            return
        result = self.storage.collect(a["id"], job_id=a["job_id"], revision_id=a["revision_id"],
                                      environment=self.environment_reference(a),
                                      dataset_ids=a["manifest"].get("dataset_ids", []))
        row["metadata"]["collection_errors"] = result["errors"]
        checkpoints = result["checkpoints"]
        row["metadata"]["last_completed_checkpoint_id"] = checkpoints[0]["id"] if checkpoints else None

    def environment_reference(self, a):
        base = a.get("image_digest") or (self.backend.image if self.backend else "simulated-fixture-v1")
        return f"{base}#wheels={Path(a['cache_path']).name}" if a.get("cache_path") else base

    def resume(self, a):
        if a["kind"] == "BUILD" or not a.get("checkpoint_id"):
            return None
        return self.storage.resume_path(a["checkpoint_id"], job_id=a["job_id"], revision_id=a["revision_id"],
                                        dataset_ids=a["manifest"].get("dataset_ids", []),
                                        environment=self.environment_reference(a))

    def fixture(self, row):
        a, now = row["a"], time.time()
        if row.get("stop_reason") and row["stage"] == "INTENT":
            return self.finish(row, "CANCELLED", row["stop_reason"])
        if row["stage"] == "INTENT":
            try:
                self.resume(a)
            except (ContractError, OSError) as exc:
                self.log(row, f"Resume validation failed: {exc}")
                return self.finish(row, "FAILED", "CHECKPOINT_INCOMPATIBLE")
            self.start_clock(row, now)
            self.log(row, "[simulation] Reserved one fixed half-slot. Uploaded source is not executed.")
            self.observe(row, "RUNNING", started_at=now, deadline=row["deadline"], container_id=None,
                         metadata={"execution_mode": "fixture"}, metrics=self.fixture_metrics(row, now))
            return
        remaining = remain(row, now, time.monotonic())
        checkpoint, term = phase_limits(a["budget"])
        if remaining <= checkpoint + term:
            self.stop(row, "TIME_LIMIT")
        elapsed = max(0, now-row["start"])
        if not row.get("stop_reason") and elapsed < (1 if a["kind"] == "BUILD" else self.demo_seconds):
            period = int(elapsed // 15)
            if row.get("period") != period:
                self.log(row, f"[simulation] Step {period + 1:03}. Fixed half-slot remains assigned.")
                row["period"] = period
                self.observe(row, "RUNNING", started_at=row["start"], deadline=row["deadline"],
                             metrics=self.fixture_metrics(row, now), metadata={"execution_mode": "fixture"})
            return
        paths = self.storage.attempt_paths(a["id"])
        if a["kind"] == "BUILD":
            row["metadata"]["image_digest"] = "simulated-build:" + a["revision_id"]
        else:
            data = {"execution_mode": "fixture", "job_id": a["job_id"], "attempt_id": a["id"],
                    "message": "Synthetic result generated by the demonstration worker."}
            (paths["outputs"]/"simulation.json").write_text(json.dumps(data, indent=2)+"\n", encoding="utf8")
            self.collect(row)
        reason = row.get("stop_reason") or "SUCCEEDED"
        self.log(row, f"[simulation] Fixture stopped: {reason}.")
        self.finish(row, {"USER_CANCELLED": "CANCELLED", "TIME_LIMIT": "TIMED_OUT"}.get(reason, "SUCCEEDED"), reason)

    @staticmethod
    def fixture_metrics(row, now):
        number = int(max(0, now-row["start"]) // 15)
        return {"scope": "attempt", "status": "SIMULATED", "observed_at": now, "memory_bytes": None,
                "gpu_activity": 62+(number % 7)*4 if row["a"]["envelope"].get("gpu_fraction") else None,
                "gpu_activity_status": "SIMULATED", "gpu_activity_scope": "simulated slot",
                "interval_seconds": 15, "execution_mode": "fixture"}

    def stop_signal(self, row, sig):
        key = sig.lower() + "_at"
        if not row["metadata"].get(key):
            self.backend.signal(row["a"], sig)
            row["metadata"][key] = time.time()
            self.journal.save(row)

    def ingest_logs(self, row):
        if row["log_bytes"] >= self.storage.quotas["log_bytes"]:
            return
        cursor = row.get("log_cursor")
        duplicate_count = row.get("cursor_count", 0)
        lines = self.backend.logs(row["a"], cursor)
        if cursor and lines and not any(line.partition(" ")[0] == cursor for line in lines):
            self.log(row, "[worker] Docker's retained log window advanced. Older output may be unavailable.")
        block, block_bytes = [], 0
        for line in lines:
            stamp, _, message = line.partition(" ")
            if cursor and stamp == cursor and duplicate_count:
                duplicate_count -= 1
                continue
            if not stamp.endswith("Z"):
                continue
            block.append(message)
            block_bytes += len(message.encode("utf-8")) + 1
            if block_bytes >= 8192:
                self.log(row, "\n".join(block))
                block, block_bytes = [], 0
            if len(message) < 4096 and message.startswith("{"):
                try:
                    report = json.loads(message)
                except (ValueError, RecursionError):
                    report = {}
                if isinstance(report, dict) and report.get("gpu_hub_event") == "stop_acknowledged":
                    row["metadata"]["application_acknowledged_at"] = stamp
                if isinstance(report, dict) and report.get("gpu_hub_event") == "application_allocation_error":
                    # An application report is useful context but cannot prove a GPU or cgroup OOM.
                    row["metadata"]["application_allocation_error_at"] = stamp
            if stamp == row.get("log_cursor"):
                row["cursor_count"] += 1
            else:
                row["log_cursor"], row["cursor_count"] = stamp, 1
        if block:
            self.log(row, "\n".join(block))
        self.journal.save(row)

    def storage_pressure(self, row):
        now = time.time()
        if now - row.get("disk_observed_at", 0) < 3:
            return
        row["disk_observed_at"] = now
        paths = self.storage.attempt_paths(row["a"]["id"])
        quotas = self.storage.quotas
        limits = ([(Path(row["build"]["staging"]), row["build"]["max_bytes"], 65536)] if row.get("build") else
                  [(paths["outputs"], quotas["output_bytes"], 2048),
                   (paths["checkpoints"], quotas["checkpoint_bytes"], max(1, quotas["checkpoint_count"])*2048)])
        measurements = [sample_tree(path, byte_limit, files) for path, byte_limit, files in limits]
        free = shutil.disk_usage(self.storage.root).free
        row["metadata"]["storage"] = {"observed_at": now, "samples": measurements, "free_bytes": free,
                                      "enforcement": "polling"}
        if free < quotas["min_free_bytes"] or any(sample["over_limit"] for sample in measurements):
            self.stop(row, "RESOURCE_PRESSURE_STOP")

    def create(self, row):
        a = row["a"]
        paths = self.storage.attempt_paths(a["id"])
        self.storage.ensure_headroom()
        source = self.storage.source_path(a["revision_id"])
        for key in ("outputs", "checkpoints"):
            paths[key].chmod(0o777)  # isolated leaf; attempt parent is private
        paths["control"].chmod(0o755)
        datasets = {identifier(k): self.storage.root/"datasets"/k for k in a["manifest"]["dataset_ids"]}
        if any(not path.is_dir() for path in datasets.values()):
            raise AdmissionError("Approved dataset has not been staged on this node")
        resume = self.resume(a)
        build = None
        if a.get("inventory", {}).get("image_digest") not in (None, self.backend.image):
            raise AdmissionError("Curated runtime image changed since reservation")
        if a["kind"] == "BUILD":
            from packages.execution_backends.builds import build_spec, cached_environment
            build = row.get("build") or build_spec(
                self.storage.root, a, source, self.backend.image, self.backend.inventory["architecture"])
            row["build"] = build
            self.journal.save(row)
            cached = cached_environment(build)
            if cached:
                row["metadata"].update(cached, cache_hit=True)
                self.finish(row, "SUCCEEDED", "ENVIRONMENT_CACHE_HIT")
                return
        self.backend.create(a, source, paths, datasets, resume, build=build)

    def docker(self, row):
        a = row["a"]
        if row["stage"] == "COLLECTED":
            box = self.backend.inspect(a)
            if box:
                if not self.backend.released(a, box):
                    raise RuntimeUnavailable("Container cleanup is unconfirmed")
                self.backend.remove(a)
            self.finish(row, row["result"], row["reason"])
            return
        box = self.backend.inspect(a)
        if box is None:
            if row["stage"] != "INTENT":
                raise RuntimeUnavailable("Journal container disappeared before confirmed exit")
            if row.get("stop_reason"):
                return self.finish(row, "CANCELLED", row["stop_reason"])
            try:
                self.create(row)
            except (AdmissionError, ContractError, FileNotFoundError, ValueError) as exc:
                self.log(row, f"Launch refused: {exc}")
                return self.finish(row, "FAILED", "LAUNCH_FAILED")
            if row["stage"] == "DONE":
                return  # A verified dependency environment was already published.
            box = self.backend.inspect(a)
            if not box:
                raise RuntimeUnavailable("Container creation could not be confirmed")
        container = box["State"]
        state = container["Status"]
        if state == "created" and not row.get("stop_reason"):
            row["stage"] = "LAUNCH_SENT"
            self.observe(row, "STARTING", container_id=box["Id"])
            self.backend.start(a)
            return
        if state == "running":
            self.start_clock(row, timestamp(container["StartedAt"]))
            remaining = remain(row, time.time(), time.monotonic())
            grace, term = phase_limits(a["budget"])
            if remaining <= grace+term:
                self.stop(row, "TIME_LIMIT")
            # Check the deadline before potentially slower metrics/collection operations.
            if remaining <= 0:
                self.stop_signal(row, "KILL")
                return
            self.storage_pressure(row)
            metrics = self.backend.metrics(box)
            pools = [("host", metrics.get("memory_bytes"), a["envelope"]["memory_bytes"])]
            if "gpu_memory" in metrics:
                gpu = metrics["gpu_memory"]
                pools.append(("MIG", gpu["used_bytes"], gpu["total_bytes"]))
            for pool, usage, limit in pools:
                if usage is None or not limit:
                    continue
                warning = a["envelope"].get("warning_percent", 85)
                key = pool + "_memory_warning_at"
                if usage * 100 >= warning * limit and not row["metadata"].get(key):
                    row["metadata"][key] = time.time()
                    self.log(row, f"{pool} memory reached the {warning}% warning threshold.")
                if usage * 100 >= a["envelope"].get("stop_percent", 90) * limit:
                    self.stop(row, "RESOURCE_PRESSURE_STOP")
            if row.get("stop_reason"):
                stop_at = row["metadata"]["requested_at"]
                metadata = row["metadata"]
                metadata.setdefault("grace_deadline", min(row["deadline"]-term, stop_at+grace))
                control = self.storage.attempt_paths(a["id"])["control"]
                temp = control/"stop.tmp"
                temp.write_text(json.dumps({"reason": row["stop_reason"], "deadline": metadata["grace_deadline"]}))
                temp.chmod(0o644)
                temp.replace(control/"stop.json")
                if min(remaining, max(0, stop_at+grace+term - time.time())) <= 0:
                    self.stop_signal(row, "KILL")
                elif time.time() >= metadata["grace_deadline"] or remaining <= term:
                    self.stop_signal(row, "TERM")
            self.ingest_logs(row)
            self.observe(row, "STOPPING" if row.get("stop_reason") else "RUNNING", started_at=row["start"],
                         deadline=row["deadline"], container_id=box["Id"], metrics=metrics, metadata=row["metadata"])
            return
        if state not in {"created", "exited"} or not self.backend.released(a, box):
            raise RuntimeUnavailable(f"Cleanup not established ({state})")
        if state == "exited":
            self.start_clock(row, timestamp(container["StartedAt"]))
            self.ingest_logs(row)
        row["metadata"].update(exit_code=container.get("ExitCode"), oom_killed=container.get("OOMKilled"),
                               execution_mode="docker", finished_at=container.get("FinishedAt"))
        reason = row.get("stop_reason")
        if container.get("OOMKilled"):
            reason = "HOST_MEMORY_LIMIT"
        if not reason and row.get("deadline") and timestamp(container["FinishedAt"]) >= row["deadline"]:
            reason = "TIME_LIMIT"
        if not reason:
            reason = "SUCCEEDED" if container.get("ExitCode") == 0 else "APPLICATION_ERROR"
        if a["kind"] == "BUILD" and reason == "SUCCEEDED":
            from packages.execution_backends.builds import publish_build
            try:
                row["metadata"].update(publish_build(row["build"]))
            except (OSError, ValueError) as exc:
                self.log(row, f"Build collection failed: {exc}")
                reason = "ENVIRONMENT_BUILD_FAILED"
        self.collect(row)
        if row["metadata"].get("collection_errors") and reason == "SUCCEEDED":
            reason = "OUTPUT_COLLECTION_FAILED"
        row["reason"] = reason
        row["result"] = {"SUCCEEDED": "SUCCEEDED", "USER_CANCELLED": "CANCELLED",
                         "TIME_LIMIT": "TIMED_OUT"}.get(reason, "FAILED")
        row["stage"] = "COLLECTED"
        self.journal.save(row)

    def tick(self):
        rows = self.journal.active()
        # Compare only this node's managed Docker names. Never touch an unknown container.
        if self.backend:
            try:
                self.orphans = self.backend.owned() - {name(row["a"]) for row in rows}
            except RuntimeUnavailable as exc:
                self.orphans = {str(exc)}
        try:
            mem = memory()
            reserve = getattr(self, "reserve", 0)
            pressure = bool(self.orphans) or (bool(self.backend) and mem.get("MemAvailable", 0) < reserve)
            self.store.heartbeat(self.node, pressure=pressure, metrics={
                "observed_at": time.time(), "scope": "node", "status": "MEASURED" if self.backend else "SIMULATED",
                "memory_available_bytes": mem.get("MemAvailable") if self.backend else None,
                "reconciliation_required": bool(self.orphans)})
            for a in self.store.commands(self.node):
                if not self.orphans or self.journal.row(a["id"]) or name(a) in self.orphans:
                    self.ingest(a)
        except (sqlite3.Error, OSError) as exc:
            print(f"Controller unavailable: {exc}", file=sys.stderr)
        for row in self.journal.active():
            if row["stage"] not in {"DONE", "QUARANTINED"}:
                try:
                    if row["mode"] != ("docker" if self.backend else "fixture"):
                        raise RuntimeUnavailable("Journal execution backend changed")
                    if self.backend:
                        self.docker(row)
                    else:
                        self.fixture(row)
                except (RuntimeUnavailable, OSError, ValueError, sqlite3.Error) as exc:
                    self.observe(row, "UNKNOWN", reason=str(exc)[:500], metadata={"cleanup_confirmed": False})
            try:
                self.flush(row)
            except (OSError, sqlite3.Error) as exc:
                print(f"Controller observation pending: {exc}", file=sys.stderr)
