import datetime
import json
import os
import platform
import re
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

from packages.observability import cgroup_usage, gpu_inventory


class RuntimeUnavailable(RuntimeError):
    """Execution or cleanup could not be established; retain this allocation."""


class AdmissionError(ValueError):
    """A pre-launch validation failed. No process was started by this operation."""


def invoke(*args, timeout=10, missing=False, combine=False):
    try:
        p = subprocess.run(["docker", *args], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT if combine else subprocess.PIPE,
                           timeout=timeout, text=True, encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeUnavailable(f"Docker unavailable: {exc}") from exc
    if p.returncode:
        message = (p.stderr or p.stdout or "").strip()
        if missing and ("No such object:" in message or "No such container:" in message):
            return None
        raise RuntimeUnavailable(message[:600] or "Docker call failed")
    return p.stdout


def timestamp(value):
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def name(attempt):
    if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,100}", attempt["id"]):
        raise AdmissionError("Invalid opaque attempt identifier")
    return f"gpuhub-{attempt['id']}-{int(attempt['generation'])}"


def bind(source, target, writable=False):
    source = Path(source).resolve(strict=True)
    if any(ch in str(source) + target for ch in ',\n\r"'):
        raise AdmissionError("Unsupported mount path")
    return ["--mount", f"type=bind,source={source},target={target}" + ("" if writable else ",readonly")]


CAPABILITIES = {
    "cpu_hard_quota", "host_memory_hard_limit", "gpu_compute_aggregate_limit",
    "gpu_memory_aggregate_limit", "descendant_containment", "routing_bypass_prevention",
    "runtime_deadline_enforcement", "verified_runtime_configuration",
}


class SparkFixedHalfBackend:
    def validate_profile(self, *_):
        raise AdmissionError("BLOCKED_UNVERIFIED_ENFORCEMENT: no qualified GB10 aggregate boundary and teardown")


class DellMigBackend:
    def __init__(self, evidence=None):
        self.evidence = evidence or {}

    def validate_profile(self, inventory):
        e = self.evidence
        if not e.get("fingerprint") or e["fingerprint"] != inventory.get("fingerprint"):
            raise AdmissionError("Dell qualification fingerprint missing or changed")
        if any(e.get("capabilities", {}).get(k) != "VERIFIED" for k in CAPABILITIES):
            raise AdmissionError("Dell qualification evidence is incomplete")
        if not e.get("observed_at"):
            raise AdmissionError("Dell qualification has no test timestamp")
        gpu, nvidia, error = gpu_inventory()
        if (error or nvidia != inventory.get("nvidia") or gpu != inventory.get("gpu_devices") or
                len(gpu) != 1 or gpu[0]["mig_mode"] != "Enabled" or
                "RTX PRO 6000" not in gpu[0]["model"] or "Blackwell" not in gpu[0]["model"]):
            raise AdmissionError("Live Dell GPU and fixed layout do not match qualification")
        layout = nvidia.get("layout") or ""
        instances = re.findall(r"^\s*MIG\s+(\S+)\s+Device\s+\d+:\s*\(UUID:\s*(MIG-[\w-]+)\)", layout, re.M)
        devices = e.get("devices", {})
        if (len(instances) != 2 or any(p != "2g.48gb" for p, _ in instances)
                or set(devices) != {"A", "B"} or len(set(devices.values())) != 2
                or {u for _, u in instances} != set(devices.values())
                or any(type(e.get("device_memory_bytes", {}).get(s)) is not int or
                       e["device_memory_bytes"][s] <= 0 for s in devices)):
            raise AdmissionError("Expected two measured, preprovisioned 2g.48gb MIG devices")
        # A working per-instance process API is mandatory for release verification.
        for slot, device in devices.items():
            self.processes(device)
            if self.memory(device)["total_bytes"] != e["device_memory_bytes"][slot]:
                raise AdmissionError("MIG device memory readback differs from qualification")

    @contextmanager
    def handle(self, device):
        try:
            import pynvml
            pynvml.nvmlInit()
            try:
                yield pynvml, pynvml.nvmlDeviceGetHandleByUUID(device)
            finally:
                pynvml.nvmlShutdown()
        except Exception as exc:
            raise RuntimeUnavailable("Per-instance NVML readback unavailable; quarantine allocation") from exc

    def processes(self, device):
        with self.handle(device) as (nvml, handle):
            compute = nvml.nvmlDeviceGetComputeRunningProcesses(handle)
            graphics = nvml.nvmlDeviceGetGraphicsRunningProcesses(handle)
            return [p.pid for p in [*compute, *graphics]]

    def memory(self, device):
        with self.handle(device) as (nvml, handle):
            sample = nvml.nvmlDeviceGetMemoryInfo(handle)
            return {"scope": "MIG device", "status": "MEASURED", "observed_at": time.time(),
                    "total_bytes": sample.total, "used_bytes": sample.used, "free_bytes": sample.free}


