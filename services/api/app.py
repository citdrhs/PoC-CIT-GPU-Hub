"""Versioned student and operator HTTP surface. Runtime authority belongs to the worker."""
from __future__ import annotations

import base64
import binascii
import ipaddress
import os
import re
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException
from starlette.background import BackgroundTask

from packages.contracts import ContractError, Storage, validate_manifest
from services.scheduler.store import Store, StoreError, TERMINAL
from .catalog import ApiError, Catalog, DEV_USERS, ENVIRONMENTS


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class DevLogin(StrictBody):
    identity: str


class ProjectRequest(StrictBody):
    name: str = Field(min_length=1, max_length=96)


class RevisionRequest(StrictBody):
    filename: str = Field(min_length=1, max_length=255)
    content_base64: str
    manifest: dict


class Submission(StrictBody):
    revision_id: str = Field(min_length=1, max_length=128)
    profile: str = "simulated"


class Resume(StrictBody):
    checkpoint_id: str = Field(min_length=1, max_length=128)


class Member(StrictBody):
    user_id: str = Field(min_length=1, max_length=128)


class StudentPolicy(StrictBody):
    disabled: bool | None = None
    runtime_override_seconds: int | None = Field(None, ge=1)


def error_response(code, message, status):
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status,
                        headers={"Cache-Control":"no-store", "X-Content-Type-Options":"nosniff"})


def local_address(value):
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


class GuardRequests:
    """Bound both declared and chunked request bodies before JSON/base64 decoding."""
    def __init__(self, app, *, dev_auth, upload_limit):
        self.app, self.dev_auth, self.upload_limit = app, dev_auth, upload_limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope)
        if self.dev_auth:
            try:
                # Check the socket peer too. Forwarded headers never confer local authentication.
                safe = bool(request.client and local_address(request.client.host)
                            and local_address(request.url.hostname or ""))
                origin = request.headers.get("origin")
                if origin:
                    safe = safe and urlsplit(origin).netloc == request.url.netloc
            except ValueError:
                safe = False
            if not safe:
                return await error_response("LOCAL_ONLY", "Development authentication requires a local connection.", 403)(scope, receive, send)
        if scope["method"] not in ("POST", "PUT", "PATCH"):
            return await self.app(scope, receive, send)
        limit = self.upload_limit if scope["path"].endswith("/revisions") else 64 * 1024
        declared = request.headers.get("content-length")
        if declared and (not re.fullmatch(r"[0-9]{1,18}",declared) or int(declared) > limit):
            return await error_response("UPLOAD_TOO_LARGE", "Request exceeds the configured byte limit.", 413)(scope, receive, send)
        payload = bytearray()
        while True:
            message = await receive()
            if message["type"] != "http.request":
                return
            chunk = message.get("body", b"")
            if len(payload) + len(chunk) > limit:
                return await error_response("UPLOAD_TOO_LARGE", "Request exceeds the configured byte limit.", 413)(scope, receive, send)
            payload.extend(chunk)
            if not message.get("more_body"):
                break
        if payload and request.headers.get("content-type", "").split(";")[0] != "application/json":
            return await error_response("INVALID_CONTENT_TYPE", "Send a JSON request.", 415)(scope, receive, send)

        async def replay():
            nonlocal payload
            if payload is None:
                return await receive()
            body = bytes(payload)
            payload = None
            return {"type":"http.request","body":body,"more_body":False}
        return await self.app(scope, replay, send)


