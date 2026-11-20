# Access and execution boundaries

GPU Hub currently supports a trusted, single-host pilot. Students interact through the API. The host administrator controls the environment catalog, OS accounts, data volume, Docker daemon and worker configuration. The development login gives anyone with local access a choice of four demonstration identities, including a teacher. Use a separate state directory for real work.

## Identities and visibility

| Actor | Access |
| --- | --- |
| Signed-in student | Submit to an accessible project, inspect project history, read shared source, logs, verified checkpoints and outputs, and control their own jobs. |
| Project owner | Grant a registered account project access. This grant includes historical project data. |
| Teacher with an admin token | Inspect jobs across projects, cancel or resume attempts, edit student access and runtime limits, review node qualification and audit events, drain or reopen nodes. A read-only token limits HTTP access to reads. |
| Local system operator | Provision or revoke scoped tokens, install reviewed images and wheel files, choose reserves, maintain private state, configure backups and review qualification evidence. |

The classroom queue shows job names and approved aliases. Node cards show occupancy and public job labels. Choose shareable names. Logs, revision contents, artifacts and checkpoint listings require a project or administrative access check. The API maps opaque artifact IDs to their owning attempts before serving bytes.

Development auth checks the request host, socket peer and browser origin. Keep this mode on a private loopback interface. A reverse proxy or port forward can make remote connections appear local. The normal API mode rejects development credentials even if an old development database is present. An operator-issued token has a random secret, a database digest, an expiry and read/write/admin scope as applicable. Disabling an account or revoking its tokens takes effect on subsequent API calls. The CLI uses an OS keyring when saving credentials. The browser keeps the active token in session storage. Keep tokens out of source files, manifests, job arguments, shell history and application output.

## Host authority

The browser, CLI and API never import student Python. SQLite stores trusted control-plane state. The API, scheduler and worker currently share a writable control database on a local filesystem. OS ownership of that database and of the worker journal grants control-plane authority. Anyone with shell write access to the database can bypass API authorization. Keep both outside student home directories and shared project mounts.

Run each service as a supervised process. For a native Docker pilot, give daemon access only to the trusted worker account. Keep API and scheduler accounts outside the Docker group and use tightly scoped filesystem ACLs for the shared service state. Student accounts should have neither daemon access nor access to these state paths. The convenience development launcher inherits one OS identity for all three processes. On a host where that identity controls Docker, this launcher provides process separation only. Shared writable state also means separate service accounts alone do not fully isolate a compromised API.

Remote workers and OIDC integration are future work. Extending the trust boundary across machines requires authenticated worker transport, a controller-owned database, per-worker credentials, approved identity mapping and verified data staging or shared storage. The local SQLite WAL database belongs on a local filesystem.

Platform references cover the Docker daemon's host privileges and SQLite's same-host WAL requirements. [Docker Engine security](https://docs.docker.com/engine/security/) · [SQLite write-ahead logging](https://sqlite.org/wal.html)

## Submitted work

The API validates immutable source, manifest fields, paths, dataset access and environment names. CPU and memory limits, device identities, runtime flags, host mounts and Unix identity come from the worker. The worker passes a program and argument array directly to the runtime. On a native Linux worker the job runs with UID/GID 65532, dropped capabilities, a read-only root filesystem, private cgroup namespace, no external network and no additional swap. A trusted bootstrap checks the cgroup values before invoking submitted code. Confirm the runtime and kernel behavior with the opt-in native tests on the intended host.

Source, approved datasets, the control directory and resume data are read-only mounts. Writable attempt outputs and checkpoint directories live beneath a private service-owned parent. Builds use a reserved envelope and an operator-maintained offline wheelhouse. Keep the wheelhouse and the installed application/SDK readable by the container user but writable only by trusted operators. A hash pin establishes file identity. Review package origin and contents before admitting wheels to the catalog.

The worker hashes completed checkpoint files and records their revision, environment and dataset identity. Only the authorized application loads training state. File integrity verification cannot establish the safety of an application-defined serialization format.

## Resource and evidence boundaries

| Surface | Implemented control | Qualification or deployment work |
| --- | --- | --- |
| CPU, host memory, process count | Fixed reservation and Docker limits on the workload hierarchy, plus startup readback. | Run throttling, descendant-memory and cleanup tests under the school's kernel, Docker configuration and workload. |
| Live writable disk | Periodic size/headroom checks can request a stop. Publication independently checks collection and project budgets. | Provision tested filesystem quotas on the writable volume before relying on hard per-attempt disk limits. A writer can grow between samples. |
| GPU | Demo capacity is simulated. The Spark adapter blocks strict GPU execution. Dell requires evidence matching the host and two exact MIG instances. | Validate both hardware isolation and failure cleanup on idle hardware. An administrative undrain only changes scheduling intent. |
| Host and credentials | Docker access and service files remain with the trusted host; users reach artifacts through authorized routes. | Review OS ACLs, key management, TLS, backup access and default runtime security profiles before opening the system to the school. |

See the [teacher guide](operations.md), [storage contract](storage-and-checkpoints.md) and [qualification procedure](qualification.md).
