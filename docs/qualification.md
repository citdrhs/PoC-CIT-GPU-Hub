# Hardware qualification

The shipped demonstration uses a synthetic two-slot worker. Physical CPU containment, DGX Spark GPU isolation, and the Dell MIG configuration remain unverified on the school machines. A successful simulated run provides no hardware evidence. Keep qualification records with the operator and use the same operating account, configuration and image for repeat measurements.

## Read-only inventory

Run from the repository root on each candidate worker. Capture the output, date, node, OS/kernel, architecture and exact software versions in the qualification record.

```sh
python -m services.worker probe
docker version
docker info
nvidia-smi -L
nvidia-smi --query-gpu=name,uuid,pci.bus_id,driver_version,vbios_version,mig.mode.current --format=csv
nvidia-ctk --version
cat /proc/meminfo
cat /sys/fs/cgroup/cgroup.controllers
cat /proc/pressure/memory
```

The probe reports OS-visible memory in bytes, accessible CPU affinity, cgroup controllers, Docker configuration, NVIDIA inventory, metric availability and an inventory fingerprint. Missing commands or unavailable counters produce diagnostics or null values. Record the proposed host reserve in bytes and CPU reserve separately. Collect host pressure at idle and during two bounded jobs before adopting those reserves for the pilot.

## Native CPU-container qualification

Use an idle, native Linux host with a local Unix Docker socket, cgroup v2 CPU, memory and PIDs controllers, and the curated local Python image built using the repository instructions. Run the test in the worker's environment. The integration suite creates temporary projects and sends CPU jobs through the worker. One case exercises a parent and child sharing a half-CPU quota. Another requests a bounded child allocation above a 128 MiB cgroup limit. Both require confirmed cleanup.

```sh
GPUHUB_IMAGE=cit-gpu-hub:local GPUHUB_TEST_DOCKER=1 python -m unittest discover -s tests/hardware -p test_execution.py -v
```

The Windows development host lacked a running Linux Docker engine. Both native tests reported skipped. A simulated worker test covers controller outage, durable intent, stop, and restart logic; physical process-tree cleanup, deadline accuracy, recovery after daemon failure, stress behavior and containment still require measurements. Capture the observed CPU throttling, actual cgroup limits, OOM evidence and surviving host/neighbor workload.

The container runs as UID/GID 65532. The immutable source, SDK, bootstrap, public dataset files, native wheelhouse and published build cache must be readable from their respective private mounts by that identity. Keep operator credentials and the writable controller database outside those mounts.

## Dell setup during a maintenance window

This adapter targets **one RTX PRO 6000 Blackwell parent GPU**, exactly two plain `2g.48gb` GPU instances, and one full-sized compute instance in each. NVIDIA lists each profile at half the SMs and half the memory on this card. Confirm card edition, firmware, driver and Container Toolkit compatibility against the current NVIDIA tables. Workstation and Max-Q Workstation editions require compute display mode and compatible vBIOS. Handle any required display-mode or firmware change through the school's normal maintenance process with independent remote access available. [Supported GPUs](https://docs.nvidia.com/datacenter/tesla/mig-user-guide/latest/supported-gpus.html) · [Profile table](https://docs.nvidia.com/datacenter/tesla/mig-user-guide/latest/supported-mig-profiles.html) · [Prerequisites and setup](https://docs.nvidia.com/datacenter/tesla/mig-user-guide/latest/getting-started-with-mig.html)

1. Drain the node. Wait for every allocation to exit and verify that the parent GPU has no active clients. Resolve any unknown allocations before changing the device. Save its original layout and obtain approval for the maintenance change.
2. Set `TARGET_GPU` to the confirmed full GPU UUID from the inventory. Inspect the local command reference with `nvidia-smi mig --help`. Inspect MIG mode and existing instances. If the target layout already exists, use its UUIDs and proceed to readback. If a different layout exists, stop and have its owner explicitly approve a separate reconfiguration.
3. On an idle card with compatible prerequisites, enable MIG if required. This command changes GPU mode and may reset the selected GPU. Confirm the active mode after it completes. A pending change needs operator resolution before continuing.

```sh
sudo nvidia-smi -i "${TARGET_GPU:?set the confirmed full GPU UUID}" -mig 1
nvidia-smi -i "$TARGET_GPU" --query-gpu=mig.mode.current,mig.mode.pending --format=csv
nvidia-smi mig -i "$TARGET_GPU" -lgip
nvidia-smi mig -i "$TARGET_GPU" -lgi
```

4. On an empty layout with two placements available for the exact profile, create both GPU instances and the corresponding compute instances. A partial or failed creation requires a new inventory and operator review.

```sh
sudo nvidia-smi mig -i "${TARGET_GPU:?}" -cgi 2g.48gb,2g.48gb -C
nvidia-smi mig -i "$TARGET_GPU" -lgi
nvidia-smi mig -i "$TARGET_GPU" -lci
nvidia-smi -L
```

