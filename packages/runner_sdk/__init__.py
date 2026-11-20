"""Small in-container training helper. Requires only the Python standard library."""

import hashlib
import json
import os
import shutil
import signal
import stat
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


def _flush_directory(path):
    if hasattr(os, "O_DIRECTORY"):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class Runner:
    """Call boundary() after an optimizer step when the application can safely save."""
    def __init__(self, *, output_dir=None, checkpoint_dir=None, control_dir=None, resume_dir=None,
                 keep=2, max_bytes=None, min_free_bytes=None):
        self.outputs = Path(output_dir or os.environ["GPUHUB_OUTPUT_DIR"])
        self.checkpoints = Path(checkpoint_dir or os.environ["GPUHUB_CHECKPOINT_DIR"])
        self.control = Path(control_dir or os.environ["GPUHUB_CONTROL_DIR"])
        resume = resume_dir or os.getenv("GPUHUB_RESUME_DIR")
        self.resume = Path(resume) if resume else None
        self.keep = max(2, keep)
        self.max_bytes = int(os.getenv("GPUHUB_CHECKPOINT_BYTES", 128*1024**2) if max_bytes is None else max_bytes)
        self.min_free_bytes = int(os.getenv("GPUHUB_MIN_FREE_BYTES", 0) if min_free_bytes is None else min_free_bytes)
        self._signal_stop = False
        self._writer = self._validator = None
        self._previous_handlers = {}

    def __enter__(self):
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                self._previous_handlers[sig] = signal.signal(sig, self._stop_signal)
        return self

    def __exit__(self, exc_type, exc, traceback):
        for sig, previous in self._previous_handlers.items():
            signal.signal(sig, previous)
        if isinstance(exc, MemoryError):
            self.report("application_allocation_error", exception_type=exc_type.__name__)
        return False

    def _stop_signal(self, signum, frame):
        self._signal_stop = True

    @property
    def stop_requested(self):
        return self._signal_stop or (self.control/"stop.json").is_file()

    def report(self, event, **fields):
        print(json.dumps({"gpu_hub_event": event, **fields}, allow_nan=False), flush=True)

    def register_checkpoint(self, writer, validator=None):
        """writer(directory,step) serializes state. validator(directory) checks the result."""
        self._writer, self._validator = writer, validator

    def save(self, step):
        if not callable(self._writer) or type(step) is not int or step < 0:
            raise ValueError("Saving requires a registered writer and a nonnegative training step.")
        self.checkpoints.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(self.checkpoints).free <= self.min_free_bytes:
            raise OSError("Insufficient checkpoint disk headroom.")
        stage = Path(tempfile.mkdtemp(prefix=".writing-", dir=self.checkpoints))
        try:
            self._writer(stage, step)
            if self._validator:
                self._validator(stage)
            files = []
            size = 0
            for path in sorted(stage.rglob("*")):
                st = path.lstat()
                if stat.S_ISDIR(st.st_mode):
                    path.chmod(0o755)
                    continue
                if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or path == stage/"checkpoint.json":
                    raise ValueError("Invalid file in checkpoint callback output.")
                size += st.st_size
                if size > self.max_bytes or len(files) >= 2047:
                    raise ValueError("Checkpoint exceeds the configured file or byte limit.")
                digest = hashlib.sha256()
                with path.open("rb+") as stream:
                    while chunk := stream.read(1024**2):
                        digest.update(chunk)
                    os.fsync(stream.fileno())
                path.chmod(0o644)
                files.append(dict(name=path.relative_to(stage).as_posix(), size_bytes=st.st_size,
                                  sha256=digest.hexdigest()))
            if not files:
                raise ValueError("Checkpoint callback produced no state files.")
            meta = dict(format_version=1, step=step, files=files,
                        completed_at=datetime.now(timezone.utc).isoformat())
            with (stage/"checkpoint.json").open("x", encoding="utf-8") as stream:
                json.dump(meta, stream)
                stream.flush()
                os.fsync(stream.fileno())
            # The worker and application can have distinct UIDs. The parent
            # attempt is private on the host and only mounted into this job.
            (stage/"checkpoint.json").chmod(0o644)
            stage.chmod(0o755)
            _flush_directory(stage)
            final = self.checkpoints/f"step-{step:020d}-{uuid.uuid4().hex}"
            stage.rename(final)
            _flush_directory(self.checkpoints)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
        # A failed write never removes the last good checkpoint.
        completed = sorted(self.checkpoints.glob("step-*"), reverse=True)
        for old in completed[self.keep:]:
            if (old/"checkpoint.json").is_file() and old != final:
                shutil.rmtree(old)
        self.report("checkpoint_written", step=step, directory=final.name, bytes=size)
        return final

    def boundary(self, step, *, interval_steps):
        if type(interval_steps) is not int or interval_steps < 1:
            raise ValueError("interval_steps must be positive.")
        stop = self.stop_requested
        if stop:
            self.report("stop_acknowledged", step=step)
        if self._writer and (stop or step % interval_steps == 0):
            self.save(step)
        return not stop
