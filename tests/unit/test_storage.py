import concurrent.futures
import io
import json
import os
import sqlite3
import stat
import zipfile

import pytest

from packages.contracts import ContractError, Storage, validate_manifest
from packages.contracts.retention import prune
from packages.runner_sdk import Runner


MANIFEST = dict(schema_version=1, name="Test", entrypoint="train.py", environment="python-cpu-v1")


def archive(items):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as output:
        for name, body in items:
            output.writestr(name, body)
    return data.getvalue()


@pytest.fixture
def storage(tmp_path):
    return Storage(tmp_path/"data", quotas={"min_free_bytes": 0})


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("owner_id", "admin"), ("args", "arg"), ("entrypoint", "../train.py"),
    ("runtime_seconds", 7201), ("runtime_seconds", False), ("dataset_ids", ["/etc"]),
    ("output_dir", "checkpoints"), ("checkpoint", {"mode": "cooperative", "host_dir": ".."}),
    ("depends_on", "untrusted"), ("entrypoint", "bad\\train.py"),
])
def test_manifest_rejects_invalid_data(field, value):
    with pytest.raises(ContractError):
        validate_manifest({**MANIFEST, field: value})


@pytest.mark.parametrize("extra", [
    ["../file.py"], ["/etc/file.py"], ["C:/file.py"], ["NUL.py"],
    ["TRAIN.py"], ["mod", "mod/child.py"], ["MOD/one", "mod/two"], ["a/../b"],
    ["file", "file:alternate"], ["outputs/old.json"], ["trailing. "],
])
def test_archives_reject_unsafe_or_ambiguous_paths(storage, extra):
    content = archive([(n, b"") for n in ["train.py", *extra]])
    with pytest.raises(ContractError):
        storage.put_revision("test", "source.zip", content, MANIFEST)
    assert not list((storage.root/"revisions").iterdir())


def test_reject_archive_link_and_oversize_source(storage):
    # zipfile normalizes OS-native separators while writing. Preserve a literal
    # backslash in both ZIP filename records to test incoming archives on any OS.
    incoming = archive([("train.py", b""), ("bad/file.py", b"")]).replace(b"bad/file.py", b"bad\\file.py")
    with pytest.raises(ContractError):
        storage.put_revision("project", "upload.zip", incoming, MANIFEST)
    link = zipfile.ZipInfo("train.py")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(ContractError):
        storage.put_revision("project", "upload.zip", archive([(link, "other.py")]), MANIFEST)
    storage.quotas["source_bytes"] = 3
    with pytest.raises(ContractError):
        storage.put_revision("project", "train.py", b"1234", MANIFEST)


def test_immutable_idempotent_snapshots_and_concurrent_project_quota(storage):
    storage.quotas["project_bytes"] = 4
    def upload(data):
        try:
            return storage.put_revision("p", "train.py", data, MANIFEST)
        except ContractError as exc:
            assert exc.code == "STORAGE_QUOTA_EXCEEDED"
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as tasks:
        rows = list(tasks.map(upload, (b"aaaa", b"bbbb")))
    accepted = [r for r in rows if r]
    assert len(accepted) == 1
    file = storage.source_path(accepted[0]["id"])/"train.py"
    snapshot = file.read_bytes()
    assert storage.put_revision("p", "train.py", snapshot, MANIFEST) == accepted[0]
    assert storage.put_revision("different", "train.py", snapshot, MANIFEST)["id"] != accepted[0]["id"]


def setup_run(storage):
    revision = storage.put_revision("proj", "train.py", b"print(1)", MANIFEST)["id"]
    paths = storage.attempt_paths("attempt-1")
    runner = Runner(output_dir=paths["outputs"], checkpoint_dir=paths["checkpoints"], control_dir=paths["control"])
    identity = dict(job_id="job-1", revision_id=revision, environment="sha256:env", dataset_ids=["numbers-v1"])
    return paths, runner, identity


def save_state(path, step):
    (path/"state.json").write_text(json.dumps({"step": step}), encoding="utf8")