5. Record the two unique MIG UUIDs, their stable A/B assignment, driver, firmware and per-instance capacity. The worker requires functioning host Python NVML bindings (`import pynvml`). Read the actual per-handle byte totals using the same bindings. Instance recreation can change the UUIDs. After a reset or reboot, inspect the layout again before admission. The scheduler never provisions, resets or enlarges a GPU. [MIG creation and persistence](https://docs.nvidia.com/datacenter/tesla/mig-user-guide/latest/getting-started-with-mig.html) · [NVML device queries](https://docs.nvidia.com/deploy/nvml-api/latest/api/group__nvmlDeviceQueries.html)

```sh
python - <<'PY'
import os
from packages.execution_backends.docker import DellMigBackend
backend = DellMigBackend()
for slot in ("A", "B"):
    device = os.environ["MIG_SLOT_" + slot]
    print(slot, device, backend.memory(device), backend.processes(device))
PY
```

Set `MIG_SLOT_A` and `MIG_SLOT_B` to the two measured UUIDs first. The host reserve applies to system RAM. Each MIG device already represents one half-card allocation.

## Evidence and admission

Set `GPUHUB_IMAGE` to the approved local native image and set `GPUHUB_RESERVE_BYTES` and `GPUHUB_RESERVE_CPUS` to the operator-chosen integers. The following command prints a startup refusal containing the candidate **worker qualification fingerprint**. Without an evidence file the Dell path stops before node registration or runtime launch. This fingerprint includes the inventory fingerprint, immutable image ID, build recipe and both reserves.

```sh
python -m services.worker run --node dell --backend dell-mig --name "Dell GPU worker" --once
```

The JSON below is an **unqualified template**. Store the measured records, operator, dates, exact versions, raw test outputs and failure/recovery observations alongside it. Set each capability to `VERIFIED` only after the physical acceptance tests for that capability pass. The timestamp is UTC Unix seconds. Both device memory values are integer bytes reported by NVML. Protect the file from modification by student accounts.

```json
{
  "fingerprint": "",
  "observed_at": 0,
  "capabilities": {
    "cpu_hard_quota": "UNKNOWN",
    "host_memory_hard_limit": "UNKNOWN",
    "gpu_compute_aggregate_limit": "UNKNOWN",
    "gpu_memory_aggregate_limit": "UNKNOWN",
    "descendant_containment": "UNKNOWN",
    "routing_bypass_prevention": "UNKNOWN",
    "runtime_deadline_enforcement": "UNKNOWN",
    "verified_runtime_configuration": "UNKNOWN"
  },
  "devices": {"A": "", "B": ""},
  "device_memory_bytes": {"A": 0, "B": 0}
}
```

On an idle, qualified Dell, set the absolute file path and run the readback test. It checks the fingerprint, current parent and layout, unique MIG mappings, measured memory and absence of compute/graphics clients on both instances. It requires the same image and reserve environment variables as the worker. The delivered run skipped this test because a qualified Dell and operator evidence were unavailable.

```sh
export GPUHUB_TEST_DELL_EVIDENCE=/etc/gpuhub/qualification/dell.json
python -m unittest discover -s tests/hardware -p test_execution.py -v
```

After all physical acceptance work passes, start the Dell worker only in a supported deployment with authorized access to its controller database and storage.

```sh
python -m services.worker run --node dell --name "Dell GPU worker" --backend dell-mig --evidence "$GPUHUB_TEST_DELL_EVIDENCE"
```

At launch the worker checks the live layout and assigned device UUID and supplies that UUID to Docker. It checks the container's device request, host caps and runtime identity. Before release it requires confirmed container exit and empty per-instance process readback. A readback failure retains the allocation. Qualify again after a relevant driver, firmware, image, device mapping or enforcement change. This release's control plane uses local SQLite and local storage. A separate physical Dell must stay outside the Spark's active school deployment until an authenticated cross-host coordination and storage/staging design has been implemented and tested.

## Outstanding physical acceptance work

| Condition | Evidence required on the proposed node |
| --- | --- |
| Fixed GPU compute and memory | Read back both capacity allocations, including SM counts and per-instance usable memory, with one job active, then two. Starting or finishing the neighbor must leave the survivor's allocation unchanged. |
| Isolation of child processes and device access | Run normal multi-process and multi-context training in each slot. Verify every child has only its own assigned device and charged CPU/memory envelope. Verify the parent GPU, other slot, runtime socket and management capabilities are inaccessible from student jobs. |
| Memory pressure | Measure host, device, pinned and managed allocations with a bounded test job. Confirm the configured hard boundary, record warning and stop behavior, and verify the host and neighboring job remain usable. |
| Deadlines and recovery | Exercise cancellation during startup, runtime expiry, scheduler/API outage, worker restart, lost heartbeat, worker journal loss and failed cleanup. Record elapsed deadlines, checkpoint outcomes, descendants, GPU clients, and retention of unknown allocations. |
| Data and builds | Verify host quotas/headroom, bounded offline build execution, actual artifact and checkpoint access after container removal, and explicit checkpoint resume inside the target image. |
| Classroom pilot | Run representative student projects under two-user load. Measure reserve headroom, queue waits, pressure, checkpoint overhead and failure rates before selecting pilot settings. |

## DGX Spark

The DGX Spark remains `BLOCKED_UNVERIFIED_ENFORCEMENT`. The repository has no enabled Spark GPU launch path. An operator resume, a per-client MPS percentage, a utilization graph or a successful probe cannot qualify it. CPU-only work and simulated development can run under the corresponding backend. [NVIDIA supported MIG devices](https://docs.nvidia.com/datacenter/tesla/mig-user-guide/latest/supported-gpus.html) · [Spark known issues](https://docs.nvidia.com/dgx/dgx-spark/known-issues.html)

A future adapter needs physical evidence that all contexts share the half-compute limit, that every host/device/pinned/managed allocation fits one combined physical-memory envelope, and that routing cannot broaden device access. Test idle-slot behavior, restart, resource pressure and failure containment. For MPS, a witnessed cooperative synchronization or supported GPU-client termination must precede forced OS termination. Verify the other slot remains healthy. Record scoped metric availability and treat unsupported counters as unavailable. The explicit Spark hardware test stays skipped until such an adapter and qualified test host exist. [NVIDIA MPS guide](https://docs.nvidia.com/deploy/mps/)