class CpuContainerBackend:
    mode = "docker"

    def __init__(self, node, image, inventory, evidence=None):
        self.node = node
        self.inventory = inventory
        self.mig = DellMigBackend(evidence) if evidence else None
        if (inventory["os"] != "Linux" or inventory["runtime_os"] != "linux"
                or inventory["cgroup_version"] != 2 or str(inventory["runtime_cgroup_version"]) != "2"
                or not {"memory", "cpu", "pids"}.issubset(inventory["controllers"])):
            raise AdmissionError("Run the worker on native Linux with a local Docker daemon and cgroup v2 cpu/memory/pids")
        context = json.loads(invoke("context", "inspect"))[0]["Endpoints"]["docker"]["Host"]
        endpoint = os.environ.get("DOCKER_HOST", context)
        if not endpoint.startswith("unix://"):
            raise AdmissionError("Worker requires a local Unix Docker socket")
        image_info = json.loads(invoke("image", "inspect", image))[0]
        aliases = {"x86_64": "amd64", "aarch64": "arm64"}
        if image_info["Architecture"] != aliases.get(platform.machine(), platform.machine()):
            raise AdmissionError("Curated environment architecture does not match this host")
        if image_info.get("Os") != "linux" or image_info.get("Config", {}).get("Volumes"):
            raise AdmissionError("Curated runtime must be a Linux image without implicit writable volumes")
        self.image = image_info["Id"]
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", self.image):
            raise AdmissionError("Missing immutable local image ID")
        if self.mig:
            self.mig.validate_profile(inventory)

    def owned(self):
        return {n for n in invoke("ps", "-a", "--filter", "label=cit.gpuhub.managed=v1",
                "--filter", f"label=cit.gpuhub.node={self.node}", "--format", "{{.Names}}").splitlines() if n}

    def inspect(self, attempt):
        data = invoke("container", "inspect", name(attempt), missing=True)
        if data is None:
            return None
        box = json.loads(data)[0]
        labels = box["Config"].get("Labels") or {}
        expected = {"cit.gpuhub.managed": "v1", "cit.gpuhub.node": self.node,
                    "cit.gpuhub.attempt": attempt["id"], "cit.gpuhub.generation": str(attempt["generation"])}
        if any(labels.get(k) != v for k, v in expected.items()):
            raise RuntimeUnavailable("Container identity mismatch; operator reconciliation required")
        env = attempt["envelope"]
        host = box["HostConfig"]
        bound = {"Memory": env["memory_bytes"], "MemorySwap": env["memory_bytes"], "CpuPeriod": env["cpu_period_us"],
                 "CpuQuota": env["cpu_quota_us"], "PidsLimit": env["pids_max"], "Privileged": False,
                 "ReadonlyRootfs": True, "NetworkMode": "none", "CgroupnsMode": "private"}
        if any(host.get(k) != v for k, v in bound.items()) or box["Config"]["User"] != "65532:65532":
            raise RuntimeUnavailable("Configured host resource or confinement controls differ from reservation")
        requests = host.get("DeviceRequests") or []
        gpu = attempt.get("kind") != "BUILD" and attempt.get("profile") == "dell-mig"
        if gpu:
            expected_device = env.get("device_id")
            if not expected_device or len(requests) != 1 or requests[0].get("DeviceIDs") != [expected_device]:
                raise RuntimeUnavailable("Container device request differs from the fixed slot")
        elif requests or host.get("Devices") or host.get("Runtime") != "runc":
            raise RuntimeUnavailable("CPU container has unexpected device configuration")
        return box

    def create(self, a, source, paths, dataset_dirs, resume, *, build=None):
        env, m = a["envelope"], a["manifest"]
        limit = {k: int(env[k]) for k in ("memory_bytes", "cpu_quota_us", "cpu_period_us", "pids_max")}
        if min(limit.values()) <= 0:
            raise AdmissionError("Invalid resource envelope")
        gpu = bool(self.mig and not build and a["profile"] == "dell-mig")
        if gpu:
            self.mig.validate_profile(self.inventory)
            device = self.mig.evidence["devices"][a["slot"]]
            if a["envelope"].get("device_id") != device:
                raise AdmissionError("Assigned MIG device differs from the qualified slot mapping")
            if self.mig.processes(device):
                raise RuntimeUnavailable("Assigned MIG device is occupied")
        sdk = Path(__file__).resolve().parents[1] / "runner_sdk"
        supervisor = Path(__file__).resolve().parents[2] / "services/worker/bootstrap.py"
        cpu, period = limit["cpu_quota_us"], limit["cpu_period_us"]
        mem, pids = limit["memory_bytes"], limit["pids_max"]
        args = ["create", "--name", name(a), "--pull", "never", "--network", "none",
                "--user", "65532:65532", "--read-only", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges=true", "--cgroupns", "private",
                "--cpu-period", str(period), "--cpu-quota", str(cpu), "--memory", str(mem),
                "--memory-swap", str(mem), "--pids-limit", str(pids), "--shm-size", "16m",
                "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m,mode=1777",
                "--tmpfs", "/work:rw,nosuid,nodev,size=16m,mode=1777", "--workdir", "/work",
                "--log-driver", "json-file", "--log-opt", "max-size=1m", "--log-opt", "max-file=2",
                "--restart", "no", "--no-healthcheck", "--entrypoint", "python"]
        if gpu:
            args.extend(["--gpus", f"device={device}", "--env", "NVIDIA_DRIVER_CAPABILITIES=compute,utility"])
        else:
            args.extend(["--runtime", "runc", "--env", "NVIDIA_VISIBLE_DEVICES=void"])
        for label, value in {"managed": "v1", "node": self.node, "attempt": a["id"],
                             "generation": str(a["generation"])}.items():
            args.extend(["--label", f"cit.gpuhub.{label}={value}"])
        check = {"cpu.max": f"{cpu} {period}", "memory.max": str(mem),
                 "memory.swap.max": "0", "pids.max": str(pids)}
        output = "/work/" + m.get("output_dir", "outputs")
        checkpoint = "/work/" + (m.get("checkpoint") or {}).get("directory", "checkpoints")
        vars_ = {"GPUHUB_CGROUP_READBACK": json.dumps(check), "GPUHUB_RUNTIME_SECONDS": str(a["budget"]),
                 "GPUHUB_OUTPUT_DIR": output, "GPUHUB_CHECKPOINT_DIR": checkpoint,
                 "GPUHUB_CONTROL_DIR": "/control", "PYTHONPATH": "/opt/hub:/deps:/source",
                 "GPUHUB_ATTEMPT_ID": a["id"], "GPUHUB_JOB_ID": a["job_id"],
                 "GPUHUB_REVISION_ID": a["revision_id"], "GPUHUB_ENVIRONMENT": self.image,
                 "GPUHUB_DATASET_IDS": json.dumps(m.get("dataset_ids", [])),
                 "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1", "HOME": "/tmp"}
        for key in ("GPUHUB_CHECKPOINT_BYTES", "GPUHUB_MIN_FREE_BYTES"):
            if key in os.environ:
                vars_[key] = os.environ[key]
        args.extend(bind(source, "/src" if build else "/source"))
        args.extend(bind(supervisor, "/opt/bootstrap.py"))
        args.extend(bind(sdk, "/opt/hub/runner_sdk"))
        args.extend(bind(paths["control"], "/control"))
        if build:
            args.extend(bind(build["staging"], "/build", writable=True))
            args.extend(bind(build["wheelhouse"], "/wheels"))
            argv = build["argv"]
        else:
            args.extend(bind(paths["outputs"], "/job-data/outputs", writable=True))
            args.extend(bind(paths["checkpoints"], "/job-data/checkpoints", writable=True))
            if a.get("cache_path"):
                args.extend(bind(a["cache_path"], "/deps"))
            for dataset, path in dataset_dirs.items():
                args.extend(bind(path, f"/datasets/{dataset}"))
            if resume:
                args.extend(bind(resume, "/resume"))
                vars_["GPUHUB_RESUME_DIR"] = "/resume"
            argv = ["/source/" + m["entrypoint"], *m.get("args", [])]
        for key, value in vars_.items():
            args.extend(["--env", f"{key}={value}"])
        invoke(*args, self.image, "-I", "-S", "/opt/bootstrap.py", *argv)

    def start(self, a):
        invoke("start", name(a))

    def signal(self, a, sig):
        invoke("kill", "--signal", sig, name(a))

    def released(self, a, box):
        if box is None or box["State"]["Status"] not in {"exited", "created"} or box["State"]["Pid"] != 0:
            return False
        if a["kind"] != "BUILD" and a["profile"] == "dell-mig":
            if not self.mig or a["envelope"].get("device_id") != self.mig.evidence["devices"][a["slot"]]:
                return False
            return not self.mig.processes(a["envelope"]["device_id"])
        return True

    def remove(self, a):
        invoke("rm", name(a))

    def logs(self, a, after=None):
        argv = ["logs", "--timestamps"]
        if after:
            argv += ["--since", after]
        return invoke(*argv, name(a), combine=True).splitlines()

    def metrics(self, box):
        data = cgroup_usage(box["State"]["Pid"])
        result = {"status": "MEASURED" if data else "UNAVAILABLE", "observed_at": time.time(),
                  "scope": "attempt", "cgroup": data,
                  "memory_bytes": int(data["memory.current"]) if "memory.current" in data else None,
                  "gpu_activity": None, "gpu_activity_status": "UNAVAILABLE"}
        requests = box["HostConfig"].get("DeviceRequests")
        if self.mig and requests:
            result["gpu_memory"] = self.mig.memory(requests[0]["DeviceIDs"][0])
        return result


class SimulatedGpuBackend(CpuContainerBackend):
    """Only GPU allocation is modeled. CPU execution uses the capped Linux container."""
