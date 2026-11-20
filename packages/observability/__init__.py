"""Read-only host discovery. Inventory is never qualification evidence on its own."""
import hashlib
import json
import os
import platform
import shutil
import subprocess
import time
from pathlib import Path
from xml.etree import ElementTree


def command(argv, timeout=5):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    if result.returncode:
        return None, result.stderr.strip()[:600] or "Command failed"
    return result.stdout.strip(), None


def read(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def memory():
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD)] + [
                (key, ctypes.c_ulonglong) for key in (
                    "total", "available", "pagefile", "available_pagefile",
                    "virtual", "available_virtual", "extended_virtual")]

        value = MemoryStatus()
        value.length = ctypes.sizeof(value)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(value)):
            return {"MemTotal": value.total, "MemAvailable": value.available}
        return {}
    info = read("/proc/meminfo") or ""
    return {row.split(":")[0]: int(row.split()[1]) * 1024 for row in info.splitlines()
            if row.startswith(("MemTotal:", "MemAvailable:"))}


def gpu_inventory():
    if not shutil.which("nvidia-smi"):
        return [], None, "nvidia-smi unavailable"
    xml, error = command(["nvidia-smi", "-q", "-x"])
    if error:
        return [], None, error
    try:
        tree = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return [], None, "Invalid NVIDIA inventory XML"
    listing, _ = command(["nvidia-smi", "-L"])
    return [{"model": g.findtext("product_name"), "uuid": g.findtext("uuid"),
             "memory_report": g.findtext("fb_memory_usage/total"),
             "mig_mode": g.findtext("mig_mode/current_mig"),
             "firmware": g.findtext("vbios_version")} for g in tree.findall("gpu")], {
                 "driver": tree.findtext("driver_version"), "layout": listing}, None


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def probe():
    mem = memory()
    ctrls = read("/sys/fs/cgroup/cgroup.controllers")
    info, docker_error = command(["docker", "info", "--format", "{{json .}}"])
    try:
        docker = json.loads(info or "{}")
    except (TypeError, ValueError):
        docker, docker_error = {}, "Invalid Docker info response"
    devices, nvidia, gpu_error = gpu_inventory()
    cpu_count = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count()
    inventory = {
        "kernel": platform.release(), "os": platform.system(), "architecture": platform.machine(),
        "logical_cpus": cpu_count, "cpu_affinity": sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
        "memory_bytes": mem.get("MemTotal"), "cgroup_version": 2 if ctrls is not None else None,
        "controllers": sorted((ctrls or "").split()), "runtime_version": docker.get("ServerVersion"),
        "runtime_id": docker.get("ID"), "runtime_architecture": docker.get("Architecture"),
        "runtime_os": docker.get("OSType"), "runtime_cgroup_version": docker.get("CgroupVersion"),
        "runtime_cgroup_driver": docker.get("CgroupDriver"),
        "runtime_drivers": sorted((docker.get("Runtimes") or {}).keys()), "gpu_devices": devices,
        "nvidia": nvidia, "mps": "UNKNOWN", "docker_error": docker_error, "gpu_error": gpu_error,
        "host_memory_available_bytes": mem.get("MemAvailable"), "host_memory_pressure": read("/proc/pressure/memory"),
        "metrics": {"gpu_activity": {"value": None, "scope": "instance", "status": "UNSUPPORTED"},
                    "host_memory": {"scope": "node", "status": "MEASURED" if mem else "UNSUPPORTED"}},
        "observed_at": time.time(),
    }
    possible = "AVAILABLE_UNVERIFIED"
    inventory["capabilities"] = {
        "cpu_hard_quota": possible if "cpu" in inventory["controllers"] else "UNKNOWN",
        "host_memory_hard_limit": possible if "memory" in inventory["controllers"] else "UNKNOWN",
        "descendant_containment": possible if "pids" in inventory["controllers"] else "UNKNOWN",
        "gpu_compute_aggregate_limit": "UNKNOWN", "gpu_memory_aggregate_limit": "UNKNOWN",
        "unified_memory_aggregate_accounting": "UNKNOWN", "routing_bypass_prevention": "UNKNOWN",
        "runtime_deadline_enforcement": "UNKNOWN", "verified_runtime_configuration": "UNKNOWN"}
    inventory["enforcement_readback"] = None
    transient = {"host_memory_available_bytes", "host_memory_pressure", "observed_at", "docker_error", "gpu_error"}
    inventory["fingerprint"] = fingerprint({k: v for k, v in inventory.items() if k not in transient})
    return inventory


def cgroup_usage(pid):
    """Read the actual charged cgroup v2 hierarchy from the native Linux worker."""
    relative = read(f"/proc/{pid}/cgroup")
    if not relative:
        return {}
    paths = [row[3:] for row in relative.splitlines() if row.startswith("0::/")]
    if len(paths) != 1 or paths[0] == "/":
        return {}
    base = Path("/sys/fs/cgroup").resolve()
    path = (base / paths[0].lstrip("/")).resolve()
    if not path.is_relative_to(base):
        return {}
    result = {}
    for key in ("cpu.max", "memory.max", "memory.current", "memory.peak", "memory.swap.max",
                "memory.events", "pids.current", "pids.max", "cpu.stat"):
        value = read(path / key)
        if value is not None:
            result[key] = value
    return result
