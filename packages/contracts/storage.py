"""Local storage for independent services on one trusted host and filesystem.

Only attempt scratch is mounted writable. Collection is called after exit and
descendant cleanup, when a student can no longer change the tree being walked.
"""

import hashlib
import io
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import time
import zipfile
import zlib
from contextlib import closing, contextmanager
from pathlib import Path

from .manifest import ContractError, identifier, safe_path, validate_manifest

MIB = 1024**2
DEFAULTS = dict(upload_bytes=8*MIB, source_bytes=32*MIB, source_files=512,
                output_bytes=64*MIB, checkpoint_bytes=128*MIB, checkpoint_count=5,
                project_bytes=1024*MIB, log_bytes=4*MIB, dataset_bytes=2048*MIB,
                cache_bytes=2048*MIB, min_free_bytes=256*MIB)
FILE_LIMIT = 2048


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _hash(value):
    return hashlib.sha256(value).hexdigest()


def _limit(message):
    raise ContractError(message, "STORAGE_QUOTA_EXCEEDED")


def _regular(path):
    st = path.lstat()
    if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1
            or getattr(st, "st_file_attributes", 0) & 1024):
        raise ContractError("Collection only accepts private regular files.", "INVALID_OUTPUT")
    return st.st_size


def _readtree(root):
    """Bound both directory traversal work and metadata memory."""
    dirs = [root]
    entries = 0
    while dirs:
        directory = dirs.pop()
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 1024:
            raise ContractError("Invalid collection directory.", "INVALID_OUTPUT")
        with os.scandir(directory) as children:
            for child in children:
                entries += 1
                if entries > FILE_LIMIT:
                    _limit("Too many entries in one collection.")
                path = Path(child.path)
                safe_path(path.relative_to(root).as_posix())
                if child.is_dir(follow_symlinks=False):
                    dirs.append(path)
                else:
                    yield path, _regular(path)


def _copy(stream, target, limit):
    digest = hashlib.sha256()
    size = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as dest:
        while chunk := stream.read(min(MIB, limit-size+1)):
            size += len(chunk)
            if size > limit:
                _limit("Uncompressed data exceeds its byte limit.")
            digest.update(chunk)
            dest.write(chunk)
        dest.flush()
        os.fsync(dest.fileno())
    return size, digest.hexdigest()


def _mount_readable(directory):
    # The host-only ancestor remains private. The container has another UID and
    # receives this directory as a read-only bind mount, including under umask 077.
    directory.chmod(0o755)
    for path in directory.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)


