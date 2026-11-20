import concurrent.futures
import threading

import pytest

from packages.resource_policy import fixed_half
from services.scheduler.store import BLOCKED, CAPABILITIES, Store, StoreError


@pytest.fixture
def hub(tmp_path):
    now = [1000.0]
    store = Store(tmp_path / "hub.db", clock=lambda: now[0], heartbeat_timeout=45)
    store.register_node("lab", "Lab simulator", "simulated", {"memory_bytes": 128 << 30, "logical_cpus": 20},
                        16 << 30, 4)
    return store, now


def job(store, user, key, **kwargs):
    profile = kwargs.pop("profile", "simulated")
    return store.submit(user, "revision-immutable", {"name": key, "environment": "python-cpu-v1",
                       "runtime_seconds": 600, **kwargs}, key, profile=profile)


def complete(store, attempt, state="SUCCEEDED", **kwargs):
    return store.observe(attempt["id"], attempt["generation"], state, cleanup_confirmed=True, **kwargs)


def test_policy_inventory_units_rounding_and_cpu_period():
    a = fixed_half(128 << 30, 16 << 30, 20, 4)
    assert a.memory_bytes == 56 << 30
    assert a.cpu_quota_us == 8 * a.cpu_period_us
    assert a.swap_bytes == 0
    decimal = fixed_half(128_000_000_000, 16 << 30, 9, 2)
    assert decimal.memory_bytes != a.memory_bytes
    assert decimal.cpu_quota_us == 350_000
    assert 2 * decimal.memory_bytes + (16 << 30) <= 128_000_000_000
    assert 128_000_000_000 - (16 << 30) - 2 * decimal.memory_bytes < 2 << 20


@pytest.mark.parametrize("args", [(0, 0, 20, 4), (1024, 512, 8, 0),
                                  (2 << 30, -1, 8, 0), (2 << 30, 0, 8, 8),
                                  (2 << 30, 0, 8.1, 0), (True, 0, 8, 0)])
def test_invalid_policy(args):
    with pytest.raises(ValueError):
        fixed_half(*args)


def test_fixed_slots_and_distinct_principals(hub):
    s, now = hub
    first = job(s, "alice", "1")
    a, = s.tick()
    assert a["envelope"]["gpu_fraction"] == "1/2"
    assert a["state"] == "STARTING"
    for index in range(10):
        now[0] += .1
        job(s, "alice", str(index+2))
    b_job = job(s, "bob", "1")
    now[0] += 1
    c_job = job(s, "charlie", "1")
    b, = s.tick()
    assert b["job_id"] == b_job["id"]
    assert s.job(c_job["id"])["waiting_reason"] == "Waiting for a free half-capacity slot"
    assert all(j["waiting_reason"] == "User already has an active allocation" for j in
               s.jobs("alice") if j["id"] != first["id"])
    complete(s, b)
    assert s.attempt(a["id"])["envelope"] == a["envelope"]
    c, = s.tick()
    assert c["job_id"] == c_job["id"]


def test_builds_and_runs_share_envelopes(hub):
    s, now = hub
    a_job = job(s, "alice", "build", dependencies_lock="requirements.lock")
    a, = s.tick()
    assert a["kind"] == "BUILD" and a["envelope"]["gpu_fraction"] is None
    now[0] += 1
    job(s, "alice", "try-again", dependencies_lock="requirements.lock")
    job(s, "bob", "training")
    b, = s.tick()
    assert b["user_id"] == "bob"
    assert len(s.commands("lab")) == 2
    complete(s, a, metadata={"image_digest": "sha256:verified", "cache_path": "/cache/verified"})
    run, = s.tick()
    assert run["job_id"] == a_job["id"] and run["kind"] == "RUN" and run["generation"] == 2
    assert run["image_digest"] == "sha256:verified"
    assert run["envelope"]["gpu_fraction"] == "1/2"


def test_concurrent_submission_ticks_and_reopen(hub):
    s, _ = hub
    barrier = threading.Barrier(6)

    def submit_parallel(_):
        barrier.wait()
        return job(s, "alice", "same-key")["id"]

    with concurrent.futures.ThreadPoolExecutor(6) as pool:
        assert len(set(pool.map(submit_parallel, range(6)))) == 1
        for i in range(5):
            job(s, str(i), "a")
        results = list(pool.map(lambda _: s.tick(), range(10)))
    launched = [a for batch in results for a in batch]
    assert len(launched) == 2
    assert len({a["slot"] for a in launched}) == len({a["user_id"] for a in launched}) == 2
    reopened = Store(s.db_path, clock=s.clock)
    assert {a["id"] for a in reopened.commands("lab")} == {a["id"] for a in launched}
    assert reopened.tick() == []


