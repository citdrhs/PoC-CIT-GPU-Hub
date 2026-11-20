# Teacher and operator guide

## Select the execution mode

| Mode | What the student gets | How to start |
| --- | --- | --- |
| Classroom demonstration | Real authentication, queueing, reservations, API and artifacts. A timer generates clearly marked example activity. Uploaded Python stays unexecuted. | `python -m deploy.dev --demo` |
| Native CPU pilot | Submitted Python runs inside Docker with cgroup CPU/memory/PID limits. The simulated profile adds a labeled half-GPU allocation. The CPU profile receives no GPU assignment. | On an idle Linux host, configure the image and reserves below, then run the services individually. |
| Dell MIG | Two operator-provisioned half-card instances, subject to fingerprint, live device readback and completed qualification. | Follow [hardware qualification](qualification.md) before selecting the Dell backend. |
| DGX Spark | Read-only discovery and a blocked strict GPU target. | Run the probe, record the results and follow the Spark qualification workstream. |

Install the Python workspace using the [repository setup](../README.md). The demo binds to `http://127.0.0.1:8765`. Choose **Teacher** for the class-wide view. Use **Node administration** for policy, reservations, capability evidence and admission controls. The convenience launcher enables local demonstration identities in both its demo and Docker modes. Use individual services for a pilot with operator-issued credentials.

## Start the native CPU pilot

1. Use a native Linux host with a local Unix Docker socket, cgroup v2 and the `cpu`, `memory` and `pids` controllers. Record the read-only inventory with `python -m services.worker probe`. Review OS-visible memory and available CPUs. Measure the OS and platform reserve before assigning student capacity.
2. Prepare an operator-owned state directory outside the checkout and student home directories. Set a restrictive umask, typically `077`. Use one local control database and storage root across all three services. Configure private service access and runtime read permissions as described in [access boundaries](security.md). Review permissions separately on pre-existing files.
3. Select a reviewed, native Linux Python image by registry digest for `PYTHON_BASE`. It must contain the interpreter and, for offline dependency builds, pip. Set `GPUHUB_RESERVE_BYTES` and `GPUHUB_RESERVE_CPUS` to the measured host's selected reserves. Set the same runtime/retention settings in every service that reads them. The worker resolves the resulting local tag to an immutable image ID.

```sh
export GPUHUB_DB=/srv/cit-gpu-hub/hub.sqlite3
export GPUHUB_DATA=/srv/cit-gpu-hub/data
export GPUHUB_IMAGE=cit-gpu-hub:local
docker build --build-arg PYTHON_BASE="${PYTHON_BASE:?select a reviewed native image by digest}" \
  -f deploy/examples/Dockerfile -t "$GPUHUB_IMAGE" .
```

Start each service from the installed environment. Under an OS service manager, supply the paths and environment settings to each unit and allow Docker access only for the worker. During local validation, use three separate terminals.

Example [systemd units](../deploy/systemd) assume a checkout and virtual environment under `/opt/cit-gpu-hub`, a private writable `/srv/cit-gpu-hub`, and `/etc/cit-gpu-hub/runtime.env` containing the five GPUHUB settings above (DB, data, image, reserve bytes and reserved CPUs), one assignment per line. Set the service's primary group to `gpuhub`; give the worker unit its Docker supplementary group. The other units share the controller's trusted state. Initialize the catalog before starting workers that need the preloaded dataset. Review paths, Docker socket permissions, OS identities and filesystem quotas on the native host before enabling the units. These templates await native-host validation.

```sh
python -m services.api --port 8765
```

```sh
python -m services.worker run --backend docker --node cit-cpu --name "CIT CPU" \
  --reserve-bytes "${GPUHUB_RESERVE_BYTES:?set the measured reserve}" \
  --reserve-cpus "${GPUHUB_RESERVE_CPUS:?set the reserved CPU count}"
```

```sh
python -m services.scheduler
```

For projects with a dependency lock, configure `GPUHUB_WHEELHOUSE` to an operator-owned directory of reviewed native wheels. The worker performs hash-checked offline installation in a reserved build container. A job stays queued if its environment or a required approved dataset is unavailable on eligible nodes.

## Accounts and routine operation

Use the same `GPUHUB_DB` and `GPUHUB_DATA` as the API when issuing or revoking credentials. An issue command prints the bearer token once. Deliver it privately and use `gpuhub login` to save it in the user's OS keyring. In the browser, choose **Sign in with a scoped token**. Select a school-approved public alias for the queue.

```sh
python -m services.api issue-token --identity cit-teacher --alias Teacher --role admin --hours 8
python -m services.api issue-token --identity cit-student-01 --alias Student01 --role student --hours 8
python -m services.api revoke-tokens --identity cit-student-01
```

Add `--read-only` when issuing a monitoring credential. Token revocation closes API access and leaves existing reservations to their normal job lifecycle. To stop active work, inspect and cancel the job under a teacher identity.

