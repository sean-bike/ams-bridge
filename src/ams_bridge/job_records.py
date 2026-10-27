"""Minimal persisted job identity, shared by control and data access."""
from pathlib import Path
import re

from .common import BridgeError, is_link, local_file, read_json, write_json
from .records import record_version


def locate(job):
    if isinstance(job, dict):
        record_version(job, "job")
        if not isinstance(job.get("local_directory"), str):
            raise BridgeError("JobHandle.local_directory: expected a path")
        directory = Path(job["local_directory"]).absolute()
    else:
        path = Path(job).absolute()
        if not path.is_dir() and path.name != "job.json":
            raise BridgeError("job must name a directory or its job.json")
        directory = path if path.is_dir() else path.parent
    if any(is_link(p) for p in (directory, *directory.parents)):
        raise BridgeError("job directory contains a symbolic link / junction", "unsafe_path")
    directory = directory.resolve()
    path = local_file(directory, "job.json")
    stored = read_json(path) if path.is_file() or not isinstance(job, dict) else dict(job)
    if isinstance(job, dict):
        for key in ("run_id", "input_fingerprint", "remote_directory", "ssh_host"):
            if job.get(key) != stored.get(key):
                raise BridgeError("supplied JobHandle disagrees with archived job", "identity_mismatch")
    return stored, directory


def save_handle(directory, handle):
    write_json(local_file(directory, "job.json"), handle)


def load_job(handle):
    handle, directory = locate(handle)
    record_version(handle, "job")
    request = read_json(local_file(directory, "request.json"))
    record_version(request, "request")
    if (any(not isinstance(handle.get(k), str) for k in ("run_id", "input_fingerprint", "ssh_host", "remote_directory", "submission")) or
            not re.fullmatch(r"[0-9a-f]{32}", handle["run_id"]) or
            not re.fullmatch(r"[0-9a-f]{64}", handle["input_fingerprint"]) or
            not isinstance(request.get("execution"), dict) or not isinstance(request.get("prepared"), dict) or
            not isinstance(request["prepared"].get("case"), dict) or
            any(not isinstance(request["execution"].get(k), str) for k in ("remote_root", "ssh_host"))):
        raise BridgeError("invalid persisted job identity/request")
    execution = request["execution"]
    if (request["run_id"] != handle["run_id"] or request["input_fingerprint"] != handle["input_fingerprint"] or
            handle["remote_directory"] != execution["remote_root"] + "/" + handle["run_id"] or
            handle["ssh_host"] != execution["ssh_host"] or
            request["prepared"]["fingerprint"] != handle["input_fingerprint"]):
        raise BridgeError("persisted job identity mismatch", "identity_mismatch")
    return {**handle, "local_directory": str(directory)}, request, directory
