"""Acceptance test for the same seed, scheduler and worker used by the classroom launcher."""
from argparse import Namespace

from deploy import dev
from services.api.app import create_app
from services.worker.__main__ import provision
from tests.support.asgi import LocalASGI


def test_classroom_setup_replay_and_rotation(tmp_path, monkeypatch):
    db, root = tmp_path / "control.db", tmp_path / "data"
    api = LocalASGI(create_app(db, root, dev_auth=True))

    def request(base, path, body=None, token=None, key=None):
        headers = {"Authorization": "Bearer " + token} if token else {}
        if key:
            headers["Idempotency-Key"] = key
        response = api.request("GET" if body is None else "POST", path, headers=headers, json=body)
        response.raise_for_status()
        return response.json()

    monkeypatch.setattr(dev, "request", request)
    dev.seed("local")
    dev.seed("local")
    worker = provision(Namespace(db=db, root=root, node="demo", name="Classroom", backend="demo",
                                 demo_seconds=600, reserve_bytes=None, reserve_cpus=None))
    try:
        store = worker.store
        store.tick()
        worker.tick()
        store.tick()
        jobs = {j["name"]: j for j in store.jobs()}
        assert len(jobs) == 4
        active = [j for j in jobs.values() if j["state"] == "RUNNING"]
        assert {j["user_id"] for j in active} == {"alex", "blair"}
        assert jobs["Image classifier"]["state"] == jobs["Learning rate sweep"]["state"] == "QUEUED"
        assert jobs["Image classifier"]["waiting_reason"] != jobs["Learning rate sweep"]["waiting_reason"]
        store.cancel(jobs["ResNet baseline"]["id"])
        assert len(store.commands("demo")) == 2
        worker.tick()
        store.tick()
        worker.tick()
        assert store.job(jobs["Image classifier"]["id"])["state"] == "RUNNING"
        assert store.job(jobs["Robot vision training"]["id"])["state"] == "RUNNING"
    finally:
        worker.journal.close()
