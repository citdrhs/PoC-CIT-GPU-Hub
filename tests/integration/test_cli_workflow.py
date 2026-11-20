"""Run the entrypoint, API, scheduler and demonstration worker together."""
import io
import json
import urllib.error
import urllib.request
from types import SimpleNamespace

from apps.cli.main import main
from services.api.app import create_app
from services.worker.__main__ import provision
from tests.support.asgi import LocalASGI


def test_terminal_submit_log_cancel_and_download(tmp_path, monkeypatch, capsys):
    db, data = tmp_path / "hub.db", tmp_path / "data"
    app = create_app(db, data, dev_auth=True)
    api = LocalASGI(app)

    class Transport:
        def open(self, request, timeout=None):
            response = api.request(request.get_method(), request.full_url,
                                   headers=dict(request.header_items()), content=request.data)
            buf = io.BytesIO(response.content)
            if response.is_error:
                raise urllib.error.HTTPError(request.full_url, response.status_code, response.reason_phrase,
                                             response.headers, buf)
            return buf

    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: Transport())
    worker = provision(SimpleNamespace(db=db, root=data, node="dev", name="Development", backend="demo",
                                       demo_seconds=3600, reserve_bytes=None, reserve_cpus=None))

    def invoke(*args, identity="alex"):
        assert main(["--json", "--identity", identity, *args]) == 0
        return json.loads(capsys.readouterr().out)

    try:
        source = tmp_path / "train.py"
        source.write_text("print('hello')", encoding="utf-8")
        job = invoke("submit", str(source))["id"]
        worker.store.tick()
        worker.tick()
        assert invoke("status", job)["state"] == "RUNNING"
        assert invoke("dashboard")["nodes"][0]["slots"][0]["job_id"] == job
        assert invoke("jobs", identity="blair")["items"] == []
        invoke("cancel", job)
        worker.tick()
        assert invoke("status", job)["state"] == "CANCELLED"
        assert invoke("logs", job)["terminal"]
        out = tmp_path / "downloaded"
        files = invoke("download", job, "--output", str(out))["items"]
        assert len(files) == 1
        assert json.loads((out / "simulation.json").read_text())["job_id"] == job
    finally:
        worker.journal.close()
