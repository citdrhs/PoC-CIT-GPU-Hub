"""Prepare hash-locked, offline wheel installations inside a reserved build container."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tempfile
from contextlib import closing

from packages.contracts.manifest import safe_path

RECIPE = "offline-wheels-v1"
LOCK_LIMIT = 128 * 1024
PACKAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*==[A-Za-z0-9][A-Za-z0-9.!+_-]*")
HASH = re.compile(r"--hash=sha256:[a-fA-F0-9]{64}")


def _validate_lock(raw):
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Dependency lock must be UTF-8") from exc
    entries = []
    for line in text.replace("\r\n", "\n").replace("\\\n", " ").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        words = line.split()
        if (len(words) < 2 or not PACKAGE.fullmatch(words[0])
                or not all(HASH.fullmatch(word) for word in words[1:])):
            raise ValueError("Each dependency must be exactly pinned and carry SHA-256 wheel hashes")
        entries.append(words[0].split("==")[0].lower())
    if not 1 <= len(entries) <= 256 or len(entries) != len(set(entries)):
        raise ValueError("A dependency lock must contain 1–256 distinct wheel pins")


def _limit(name, default):
    value = int(os.environ.get(name, default))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def build_spec(root: Path, command: dict, source: Path, image_id: str, arch: str) -> dict:
    """The worker owns root and source. Only staging is writable by the build."""
    lock = safe_path(command["manifest"]["dependencies_lock"])
    lock_path = (source / lock).resolve(strict=True)
    if not lock_path.is_relative_to(source.resolve()) or not lock_path.is_file():
        raise ValueError("Dependency lock is outside its immutable revision")
    with lock_path.open("rb") as stream:
        raw = stream.read(LOCK_LIMIT + 1)
    if len(raw) > LOCK_LIMIT:
        raise ValueError("Dependency lock exceeds 128 KiB")
    _validate_lock(raw)
    wheel_setting = os.environ.get("GPUHUB_WHEELHOUSE")
    if not wheel_setting:
        raise ValueError("Configure the operator-owned native GPUHUB_WHEELHOUSE before building")
    wheelhouse = Path(wheel_setting).resolve(strict=True)
    if not wheelhouse.is_dir() or any(
        p.suffix != ".whl" or p.is_symlink() or not p.is_file() for p in wheelhouse.iterdir()
    ):
        raise ValueError("The offline wheelhouse must contain only approved wheel files")
    fingerprint = hashlib.sha256(b"\0".join([RECIPE.encode(), arch.encode(), image_id.encode(), raw])).hexdigest()
    cache = root.resolve() / "environment-cache"
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    max_bytes = _limit("GPUHUB_BUILD_MAX_BYTES", 512 << 20)
    cache_bytes = _limit("GPUHUB_CACHE_BYTES", 2048 << 20)
    max_bytes = min(max_bytes, cache_bytes)
    if shutil.disk_usage(cache).free < max_bytes + int(os.environ.get("GPUHUB_MIN_FREE_BYTES", 256 << 20)):
        raise ValueError("Insufficient free space to stage this environment")
    if "id" in command and "generation" in command:
        identity = f"{command['id']}:{command['generation']}".encode()
        staging = cache / ("staging-" + hashlib.sha256(identity).hexdigest())
        staging.mkdir(mode=0o700, exist_ok=True)
    else:
        staging = Path(tempfile.mkdtemp(prefix="staging-", dir=cache))
    # The service-owned parent restricts traversal. The container writes as a different UID.
    staging.chmod(0o777)
    return {"fingerprint": fingerprint, "staging": str(staging), "wheelhouse": str(wheelhouse),
            "image_id": image_id, "max_bytes": max_bytes, "cache_bytes": cache_bytes,
            "argv": ["-m", "pip", "--isolated", "--disable-pip-version-check", "install",
                     "--no-input", "--no-index", "--no-deps", "--no-cache-dir",
                     "--only-binary=:all:", "--require-hashes", "--no-compile",
                     "--find-links=/wheels", "--target=/build", "-r", "/src/" + lock]}


def _inspect_tree(root, limit):
    size = count = 0
    for base, dirs, files in os.walk(root, followlinks=False):
        for entry in dirs + files:
            info = (Path(base) / entry).lstat()
            if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                size += info.st_size
            elif not stat.S_ISDIR(info.st_mode):
                raise ValueError("Prepared environments may contain only directories and regular private files")
            count += 1
            if size > limit or count > 65536:
                raise ValueError("Prepared environment exceeds the configured build limit")
    if not count:
        raise ValueError("The build produced an empty environment")
    return size


def discard_stage(spec, storage_root):
    stage = Path(spec["staging"])
    if (stage.parent.resolve() != (Path(storage_root)/"environment-cache").resolve()
            or not stage.name.startswith("staging-") or stage.is_symlink()):
        raise ValueError("Invalid builder staging directory")
    if stage.exists():
        shutil.rmtree(stage)


def cached_environment(spec):
    """Reuse a compatible wheel set across distinct revisions with the same lock content."""
    cache = Path(spec["staging"]).parent
    fingerprint = spec["fingerprint"]
    final = cache/fingerprint
    if not final.is_dir() or final.is_symlink() or not (cache/"cache.sqlite3").is_file():
        return None
    with closing(sqlite3.connect(cache/"cache.sqlite3", timeout=30)) as db:
        if not db.execute("SELECT 1 FROM allocations WHERE fingerprint=?", (fingerprint,)).fetchone():
            return None
    return {"image_digest": spec["image_id"], "build_fingerprint": fingerprint, "cache_path": str(final)}


def publish_build(spec):
    """Commit the cache charge before rename, so an interrupted publication can be replayed."""
    staging = Path(spec["staging"])
    fingerprint = spec["fingerprint"]
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint) or not staging.name.startswith("staging-"):
        raise ValueError("Invalid builder journal")
    final = staging.parent / fingerprint
    # A second builder may have produced the same immutable dependency set.
    # Only service-owned code renames completed builds beneath this private root.
    with closing(sqlite3.connect(staging.parent / "cache.sqlite3", timeout=30)) as db:
        db.execute("CREATE TABLE IF NOT EXISTS allocations(fingerprint TEXT PRIMARY KEY, size INTEGER NOT NULL)")
        with db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM allocations WHERE fingerprint=?", (fingerprint,)).fetchone():
                size = _inspect_tree(final if final.is_dir() else staging, spec["max_bytes"])
                used = db.execute("SELECT COALESCE(SUM(size),0) FROM allocations").fetchone()[0]
                if used + size > spec["cache_bytes"]:
                    raise ValueError("Prepared environment cache quota reached")
                db.execute("INSERT INTO allocations VALUES(?,?)", (fingerprint, size))
        with db:
            db.execute("BEGIN IMMEDIATE")
            if not final.exists():
                actual = _inspect_tree(staging, spec["max_bytes"])
                charged = db.execute("SELECT size FROM allocations WHERE fingerprint=?", (fingerprint,)).fetchone()[0]
                if actual != charged:
                    raise ValueError("Prepared environment size differs from its reserved cache charge")
                staging.rename(final)
            elif not final.is_dir() or final.is_symlink():
                raise ValueError("Invalid published environment")
            if staging.exists():
                shutil.rmtree(staging)
    return {"image_digest": spec["image_id"], "build_fingerprint": fingerprint, "cache_path": str(final)}
