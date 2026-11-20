import hashlib
from pathlib import Path

import pytest

from packages.execution_backends.builds import build_spec, cached_environment, discard_stage, publish_build


def test_cache_publication_and_lock_contract(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    wh = tmp_path / "wheels"
    wh.mkdir()
    monkeypatch.setenv("GPUHUB_WHEELHOUSE", str(wh))
    monkeypatch.setenv("GPUHUB_BUILD_MAX_BYTES", "1024")
    lock = src / "requirements.lock"
    cmd = {"manifest": {"dependencies_lock": lock.name}}
    lock.write_text("example==1.0 --hash=sha256:" + hashlib.sha256(b"example").hexdigest())
    def prepare():
        return build_spec(tmp_path, cmd, src, "sha256:example", "arm64")

    first, second = prepare(), prepare()
    assert first["staging"] != second["staging"] and first["fingerprint"] == second["fingerprint"]
    assert "--no-index" in first["argv"] and "--require-hashes" in first["argv"]
    for item in (first, second):
        (Path(item["staging"]) / "example.py").write_bytes(b"example")
    result = publish_build(first)
    assert cached_environment(second) == result
    assert publish_build(first) == result  # worker restarted after rename and before journal write
    assert publish_build(second)["cache_path"] == result["cache_path"]
    assert (Path(result["cache_path"]) / "example.py").read_bytes() == b"example"
    lock.write_text("example>=1")
    with pytest.raises(ValueError, match="exactly pinned"):
        prepare()


def test_publication_budget_and_staging_replay(tmp_path, monkeypatch):
    source = tmp_path/"src"
    source.mkdir()
    wheels = tmp_path/"wheels"
    wheels.mkdir()
    monkeypatch.setenv("GPUHUB_WHEELHOUSE", str(wheels))
    monkeypatch.setenv("GPUHUB_CACHE_BYTES", "4")
    lock = source/"requirements.lock"
    def task(attempt, version):
        lock.write_text(f"example=={version} --hash=sha256:" + "a"*64)
        row = {"id":attempt, "generation":1, "manifest":{"dependencies_lock":lock.name}}
        return build_spec(tmp_path, row, source, "sha256:image", "arm64")
    a, replay = task("first", 1), task("first", 1)
    assert a["staging"] == replay["staging"]
    (Path(a["staging"])/"example.py").write_bytes(b"one")
    result = publish_build(a)
    b = task("second", 2)
    (Path(b["staging"])/"example.py").write_bytes(b"two")
    with pytest.raises(ValueError, match="cache quota"):
        publish_build(b)
    discard_stage(b, tmp_path)
    assert not Path(b["staging"]).exists()
    assert (Path(result["cache_path"])/"example.py").read_bytes() == b"one"
