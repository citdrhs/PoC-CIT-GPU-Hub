# Source, output and checkpoint storage

Use one operator-owned filesystem shared by the API, scheduler and worker on the same host. Protect the state directory from interactive users. Back up the control database, storage catalog, published objects and revision trees consistently. A new host needs an explicitly configured shared filesystem or verified staging before it can run jobs referencing these objects.

## Submitting source

Upload a Python file or a ZIP with project-relative paths. The entrypoint and optional dependency lockfile must be regular files in the upload. Revision identity covers the project, normalized manifest, filenames and source hashes. Reuploading identical contents reuses the immutable revision. The API derives project ownership from the authenticated account.

Source validation rejects archive links, special entries, encrypted archives, traversal, duplicate names, case and normalization collisions, file/directory conflicts and unsupported compression. Only stored and deflated ZIP entries are accepted.

## Configurable limits

Prefix each variable in this table with `GPUHUB_`. Numeric byte limits use bytes. Restart the services with a consistent configuration after changes. The storage catalog transactionally checks project capacity before publishing an immutable revision, output or checkpoint.

| Variable | Default | Scope |
| --- | --- | --- |
| UPLOAD_BYTES | 8 MiB | Uploaded file, before extraction |
| SOURCE_BYTES / SOURCE_FILES | 32 MiB / 512 | Expanded source and archive entries |
| OUTPUT_BYTES | 64 MiB | Published output per attempt |
| CHECKPOINT_BYTES / CHECKPOINT_COUNT | 128 MiB / 5 | Total verified checkpoint bytes and count per attempt |
| PROJECT_BYTES | 1 GiB | Published revisions, outputs and checkpoints combined |
| MIN_FREE_BYTES | 256 MiB | Headroom for admission and publication |
| LOG_BYTES | 4 MiB | Storage configuration for captured logs; consult worker limits |
| DATASET_BYTES | 2 GiB | Configuration target for operator-managed datasets |
| CACHE_BYTES | 2 GiB | Aggregate published wheel environments, charged once per immutable build fingerprint |

Checkpoint and output publication runs after confirmed exit, against paths owned by that attempt. Only regular files are copied. The host hashes checkpoint payloads, compares the application's manifest to its own observations and stores verified data outside the submitted workspace. Duplicate collection reuses the same object. Access checks occur before downloading or resuming by ID. Bounded collection protects the publication store. Use operator-managed filesystem quotas for hard limits on growth of live bind mounts.

## Training integration

The runtime supplies writable `GPUHUB_OUTPUT_DIR` and `GPUHUB_CHECKPOINT_DIR`, a private `GPUHUB_CONTROL_DIR`, and an optional read-only `GPUHUB_RESUME_DIR`. Read explicit resume state inside the application. The complete CPU example maintains position, weight, optimizer momentum and learning rate.

```python
from runner_sdk import Runner

with Runner() as runner:
    if runner.resume:
        load_training_state(runner.resume)
    runner.register_checkpoint(save_training_state, verify_training_state)
    for step in remaining_steps():
        train_one_step()
        if not runner.boundary(step, interval_steps=100):
            break
```

The writer receives a temporary directory and integer step. The optional verifier reads the written state and raises on an invalid save. The helper hashes the files, flushes them and atomically renames the completed directory. A failed write preserves the previous completed version. The host checks metadata and hashes before publishing. Checkpoint verification does not require the API or worker to deserialize application state. A hard kill can interrupt the latest write.

## Retention

Preview eligible scratch and output retirement with the paths used by your running services.

```sh
python -m packages.contracts.retention --db /state/hub.sqlite --storage /state/data --days 30
```

Add `--apply` to retire the reviewed files. The tool requires a completed collection receipt, confirmed termination, an expired finish time and no outstanding reservation. It preserves all verified checkpoints, including every resume reference, and all revision records. A project reaching its byte ceiling receives a quota error. Failed collections stay available for operator inspection. The operator selects when to run retention.