In **Node administration**, select a student account to change its maximum runtime or disable access. The limit applies to future submissions and resumes. A blank override restores the hub-wide default. Disabling the account revokes its tokens and holds its queued jobs. Its running attempts continue under their original limits. After re-enabling the account, issue a new token or sign in again in the local demo. The audit trail records the teacher and the policy change.

| Teacher action | Command | Expected result |
| --- | --- | --- |
| Inspect the classroom | `gpuhub dashboard`, `gpuhub queue` | Two fixed slots per worker and explicit waiting reasons. |
| Investigate a job | `gpuhub status JOB_ID`, `gpuhub events JOB_ID`, `gpuhub logs JOB_ID --follow` | Assigned limits, observed state, deadline and retained logs. |
| Prepare for maintenance | `gpuhub drain NODE_ID` | New admissions stop. Existing owners keep their envelope. |
| Stop and recover work | `gpuhub cancel JOB_ID`, then `gpuhub checkpoints JOB_ID` | Stop intent persists. Use only a completed checkpoint ID with `gpuhub resume JOB_ID --checkpoint CHECKPOINT_ID`. |
| Reopen admission | `gpuhub qualification NODE_ID`, then `gpuhub undrain NODE_ID` | Eligible nodes reopen once heartbeat, pressure and cleanup permit it. |

## Outages and maintenance

**API or scheduler restart.** Restart the affected service against the same control database. The worker keeps its journal, original attempt identity, local deadline and resource limits. The scheduler reads active claims from SQLite. New reservations resume after the scheduler and worker are healthy. The convenience launcher supervises all development processes together, so use the individual service commands when testing independent restarts.

**Worker restart.** Retain the worker ID, backend, control database, storage root and `data/workers/NODE_ID/journal.db`. Match the original image ID, reserve and GPU configuration. Inspect the worker's labeled containers and compare attempt ID, node, generation and current state with the journal. The worker reuses an existing execution and adopts its original deadline. Unmatched containers or an ambiguous journal require operator investigation.

**Unknown execution or missing heartbeat.** Drain the node and record the job, attempt, generation, last heartbeat and container state. The scheduler retains the allocation while execution may still be active. Use read-only inspection on the original worker.

```sh
docker ps -a --filter label=cit.gpuhub.managed=v1 --filter label=cit.gpuhub.node=cit-cpu
docker container inspect CONTAINER_NAME
```

Restore the original journal and service connection if possible. Require confirmed container exit, descendant cleanup, required device cleanup and successful reconciliation before reusing a slot. If the retained evidence cannot establish those facts, keep the node quarantined. This version has no automatic cross-host fencing or administrative force-release endpoint.

**Host reboot.** A previous boot's monotonic timer cannot establish additional runtime. Restore the same state and journal, review attempt and container observations, and keep uncertain allocations held. A later submission or explicit checkpoint resume requires a confirmed terminal outcome for the original attempt.

**Changing reserves, images or device layout.** Drain first and wait for reservations to clear. Record old and new inventories, review qualification again and restart the worker with the new configuration. A driver, host, image or device-layout change can invalidate hardware qualification.

## Storage, retention and backups

The API reports quotas in `/v1/me`; the administration page shows policy and state. The storage catalog applies byte/count quotas when accepting source or publishing outputs and verified checkpoints. Docker rotates its local logs. The worker bounds ingestion and samples writable disk growth. Live bind mounts require separately configured filesystem quotas for hard write-time enforcement. Monitor free space for control databases, Docker logs, staging and the builder cache. See [storage settings](storage-and-checkpoints.md).

Preview expired artifacts and attempt scratch with the paths from the running pilot.

```sh
python -m packages.contracts.retention --db "$GPUHUB_DB" --storage "$GPUHUB_DATA" --days 30
```

After reviewing the inventory and school retention requirements, add `--apply` to retire eligible outputs and scratch. The command preserves source revisions and verified checkpoints. Its eligibility check requires confirmed termination, a completed collection receipt and an expired attempt without a live reservation.

For a consistent maintenance backup, drain all workers, wait for confirmed cleanup and collection, then stop the scheduler, API and workers. Snapshot the entire state directory, including control and storage databases with any SQLite journal/WAL files, worker journals, immutable revisions, datasets, cached environments, artifacts and checkpoint payloads. Save image digests, native wheelhouse and operator configuration separately. Protect backups like the running service because they include project content, logs and hashed credential records.

Restore the snapshot to an isolated local state directory with the same OS permissions. Keep restored workers disconnected from active original workers. Check project history, sample source and artifact downloads, checkpoint integrity, reservations and journal identities. Revoke restored credentials that should no longer be valid. Reconcile or keep drained any node with outstanding execution evidence before permitting new admissions.

## Next milestone

On an idle native Linux Docker host, build the curated image and run `GPUHUB_TEST_DOCKER=1 python -m pytest tests/hardware/test_execution.py -v`. Record actual hard-limit readbacks, CPU throttling and descendant-memory results. Run the three-user workflow with submitted CPU code, cancel a running job, verify the queued user starts and download the original job's artifact. Exercise controller and worker restarts and load a published checkpoint inside its own environment. Review the results with the school operator before enrolling students or enabling physical GPU execution.
