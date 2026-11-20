"""Trusted PID 1 inside the Linux container. The host reserves exit code 125."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def expose_source(source, workspace):
    """Source entries resolve in the working directory while writable mounts keep their own paths."""
    for item in source.iterdir():
        target = workspace / item.name
        if target.is_dir() and item.is_dir():
            expose_source(item, target)
        elif not target.exists():
            target.symlink_to(item, target_is_directory=item.is_dir())


def main():
    clock = time.monotonic()
    budget = int(os.environ["GPUHUB_RUNTIME_SECONDS"])
    expected = json.loads(os.environ["GPUHUB_CGROUP_READBACK"])
    for filename, value in expected.items():
        if (Path("/sys/fs/cgroup") / filename).read_text().strip() != value:
            print(f"GPU Hub refused launch: {filename} readback mismatch", file=sys.stderr, flush=True)
            return 125
    terminate = False

    def request_term(_signal, _frame):
        nonlocal terminate
        terminate = True

    signal.signal(signal.SIGTERM, request_term)
    if Path("/source").is_dir():
        for key, mount in (("GPUHUB_OUTPUT_DIR", "/job-data/outputs"), ("GPUHUB_CHECKPOINT_DIR", "/job-data/checkpoints")):
            target = Path(os.environ[key])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(mount, target_is_directory=True)
        expose_source(Path("/source"), Path("/work"))
    child = subprocess.Popen([sys.executable, *sys.argv[1:]], start_new_session=True)
    term_sent = False
    while child.poll() is None:
        remaining = clock + budget - time.monotonic()
        if remaining <= 0:
            # PID 1 exiting ends the complete container PID namespace.
            return 124
        if (terminate or remaining <= min(15, budget // 4)) and not term_sent:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            term_sent = True
        time.sleep(min(0.1, remaining))
    return child.returncode if child.returncode >= 0 else 128 - child.returncode


if __name__ == "__main__":
    sys.exit(main())