def test_cancel_startup_and_unconfirmed_exit(hub):
    s, _ = hub
    first = job(s, "alice", "a")
    a, = s.tick()
    assert s.cancel(first["id"])["desired_state"] == "STOP"
    s.observe(a["id"], 1, "RUNNING", started_at=1000, deadline=1600)
    assert s.commands("lab")[0]["desired_state"] == "STOP"
    s.observe(a["id"], 1, "CANCELLED", cleanup_confirmed=False)
    assert s.job(first["id"])["state"] == "UNKNOWN"
    assert s.nodes()[0]["status"] == "QUARANTINED"
    job(s, "bob", "b")
    assert s.tick() == []
    complete(s, a, "CANCELLED")
    assert s.tick()[0]["user_id"] == "bob"


def test_heartbeat_loss_never_relaunches_and_timeout_persists(hub):
    s, now = hub
    job(s, "alice", "training")
    a, = s.tick()
    s.observe(a["id"], 1, "RUNNING", started_at=1000, deadline=1800)
    assert s.attempt(a["id"])["deadline"] == 1600
    now[0] += 60
    assert s.tick() == []
    assert s.job(a["job_id"])["state"] == "UNKNOWN"
    s.heartbeat("lab")
    assert s.nodes()[0]["status"] == "QUARANTINED"
    reopened = Store(s.db_path, clock=s.clock)
    reopened.observe(a["id"], 1, "RUNNING", started_at=1060, deadline=1660)
    assert reopened.commands("lab")[0]["deadline"] == 1600
    assert reopened.nodes()[0]["status"] == "ONLINE"
    with pytest.raises(StoreError, match="Generation"):
        reopened.observe(a["id"], 2, "SUCCEEDED", cleanup_confirmed=True)


def test_heartbeat_detects_gap_even_before_tick(hub):
    s, now = hub
    job(s, "alice", "training")
    a, = s.tick()
    now[0] += 80
    s.heartbeat("lab")
    assert s.attempt(a["id"])["state"] == "UNKNOWN"
    assert s.nodes()[0]["status"] == "QUARANTINED"


def test_planned_and_physical_gates(hub):
    s, _ = hub
    spark = s.plan_node("spark", "Spark", "spark-fixed-half")
    assert spark["inventory"] == {} and spark["envelope"] is None
    assert [slot["state"] for slot in spark["slots"]] == ["BLOCKED", "BLOCKED"]
    s.drain("spark", False, "teacher")
    work = job(s, "alice", "strict", profile="spark-fixed-half")
    assert s.tick() == []
    assert "unverified" in s.job(work["id"])["waiting_reason"]
    inv = {"memory_bytes": 60 << 30, "logical_cpus": 8, "fingerprint": "actual"}
    proof = {"fingerprint": "different", "observed_at": 999, "capabilities": {k: "VERIFIED" for k in CAPABILITIES},
             "devices": {"A": "MIG-A", "B": "MIG-B"}, "device_memory_bytes": {"A": 48_000_000_000, "B": 48_000_000_000}}
    reg = lambda: s.register_node("dell", "Dell", "dell-mig", inv, 2 << 30, evidence=proof)
    assert reg()["qualification_status"] == BLOCKED
    proof["fingerprint"] = "actual"
    assert reg()["qualification_status"] == "VERIFIED"
    job(s, "bob", "strict", profile="dell-mig")
    attempt, = s.tick()
    assert attempt["envelope"]["gpu_memory_bytes"] == 48_000_000_000
    assert attempt["envelope"]["device_id"] == "MIG-A"
    inv["fingerprint"] = "changed"
    assert reg()["draining"]
    assert s.attempt(attempt["id"])["state"] == "UNKNOWN"
    assert len(s.commands("dell")) == 1
    # Even a purported all-VERIFIED snapshot cannot enable the absent Spark teardown backend.
    proof["fingerprint"] = "changed"
    assert s.register_node("spark", "Spark", "spark-fixed-half", inv, 0,
                           evidence=proof)["qualification_status"] == BLOCKED


