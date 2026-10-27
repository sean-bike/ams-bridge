"""Current records; results are atomically replaced, without evaluation history."""
import re
from datetime import datetime, timedelta
from pathlib import Path

from .common import BridgeError, now, read_json, relative, write_json

BRIDGE_VERSION = "0.5.1.dev1"
RESULT_RULES_VERSION = "3.0.0"


def metadata(kind, run_id=None, input_fingerprint=None, source=None):
    return {"schema_version": 2, "bridge_version": BRIDGE_VERSION, "record_type": kind,
            "run_id": run_id, "input_fingerprint": input_fingerprint, "created_at": now(), "source": source or {}}


def record_version(record, kind):
    if not isinstance(record, dict):
        raise BridgeError(f"{kind}: expected a record object")
    version = record.get("schema_version")
    if type(version) is not int or version != 2:
        raise BridgeError(f"{kind}: unsupported schema_version: {version!r}")
    if version == 2:
        if not {"record_type", "bridge_version", "source", "created_at", "run_id", "input_fingerprint"}.issubset(record):
            raise BridgeError(f"{kind}: missing versioned metadata")
        if record.get("record_type") != kind or not isinstance(record.get("bridge_version"), str) or not record["bridge_version"]:
            raise BridgeError(f"{kind}: missing or invalid record_type / bridge_version")
        if not isinstance(record.get("source"), dict):
            raise BridgeError(f"{kind}.source: expected an object")
        try:
            created = datetime.fromisoformat(record["created_at"])
            if created.utcoffset() != timedelta(0):
                raise ValueError("UTC required")
        except (KeyError, TypeError, ValueError) as exc:
            raise BridgeError(f"{kind}.created_at: expected a UTC timestamp") from exc
        for key, pattern in (("run_id", r"[0-9a-f]{32}"), ("input_fingerprint", r"[0-9a-f]{64}")):
            value = record.get(key)
            if value is not None and (not isinstance(value, str) or not re.fullmatch(pattern, value)):
                raise BridgeError(f"{kind}.{key}: invalid identifier")
    return version


def validate_files(files):
    if not isinstance(files, list):
        raise BridgeError("files: expected an array")
    names = set()
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "source", "sha256", "size"}:
            raise BridgeError("files: invalid file record fields")
        name = relative(item["path"], "files.path")
        if name != item["path"] or name.casefold() in names or name == ".":
            raise BridgeError("files.path: duplicate or noncanonical path")
        names.add(name.casefold())
        if (not isinstance(item["source"], str) or not item["source"] or not isinstance(item["sha256"], str) or
                not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) or type(item["size"]) is not int or item["size"] < 0):
            raise BridgeError(f"files: invalid source, hash or size for {name}")


def load_result(path):
    result = read_json(path)
    record_version(result, "result")
    if result.get("rules_version") != RESULT_RULES_VERSION:
        raise BridgeError("unsupported result rules_version")
    if any(not isinstance(result.get(key), dict) for key in ("execution", "solver", "collection")):
        raise BridgeError("invalid result sections")
    return result


def save_result(directory, result):
    record = {**result, **metadata("result", result["run_id"], result["input_fingerprint"]),
              "rules_version": RESULT_RULES_VERSION}
    write_json(Path(directory) / "result.json", record)
    return record
