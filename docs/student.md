# Running a project on GPU Hub

Use the dashboard or the `gpuhub` terminal client with the same account. The overview shows each node's two slots and queued projects. Open a job to see its allocation, deadline, logs, checkpoints, and downloadable outputs.

## Sign in

Install the terminal client from the repository in an activated Python 3.11+ virtual environment.

```sh
python -m pip install -e ".[cli]"
```

For the local demonstration, start the hub with the command in the README, then open another terminal.

```sh
gpuhub login --identity alex
gpuhub me
gpuhub dashboard
```

`login` saves the token in your operating system's credential store. On a machine without a keyring, select the local development identity per command, for example `gpuhub jobs --identity alex`. The development API listens on loopback and labels its sample accounts. On a school deployment, use the HTTPS origin your teacher provides as `GPUHUB_URL` and run `gpuhub login` to enter your issued token at the hidden prompt. Keep tokens out of projects and shell history. For an automated session, the CLI reads `GPUHUB_TOKEN` from its environment.

## Submit

Run these commands from the repository root. The development fixture produces a synthetic result on a timer. A CPU job executes your Python when a compatible Docker worker is online.

```sh
gpuhub environments
gpuhub datasets
gpuhub submit examples/hello_cpu --profile simulated
gpuhub jobs
gpuhub queue
```

Copy the returned job `id`. In the remaining examples, replace `JOB_ID`, `PROJECT_ID`, and `CHECKPOINT_ID` with IDs from your own output. For a single-file CPU project, submit the Python file directly.

```sh
gpuhub submit train.py --name "Vision baseline" --profile cpu --runtime 600
```

The CLI creates a project, uploads an immutable revision, and submits that revision. To keep successive versions under the same project, run `gpuhub projects` and add `--project PROJECT_ID` to later submissions. For a multi-file project, put `gpuhub.json` in its root, alongside the entrypoint. Use the existing example manifests as a starting point. The CLI also accepts a ZIP plus an explicit manifest, for example `gpuhub submit project.zip --manifest gpuhub.json --profile cpu`. Reference approved environments and dataset IDs. Keep source paths relative and include only files your job needs.

One student can hold one active reservation across the hub, including preparation. Each accepted execution has a fixed CPU and memory budget. A qualified GPU profile adds at most half the parent GPU's execution resources. The node reserves host memory before dividing the remainder between slots. Check `gpuhub me` for the effective runtime and storage policies. The demo's Spark entry remains blocked pending hardware qualification.

## Monitor and collect results

```sh
gpuhub status JOB_ID
gpuhub logs JOB_ID --follow
gpuhub events JOB_ID
gpuhub artifacts JOB_ID
gpuhub download JOB_ID --output ./results
```

Press Ctrl+C to end the log viewer while the job continues. Log following reconnects from its last received cursor. For scripts, append `--json` to a command. `gpuhub logs JOB_ID --json` returns a `next_cursor` you can pass to `gpuhub logs JOB_ID --cursor 42` after replacing `42` with that value. With `--follow --json`, each log record is emitted on its own line.

Write your results into the directory in `GPUHUB_OUTPUT_DIR`. The worker collects outputs after exit and the CLI verifies size and SHA-256 on download. Choose a new destination for each download. If several attempts published the same filename, select one using `--artifact ARTIFACT_ID`. Project members and teachers can read project details. The submitting student and teachers can cancel or resume a job.

## Stop and resume training

On a Docker worker, try the included training example. It saves position and optimizer state periodically. The following settings give time to inspect the running attempt and request a stop.

```sh
gpuhub submit examples/checkpoint_training --profile cpu --argument=--steps --argument 1500 --argument=--delay --argument 0.1
gpuhub status JOB_ID
gpuhub logs JOB_ID --follow
```

Wait for a completed checkpoint event in the training log. End log following with Ctrl+C or use a second terminal.

```sh
gpuhub cancel JOB_ID
gpuhub status JOB_ID
gpuhub events JOB_ID
gpuhub checkpoints JOB_ID
```

Cancellation records a stop request. Check `status` for the confirmed terminal state and `checkpoints` for stored, verified IDs. A running or stopping attempt still owns its slot. The worker publishes completed checkpoints after exit. A forced stop may leave only an earlier completed save. Resume from one of the listed IDs.

```sh
gpuhub resume JOB_ID --checkpoint CHECKPOINT_ID
gpuhub status JOB_ID
```

Resume queues a new attempt under the same job ID. It uses the original source and compatible environment and datasets. The application loads its saved state in the execution environment. Integrate the runner into your own training loop by following the [checkpoint example](../examples/checkpoint_training/train.py) and [storage guide](storage-and-checkpoints.md).

## When a job waits or fails

| What you see | What to do |
| --- | --- |
| Waiting for a free half-capacity slot | Leave the job queued. Its waiting time counts toward scheduling priority. |
| User already has an active allocation | Let your other build or run finish, or cancel it and wait for confirmed cleanup. |
| Compatible worker unavailable or GPU enforcement unverified | Check `gpuhub nodes`. Your teacher can identify available execution profiles. |
| Invalid manifest, unavailable environment, or forbidden dataset | Check `gpuhub environments`, `gpuhub datasets`, the entrypoint path and argument list. Upload the corrected revision. |
| Runtime or memory limit reached | Inspect the attempt outcome and last completed checkpoint. Reduce memory demand or choose a longer runtime within the policy for a new submission. The wall-clock cap includes shutdown time. |
| UNKNOWN or quarantined | Ask your teacher to reconcile the worker. The original attempt may still be running and keeps its reservation. |
| No checkpoint IDs after exit | Start a new submission if the run needs to restart. For future runs, integrate periodic checkpoints and budget enough time and storage to write them. |