def test_idempotent_retries_resume_and_log_cursors(hub, monkeypatch):
    s, _ = hub
    first = job(s, "alice", "r")
    assert job(s, "alice", "r")["id"] == first["id"]
    with pytest.raises(StoreError) as exc:
        job(s, "alice", "r", runtime_seconds=10)
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"
    a, = s.tick()
    monkeypatch.setenv("HUB_LOG_BYTES", "8")
    s.append_log(a["id"], "first")
    page = s.logs(first["id"])
    s.append_log(a["id"], "second")
    assert [r["text"] for r in s.logs(first["id"], page[-1]["id"])] == ["sec"]
    assert "LOG_TRUNCATED" in [e["kind"] for e in s.events(first["id"])]
    complete(s, a, "TIMED_OUT")
    resumed = s.resume(first["id"], "alice", "ckpt-1", "resume-key")
    assert resumed["id"] == first["id"]
    new, = s.tick()
    assert new["checkpoint_id"] == "ckpt-1" and new["generation"] == 2
    assert s.resume(first["id"], "alice", "ckpt-1", "resume-key")["id"] == first["id"]
    assert len(s.commands("lab")) == 1


def test_bounded_wait_and_preparation_deadlines(hub, monkeypatch):
    s, now = hub
    a = job(s, "alice", "r")
    attempt, = s.tick()
    now[0] += 11
    monkeypatch.setenv("HUB_LAUNCH_TIMEOUT_SECONDS", "10")
    s.tick()
    assert s.job(a["id"])["desired_state"] == "STOP"
    assert s.commands("lab")[0]["id"] == attempt["id"]
    b = job(s, "alice", "blocked")
    now[0] += 10
    monkeypatch.setenv("HUB_QUEUE_TIMEOUT_SECONDS", "8")
    s.tick()
    assert s.job(b["id"])["state"] == "TIMED_OUT"


def test_pressure_and_drain_prevent_new_admissions(hub):
    s, _ = hub
    job(s, "alice", "a")
    a, = s.tick()
    job(s, "bob", "b")
    s.heartbeat("lab", pressure=True)
    assert s.tick() == []
    assert s.attempt(a["id"])["envelope"]["memory_bytes"] == 56 << 30
    s.heartbeat("lab")
    s.drain("lab", True, "teacher")
    assert s.tick() == []
    s.drain("lab", False, "teacher")
    assert len(s.tick()) == 1


def test_earliest_eligible_and_per_pool_usage(hub):
    s, now = hub
    job(s, "alice", "earlier incompatible", environment="other")
    training = job(s, "alice", "later compatible")
    a, = s.tick()
    assert a["job_id"] == training["id"]
    now[0] += 40
    complete(s, a)
    job(s, "alice", "again")
    now[0] += 1
    job(s, "new-user", "first")
    b, c = s.tick()
    assert [b["user_id"], c["user_id"]] == ["new-user", "alice"]


def test_worker_replay_deduplicates_log_records(hub):
    s, _ = hub
    original = job(s, "alice", "a")
    a, = s.tick()
    s.append_log(a["id"], "epoch 1", seq=1)
    s.append_log(a["id"], "epoch 1", seq=1)
    with pytest.raises(StoreError, match="missing"):
        s.append_log(a["id"], "epoch 3", seq=3)
    s.append_log(a["id"], "epoch 2", seq=2)
    assert [r["text"] for r in s.logs(original["id"])] == ["epoch 1", "epoch 2"]


def test_cpu_profile_never_falls_back_to_an_execution_fixture(hub):
    s, _ = hub
    a = job(s, "alex", "needs-execution", profile="cpu")
    assert s.tick() == []
    inv = {"logical_cpus": 6, "memory_bytes": 16 << 30, "execution_mode": "docker"}
    s.register_node("docker", "CPU with simulated GPU", "simulated", inv, 2 << 30)
    run, = s.tick()
    assert run["job_id"] == a["id"] and run["node_id"] == "docker"
    assert run["envelope"]["gpu_fraction"] is None


def test_earliest_runnable_job_wins_across_two_nodes(hub):
    s, now = hub
    inv = {"logical_cpus": 6, "memory_bytes": 16 << 30}
    s.register_node("z-cpu", "CPU node", "cpu", inv, 2 << 30)
    earlier = job(s, "alex", "cpu-work", profile="cpu")
    now[0] += 1
    job(s, "alex", "later-simulation")
    a, = s.tick()
    assert a["job_id"] == earlier["id"]


