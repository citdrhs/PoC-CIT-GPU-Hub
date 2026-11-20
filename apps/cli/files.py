import base64
import hashlib
import io
import json
import ntpath
import os
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath

from .client import ClientError, segment


DEFAULT_UPLOAD_BYTES = 8 * 1024 * 1024
EXCLUDE = {".git", ".venv", "venv", "__pycache__", "node_modules", ".pytest_cache", ".env"}


def linked(path):
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def make_upload(path, budget):
    if linked(path):
        raise ClientError("UNSAFE_SOURCE", "Choose a source file or directory directly.")
    if path.is_file() and path.suffix.lower() in {".py", ".zip"}:
        with path.open("rb") as f:
            content = f.read(budget + 1)
        if len(content) > budget:
            raise ClientError("UPLOAD_TOO_LARGE", "Source exceeds the API upload budget.")
        return path.name, content
    if not path.is_dir():
        raise ClientError("INVALID_SOURCE", "Submit a Python file, a ZIP, or a project directory.")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        count = 0
        for base, dirs, files in os.walk(path):
            dirs[:] = sorted(d for d in dirs if d not in EXCLUDE)
            if any(linked(Path(base) / d) for d in dirs):
                raise ClientError("UNSAFE_SOURCE", "Project contains a directory link.")
            for file in sorted(files):
                if file in EXCLUDE or file.startswith(".env."):
                    continue
                src = Path(base) / file
                if linked(src) or not stat.S_ISREG(src.stat().st_mode):
                    raise ClientError("UNSAFE_SOURCE", "Project contains a link or a special file.")
                count += 1
                if count > 4096:
                    raise ClientError("SOURCE_TOO_LARGE", "Project exceeds the CLI archive limit of 4096 files.")
                archive.write(src, src.relative_to(path).as_posix())
                if buf.tell() > budget:
                    raise ClientError("UPLOAD_TOO_LARGE", "Project exceeds the API upload budget.")
    if len(buf.getbuffer()) > budget:
        raise ClientError("UPLOAD_TOO_LARGE", "Project exceeds the API upload budget.")
    return path.name + ".zip", buf.getvalue()


def revision_payload(args, policy):
    source = Path(args.path).absolute()
    manifest_file = Path(args.manifest) if args.manifest else None
    if manifest_file is None and source.is_dir():
        manifest_file = next((source / f for f in ("gpuhub.json", "gpuhub.yaml", "gpuhub.yml")
                              if (source / f).is_file()), None)
    if manifest_file and linked(manifest_file):
        raise ClientError("UNSAFE_SOURCE", "The manifest must be a regular file.")
    manifest = {}
    if manifest_file:
        raw = manifest_file.read_text(encoding="utf-8")
        if manifest_file.suffix.lower() in {".yaml", ".yml"}:
            try:
                import yaml
            except ImportError as exc:
                raise ClientError("YAML_UNAVAILABLE", "Install the cli extra or use a JSON manifest.") from exc
            try:
                manifest = yaml.safe_load(raw)
            except yaml.YAMLError as exc:
                raise ClientError("INVALID_MANIFEST", "Invalid YAML manifest.") from exc
        else:
            manifest = json.loads(raw)
    if not isinstance(manifest, dict):
        raise ClientError("INVALID_MANIFEST", "The manifest must contain a JSON object.")
    if args.entrypoint:
        manifest["entrypoint"] = args.entrypoint
    if "entrypoint" not in manifest:
        if source.suffix.lower() == ".py":
            manifest["entrypoint"] = source.name
        elif source.is_dir() and (source / "train.py").is_file():
            manifest["entrypoint"] = "train.py"
        else:
            raise ClientError("MISSING_ENTRYPOINT", "Specify --entrypoint or provide gpuhub.json.")
    for field, val in (("name", args.name), ("environment", args.environment), ("runtime_seconds", args.runtime),
                       ("dataset_ids", args.dataset), ("args", args.argument)):
        if val is not None:
            manifest[field] = val
    manifest.setdefault("schema_version", 1)
    manifest.setdefault("name", source.stem)
    manifest.setdefault("environment", "python-cpu-v1")
    manifest.setdefault("runtime_seconds", 3600)
    budget = (policy.get("quotas") or {}).get("upload_bytes", DEFAULT_UPLOAD_BYTES)
    filename, content = make_upload(source, budget)
    return {"filename": filename, "manifest": manifest,
            "content_base64": base64.b64encode(content).decode("ascii")}


def destination(root, name):
    windows = PureWindowsPath(name)
    posix = PurePosixPath(name)
    if (not name or windows.anchor or posix.is_absolute() or "\\" in name or ":" in name
            or any(p in {"", ".", ".."} or p.rstrip(" .") != p or reserved_filename(p)
                   for p in name.split("/"))):
        raise ClientError("UNSAFE_FILENAME", "The API supplied an unsafe artifact filename.")
    path = root.joinpath(*posix.parts)
    if not path.resolve().is_relative_to(root):
        raise ClientError("UNSAFE_FILENAME", "Artifact destination escapes the output directory.")
    for p in [path, *path.parents]:
        if p == root:
            break
        if linked(p):
            raise ClientError("UNSAFE_FILENAME", "Artifact destination contains a link.")
    return path


def reserved_filename(name):
    if hasattr(ntpath, "isreserved"):
        return ntpath.isreserved(name)
    return PureWindowsPath(name).is_reserved()


def download(client, job_id, output, selected=None):
    items = client.call(f"/jobs/{segment(job_id)}/artifacts")["items"]
    if selected:
        items = [a for a in items if a["id"] == selected]
        if not items:
            raise ClientError("ARTIFACT_NOT_FOUND", "No matching artifact in this job.")
    root = Path(output).resolve()
    pending = [(a, destination(root, a["name"])) for a in items]
    targets = [str(p).casefold() for _, p in pending]
    if len(set(targets)) != len(targets):
        raise ClientError("DUPLICATE_FILENAME", "Choose an artifact with --artifact to resolve repeated filenames.")
    for _, path in pending:
        if path.exists():
            raise ClientError("DESTINATION_EXISTS", f"{path} already exists. Choose a new output directory.")
    downloaded = []
    for item, path in pending:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
            temp = Path(tmp.name)
        try:
            size = 0
            sha = hashlib.sha256()
            with client.open("/artifacts/" + segment(item["id"]) + "/download") as response, temp.open("wb") as f:
                while chunk := response.read(65536):
                    size += len(chunk)
                    if size > item["size_bytes"]:
                        raise ClientError("SIZE_MISMATCH", "Artifact exceeds its declared size.")
                    sha.update(chunk)
                    f.write(chunk)
            if size != item["size_bytes"] or sha.hexdigest() != item["sha256"]:
                raise ClientError("INTEGRITY_ERROR", "Artifact differs from the recorded size or SHA-256.")
            # Publish complete bytes only. Both operations reject a competing destination.
            if os.name == "nt":
                temp.rename(path)
            else:
                os.link(temp, path)
            downloaded.append({"id": item["id"], "path": str(path), "size_bytes": size, "sha256": sha.hexdigest()})
        finally:
            temp.unlink(missing_ok=True)
    return {"items": downloaded}