def create_app(db_path=None, data_root=None, *, dev_auth=False):
    path = Path(db_path or os.environ.get("GPUHUB_DB", ".gpuhub/hub.sqlite3"))
    root = Path(data_root or os.environ.get("GPUHUB_DATA", ".gpuhub/data")).resolve()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    store = Store(path)
    catalog = Catalog(path, root, dev_auth)
    storage = Storage(root)
    app = FastAPI(title="CIT GPU Hub", version="1.0", docs_url=None, redoc_url=None)
    app.state.store, app.state.catalog, app.state.storage = store, catalog, storage
    app.add_middleware(GuardRequests, dev_auth=dev_auth,
                       upload_limit=(storage.quotas["upload_bytes"]+2)//3*4 + 65536)
    maximum_runtime = int(os.environ.get("HUB_MAX_RUNTIME_SECONDS", "7200"))
    if maximum_runtime <= 0:
        raise ValueError("HUB_MAX_RUNTIME_SECONDS must be a positive integer")
    policy = {"version":"fixed_half_v1", "slots_per_node":2, "idle_borrowing":False,
              "max_active_per_user":1, "runtime_max_seconds":maximum_runtime,
              "memory_warning_percent":85, "memory_stop_percent":90, "quotas":storage.quotas}

    @app.exception_handler(ApiError)
    @app.exception_handler(StoreError)
    async def controlled_error(request, exc):
        return error_response(exc.code, str(exc), exc.status)

    @app.exception_handler(ContractError)
    async def contract_error(request, exc):
        return error_response(exc.code, str(exc), 422)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        problem = exc.errors()[0]
        where = ".".join(map(str, problem["loc"]))
        return error_response("INVALID_REQUEST", f"{where}: {problem['msg']}", 422)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return error_response("NOT_FOUND" if exc.status_code == 404 else "HTTP_ERROR", str(exc.detail), exc.status_code)

    @app.middleware("http")
    async def browser_headers(request, call_next):
        response = await call_next(request)
        response.headers.update({"Cache-Control":"no-store","X-Content-Type-Options":"nosniff",
                                 "Referrer-Policy":"no-referrer", "X-Frame-Options":"DENY",
                                 "Content-Security-Policy":"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'"})
        return response

    bearer = HTTPBearer(auto_error=False)

    def current(request: Request, cred: HTTPAuthorizationCredentials | None = Depends(bearer)):
        if not cred or cred.scheme.lower() != "bearer" or len(cred.credentials)>256:
            raise ApiError("AUTH_REQUIRED", "Sign in to GPU Hub.", 401)
        user = catalog.authenticate(cred.credentials)
        scope = "read" if request.method == "GET" else "write"
        if scope not in user["scopes"]:
            raise ApiError("FORBIDDEN", "Token lacks the required scope.", 403)
        return user

    def admin(user=Depends(current)):
        if user["role"] != "admin" or "admin" not in user["scopes"]:
            raise ApiError("FORBIDDEN", "This operation requires a teacher account.", 403)
        return user

    def idempotency_key(key: str | None = Header(default=None, alias="Idempotency-Key")):
        if key is None or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", key):
            raise ApiError("INVALID_IDEMPOTENCY_KEY", "Supply an Idempotency-Key of 1–128 identifier characters.")
        return key

    def validate_catalog(manifest, user):
        manifest = validate_manifest(manifest, max_runtime=maximum_runtime)
        if manifest["environment"] not in {e["id"] for e in ENVIRONMENTS}:
            raise ApiError("ENVIRONMENT_UNAVAILABLE", "Select a catalog environment.", 422)
        if set(manifest["dataset_ids"]) - {r["id"] for r in catalog.datasets(user)}:
            raise ApiError("DATASET_FORBIDDEN", "A requested dataset is unavailable to this identity.", 403)
        return manifest

    def job_access(jid, user):
        job = store.job(jid)
        if not job:
            raise ApiError("NOT_FOUND", "Job unavailable.", 404)
        if user["role"] != "admin" and job["user_id"] != user["id"]:
            try:
                catalog.revision(job["revision_id"], user)
            except ApiError:
                raise ApiError("NOT_FOUND", "Job unavailable.", 404) from None
        return job

    def public_job(job):
        # Host paths and machine configuration are operator-only.
        attempts = []
        for a in job["attempts"]:
            row = {k:v for k,v in a.items() if k not in {"inventory","cache_path","container_id","build_key"}}
            row["metadata"] = {k:v for k,v in row["metadata"].items() if k not in {"cache_path","paths","devices"}}
            row["envelope"] = {k:v for k,v in row["envelope"].items() if k not in {"device","device_id","gpu_uuid"}}
            attempts.append(row)
        return {**job, "attempts":attempts}

    def writable_job(jid, user):
        job = job_access(jid,user)
        if job["user_id"] != user["id"] and user["role"] != "admin":
            raise ApiError("FORBIDDEN", "Only the submitting user or teacher may control this job.", 403)
        return job

    @app.get("/healthz")
    def health():
        with catalog.connect() as db:
            db.execute("SELECT 1")
        return {"status":"ok"}

    @app.get("/v1/config")
    def config():
        return {"dev_auth":dev_auth, "identities":[dict(id=i,alias=a,role=r) for i,a,r in DEV_USERS] if dev_auth else [],
                "policy":policy}

    @app.post("/v1/auth/dev")
    def login(body: DevLogin):
        identity = next((row for row in DEV_USERS if row[0] == body.identity), None)
        if not dev_auth or identity is None:
            raise ApiError("AUTH_REQUIRED", "Development identity unavailable.", 403)
        return catalog.issue(*identity, "dev", 8*3600)

    @app.get("/v1/me")
    def me(user=Depends(current)):
        effective = {**policy, "hub_runtime_max_seconds":maximum_runtime}
        if user["role"] == "student":
            individual = store.student_policy(user["id"])
            effective["runtime_max_seconds"] = individual["effective_runtime_max_seconds"]
            effective["runtime_override_seconds"] = individual["runtime_override_seconds"]
        return {"user":user, "policy":effective, "dev_auth":dev_auth}

    @app.get("/v1/environments")
    def environments(user=Depends(current)):
        return {"items":ENVIRONMENTS}

    @app.get("/v1/datasets")
    def datasets(user=Depends(current)):
        return {"items":catalog.datasets(user)}

    @app.get("/v1/projects")
    def projects(user=Depends(current)):
        return {"items":catalog.projects(user)}

    @app.post("/v1/projects", status_code=201)
    def create_project(body: ProjectRequest, user=Depends(current)):
        name = body.name.strip()
        if not name or not name.isprintable():
            raise ApiError("INVALID_REQUEST", "Use a printable project name.", 422)
        return catalog.create_project(name,user)

    @app.get("/v1/projects/{pid}")
    def project(pid: str, user=Depends(current)):
        return {**catalog.project(pid,user), "revisions":catalog.revisions(pid)}

    @app.post("/v1/projects/{pid}/members")
    def add_member(pid: str, body: Member, user=Depends(current)):
        project = catalog.project(pid,user)
        if user["id"] != project["owner_id"] and user["role"] != "admin":
            raise ApiError("FORBIDDEN","Only the owner or a teacher may grant access.",403)
        with catalog.connect() as db:
            if not db.execute("SELECT 1 FROM users WHERE id=? AND disabled=0",(body.user_id,)).fetchone():
                raise ApiError("NOT_FOUND","Identity unavailable.",404)
            db.execute("INSERT OR IGNORE INTO project_members VALUES (?,?)",(pid,body.user_id))
        return {"project_id":pid,"user_id":body.user_id}

    @app.post("/v1/projects/{pid}/revisions",status_code=201)
    def revision(pid: str, body: RevisionRequest, user=Depends(current)):
        catalog.project(pid,user)
        manifest = validate_catalog(body.manifest,user)
        try:
            content = base64.b64decode(body.content_base64,validate=True)
        except (ValueError,binascii.Error):
            raise ApiError("INVALID_SOURCE","Source must be a base64-encoded Python file or ZIP.",422) from None
        record = storage.put_revision(pid,body.filename,content,manifest,max_runtime=maximum_runtime)
        catalog.record_revision(record)
        return {k:record[k] for k in ("id","project_id","manifest","source_hash","size_bytes")}

    @app.get("/v1/projects/{pid}/revisions/{rid}/download")
    def source(pid: str, rid: str, user=Depends(current)):
        revision = catalog.revision(rid,user)
        if revision["project_id"] != pid:
            raise ApiError("NOT_FOUND","Revision unavailable.",404)
        directory = storage.source_path(rid)
        storage.ensure_headroom(revision["size_bytes"]+65536)
        with tempfile.NamedTemporaryFile(dir=root/"staging",suffix=".zip",delete=False) as handle:
            archive = Path(handle.name)
        try:
            with zipfile.ZipFile(archive,"w",compression=zipfile.ZIP_STORED) as bundle:
                for path in sorted(directory.rglob("*")):
                    if path.is_file():
                        bundle.write(path,path.relative_to(directory).as_posix())
            return FileResponse(archive,media_type="application/zip",filename=rid+".zip",
                                background=BackgroundTask(archive.unlink,missing_ok=True))
        except BaseException:
            archive.unlink(missing_ok=True)
            raise

    @app.post("/v1/jobs",status_code=201)
    def submit(body: Submission, user=Depends(current), key=Depends(idempotency_key)):
        rev = catalog.revision(body.revision_id,user)
        manifest = validate_catalog(rev["manifest"],user)
        profile = {"spark":"spark-fixed-half","dell":"dell-mig"}.get(body.profile,body.profile)
        if profile not in ("simulated","cpu","spark-fixed-half","dell-mig"):
            raise ApiError("INVALID_MANIFEST","Unknown execution profile.",422)
        return public_job(store.submit(user["id"],rev["id"],manifest,key,profile=profile))

    @app.get("/v1/jobs")
    def jobs(state: str | None=None, project_id: str | None=None, user=Depends(current)):
        with catalog.connect() as db:
            revisions = {row["id"]: row["project_id"] for row in db.execute("""SELECT r.id,r.project_id FROM revisions r
                WHERE ?='admin' OR EXISTS (SELECT 1 FROM project_members m WHERE
                m.project_id=r.project_id AND m.user_id=?)""",(user["role"],user["id"]))}
        return {"items":[public_job(j) for j in store.jobs() if (j["revision_id"] in revisions or j["user_id"]==user["id"]
            or user["role"]=="admin") and (not state or j["state"]==state.upper())
            and (not project_id or revisions.get(j["revision_id"])==project_id)]}

    @app.get("/v1/queue")
    def queue(user=Depends(current)):
        aliases = catalog.aliases()
        with catalog.connect() as db:
            accessible = {r[0] for r in db.execute("""SELECT r.id FROM revisions r JOIN project_members m
                        ON m.project_id=r.project_id WHERE m.user_id=?""",(user["id"],))}
        rows = []
        for j in store.jobs():
            if j["state"] != "QUEUED":
                continue
            can_open = user["role"]=="admin" or j["user_id"]==user["id"] or j["revision_id"] in accessible
            rows.append({**{k:j[k] for k in ("id","name","state","waiting_reason","profile","created_at")},
                         "user_id": j["user_id"] if can_open else None,
                         "alias":aliases.get(j["user_id"],"Student"),"can_open":can_open})
        return {"items":sorted(rows, key=lambda r:(r["created_at"],r["id"]))}

    @app.get("/v1/jobs/{jid}")
    def job(jid: str, user=Depends(current)):
        return public_job(job_access(jid,user))

    @app.post("/v1/jobs/{jid}/cancel")
    def cancel(jid: str, user=Depends(current)):
        writable_job(jid,user)
        return public_job(store.cancel(jid))

    @app.post("/v1/jobs/{jid}/resume")
    def resume(jid: str, body: Resume, user=Depends(current), key=Depends(idempotency_key)):
        job = writable_job(jid,user)
        aid = storage.owner_attempt(body.checkpoint_id)
        if not any(a["id"] == aid for a in job["attempts"]):
            raise ApiError("CHECKPOINT_UNAVAILABLE","Choose a completed checkpoint from this job.",409)
        records = storage.checkpoints(aid)
        saved = next((r for r in records if r["id"]==body.checkpoint_id),None)
        if (not saved or saved.get("revision_id") != job["revision_id"] or
                saved.get("job_id") != jid or saved.get("dataset_ids") != job["manifest"]["dataset_ids"]):
            raise ApiError("CHECKPOINT_UNAVAILABLE","Checkpoint does not match the immutable job input.",409)
        return public_job(store.resume(jid,user["id"],body.checkpoint_id,key))

    def page(records,cursor,job,limit):
        visible = records[:limit]
        return {"items":visible, "next_cursor":visible[-1]["id"] if visible else cursor,
                "has_more":len(records)>limit,"terminal":job["state"] in TERMINAL}

    @app.get("/v1/jobs/{jid}/logs")
    def logs(jid: str, cursor: int=Query(0,ge=0), limit: int=Query(100,ge=1,le=200), user=Depends(current)):
        job = job_access(jid,user)
        records = [{**row, "message":row["text"], "cursor":row["id"]}
                   for row in store.logs(jid,after=cursor,limit=limit+1)]
        return page(records,cursor,job,limit)

    @app.get("/v1/jobs/{jid}/events")
    def events(jid: str, cursor: int=Query(0,ge=0), limit: int=Query(100,ge=1,le=200),user=Depends(current)):
        job = job_access(jid,user)
        return page(store.events(jid,after=cursor,limit=limit+1),cursor,job,limit)

    def resources(jid,user,read):
        job = job_access(jid,user)
        rows = [{**row,"attempt_id":a["id"]} for a in job["attempts"] for row in read(a["id"])]
        return {"items":rows,"next_cursor":len(rows)}

    @app.get("/v1/jobs/{jid}/artifacts")
    def artifacts(jid: str,user=Depends(current)):
        return resources(jid,user,storage.artifacts)

    @app.get("/v1/jobs/{jid}/checkpoints")
    def checkpoints(jid: str,user=Depends(current)):
        return resources(jid,user,storage.checkpoints)

    @app.get("/v1/artifacts/{rid}/download")
    def download(rid: str, user=Depends(current)):
        aid = storage.owner_attempt(rid)
        with catalog.connect() as db:
            row = db.execute("SELECT job_id FROM attempts WHERE id=?",(aid,)).fetchone()
        if not row:
            raise ApiError("NOT_FOUND","Artifact unavailable.",404)
        job_access(row["job_id"],user)
        artifact = next((r for r in storage.artifacts(aid) if r["id"]==rid),None)
        if not artifact:
            raise ApiError("NOT_FOUND","Artifact unavailable.",404)
        return FileResponse(storage.artifact_path(aid,rid), filename=Path(artifact["name"]).name,
                            media_type="application/octet-stream")

    @app.get("/v1/nodes")
    def nodes(user=Depends(current)):
        aliases = catalog.aliases()
        items = store.nodes()
        for n in items:
            n.pop("fingerprint", None)
            n.pop("dataset_ids", None)
            n["inventory"] = {k:v for k,v in (n.get("inventory") or {}).items() if k in
                              ("memory_bytes","logical_cpus","architecture","gpu_model","memory_model","source",
                               "cpu_count","arch","model","measured_memory_bytes","simulated","memory_source",
                               "execution_mode")}
            for slot in n["slots"]:
                uid = slot.get("user_id")
                slot["alias"] = aliases.get(uid) if uid else None
                if uid and uid!=user["id"] and user["role"]!="admin":
                    slot["user_id"] = slot["attempt_id"] = None
        return {"items":items}

    @app.get("/v1/admin/nodes/{nid}/qualification")
    def qualification(nid: str,user=Depends(admin)):
        return store.qualification(nid)

    @app.post("/v1/admin/nodes/{nid}/drain")
    def drain(nid: str,user=Depends(admin)):
        store.drain(nid,True,user["id"])
        return {"id":nid,"draining":True}

    @app.post("/v1/admin/nodes/{nid}/resume")
    def undrain(nid: str,user=Depends(admin)):
        store.drain(nid,False,user["id"])
        return {"id":nid,"draining":False}

    @app.post("/v1/admin/users/{uid}/policy")
    def set_policy(uid: str, body: StudentPolicy, user=Depends(admin)):
        changes = body.model_dump(exclude_unset=True)
        if not changes or ("disabled" in changes and changes["disabled"] is None):
            raise ApiError("INVALID_POLICY","Set a student access flag or runtime limit.",422)
        if (changes.get("runtime_override_seconds") or 0) > maximum_runtime:
            raise ApiError("INVALID_POLICY","Student runtime must fit the hub-wide limit.",422)
        with catalog.connect() as db:
            target = db.execute("SELECT role FROM users WHERE id=?",(uid,)).fetchone()
        if not target:
            raise ApiError("NOT_FOUND","Student unavailable.",404)
        if uid == user["id"] or target["role"] != "student":
            raise ApiError("FORBIDDEN","Only student accounts can receive this policy.",403)
        return store.set_student_policy(uid,user["id"],**changes)

    @app.get("/v1/admin/overview")
    def overview(user=Depends(admin)):
        policies = {p["user_id"]:p for p in store.student_policies()}
        with catalog.connect() as db:
            users = [{**dict(r), **policies.get(r["id"],{})}
                     for r in db.execute("SELECT id,alias,role,disabled FROM users ORDER BY alias")]
            return {"users":users,
                    "policy":policy, "reservations":[dict(r) for r in db.execute("SELECT * FROM reservations")],
                    "audit":[dict(r) for r in db.execute("SELECT id,kind,message,at,data FROM events WHERE job_id IS NULL ORDER BY id DESC LIMIT 100")]}

    web = Path(__file__).resolve().parents[2]/"apps"/"web"
    app.mount("/assets", StaticFiles(directory=web/"assets"), name="assets")

    @app.get("/",include_in_schema=False)
    def index():
        return FileResponse(web/"index.html")
    return app