def test_live_catalog_access_rechecked_under_reservation_lock(hub, tmp_path):
    from services.api.catalog import Catalog
    s, _ = hub
    catalog = Catalog(s.db_path, tmp_path / "data", True)
    user = {"id": "alex", "role": "student"}
    project = catalog.create_project("classifier", user)
    with catalog.connect() as db:
        db.execute("INSERT INTO revisions VALUES(?,?,?, ?,0,0)",
                   ("revision-immutable", project["id"], "{}", "sample"))
    queued = job(s, "alex", "training")
    s.set_student_policy("alex", "teacher", disabled=True)
    assert not s.tick() and "disabled" in s.job(queued["id"])["waiting_reason"]
    with catalog.connect() as db:
        db.execute("UPDATE users SET disabled=0 WHERE id='alex'")
        db.execute("DELETE FROM project_members WHERE project_id=?", (project["id"],))
    assert not s.tick() and "Project access" in s.job(queued["id"])["waiting_reason"]
    with catalog.connect() as db:
        db.execute("INSERT INTO project_members VALUES(?,?)", (project["id"], user["id"]))
    assert s.tick()[0]["job_id"] == queued["id"]


def test_teacher_policy_preserves_accepted_jobs_and_checks_new_resume(hub, tmp_path):
    from services.api.catalog import Catalog
    s, _ = hub
    catalog = Catalog(s.db_path, tmp_path / "data", True)
    project = catalog.create_project("experiment", {"id": "alex", "role": "student"})
    with catalog.connect() as db:
        db.execute("INSERT INTO revisions VALUES(?,?,?, ?,0,0)",
                   ("revision-immutable", project["id"], "{}", "sample"))
    original = job(s, "alex", "old")
    s.set_student_policy("alex", "teacher", runtime_override_seconds=120)
    assert job(s, "alex", "old")["id"] == original["id"]
    with pytest.raises(StoreError, match="120-second limit"):
        job(s, "alex", "new")
    attempt, = s.tick()
    assert s.observe(attempt["id"], 1, "RUNNING", started_at=1000)["deadline"] == 1600
    complete(s, attempt)
    with pytest.raises(StoreError, match="120-second limit"):
        s.resume(original["id"], "teacher", "ckpt", "teacher-resume")
    s.set_student_policy("alex", "teacher", runtime_override_seconds=None)
    assert s.resume(original["id"], "teacher", "ckpt", "teacher-resume")["state"] == "QUEUED"
    s.set_student_policy("alex", "teacher", runtime_override_seconds=1)
    assert s.resume(original["id"], "teacher", "ckpt", "teacher-resume")["id"] == original["id"]
    with s.connection() as db:
        changes = list(db.execute("SELECT data FROM events WHERE kind='STUDENT_POLICY_CHANGED'"))
        assert len(changes) == 3 and '"actor":"teacher"' in changes[0]["data"]


def test_policy_mutation_authority_validation_and_rollback(hub, tmp_path, monkeypatch):
    from services.api.catalog import Catalog
    s, _ = hub
    catalog = Catalog(s.db_path, tmp_path / "data", True)
    token = catalog.issue("alex", "Alex", "student", "dev", 3600)["token"]
    monkeypatch.setenv("HUB_MAX_RUNTIME_SECONDS", "1200")
    with pytest.raises(StoreError, match="administrator"):
        s.set_student_policy("alex", "blair", runtime_override_seconds=100)
    with pytest.raises(StoreError, match="student account"):
        s.set_student_policy("teacher", "teacher", disabled=True)
    with pytest.raises(StoreError, match="1200 seconds"):
        s.set_student_policy("alex", "teacher", disabled=True, runtime_override_seconds=1201)
    assert catalog.authenticate(token)["id"] == "alex"
    s.set_student_policy("alex", "teacher", disabled=True, runtime_override_seconds=200)
    with pytest.raises(StoreError, match="disabled"):
        job(s, "alex", "refused", runtime_seconds=100)
    s.set_student_policy("alex", "teacher", disabled=False)
    from services.api.catalog import ApiError
    with pytest.raises(ApiError):
        catalog.authenticate(token)
    assert s.student_policy("alex")["effective_runtime_max_seconds"] == 200
    assert len(s.student_policies()) == 3
    assert s.student_policy("blair")["effective_runtime_max_seconds"] == 1200
