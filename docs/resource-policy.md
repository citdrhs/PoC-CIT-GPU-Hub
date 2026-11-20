# Resource policy

Each registered worker exposes two slots, A and B. The single-host controller uses one SQLite database on a local filesystem. The development fixture can reserve both slots for distinct students and simulate lifecycle events. Physical CPU and memory limits require the Linux Docker worker. Strict GPU execution additionally requires backend qualification.

## Capacity per slot

The trusted worker supplies OS-visible memory in bytes, the logical CPUs available to it, a combined memory reserve and a CPU reserve. The policy computes both slots from that inventory at registration.

```python
usable = memory_bytes - reserve_bytes
slot_memory = (usable // (2 * alignment_bytes)) * alignment_bytes
slot_cpu_quota_us = (logical_cpus - reserved_cpus) * period_us // 2
```

Default alignment is 1 MiB and the CPU period is 100,000 microseconds. Invalid inventory fails registration. With 128 GiB of measured RAM, a 16 GiB reserve, 20 available CPUs and 4 reserved CPUs, each slot gets 56 GiB and 800,000 microseconds of aggregate CPU time per 100,000-microsecond period. The demonstration uses these numbers as fixture data.

The frozen envelope carries its policy version, limits, warning and stop percentages, process limit, swap allowance and GPU fraction. The setting for a new job uses the capacity at allocation time. A neighbor's completion never modifies the running job. Current memory availability can close admission or trigger a cooperative stop. The process hierarchy shares one CPU and memory budget. The 85% warning and 90% cooperative stop leave the configured hard cap intact and a sudden allocation can still fail first. API observations expose the measurement time and scope when available.

A simulated GPU run receives a modeled 1/2 GPU assignment. A CPU run or dependency build receives no GPU assignment. On a qualified Dell, the one assigned whole 2g.48gb MIG instance supplies the GPU boundary, and its measured device-memory capacity is separate from host RAM. Spark would require one physical budget for combined CPU/GPU memory across all applicable paths. Its execution backend stays blocked.

## Queue selection

Within each worker's pool the scheduler does the following in one write transaction.

1. Reject stale, paused, pressured, unqualified and quarantined nodes. Recheck the submitting user's enabled status, project membership and dataset access against the catalog. Require an installed environment, staged datasets and a compatible profile.
2. Select each user's oldest eligible job by enqueue time and stable job ID. A single global active-envelope constraint excludes users already holding a training or build reservation.
3. Compute each candidate's ordering key as its enqueue timestamp plus a usage penalty. The penalty is elapsed allocation seconds accrued by that principal in the past 24 hours on the same backend, capped at 300 seconds. The lowest score wins. Earlier enqueue time then stable job ID break ties. After a successful resume, the job rejoins with a new enqueue time.
4. Fill free slots in A/B order. Persist one immutable attempt ID, a monotonically increasing per-job generation and the active reservation together. The worker consumes that record as a durable launch command. The UI reports running after worker confirmation.

This bounds the extent that new arrivals can jump an older eligible candidate to five minutes of queue age. Time spent behind earlier work still depends on the backlog, runtime limit, and availability of compatible capacity. No hardware-speed comparison is assumed between Spark, Dell and simulation. Teachers can drain nodes; this version uses a single student priority class.

Teachers can set an individual student's maximum runtime from one second through the site maximum. Clearing the override restores the site maximum. The override applies to new submissions and explicit resumes, including a teacher resuming that student's job. Previously accepted work retains its original runtime. Disabling access revokes the student's tokens and holds queued work. Active attempts keep their existing deadlines. Reenabling access allows fresh sign-in and resumes queue eligibility. The administrative event history records the actor, student and both policy values for every change.

Unique constraints cover the active (node, slot) pair and the active principal, including dependency preparation. Node-local cached environment results refer to the immutable source revision, catalog environment, dependency lock, architecture, image, recipe and worker fingerprint. When a build completes, its reservation is released after confirmation and the same logical job becomes eligible to run.

## Recovery and time limits

Unknown outcomes retain their active reservation. Missing heartbeats quarantine active attempts and block admission to that worker. A heartbeat alone cannot clear an unknown attempt; the worker must reconcile its container journal. A changed fingerprint or envelope during live execution drains the node. Clearing the administrative drain never qualifies an unsupported backend. Cancellation during startup records durable stop intent. An unconfirmed exit retains the reservation even if the application reported a terminal result.

The attempt deadline is computed from the observed process start, survives restarts, and includes the worker's checkpoint and termination grace. Confirmed termination and backend cleanup allow resource release. Application progress after the latest completed checkpoint can be lost on a hard failure.

| Setting | Default | Purpose |
| --- | ---: | --- |
| `HUB_MAX_RUNTIME_SECONDS` | 7200 s | Maximum submitted attempt runtime |
| `HUB_HEARTBEAT_TIMEOUT` | 45 s | Worker liveness threshold |
| `HUB_QUEUE_TIMEOUT_SECONDS` | 604800 s | Maximum pending queue time |
| `HUB_LAUNCH_TIMEOUT_SECONDS` | 180 s | Request stop on a missing startup acknowledgement while retaining the claim |
| `HUB_BUILD_TIMEOUT_SECONDS` | 900 s | Maximum dependency builder runtime |
| `HUB_LOG_BYTES` | 4194304 bytes | Database log bytes accepted per attempt; overflow generates a truncation event |

The worker and API also enforce storage-specific limits. Poll logs and event pages using each returned record's numeric cursor. Worker log sequence receipts make a replay after a worker crash idempotent.

## Local verification

```sh
python -m pytest -q tests/unit/test_scheduler.py
python -m services.scheduler --db .gpuhub/hub.sqlite3 --once
```

The policy tests exercise byte rounding, alone/paired/queued execution, reservation contention across threads, bounded builders, cancellation during startup, stale heartbeats, role and dataset eligibility, incomplete cleanup, restart recovery and fingerprint gates. They only verify the control-plane rules. The hardware runbook covers separate physical qualification.
