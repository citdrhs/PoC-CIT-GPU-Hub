"""Operator opt-in. Tests create disposable, bounded CPU containers on an idle Linux host."""
import json
import os
import platform
import tempfile
import time
import unittest
from pathlib import Path

from packages.execution_backends.docker import CpuContainerBackend, DellMigBackend
from packages.observability import probe
from services.scheduler.store import Store
from services.worker.agent import Worker


@unittest.skipUnless(platform.system() == "Linux" and os.getenv("GPUHUB_TEST_DOCKER") == "1",
                     "requires operator opt-in GPUHUB_TEST_DOCKER=1 on an idle native Linux Docker cgroup v2 host")
class NativeResourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = Store(root/"hub.db")
        inv = probe()
        self.backend = CpuContainerBackend("ci-native", os.getenv("GPUHUB_IMAGE", "cit-gpu-hub:local"), inv)
        inv.update(image_digest=self.backend.image, execution_mode="docker")
        self.store.register_node("ci-native", "CPU integration test", "simulated", inv,
                                 inv["memory_bytes"]-(256 << 20), reserved_cpus=inv["logical_cpus"]-1,
                                 environments=["python-cpu-v1"])
        self.worker = Worker(self.store, root/"data", "ci-native", self.backend)

    def tearDown(self):
        self.worker.journal.close()
        self.temp.cleanup()

    def execute(self, script):
        manifest = {"schema_version": 1, "name": "cgroup-test", "entrypoint": "train.py",
                    "environment": "python-cpu-v1", "runtime_seconds": 60}
        rev = self.worker.storage.put_revision("test-project", "train.py", script.encode(), manifest)
        job = self.store.submit("test-operator", rev["id"], rev["manifest"], "one")
        attempt = self.store.tick()[0]["id"]
        end = time.monotonic()+80
        while time.monotonic() < end:
            self.worker.tick()
            state = self.store.job(job["id"])["state"]
            if state in {"SUCCEEDED", "FAILED", "TIMED_OUT"}:
                return attempt, state
            time.sleep(.2)
        self.fail("Worker did not confirm termination within the bounded test window")

    def test_parent_and_child_share_cpu_quota(self):
        script = """import json, os, pathlib, subprocess, sys, time
c = pathlib.Path('/sys/fs/cgroup')
p = subprocess.Popen([sys.executable, '-c', 'while True: pass'])
try:
    end = time.monotonic()+3
    while time.monotonic() < end: pass
finally:
    p.terminate()
    p.wait()
out = {name: (c/name).read_text() for name in ['cpu.max','cpu.stat','memory.max','memory.swap.max','pids.max']}
(pathlib.Path(os.environ['GPUHUB_OUTPUT_DIR'])/'limits.json').write_text(json.dumps(out))
"""
        attempt, state = self.execute(script)
        self.assertEqual(state, "SUCCEEDED")
        art = self.worker.storage.artifacts(attempt)[0]
        data = json.loads(self.worker.storage.artifact_path(attempt, art["id"]).read_text())
        self.assertEqual(data["cpu.max"].strip(), "50000 100000")
        self.assertGreater(int(dict(line.split() for line in data["cpu.stat"].splitlines())["nr_throttled"]), 0)
        self.assertEqual(int(data["memory.max"]), 128 << 20)
        self.assertEqual(int(data["memory.swap.max"]), 0)
        self.assertEqual(int(data["pids.max"]), 256)

    def test_descendant_allocation_remains_contained(self):
        script = """import subprocess, sys
child = subprocess.run([sys.executable, '-c', 'b=bytearray(190*1024**2)'])
raise SystemExit(0 if child.returncode == -9 else 1)
"""
        attempt, state = self.execute(script)
        evidence = self.store.attempt(attempt)
        self.assertTrue(evidence["cleanup_confirmed"])
        self.assertTrue(state == "SUCCEEDED" or evidence["reason"] == "HOST_MEMORY_LIMIT",
                        "Expected kernel containment of the isolated child allocation")


@unittest.skipUnless(os.getenv("GPUHUB_TEST_DELL_EVIDENCE"), "requires an operator-qualified idle Dell with evidence and NVML")
class DellReadbackTests(unittest.TestCase):
    def test_current_layout_and_empty_assigned_instances(self):
        from packages.execution_backends.builds import RECIPE
        from packages.observability import fingerprint
        inv = probe()
        backend = CpuContainerBackend("ci-dell", os.environ["GPUHUB_IMAGE"], inv)
        inv["fingerprint"] = fingerprint([inv["fingerprint"], backend.image, RECIPE,
                                         int(os.environ["GPUHUB_RESERVE_BYTES"]), int(os.environ["GPUHUB_RESERVE_CPUS"])])
        evidence = json.loads(Path(os.environ["GPUHUB_TEST_DELL_EVIDENCE"]).read_text())
        mig = DellMigBackend(evidence)
        mig.validate_profile(inv)
        self.assertTrue(all(not mig.processes(v) for v in evidence["devices"].values()))


class SparkQualificationTests(unittest.TestCase):
    @unittest.skip("GB10 aggregate GPU and unified-memory enforcement and MPS teardown are unqualified; no adapter enabled")
    def test_physical_spark_qualification(self):
        pass
