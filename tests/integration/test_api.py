import base64
import hashlib
import io
import json
import time
import zipfile

import pytest

from services.api.app import create_app
from tests.support.asgi import LocalASGI


MANIFEST = {"schema_version":1,"name":"Vector lab","entrypoint":"train.py","environment":"python-cpu-v1"}
SOURCE = b"# classroom fixture\nprint(2 + 2)\n"


@pytest.fixture
def hub(tmp_path):
    app = create_app(tmp_path/"control.db",tmp_path/"data",dev_auth=True)
    return app, LocalASGI(app)


def login(c, name):
    result = c.post("/v1/auth/dev",json={"identity":name})
    assert result.status_code == 200, result.text
    return {"Authorization":"Bearer "+result.json()["token"]}


def submission(c, user, name="Vector lab"):
    p = c.post("/v1/projects",headers=user,json={"name":name})
    assert p.status_code == 201, p.text
    pid = p.json()["id"]
    data = {"filename":"train.py","manifest":{**MANIFEST,"name":name},
            "content_base64":base64.b64encode(SOURCE).decode()}
    rev = c.post(f"/v1/projects/{pid}/revisions",headers=user,json=data)
    assert rev.status_code == 201, rev.text
    return pid, rev.json()["id"]


def submit(c, headers, revision, *, key="experiment-a",profile="simulated"):
    return c.post("/v1/jobs",headers={**headers,"Idempotency-Key":key},
                  json={"revision_id":revision,"profile":profile})


def test_project_acl_applies_to_history_data_and_control(hub):
    app,c = hub
    alex,blair,teacher = [login(c,n) for n in ("alex","blair","teacher")]
    pid,rid = submission(c,alex)
    job = submit(c,alex,rid).json()
    jid = job["id"]
    assert submit(c,alex,rid).json()["id"] == jid
    assert submit(c,alex,rid,profile="cpu").status_code == 409
    assert submit(c,blair,rid).status_code == 404
    assert c.get("/v1/jobs",headers=blair).json()["items"] == []
    waiting = c.get("/v1/queue",headers=blair).json()["items"][0]
    assert waiting["alias"] == "Alex" and not waiting["can_open"] and "manifest" not in waiting
    source = f"/v1/projects/{pid}/revisions/{rid}/download"
    for path in (source,f"/v1/projects/{pid}", f"/v1/jobs/{jid}", f"/v1/jobs/{jid}/logs",
                 f"/v1/jobs/{jid}/events",f"/v1/jobs/{jid}/checkpoints",f"/v1/jobs/{jid}/artifacts"):
        assert c.get(path,headers=blair).status_code == 404, path
    assert c.post(f"/v1/jobs/{jid}/cancel",headers=blair,json={}).status_code == 404
    with zipfile.ZipFile(io.BytesIO(c.get(source,headers=alex).content)) as z:
        assert z.read("train.py") == SOURCE
    assert c.post(f"/v1/projects/{pid}/members",headers=alex,json={"user_id":"blair"}).status_code == 200
    assert c.get(f"/v1/jobs/{jid}",headers=blair).status_code == 200
    assert c.post(f"/v1/jobs/{jid}/cancel",headers=blair,json={}).status_code == 403
    assert c.post(f"/v1/jobs/{jid}/cancel",headers=teacher,json={}).json()["state"] == "CANCELLED"


def test_dev_auth_loopback_only_and_operator_tokens_persist(tmp_path):
    db,data=tmp_path/"db",tmp_path/"data"
    app = create_app(db,data,dev_auth=True)
    client = LocalASGI(app)
    dev = login(client,"teacher")
    assert client.get("/v1/me").status_code == 401
    assert LocalASGI(app,host="192.0.2.4").post("/v1/auth/dev",json={"identity":"teacher"}).status_code==403
    assert client.post("/v1/auth/dev",json={"identity":"teacher"},headers={"Origin":"https://outside.test"}).status_code==403
    assert client.get("/v1/config",headers={"Host":"outside.test"}).status_code==403
    app = create_app(db,data)
    prod = LocalASGI(app)
    assert not prod.get("/v1/config").json()["dev_auth"]
    assert prod.get("/v1/me",headers=dev).status_code == 401
    assert prod.post("/v1/auth/dev",json={"identity":"teacher"}).status_code == 403
    catalog = app.state.catalog
    token = catalog.issue("operator1","Operator","admin","operator",60)["token"]
    hdrs = {"Authorization":"Bearer "+token}
    assert prod.get("/v1/admin/overview",headers=hdrs).status_code == 200
    read_only = catalog.issue("operator1","Operator","admin","operator",60,read_only=True)["token"]
    limited = {"Authorization":"Bearer "+read_only}
    assert prod.get("/v1/admin/overview",headers=limited).status_code == 200
    assert prod.post("/v1/projects",headers=limited,json={"name":"Read-only"}).status_code == 403
    with catalog.connect() as conn:
        assert not conn.execute("SELECT 1 FROM auth_tokens WHERE digest=?",(token,)).fetchone()
        conn.execute("UPDATE users SET disabled=1 WHERE id='operator1'")
    assert prod.get("/v1/me",headers=hdrs).status_code == 401


