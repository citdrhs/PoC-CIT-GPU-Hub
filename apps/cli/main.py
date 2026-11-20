import argparse
import getpass
import json
import os
import sys
import time
import uuid
from urllib.parse import urlencode

from .client import Client, ClientError, segment
from .display import Screen, clean, label
from .files import download, revision_payload


PROFILES = ("simulated", "cpu", "dell-mig", "spark-fixed-half")


def options():
    common = argparse.ArgumentParser(add_help=False, argument_default=argparse.SUPPRESS)
    common.add_argument("--url", help="API origin (default GPUHUB_URL or http://127.0.0.1:8765)")
    common.add_argument("--identity", help="select a development identity on a local API for this command")
    common.add_argument("--json", action="store_true", help="emit JSON; followed logs use newline-delimited JSON")
    parser = argparse.ArgumentParser(prog="gpuhub", description="CIT GPU Hub terminal client", parents=[common])
    commands = parser.add_subparsers(dest="command")
    def command(name, help_text):
        return commands.add_parser(name, help=help_text, parents=[common])
    command("dashboard", "show worker slots and the admission queue")
    command("login", "save the active identity in the OS credential store")
    for name in ("jobs", "queue", "nodes", "projects", "environments", "datasets"):
        command(name, f"list {name} visible to this identity")
    command("me", "show identity and effective policy")
    submit = command("submit", "submit a .py file, project directory, ZIP, or existing revision")
    submit.add_argument("path", nargs="?")
    submit.add_argument("--revision", help="submit an existing immutable revision")
    submit.add_argument("--project", help="upload to an existing project ID")
    submit.add_argument("--manifest", help="JSON or YAML manifest; directories automatically use gpuhub.json/.yaml")
    submit.add_argument("--name", help="job name and default project name")
    submit.add_argument("--entrypoint", help="project-relative Python entrypoint")
    submit.add_argument("--argument", action="append", help="one entrypoint argument; repeat for additional arguments")
    submit.add_argument("--dataset", action="append", help="approved dataset ID; repeat as needed")
    submit.add_argument("--runtime", type=int, help="attempt runtime in seconds, including shutdown")
    submit.add_argument("--environment", help="approved environment ID")
    submit.add_argument("--profile", choices=PROFILES, default="simulated")
    submit.add_argument("--idempotency-key", help="reuse with --revision for safe job submission retries")
    status = command("status", "inspect a job or open the cluster overview")
    status.add_argument("job_id", nargs="?")
    for name in ("cancel", "resume", "logs", "events", "checkpoints", "artifacts", "download"):
        sub = command(name, {"cancel":"request cancellation", "resume":"resume from a completed checkpoint",
                            "logs":"read attempt logs", "events":"inspect lifecycle events",
                            "checkpoints":"list completed checkpoints", "artifacts":"list available outputs",
                            "download":"download and verify job artifacts"}[name])
        sub.add_argument("job_id")
        if name == "resume":
            sub.add_argument("--checkpoint", required=True)
            sub.add_argument("--idempotency-key")
        if name in ("logs", "events"):
            sub.add_argument("--cursor", type=int, default=0)
        if name == "logs":
            sub.add_argument("--follow", "-f", action="store_true")
        if name == "download":
            sub.add_argument("--output", "-o", required=True)
            sub.add_argument("--artifact", help="download only this artifact ID")
    for name in ("drain", "undrain", "qualification"):
        sub = command(name, {"drain":"stop new admissions on a node", "undrain":"resume admissions on a node",
                             "qualification":"inspect node capability evidence"}[name])
        sub.add_argument("node_id")
    return parser


def print_json(value, stream=None):
    print(json.dumps(value, ensure_ascii=True, separators=(",", ":")), file=stream or sys.stdout, flush=True)


def logs(client, args, screen, machine):
    last = args.cursor
    if last < 0:
        raise ClientError("INVALID_CURSOR", "A log cursor must be nonnegative.")
    pause = 1
    combined = []
    while True:
        path = f"/jobs/{segment(args.job_id)}/logs?" + urlencode({"cursor": last})
        try:
            page = client.call(path)
        except ClientError as exc:
            if not args.follow or not exc.retryable:
                raise
            print(f"Reconnecting from cursor {last} in {pause}s", file=sys.stderr, flush=True)
            time.sleep(pause)
            pause = min(8, pause * 2)
            continue
        pause = 1
        previous = last
        for line in page["items"]:
            cursor = int(line["cursor"])
            if cursor <= last:
                continue
            if machine:
                if args.follow:
                    print_json(line)
                else:
                    combined.append(line)
            else:
                screen.line(clean(line["message"]))
            last = cursor
        if page.get("has_more"):
            last = max(last, int(page["next_cursor"]))
            if last <= previous:
                raise ClientError("INVALID_CURSOR", "Log stream could not advance.")
            continue
        if not args.follow or page.get("terminal"):
            if machine and not args.follow:
                print_json({"items": combined, "next_cursor": last, "terminal": page.get("terminal")})
            return
        time.sleep(1)