def test_publish_verify_and_resume_prior_checkpoint_after_failed_save(storage):
    paths, runner, identity = setup_run(storage)
    runner.register_checkpoint(save_state)
    previously = runner.save(10)
    def faulty(path, step):
        save_state(path, step)
        raise OSError("No space")
    runner.register_checkpoint(faulty)
    with pytest.raises(OSError):
        runner.save(20)
    assert list(paths["checkpoints"].iterdir()) == [previously]
    runner.register_checkpoint(save_state)
    damaged = runner.save(30)
    (damaged/"state.json").write_text("partial")
    (paths["outputs"]/"result.txt").write_text("done")
    result = storage.collect("attempt-1", **identity)
    assert len(result["checkpoints"]) == 1 and result["errors"]
    checkpoint, = result["checkpoints"]
    assert checkpoint["step"] == 10
    with pytest.raises(ContractError, match="environment"):
        storage.resume_path(checkpoint["id"], **{**identity, "environment": "new-image"})
    resume = storage.resume_path(checkpoint["id"], **identity)
    assert json.loads((resume/"state.json").read_bytes()) == {"step": 10}
    artifact, = result["artifacts"]
    assert storage.artifact_path("attempt-1", artifact["id"]).read_text() == "done"
    with pytest.raises(ContractError):
        storage.artifact_path("attempt-2", artifact["id"])
    assert storage.owner_attempt(checkpoint["id"]) == storage.owner_attempt(artifact["id"]) == "attempt-1"
    assert storage.collect("attempt-1", **identity)["checkpoints"] == [checkpoint]
    (resume/"state.json").write_text("corrupt after publication")
    with pytest.raises(ContractError, match="mismatch"):
        storage.resume_path(checkpoint["id"])


def test_soft_stop_checkpoints_at_boundary_and_output_quota(storage):
    paths, runner, identity = setup_run(storage)
    runner.register_checkpoint(save_state)
    assert runner.boundary(1, interval_steps=2)
    (paths["control"]/"stop.json").write_text("{}")
    assert not runner.boundary(2, interval_steps=30)
    storage.quotas["output_bytes"] = 1
    (paths["outputs"]/"out.txt").write_text("over")
    result = storage.collect("attempt-1", **identity)
    assert result["errors"] and not result["artifacts"]
    assert result["checkpoints"][0]["step"] == 2


def test_output_hardlinks_refused(storage, tmp_path):
    paths, _, identity = setup_run(storage)
    outside = tmp_path/"private"
    outside.write_text("retained")
    try:
        os.link(outside, paths["outputs"]/"result")
    except OSError as exc:
        pytest.skip(f"Host does not allow test hardlinks: {exc}")
    result = storage.collect("attempt-1", **identity)
    assert result["errors"] and result["artifacts"] == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX filesystem permissions required")
def test_restrictive_service_umask_still_allows_readonly_job_mounts(storage):
    old = os.umask(0o077)
    try:
        dirs, runner, identity = setup_run(storage)
        runner.register_checkpoint(save_state)
        cp = runner.save(2)
        result = storage.collect("attempt-1", **identity)
    finally:
        os.umask(old)
    resume = storage.resume_path(result["checkpoints"][0]["id"])
    src = storage.source_path(identity["revision_id"])
    for folder in [src, cp, resume]:
        assert folder.stat().st_mode & 0o005 == 0o005
        assert all(p.stat().st_mode & 0o004 for p in folder.iterdir())
    assert not (dirs["outputs"].parent.stat().st_mode & 0o077)


def test_retention_keeps_claimed_attempts_and_published_checkpoints(storage, tmp_path):
    paths, runner, identity = setup_run(storage)
    (paths["outputs"]/"file").write_text("done")
    runner.register_checkpoint(save_state)
    runner.save(4)
    cp, = storage.collect("attempt-1", **identity)["checkpoints"]
    control = tmp_path/"control.sqlite"
    with sqlite3.connect(control) as db:
        db.executescript("""CREATE TABLE attempts(id, finished_at, cleanup_confirmed);
            CREATE TABLE reservations(attempt_id);
            INSERT INTO attempts VALUES ('attempt-1',1,1);
            INSERT INTO reservations VALUES ('attempt-1');""")
    assert not prune(storage, control)["eligible_attempts"]
    with sqlite3.connect(control) as db:
        db.execute("DELETE FROM reservations")
    assert prune(storage, control)["artifacts"] == 1
    assert paths["outputs"].exists()
    assert prune(storage, control, apply=True)["artifact_bytes"] == 4
    assert not paths["outputs"].exists()
    assert not storage.artifacts("attempt-1")
    assert (storage.resume_path(cp["id"])/"state.json").is_file()