def test_intent_validation_precedes_storage_and_auth_scope(hub):
    app,c = hub
    alex = login(c,"alex")
    pid=c.post("/v1/projects",headers=alex,json={"name":"Lab"}).json()["id"]
    endpoint=f"/v1/projects/{pid}/revisions"
    base={"filename":"train.py","content_base64":base64.b64encode(SOURCE).decode()}
    for change,code in [({"privileged":True},"INVALID_MANIFEST"),({"environment":"other"},"ENVIRONMENT_UNAVAILABLE"),
                        ({"entrypoint":"../train.py"},"INVALID_MANIFEST"),({"dataset_ids":["private"]},"DATASET_FORBIDDEN"),
                        ({"runtime_seconds":86400},"RUNTIME_POLICY_EXCEEDED")]:
        response = c.post(endpoint,headers=alex,json={**base,"manifest":{**MANIFEST,**change}})
        assert response.json()["error"]["code"]==code, response.text
    assert c.get(f"/v1/projects/{pid}",headers=alex).json()["revisions"]==[]
    res = c.post(endpoint,headers={**alex,"Content-Length":str(1<<30)},json={})
    assert res.status_code==413
    assert c.post("/v1/projects",headers=alex,json={"name":"abc","owner_id":"teacher"}).status_code==422
    assert c.post("/v1/admin/nodes/spark/drain",headers=alex,json={}).status_code==403


def test_cursor_recovery_artifact_access_and_explicit_resume(hub):
    app,c = hub
    alex,other=login(c,"alex"),login(c,"casey")
    _,rid=submission(c,alex)
    jid=submit(c,alex,rid).json()["id"]
    store,storage=app.state.store,app.state.storage
    store.register_node("fixture","Fixture","simulated",{"memory_bytes":8<<30,"logical_cpus":4},1<<30)
    attempt=store.tick()[0]
    aid=attempt["id"]
    store.observe(aid,attempt["generation"],"RUNNING",started_at=time.time())
    for message in ("epoch 1","epoch 2","epoch 3"):
        store.append_log(aid,message)
    first=c.get(f"/v1/jobs/{jid}/logs?limit=2",headers=alex).json()
    assert [r["message"] for r in first["items"]] == ["epoch 1","epoch 2"]
    assert first["has_more"] and not first["terminal"]
    tail=c.get(f"/v1/jobs/{jid}/logs?cursor={first['next_cursor']}",headers=alex).json()
    assert [r["message"] for r in tail["items"]] == ["epoch 3"]
    paths=storage.attempt_paths(aid)
    (paths["outputs"]/"report.txt").write_bytes(b"epoch=3")
    cp=paths["checkpoints"]/"step-3"
    cp.mkdir()
    state=b'{"epoch":3}'
    (cp/"state.json").write_bytes(state)
    (cp/"checkpoint.json").write_text(json.dumps({"format_version":1,"step":3,"files":[
        {"name":"state.json","size_bytes":len(state),"sha256":hashlib.sha256(state).hexdigest()}]}))
    storage.collect(aid,job_id=jid,revision_id=rid,environment="fixture-digest",dataset_ids=[])
    store.observe(aid,attempt["generation"],"SUCCEEDED",cleanup_confirmed=True)
    assert c.get(f"/v1/jobs/{jid}/logs",headers=alex).json()["terminal"]
    art=c.get(f"/v1/jobs/{jid}/artifacts",headers=alex).json()["items"][0]
    link=f"/v1/artifacts/{art['id']}/download"
    assert c.get(link,headers=alex).content==b"epoch=3"
    assert c.get(link,headers=other).status_code==404
    saved=c.get(f"/v1/jobs/{jid}/checkpoints",headers=alex).json()["items"][0]
    resume=f"/v1/jobs/{jid}/resume"
    key={**alex,"Idempotency-Key":"resume-1"}
    assert c.post(resume,json={"checkpoint_id":"other"},headers=key).status_code==409
    response=c.post(resume,json={"checkpoint_id":saved["id"]},headers=key)
    assert response.status_code==200 and response.json()["state"]=="QUEUED",response.text
    assert c.post(resume,json={"checkpoint_id":saved["id"]},headers=key).json()["id"]==jid


def test_teacher_student_policy_survives_reload_and_is_audited(hub):
    app,c=hub
    teacher,student=login(c,"teacher"),login(c,"alex")
    _,rid=submission(c,student)
    route="/v1/admin/users/alex/policy"
    assert c.post(route,headers=student,json={"disabled":True}).status_code==403
    assert c.post("/v1/admin/users/teacher/policy",headers=teacher,json={"disabled":True}).status_code==403
    assert c.post(route,headers=teacher,json={"runtime_override_seconds":999999}).status_code==422
    r=c.post(route,headers=teacher,json={"runtime_override_seconds":120})
    assert r.status_code==200 and r.json()["effective_runtime_max_seconds"]==120, r.text
    assert c.get("/v1/me",headers=student).json()["policy"]["runtime_max_seconds"]==120
    assert submit(c,student,rid).json()["error"]["code"]=="RUNTIME_POLICY_EXCEEDED"
    admin=c.get("/v1/admin/overview",headers=teacher).json()
    assert next(u for u in admin["users"] if u["id"]=="alex")["effective_runtime_max_seconds"]==120
    assert any("alex" in str(e) and "teacher" in str(e) for e in admin["audit"])
    assert c.post(route,headers=teacher,json={"disabled":True}).json()["disabled"]
    assert c.get("/v1/me",headers=student).status_code==401
    assert c.post(route,headers=teacher,json={"disabled":False,"runtime_override_seconds":None}).status_code==200
    assert c.get("/v1/me",headers=student).status_code==401
    assert submit(c,login(c,"alex"),rid).status_code==201
