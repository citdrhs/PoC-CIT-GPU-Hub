import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from packages.execution_backends.docker import AdmissionError, CpuContainerBackend, DellMigBackend, SparkFixedHalfBackend
from services.scheduler.store import Store
from services.worker.__main__ import demo_inventory
from services.worker.agent import Worker, remain


class WorkerLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root/"db")
        self.store.register_node("sim", "Sim", "simulated", demo_inventory(), 16 << 30, reserved_cpus=4,
                                 environments=["python-cpu-v1"])
        self.worker = Worker(self.store, self.root/"data", "sim", None, demo_seconds=60)
        self.manifest = {"name": "fixture", "schema_version": 1, "entrypoint": "train.py",
                         "runtime_seconds": 30, "environment": "python-cpu-v1"}
        self.revision = self.worker.storage.put_revision("project-A", "train.py",
                              b'raise RuntimeError("fixture must not execute uploaded source")', self.manifest)

    def tearDown(self):
        self.worker.journal.close()
        self.tmp.cleanup()

    def submit(self):
        job = self.store.submit("alex", self.revision["id"], self.revision["manifest"], "first")
        attempt = self.store.tick()[0]
        return job["id"], attempt["id"]

    def test_restart_adopts_single_attempt_with_original_deadline_and_cancels(self):
        job, attempt = self.submit()
        self.worker.tick()
        initial = self.store.attempt(attempt)
        self.assertEqual(initial["state"], "RUNNING")
        self.worker.journal.close()
        self.worker = Worker(Store(self.root/"db"), self.root/"data", "sim", None)
        self.worker.tick()
        self.assertEqual(self.store.attempt(attempt)["deadline"], initial["deadline"])
        self.assertEqual(len(self.store.job(job)["attempts"]), 1)
        self.store.cancel(job)
        self.worker.tick()
        terminal = self.store.attempt(attempt)
        self.assertEqual(terminal["state"], "CANCELLED")
        self.assertTrue(terminal["cleanup_confirmed"])
        self.assertEqual(self.store.commands("sim"), [])
        artifacts = self.worker.storage.artifacts(attempt)
        path = self.worker.storage.artifact_path(attempt, artifacts[0]["id"])
        self.assertEqual(json.loads(path.read_text())["execution_mode"], "fixture")

    def test_cancel_before_start(self):
        job, attempt = self.submit()
        self.store.cancel(job)
        self.worker.tick()
        self.assertEqual(self.store.attempt(attempt)["state"], "CANCELLED")
        self.assertIsNone(self.store.attempt(attempt)["started_at"])

    def test_local_timeout_and_terminal_replay_during_controller_outage(self):
        job, attempt = self.submit()
        self.worker.tick()
        row = self.worker.journal.row(attempt)
        row["monotonic_deadline"] = time.monotonic()-1
        self.worker.journal.save(row)
        with patch.object(self.store, "heartbeat", side_effect=sqlite3.OperationalError("offline")), \
             patch.object(self.store, "append_log", side_effect=sqlite3.OperationalError("offline")):
            self.worker.tick()
            self.assertEqual(self.worker.journal.row(attempt)["stage"], "DONE")
            self.assertEqual(self.store.job(job)["state"], "RUNNING")
        self.worker.tick()
        self.assertEqual(self.store.job(job)["state"], "TIMED_OUT")
        self.assertEqual(self.worker.journal.row(attempt)["stage"], "REPORTED")

    def test_clock_rollback_does_not_replenish_budget(self):
        row = {"deadline": 1500, "monotonic_deadline": 12, "boot": "boot-1"}
        with patch("services.worker.agent.now_boot", return_value="boot-1"):
            self.assertEqual(remain(row, 100, 9), 3)
        with patch("services.worker.agent.now_boot", return_value="another-boot"):
            self.assertLessEqual(remain(row, 100, 9), 0)

    def test_missing_journal_quarantines_old_dispatch_after_restart(self):
        _, attempt = self.submit()
        self.worker.tick()
        self.worker.journal.close()
        # Simulate newly attached storage with no record of the previous worker.
        self.worker = Worker(self.store, self.root/"replacement-volume", "sim", None)
        self.worker.tick()
        reported = self.store.attempt(attempt)
        self.assertEqual(reported["state"], "UNKNOWN")
        self.assertEqual(reported["reason"], "MISSING_LAUNCH_JOURNAL")
        self.assertEqual(len(self.store.commands("sim")), 1)
        self.assertFalse(reported["cleanup_confirmed"])

    def test_refuse_unqualified_gpu(self):
        with self.assertRaises(AdmissionError):
            SparkFixedHalfBackend().validate_profile({"mps_percentage": 50})
        with self.assertRaises(AdmissionError):
            DellMigBackend({"fingerprint": "old"}).validate_profile({"fingerprint": "current"})

    def test_docker_command_applies_host_envelope_and_runs_nonroot(self):
        _, aid = self.submit()
        a = self.store.attempt(aid)
        a["budget"] = 30
        source = self.worker.storage.source_path(a["revision_id"])
        paths = self.worker.storage.attempt_paths(aid)
        backend = object.__new__(CpuContainerBackend)
        backend.image, backend.node, backend.mig = "sha256:"+"7"*64, "sim", None
        with patch("packages.execution_backends.docker.invoke") as call:
            backend.create(a, source, paths, {}, None)
        args = call.call_args.args
        def flag(name):
            return args[args.index(name)+1]
        self.assertEqual(flag("--memory"), str(56 << 30))
        self.assertEqual(flag("--memory-swap"), flag("--memory"))
        self.assertEqual(flag("--cpu-quota"), "800000")
        self.assertEqual(flag("--user"), "65532:65532")
        self.assertEqual(flag("--runtime"), "runc")
        self.assertEqual(flag("--network"), "none")
        self.assertEqual(flag("--cap-drop"), "ALL")
        self.assertEqual(flag("--cgroupns"), "private")
        self.assertIn("--read-only", args)
        self.assertNotIn("--gpus", args)
        self.assertIn("NVIDIA_VISIBLE_DEVICES=void", args)
        self.assertIn("readonly", next(x for x in args if "target=/source" in x))
        self.assertEqual(args[-5:], (backend.image, "-I", "-S", "/opt/bootstrap.py", "/source/train.py"))

    def test_uncertain_cleanup_holds_claim_and_cancel_during_create_never_starts(self):
        job, aid = self.submit()
        mock_backend = Mock()
        mock_backend.owned.return_value = set()
        mock_backend.image = "simulated-fixture-v1"
        self.worker.backend = mock_backend
        self.worker.ingest(self.store.attempt(aid))
        self.store.cancel(job)
        box = {"Id":"immutable-id", "State":{"Status":"created", "Pid":0, "ExitCode":0, "OOMKilled":False}}
        mock_backend.inspect.return_value = box
        mock_backend.released.return_value = False
        self.worker.tick()
        self.assertEqual(self.store.attempt(aid)["state"], "UNKNOWN")
        self.assertEqual(len(self.store.commands("sim")), 1)
        mock_backend.start.assert_not_called()
        mock_backend.remove.assert_not_called()
        mock_backend.released.return_value = True
        self.worker.tick()
        self.worker.tick()
        self.assertEqual(self.store.attempt(aid)["state"], "CANCELLED")
        mock_backend.start.assert_not_called()
        mock_backend.remove.assert_called_once()