def _unpack(filename, content, source, quotas):
    filename = safe_path(filename)
    if "/" in filename:
        raise ContractError("An upload filename cannot include a directory.")
    if filename.endswith(".py"):
        paths = [(filename, *_copy(io.BytesIO(content), source / filename, quotas["source_bytes"]))]
        return paths
    if not filename.lower().endswith(".zip"):
        raise ContractError("Upload one .py file or a ZIP archive.")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            infos = archive.infolist()
            if len(infos) > quotas["source_files"]:
                _limit("Too many source archive entries.")
            total = 0
            paths, seen, explicit = [], {}, set()
            for info in infos:
                if info.orig_filename != info.filename or info.flag_bits & 1:
                    raise ContractError("Invalid or encrypted archive entry.")
                is_dir = info.is_dir()
                name = safe_path(info.filename[:-1] if is_dir else info.filename)
                kind = stat.S_IFMT(info.external_attr >> 16)
                if (kind not in (0, stat.S_IFDIR if is_dir else stat.S_IFREG)
                        or info.external_attr & 0x400
                        or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                    raise ContractError("Unsupported archive entry type or compression.")
                parts = name.split("/")
                for end in range(1, len(parts)+1):
                    path = "/".join(parts[:end])
                    key = path.casefold()
                    shape = (path, end < len(parts) or is_dir)
                    if key in seen and seen[key] != shape:
                        raise ContractError("Archive paths conflict after normalization.")
                    seen[key] = shape
                key = name.casefold()
                if key in explicit:
                    raise ContractError("Duplicate archive entry.")
                explicit.add(key)
                if is_dir:
                    if info.file_size:
                        raise ContractError("A directory entry cannot contain data.")
                    continue
                remaining = quotas["source_bytes"] - total
                if info.file_size > remaining:
                    _limit("Expanded source exceeds its byte quota.")
                with archive.open(info) as stream:
                    size, digest = _copy(stream, source / name, remaining)
                if size != info.file_size:
                    raise ContractError("Archive entry length mismatch.")
                paths.append((name, size, digest))
                total += size
            return sorted(paths)
    except (zipfile.BadZipFile, EOFError, NotImplementedError, zlib.error) as exc:
        raise ContractError("Invalid or unsupported ZIP archive.") from exc


class Storage:
    def __init__(self, root, *, quotas=None):
        self.root = Path(root).absolute()
        self.quotas = {k: int(os.getenv("GPUHUB_" + k.upper(), v)) for k, v in DEFAULTS.items()}
        self.quotas.update(quotas or {})
        if any(type(v) is not int or v < 0 for v in self.quotas.values()):
            raise ValueError("Storage quotas must be nonnegative integers.")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        for dirname in ("staging", "revisions", "objects", "attempts", "datasets"):
            (self.root / dirname).mkdir(mode=0o700, exist_ok=True)
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS items(
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, project TEXT NOT NULL, attempt TEXT,
                size INTEGER NOT NULL, meta TEXT NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS by_attempt ON items(attempt,kind)")
            db.execute("CREATE INDEX IF NOT EXISTS by_project ON items(project)")
            db.execute("CREATE TABLE IF NOT EXISTS collections (attempt TEXT PRIMARY KEY, collected REAL NOT NULL)")

    @contextmanager
    def _db(self, *, write=False):
        with closing(sqlite3.connect(self.root / "storage.sqlite", timeout=30)) as db:
            db.row_factory = sqlite3.Row
            with db:
                if write:
                    db.execute("BEGIN IMMEDIATE")
                yield db

    def ensure_headroom(self, required_bytes=0):
        if shutil.disk_usage(self.root).free < self.quotas["min_free_bytes"] + required_bytes:
            _limit("Insufficient disk headroom.")

    def _publish(self, key, kind, project, attempt, size, meta, stage, target, budget=None):
        with self._db(write=True) as db:
            existing = db.execute("SELECT meta FROM items WHERE id=?", (key,)).fetchone()
            if existing:
                return json.loads(existing[0])
            used = db.execute("SELECT COALESCE(SUM(size),0) FROM items WHERE project=?", (project,)).fetchone()[0]
            if used + size > self.quotas["project_bytes"]:
                _limit("Project storage quota reached.")
            if budget:
                count, total = db.execute(
                    "SELECT COUNT(*),COALESCE(SUM(size),0) FROM items WHERE attempt=? AND kind=?",
                    (attempt, kind)).fetchone()
                if count >= budget[0] or total + size > budget[1]:
                    _limit(f"Attempt {kind} quota reached.")
            # A crash between rename and DB commit leaves an unpublished object.
            # The key covers all content; reconstruct from verified input on retry.
            if target.exists():
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            stage.rename(target)
            db.execute("INSERT INTO items VALUES(?,?,?,?,?,?)",
                       (key, kind, project, attempt, size, _encode(meta).decode()))
        return meta

    def put_revision(self, project_id, filename, content, manifest, *, max_runtime=7200):
        project = identifier(project_id)
        manifest = validate_manifest(manifest, max_runtime=max_runtime)
        if not isinstance(content, bytes) or len(content) > self.quotas["upload_bytes"]:
            _limit("Upload exceeds its byte quota.")
        self.ensure_headroom(self.quotas["source_bytes"])
        with tempfile.TemporaryDirectory(dir=self.root / "staging") as tmp:
            source = Path(tmp) / "source"
            source.mkdir()
            paths = _unpack(filename, content, source, self.quotas)
            names = {name for name, _, _ in paths}
            if manifest["entrypoint"] not in names or manifest.get("dependencies_lock", manifest["entrypoint"]) not in names:
                raise ContractError("The entrypoint or dependency lockfile is missing from the source.")
            for folder in (manifest["output_dir"], manifest["checkpoint"]["directory"]):
                f = folder.casefold()
                if any(p.casefold()==f or p.casefold().startswith(f+"/") or f.startswith(p.casefold()+"/") for p in names):
                    raise ContractError("Writable directories cannot shadow source files.")
            size = sum(n for _, n, _ in paths)
            digest = _hash(_encode(paths))
            key = "rev_" + _hash(_encode([project, manifest, digest]))
            meta = dict(id=key, project_id=project, source_hash=digest, manifest=manifest, size_bytes=size)
            _mount_readable(source)
            return self._publish(key, "revision", project, None, size, meta, source, self.root / "revisions" / key)

    def source_path(self, revision_id):
        path = self.root / "revisions" / identifier(revision_id)
        if not path.is_dir():
            raise ContractError("Revision unavailable.", "REVISION_UNAVAILABLE")
        return path

    def attempt_paths(self, attempt_id):
        base = self.root / "attempts" / identifier(attempt_id)
        base.mkdir(mode=0o700, exist_ok=True)
        paths = {key: base/key for key in ("outputs", "checkpoints", "control")}
        for path in paths.values():
            path.mkdir(mode=0o700, exist_ok=True)
        return paths

    def _get(self, key, kind):
        with self._db() as db:
            row = db.execute("SELECT * FROM items WHERE id=? AND kind=?", (key, kind)).fetchone()
        if row is None:
            raise ContractError(f"Unknown {kind}.", "NOT_FOUND")
        return row

    def _items(self, attempt, kind):
        with self._db() as db:
            return [json.loads(row[0]) for row in db.execute(
                "SELECT meta FROM items WHERE attempt=? AND kind=? ORDER BY id", (attempt, kind))]

    def artifacts(self, attempt_id):
        return sorted(self._items(attempt_id, "artifact"), key=lambda a: a["name"])

    def checkpoints(self, attempt_id):
        return sorted(self._items(attempt_id, "checkpoint"), key=lambda c: c["step"], reverse=True)

    def owner_attempt(self, resource_id):
        with self._db() as db:
            row = db.execute("SELECT attempt FROM items WHERE id=?", (resource_id,)).fetchone()
            return row[0] if row else None

    def artifact_path(self, attempt_id, artifact_id):
        row = self._get(artifact_id, "artifact")
        if row["attempt"] != attempt_id:
            raise ContractError("Unknown artifact.", "NOT_FOUND")
        return self.root / "objects" / row["id"]

    def _artifact(self, project, attempt, root, path, size):
        if size > self.quotas["output_bytes"]:
            _limit("Output file exceeds its byte quota.")
        self.ensure_headroom(size)
        with tempfile.TemporaryDirectory(dir=self.root / "staging") as tmp:
            stage = Path(tmp)/"object"
            with path.open("rb") as stream:
                size, digest = _copy(stream, stage, size)
            name = path.relative_to(root).as_posix()
            key = "art_" + _hash(_encode([attempt, name, digest]))
            meta = dict(id=key, attempt_id=attempt, name=name, size_bytes=size, sha256=digest)
            return self._publish(key, "artifact", project, attempt, size, meta, stage,
                                 self.root/"objects"/key, (FILE_LIMIT, self.quotas["output_bytes"]))

    def _checkpoint(self, project, attempt, path, identity):
        descriptor = path/"checkpoint.json"
        if _regular(descriptor) > MIB:
            raise ContractError("Checkpoint description is too large.")
        try:
            payload = json.loads(descriptor.read_bytes())
        except RecursionError as exc:
            raise ContractError("Checkpoint metadata is too deeply nested.") from exc
        if not isinstance(payload, dict) or type(payload.get("step")) is not int or payload["step"] < 0:
            raise ContractError("Invalid checkpoint step.")
        files = payload.get("files")
        if (type(payload.get("format_version")) is not int or payload["format_version"] != 1
                or not isinstance(files, list) or not 1 <= len(files) <= FILE_LIMIT):
            raise ContractError("Invalid checkpoint format.")
        with tempfile.TemporaryDirectory(dir=self.root/"staging") as tmp:
            stage = Path(tmp)/"data"
            stage.mkdir()
            checked, size, seen = [], 0, set()
            # Walk the filesystem first so a descriptor cannot name a file through a link.
            actual = {p.relative_to(path).as_posix(): (p,n) for p,n in _readtree(path) if p != descriptor}
            for file in files:
                if not isinstance(file, dict):
                    raise ContractError("Invalid checkpoint file.")
                name = safe_path(file.get("name"))
                if name not in actual or name.casefold() in seen:
                    raise ContractError("Missing or duplicate checkpoint file.")
                seen.add(name.casefold())
                source, n = actual.pop(name)
                if size+n > self.quotas["checkpoint_bytes"]:
                    _limit("Checkpoint byte quota exceeded.")
                self.ensure_headroom(n)
                with source.open("rb") as stream:
                    copied, digest = _copy(stream, stage/name, n)
                if copied != file.get("size_bytes") or digest != file.get("sha256"):
                    raise ContractError("Checkpoint integrity check failed.")
                checked.append(dict(name=name, size_bytes=copied, sha256=digest))
                size += copied
            if actual:
                raise ContractError("Checkpoint contains unlisted files.")
            key = "ck_" + _hash(_encode([attempt, payload["step"], checked]))
            meta = dict(id=key, attempt_id=attempt, step=payload["step"], verified=True, files=checked,
                        format_version=1, completed_at=time.time(), size_bytes=size, **identity)
            _mount_readable(stage)
            return self._publish(key, "checkpoint", project, attempt, size, meta, stage, self.root/"objects"/key,
                                 (self.quotas["checkpoint_count"], self.quotas["checkpoint_bytes"]))

    def collect(self, attempt_id, *, job_id, revision_id, environment, dataset_ids):
        project = self._get(revision_id, "revision")["project"]
        paths = self.attempt_paths(attempt_id)
        errors = []
        try:
            for path, size in _readtree(paths["outputs"]):
                self._artifact(project, attempt_id, paths["outputs"], path, size)
        except (OSError, ValueError) as exc:
            errors.append(f"Output collection: {exc}")
        try:
            folders = []
            for entry in paths["checkpoints"].iterdir():
                if len(folders) >= FILE_LIMIT:
                    _limit("Checkpoint directory contains too many entries.")
                folders.append(entry)
            for folder in sorted(folders, reverse=True):
                if folder.name.startswith("."):
                    continue  # The SDK only publishes a finished save by directory rename.
                try:
                    info = folder.lstat()
                    if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 1024:
                        raise ContractError("Invalid checkpoint directory.")
                    self._checkpoint(project, attempt_id, folder, dict(job_id=job_id, revision_id=revision_id,
                                     environment=environment, dataset_ids=list(dataset_ids)))
                except (OSError, ValueError) as exc:
                    errors.append(f"Checkpoint {folder.name!r}: {exc}")
                    if len(errors) >= 12:
                        break
        except (OSError, ValueError) as exc:
            errors.append(f"Checkpoint collection: {exc}")
        if not errors:
            with self._db(write=True) as db:
                db.execute("INSERT OR IGNORE INTO collections VALUES (?,?)", (attempt_id, time.time()))
        return dict(artifacts=self.artifacts(attempt_id), checkpoints=self.checkpoints(attempt_id), errors=errors)

    def resume_path(self, checkpoint_id, *, environment=None, revision_id=None, dataset_ids=None, job_id=None):
        row = self._get(checkpoint_id, "checkpoint")
        meta = json.loads(row["meta"])
        for field, expected in (("environment", environment), ("revision_id", revision_id),
                                ("dataset_ids", dataset_ids), ("job_id", job_id)):
            if expected is not None and meta[field] != expected:
                raise ContractError(f"Checkpoint {field} does not match this attempt.", "CHECKPOINT_INCOMPATIBLE")
        path = self.root/"objects"/row["id"]
        for info in meta["files"]:
            file = path/info["name"]
            if _regular(file) != info["size_bytes"]:
                raise ContractError("Stored checkpoint length mismatch.", "CHECKPOINT_CORRUPT")
            digest = hashlib.sha256()
            with file.open("rb") as stream:
                while chunk := stream.read(MIB):
                    digest.update(chunk)
            if digest.hexdigest() != info["sha256"]:
                raise ContractError("Stored checkpoint digest mismatch.", "CHECKPOINT_CORRUPT")
        return path
