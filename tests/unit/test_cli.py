import base64
import hashlib
import io
import json
from pathlib import Path
import zipfile

import pytest

from apps.cli.client import Client, ClientError
from apps.cli.display import Screen
from apps.cli.files import download, make_upload, revision_payload
from apps.cli.main import logs, options, submit_job


def test_local_dev_identity_is_loopback_only():
    with pytest.raises(ClientError, match="loopback"):
        Client("https://example.org").login_dev("teacher")
    with pytest.raises(ClientError, match="HTTPS"):
        Client("http://example.org")


def test_directory_submission_reads_manifest_and_ignores_private_files(tmp_path):
    (tmp_path / "gpuhub.json").write_text(json.dumps({"entrypoint": "run.py", "args": ["--quiet"]}))
    (tmp_path / "run.py").write_text("print(1)")
    (tmp_path / ".env").write_text("TEST")
    args = options().parse_args(["submit", str(tmp_path), "--runtime", "180", "--name", "Training"])
    upload = revision_payload(args, {})
    assert upload["manifest"]["entrypoint"] == "run.py"
    assert upload["manifest"]["args"] == ["--quiet"]
    assert upload["manifest"]["runtime_seconds"] == 180
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(upload["content_base64"]))) as archive:
        assert set(archive.namelist()) == {"run.py", "gpuhub.json"}
    with pytest.raises(ClientError, match="budget"):
        make_upload(tmp_path, 5)


def test_replay_existing_revision():
    class Recorder:
        def call(self, path, data, *, key):
            assert (path, data, key) == ("/jobs", {"revision_id": "r1", "profile": "simulated"}, "repeat")
            return {"id": "j1"}
    args = options().parse_args(["submit", "--revision", "r1", "--idempotency-key", "repeat"])
    assert submit_job(Recorder(), args)["id"] == "j1"


def test_log_reconnect_and_replayed_cursor(monkeypatch, capsys):
    paths = []
    class Feed:
        def call(self, path):
            paths.append(path)
            if len(paths) == 1:
                return {"items": [{"message": "first", "cursor": 3}], "has_more": False, "terminal": False}
            if len(paths) == 2:
                raise ClientError("TEMPORARY", "retry", retryable=True)
            return {"items": [{"message": "first", "cursor": 3}, {"message": "second", "cursor": 6}],
                    "has_more": False, "terminal": True}
    monkeypatch.setattr("apps.cli.main.time.sleep", lambda n: None)
    logs(Feed(), options().parse_args(["logs", "job", "-f"]), Screen(), False)
    captured = capsys.readouterr()
    assert captured.out.splitlines() == ["first", "second"]
    assert paths == ["/jobs/job/logs?cursor=0", "/jobs/job/logs?cursor=3", "/jobs/job/logs?cursor=3"]


def test_invalid_page_cannot_spin():
    class Stuck:
        def call(self, _):
            return {"items": [], "next_cursor": 4, "has_more": True}
    with pytest.raises(ClientError, match="advance"):
        logs(Stuck(), options().parse_args(["logs", "j1", "--cursor", "4"]), Screen(), False)


class ArtifactApi:
    def __init__(self, name, payload=b"hello", digest=None):
        self.data = payload
        self.item = {"id": "a", "name": name, "size_bytes": len(payload),
                     "sha256": digest or hashlib.sha256(payload).hexdigest()}

    def call(self, path):
        return {"items": [self.item]}

    def open(self, path):
        return io.BytesIO(self.data)


def test_download_integrity_atomic_and_existing_output(tmp_path):
    api = ArtifactApi("curves/weights.json")
    result = download(api, "j", tmp_path)
    path = Path(result["items"][0]["path"])
    assert path.read_bytes() == b"hello"
    with pytest.raises(ClientError, match="already exists"):
        download(api, "j", tmp_path)
    with pytest.raises(ClientError, match="SHA-256"):
        download(ArtifactApi("weights.bin", digest="wrong"), "j", tmp_path)
    assert {p.name for p in tmp_path.iterdir()} == {"curves"}


@pytest.mark.parametrize("name", ["../output", "/root/file", "C:/weights", "sub\\output", "NUL"])
def test_download_only_to_safe_project_relative_path(tmp_path, name):
    with pytest.raises(ClientError):
        download(ArtifactApi(name), "j", tmp_path)
