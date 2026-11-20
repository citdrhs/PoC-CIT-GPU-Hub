"""Install a real wheel offline, from outside the checkout, and resolve runtime assets."""
import os
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile
from unittest.mock import patch

import pytest

from packages.execution_backends.builds import build_spec, publish_build
from packages.execution_backends.docker import CpuContainerBackend
from services.scheduler.store import Store


ROOT = Path(__file__).resolve().parents[2]


def test_installed_wheel_contains_launcher_dashboard_migrations_and_container_runtime(tmp_path, monkeypatch):
    pytest.importorskip("setuptools")
    pytest.importorskip("wheel")
    source, output, installed = (tmp_path/s for s in ("source", "dist", "installed"))
    source.mkdir()
    for name in ("packages", "services", "apps", "deploy"):
        shutil.copytree(ROOT/name, source/name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copyfile(ROOT/"pyproject.toml", source/"pyproject.toml")
    def run(args, directory, **env):
        return subprocess.run([sys.executable, *args], cwd=directory,
                              env={**os.environ, **env, "PIP_DISABLE_PIP_VERSION_CHECK":"1"},
                              check=True, timeout=120, capture_output=True, text=True)
    run(["-m", "pip", "wheel", "--no-index", "--no-deps", "--no-build-isolation",
         "--wheel-dir", str(output), "."], source)
    wheel, = output.glob("*.whl")
    with zipfile.ZipFile(wheel) as archive:
        files = set(archive.namelist())
    assert {"deploy/dev.py", "deploy/examples/Dockerfile", "apps/web/index.html",
            "apps/web/assets/app.js", "services/scheduler/migrations/001_control.sql",
            "services/worker/bootstrap.py", "packages/runner_sdk/__init__.py"} <= files
    run(["-m", "pip", "install", "--no-index", "--no-deps", "--target", str(installed), str(wheel)], tmp_path)
    for module in ("deploy.dev", "services.worker", "apps.cli"):
        assert "usage:" in run(["-m", module, "--help"], tmp_path, PYTHONPATH=str(installed)).stdout.lower()
    run(["-c", "from services.api.app import create_app; "
         "from services.scheduler.store import Store; "
         "assert create_app('smoke.db','data'); assert Store('smoke.db').nodes() == []"],
        tmp_path, PYTHONPATH=str(installed))

    # Exercise the exact offline build recipe with this test-owned wheel. Docker
    # mounts are translated to local test paths; cgroup enforcement is tested on Linux.
    job_source = tmp_path/"job"
    job_source.mkdir()
    (job_source/"requirements.lock").write_text(
        "cit-gpu-hub==0.1.0 --hash=sha256:" + hashlib.sha256(wheel.read_bytes()).hexdigest())
    monkeypatch.setenv("GPUHUB_WHEELHOUSE", str(output))
    monkeypatch.setenv("GPUHUB_BUILD_MAX_BYTES", str(16 << 20))
    store = Store(tmp_path/"build-control.sqlite")
    image_id = "sha256:" + "a"*64
    store.register_node("lab", "Lab", "simulated", {"memory_bytes":16 << 30, "logical_cpus":8,
                        "image_digest":image_id}, 2 << 30, 2)
    manifest = dict(name="Wheel smoke", environment="python-cpu-v1", entrypoint="train.py",
                    dependencies_lock="requirements.lock", runtime_seconds=60)
    store.submit("alex", "rev-1", manifest, "submit-1")
    build, = store.tick()
    assert build["kind"] == "BUILD" and build["envelope"]["gpu_fraction"] is None
    spec = build_spec(tmp_path, build, job_source, image_id, "test")
    backend = object.__new__(CpuContainerBackend)
    backend.image, backend.node, backend.mig = image_id, "lab", None
    paths = {key: tmp_path/key for key in ("outputs", "checkpoints", "control")}
    for path in paths.values():
        path.mkdir()
    def create_args(attempt, **options):
        with patch("packages.execution_backends.docker.invoke") as call:
            backend.create({**attempt, "budget":60}, job_source, paths, {}, None, **options)
        return call.call_args.args
    cmd = create_args(build, build=spec)
    assert "--gpus" not in cmd
    assert any("target=/wheels,readonly" in arg for arg in cmd)
    assert cmd[-len(spec["argv"]):] == tuple(spec["argv"])
    recipe = [arg.replace("--find-links=/wheels", f"--find-links={output}")
              .replace("--target=/build", f"--target={spec['staging']}")
              .replace("/src/requirements.lock", str(job_source/"requirements.lock")) for arg in spec["argv"]]
    run(recipe, tmp_path)
    prepared = publish_build(spec)
    store.observe(build["id"],build["generation"],"SUCCEEDED",cleanup_confirmed=True,metadata=prepared)
    train, = store.tick()
    assert train["kind"] == "RUN" and train["cache_path"] == prepared["cache_path"]
    cmd = create_args(train)
    assert any("target=/deps,readonly" in arg and train["cache_path"] in arg for arg in cmd)
    assert (Path(train["cache_path"])/"packages"/"runner_sdk"/"__init__.py").is_file()