def submit_job(client, args):
    if bool(args.path) == bool(args.revision):
        raise ClientError("INVALID_SOURCE", "Specify one source path or one --revision.")
    revision = args.revision
    if not revision:
        payload = revision_payload(args, client.call("/me").get("policy") or {})
        project = args.project or client.call("/projects", {"name": payload["manifest"]["name"]})["id"]
        revision = client.call(f"/projects/{segment(project)}/revisions", payload)["id"]
    key = args.idempotency_key or uuid.uuid4().hex
    try:
        return client.call("/jobs", {"revision_id": revision, "profile": args.profile}, key=key)
    except ClientError as exc:
        if exc.retryable:
            raise ClientError(exc.code, f"{exc}. Retry with --revision {revision} --profile {args.profile}"
                              f" --idempotency-key {key}.") from exc
        raise


def authenticate(client, identity):
    provided = os.environ.get("GPUHUB_TOKEN")
    if not identity and not provided:
        config = client.call("/config")
        if config["dev_auth"]:
            if not sys.stdin.isatty():
                raise ClientError("IDENTITY_REQUIRED", "Use gpuhub login --identity ID on a local development API.")
            print("Local development identities", file=sys.stderr)
            for user in config["identities"]:
                print(f"  {clean(user['id'])}   {clean(user['role'])}", file=sys.stderr)
            print("Identity: ", end="", file=sys.stderr, flush=True)
            identity = input().strip()
            if not identity:
                raise ClientError("IDENTITY_REQUIRED", "Choose a local identity.")
        elif not sys.stdin.isatty():
            raise ClientError("TOKEN_REQUIRED", "Set GPUHUB_TOKEN for non-interactive authentication.")
    if identity:
        client.login_dev(identity)
    else:
        client.token = provided or getpass.getpass("GPU Hub access token: ")
    info = client.call("/me")
    client.save_credentials()
    return {"user": info["user"], "url": client.url}


def run(args):
    command = args.command or "dashboard"
    machine = getattr(args, "json", False)
    client = Client(getattr(args, "url", None) or os.environ.get("GPUHUB_URL", "http://127.0.0.1:8765"))
    identity = getattr(args, "identity", None) or os.environ.get("GPUHUB_IDENTITY")
    screen = Screen()
    if command == "login":
        result = authenticate(client, identity)
    else:
        if identity:
            client.login_dev(identity)
        else:
            client.get_credentials()
        if command == "logs":
            return logs(client, args, screen, machine)
        if command == "dashboard" or (command == "status" and not args.job_id):
            result = {"me": client.call("/me"), "nodes": client.call("/nodes")["items"],
                      "queue": client.call("/queue")["items"], "observed_at": time.time()}
            if not machine:
                return screen.dashboard(result)
        elif command == "status":
            job_path = f"/jobs/{segment(args.job_id)}"
            result = client.call(job_path)
            artifacts = client.call(job_path + "/artifacts")["items"]
            checkpoints = client.call(job_path + "/checkpoints")["items"]
            if not machine:
                return screen.detail(result, artifacts, checkpoints)
            result = {**result, "artifacts": artifacts, "checkpoints": checkpoints}
        elif command in ("jobs", "queue", "nodes", "projects", "environments", "datasets", "me"):
            result = client.call("/" + command)
        elif command == "submit":
            result = submit_job(client, args)
        elif command == "cancel":
            result = client.call(f"/jobs/{segment(args.job_id)}/cancel", {})
        elif command == "resume":
            result = client.call(f"/jobs/{segment(args.job_id)}/resume", {"checkpoint_id": args.checkpoint},
                                 key=args.idempotency_key or uuid.uuid4().hex)
        elif command in ("events", "artifacts", "checkpoints"):
            path = f"/jobs/{segment(args.job_id)}/{command}"
            query = "?" + urlencode({"cursor": args.cursor}) if command == "events" else ""
            result = client.call(path + query)
        elif command == "download":
            result = download(client, args.job_id, args.output, args.artifact)
        else:
            action = "resume" if command == "undrain" else command
            path = f"/admin/nodes/{segment(args.node_id)}/{action}"
            result = client.call(path) if command == "qualification" else client.call(path, {})
    if machine:
        return print_json(result)
    screen.title(command.capitalize(), client.url)
    if command == "nodes":
        for node in result["items"]:
            screen.node(node)
    elif command in ("queue", "jobs"):
        screen.table(["JOB ID", "PROJECT", "STATE", "WAITING REASON"],
                     [[job["id"], job["name"], label(job["state"]), label(job.get("waiting_reason"))]
                      for job in result["items"]])
    elif "items" in result:
        screen.records(result["items"])
    else:
        screen.fields(result)


def main(argv=None):
    args = options().parse_args(argv)
    try:
        run(args)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0
    except (ClientError, OSError, ValueError) as exc:
        code = getattr(exc, "code", "CLIENT_ERROR")
        if getattr(args, "json", False):
            print_json({"error": {"code": code, "message": str(exc)}}, sys.stderr)
        else:
            print(f"{clean(code)}  {clean(exc)}", file=sys.stderr)
        return 1
    return 0
