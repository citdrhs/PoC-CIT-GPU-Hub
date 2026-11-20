"""Trusted example equivalence. Physical runtime limits have separate Docker tests."""
import json
import os
import subprocess
import sys
from pathlib import Path

from packages.contracts import Storage


REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "examples" / "checkpoint_training"


def test_example_resumes_to_identical_step_and_optimizer_state(tmp_path):
    storage = Storage(tmp_path, quotas={"min_free_bytes": 0})
    manifest = json.loads((EXAMPLE/"gpuhub.json").read_bytes())
    revision = storage.put_revision("example", "train.py", (EXAMPLE/"train.py").read_bytes(), manifest)
    identity = dict(job_id="job", revision_id=revision["id"], environment="test-cpython", dataset_ids=[])

    def run(attempt, *, stop=False, resume=None):
        dirs = storage.attempt_paths(attempt)
        if stop:
            (dirs["control"]/"stop.json").write_text("{}")
        env = dict(os.environ, PYTHONPATH=str(REPO/"packages"), GPUHUB_OUTPUT_DIR=str(dirs["outputs"]),
                   GPUHUB_CHECKPOINT_DIR=str(dirs["checkpoints"]), GPUHUB_CONTROL_DIR=str(dirs["control"]))
        if resume:
            env["GPUHUB_RESUME_DIR"] = str(storage.resume_path(resume))
        # Execute only the version-controlled example in a child interpreter.
        subprocess.run([sys.executable, str(EXAMPLE/"train.py"), "--steps", "50", "--delay", "0"],
                       env=env, check=True, timeout=20, capture_output=True)
        return storage.collect(attempt, **identity)

    paused = run("paused", stop=True)
    assert not paused["errors"] and not paused["artifacts"]
    checkpoint, = paused["checkpoints"]
    assert checkpoint["step"] == 1
    resumed = run("resumed", resume=checkpoint["id"])
    baseline = run("baseline")
    a, = resumed["artifacts"]
    b, = baseline["artifacts"]
    assert resumed["errors"] == baseline["errors"] == []
    assert a["sha256"] == b["sha256"]
    assert json.loads(storage.artifact_path("resumed", a["id"]).read_bytes())["step"] == 50
