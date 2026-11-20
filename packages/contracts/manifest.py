"""Validate untrusted submission intent before looking up operator-owned resources."""

import re
import unicodedata

_RESERVED = re.compile(r"(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|(?:COM|LPT)[1-9¹²³])", re.I)


def _reserved(name):
    return _RESERVED.fullmatch(name.split(".", 1)[0].rstrip(" "))


def _bytes(value):
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ContractError("Invalid Unicode in submission.") from exc


class ContractError(ValueError):
    def __init__(self, message, code="INVALID_MANIFEST"):
        super().__init__(message)
        self.code = code


def safe_path(value):
    """Require a portable, normalized relative path, including on a Windows API host."""
    if not isinstance(value, str) or not value or _bytes(value) > 512:
        raise ContractError("A project path must contain between 1 and 512 UTF-8 bytes.")
    if value != unicodedata.normalize("NFC", value):
        raise ContractError("Use NFC-normalized filenames.")
    for part in value.split("/"):
        if (not part or part in {".", ".."} or part.endswith((".", " "))
                or any(char in '\\:*?"<>|' or ord(char) < 32 or ord(char) == 127 for char in part)
                or _bytes(part) > 180
                or _reserved(part)):
            raise ContractError("Project paths must be relative and portable.")
    return value


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise ContractError("Invalid resource identifier.")
    if _reserved(value) or value.endswith("."):
        raise ContractError("Reserved resource identifier.")
    return value


def _integer(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        code = "RUNTIME_POLICY_EXCEEDED" if name == "runtime_seconds" else "INVALID_MANIFEST"
        raise ContractError(f"{name} must be an integer between {low} and {high}.", code)
    return value


def _keys(value, allowed):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ContractError("Unsupported manifest field or object type.")


def validate_manifest(value, *, max_runtime=7200):
    _keys(value, ("schema_version", "name", "environment", "entrypoint", "args",
                  "dependencies_lock", "dataset_ids", "runtime_seconds", "output_dir", "checkpoint"))
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ContractError("Expected schema_version 1.")
    name = value.get("name")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 96 or not name.strip().isprintable():
        raise ContractError("Project name must contain 1–96 printable characters.")
    entry = safe_path(value.get("entrypoint"))
    if not entry.endswith(".py"):
        raise ContractError("Choose a Python .py entrypoint.")
    args = value.get("args", [])
    if (not isinstance(args, list) or len(args) > 128 or any(
            not isinstance(arg, str) or _bytes(arg) > 4096 or "\0" in arg for arg in args)):
        raise ContractError("args must be a bounded list of strings.")
    datasets = value.get("dataset_ids", [])
    if not isinstance(datasets, list) or len(datasets) > 32:
        raise ContractError("Invalid dataset list.")
    datasets = [identifier(item) for item in datasets]
    if len(datasets) != len(set(datasets)):
        raise ContractError("A dataset may only be listed once.")
    out = safe_path(value.get("output_dir", "outputs"))
    cp = value.get("checkpoint", {"mode": "none"})
    _keys(cp, ("mode", "directory", "interval_steps"))
    if cp.get("mode") not in ("none", "cooperative"):
        raise ContractError("Unknown checkpoint mode.")
    directory = safe_path(cp.get("directory", "checkpoints"))
    result = dict(schema_version=1, name=name.strip(), environment=identifier(value.get("environment")),
                  entrypoint=entry, args=list(args), dataset_ids=datasets,
                  runtime_seconds=_integer(value.get("runtime_seconds", min(3600, max_runtime)),
                                           1, max_runtime, "runtime_seconds"), output_dir=out,
                  checkpoint=dict(mode=cp["mode"], directory=directory, interval_steps=_integer(
                      cp.get("interval_steps", 200), 1, 10**9, "interval_steps")))
    writable = [out.casefold().split("/"), directory.casefold().split("/")]
    a, b = writable
    if a[:len(b)] == b or b[:len(a)] == a:
        raise ContractError("Output and checkpoint paths must be disjoint.")
    if "dependencies_lock" in value:
        result["dependencies_lock"] = safe_path(value["dependencies_lock"])
    for path in [entry, result.get("dependencies_lock", entry)]:
        parts = path.casefold().split("/")
        if any(parts[:len(w)] == w or w[:len(parts)] == parts for w in writable):
            raise ContractError("A writable directory conflicts with a required source file.")
    return result
