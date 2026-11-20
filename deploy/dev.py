"""Run the local-only development topology. Run from the repository root."""
import argparse
import base64
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

REPO = Path(__file__).resolve().parents[1]
DEMO_JOBS = (
    ("alex", "ResNet baseline"),
    ("blair", "Robot vision training"),
    ("casey", "Image classifier"),
    ("alex", "Learning rate sweep"),
)


def request(base, path, body=None, token=None, key=None):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if key:
        headers["Idempotency-Key"] = key
    if body is not None:
        headers["Content-Type"] = "application/json"
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{path}: {exc.code} {exc.read().decode()[:500]}") from exc


def seed(base):
    source = base64.b64encode(b'print("CIT demonstration job", flush=True)\n').decode()
    for number, (identity, name) in enumerate(DEMO_JOBS, 1):
        token = request(base, "/v1/auth/dev", {"identity": identity})["token"]
        owned = request(base, "/v1/projects", token=token)["items"]
        project = next((p for p in owned if p["name"] == name and p["owner_id"] == identity), None)
        if project is None:
            project = request(base, "/v1/projects", {"name": name}, token)
        manifest = {"schema_version": 1, "name": name, "environment": "python-cpu-v1",
                    "entrypoint": "train.py", "runtime_seconds": 3600, "args": [],
                    "dataset_ids": [], "output_dir": "outputs"}
        revision = request(base, f"/v1/projects/{project['id']}/revisions",
                           {"filename": "train.py", "content_base64": source, "manifest": manifest}, token)
        request(base, "/v1/jobs", {"revision_id": revision["id"], "profile": "simulated"},
                token, f"classroom-demo-{number}")
        # Keep submission order deterministic even on coarse platform clocks.
        time.sleep(.05)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=Path.cwd() / ".gpuhub")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--demo", action="store_true", help="Seed four example submissions for three identities")
    parser.add_argument("--backend", choices=["demo", "docker"], default="demo")
    parser.add_argument("--demo-seconds", type=int, default=900)
    args = parser.parse_args()
    args.state = args.state.resolve()
    base = f"http://127.0.0.1:{args.port}"
    with socket.socket() as check:
        try:
            check.bind(("127.0.0.1", args.port))
        except OSError:
            parser.exit(2, f"Port {args.port} is already in use. Select --port or stop the existing hub.\n")
    args.state.mkdir(mode=0o700, parents=True, exist_ok=True)
    db, data = args.state / "hub.sqlite3", args.state / "data"
    env = {**os.environ, "GPUHUB_DB": str(db), "GPUHUB_DATA": str(data), "PYTHONUNBUFFERED": "1"}
    from services.scheduler.store import Store
    store = Store(db)
    if args.demo:
        store.plan_node("dgx-spark", "DGX Spark", "spark-fixed-half")
    children = []
    def start(module, *argv):
        child = subprocess.Popen([sys.executable, "-m", module, *map(str, argv)], cwd=REPO, env=env)
        children.append(child)

    try:
        start("services.api", "--dev-auth", "--port", args.port)
        for _ in range(100):
            if children[0].poll() is not None:
                raise RuntimeError("The API exited during startup.")
            try:
                if request(base, "/healthz").get("status") == "ok":
                    break
            except (OSError, RuntimeError):
                pass
            time.sleep(.1)
        else:
            raise RuntimeError("API did not become ready within the startup window.")
        seeded = args.state / "classroom-demo-v1.complete"
        if args.demo and not seeded.exists():
            seed(base)
            seeded.touch()
        start("services.worker", "run", "--db", db, "--root", data, "--node",
              "cit-development", "--name", "CIT Development", "--backend", args.backend,
              "--demo-seconds", args.demo_seconds)
        start("services.scheduler", "--db", db)
        print(f"GPU Hub is listening on {base} • Development auth • {args.backend} worker", flush=True)
        while True:
            for child in children:
                if (result := child.poll()) is not None:
                    raise RuntimeError(f"Service exited ({result}): {child.args[2]}")
            time.sleep(.5)
    except KeyboardInterrupt:
        pass
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == "__main__":
    main()
