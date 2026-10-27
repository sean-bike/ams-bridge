"""Collect verified file/directory versions with bounded calls and transfers."""
import base64
import os
import re
import shlex
import shutil
import tempfile
import time
import uuid
from pathlib import Path

from .artifacts import local_manifest, manifest_hash, metadata_script, parse_metadata, safe_path_script, unpack_directory
from .common import BridgeError, digest, file_state, is_link, local_file, now, read_json, write_json
from .records import metadata, record_version
from .transport import lease_timeout_s

CONTROL_FILES = ("launch.log", "watchdog.log", "supervisor.log", "runtime_environment.txt", "started_at", "finished_at",
                 "observed_processes", "changed_process_groups", "lingering_processes", "worker_exit")


class Meter:
    def __init__(self, transport, execution):
        self.transport, self.execution = transport, execution
        self.started = time.monotonic()
        self.stats = {"control_calls": 0, "data_calls": 0, "payload_bytes_received": 0, "files_reused": 0, "files_downloaded": 0}

    def run(self, script, **kwargs):
        transfer = bool(kwargs.get("output_file") is not None or kwargs.get("input_file") is not None)
        remaining = self.execution["transfer_timeout_s"] - (time.monotonic() - self.started)
        if remaining <= 0:
            raise BridgeError("collection transfer budget expired", "budget_exceeded")
        limit = self.execution["transfer_timeout_s"] if transfer else self.execution["control_timeout_s"]
        kwargs["timeout"] = min(kwargs.get("timeout", limit), limit, remaining)
        if transfer:
            kwargs["transfer"] = True
        self.stats["data_calls" if transfer else "control_calls"] += 1
        output = kwargs.get("output_file")
        before = output.tell() if output is not None else 0
        try:
            return self.transport.run(script, **kwargs)
        finally:
            if output is not None:
                self.stats["payload_bytes_received"] += output.tell() - before

    def summary(self):
        return {**self.stats, "elapsed_s": time.monotonic() - self.started,
                "transfer_timeout_s": self.execution["transfer_timeout_s"], "operation_lease_timeout_s": lease_timeout_s(self.execution), "observed_at": now()}


def load_cache(directory, handle):
    path = local_file(directory, "collection-cache.json")
    if path.exists():
        cache = read_json(path)
        record_version(cache, "collection_cache")
        if cache["run_id"] != handle["run_id"] or cache["input_fingerprint"] != handle["input_fingerprint"] or not isinstance(cache.get("artifacts"), dict):
            raise BridgeError("invalid collection cache identity or artifacts", "identity_mismatch")
        candidates = cache["artifacts"]
    elif (directory / "result.json").is_file():
        rows = read_json(directory / "result.json").get("collection", {}).get("artifacts", [])
        if not isinstance(rows, list) or any(not isinstance(row, dict) or not isinstance(row.get("path"), str) for row in rows):
            raise BridgeError("invalid historical artifact cache")
        candidates = {row["path"]: row for row in rows if row.get("status") == "verified"}
    else:
        candidates = {}
    for name, item in candidates.items():
        if not isinstance(name, str) or not isinstance(item, dict) or not isinstance(item.get("local_path"), str):
            raise BridgeError("invalid collection cache artifact")
        _local_path(directory, item["local_path"])
    return candidates


def _remove_staging(directory, claim, handle):
    record = read_json(claim)
    record_version(record, "collection_staging")
    if (record["run_id"] != handle["run_id"] or record["input_fingerprint"] != handle["input_fingerprint"] or
            not re.fullmatch(r"collect-[0-9a-f]{32}", claim.stem) or record.get("directory") != "staging/" + claim.stem):
        raise BridgeError("collection staging claim identity mismatch", "identity_mismatch")
    target = local_file(directory, record["directory"])
    base = local_file(directory, "staging").resolve()
    if target.resolve().parent != base:
        raise BridgeError("collection staging is outside the authorized root", "unsafe_path")
    def no_links(path):
        if is_link(path):
            raise BridgeError("collection staging contains a link", "unsafe_path")
        if path.is_dir():
            for child in path.iterdir():
                no_links(child)
    if target.exists():
        no_links(target)
        shutil.rmtree(target)
    claim.unlink()


def recover_staging(directory, handle):
    base = local_file(directory, "staging")
    if base.exists():
        for claim in sorted(base.glob("collect-*.json")):
            local_file(base, claim.name)
            try:
                _remove_staging(directory, claim, handle)
            except OSError as exc:
                raise BridgeError("previous collection staging is still busy", "busy") from exc


def _begin_staging(directory, handle):
    base = local_file(directory, "staging")
    base.mkdir(parents=True, exist_ok=True)
    name = "collect-" + uuid.uuid4().hex
    claim = base / (name + ".json")
    write_json(claim, {**metadata("collection_staging", handle["run_id"], handle["input_fingerprint"]), "directory": "staging/" + name})
    target = base / name
    target.mkdir()
    return target, claim


def _local_path(directory, value):
    path = Path(value)
    if path.is_absolute():
        try:
            value = path.resolve().relative_to(directory.resolve()).as_posix()
        except ValueError as exc:
            raise BridgeError("cached artifact path is outside the bound job", "unsafe_path") from exc
    return local_file(directory, str(value).replace(os.sep, "/"))


def _matches(path, item, snapshot, published=None):
    try:
        if published and published.get("sha256") == (manifest_hash(snapshot["members"]) if item.get("type") == "directory" else snapshot["members"][0]["sha256"]):
            members = snapshot["members"]
            states = {}
            for member in members:
                child = local_file(path, member["path"]) if item.get("type") == "directory" else path
                if is_link(child) or (member["type"] == "file" and child.stat().st_nlink != 1):
                    return False
                states[member["path"]] = file_state(child)
            if states == published.get("local_state"):
                return True
        if item.get("type", "file") == "directory":
            return path.is_dir() and local_manifest(path, item["limits"]) == snapshot["members"]
        row = snapshot["members"][0]
        return path.is_file() and not is_link(path) and path.stat().st_nlink == 1 and path.stat().st_size == row["size"] and digest(path) == row["sha256"]
    except FileNotFoundError:
        return False


def _controls(transport, handle):
    script = "set -eu\nprintf 'AMS_CONTROLS_2\\0'\n"
    for name in CONTROL_FILES:
        path = handle["remote_directory"] + "/control/" + name
        script += f"printf '%s\\0' {shlex.quote(name)}\n"
        script += f"if test -f {shlex.quote(path)} && test ! -L {shlex.quote(path)}; then tail -c 65537 -- {shlex.quote(path)} | base64 -w 0; fi\nprintf '\\0'\n"
    response = transport.run(script)
    parts = response.stdout.split(b"\0")
    if parts[0] != b"AMS_CONTROLS_2" or parts[-1] != b"" or len(parts) != 2 + 2 * len(CONTROL_FILES):
        raise BridgeError("invalid control log framing", "invalid_remote_record")
    result = {}
    for index, name in enumerate(CONTROL_FILES):
        if parts[1 + 2 * index] != name.encode():
            raise BridgeError("invalid control log source", "invalid_remote_record")
        try:
            data = base64.b64decode(parts[2 + 2 * index], validate=True)
        except ValueError as exc:
            raise BridgeError("invalid control log payload", "invalid_remote_record") from exc
        if len(data) > 65537:
            raise BridgeError("control log budget exceeded", "budget_exceeded")
        if data:
            result[name] = {"text": data[-65536:].decode("utf-8", "replace"), "truncated": len(data) > 65536}
    return result


def resolve_file(records, name):
    matches = []
    for item in records:
        if item["status"] != "verified":
            continue
        if item["path"] == name and item.get("type", "file") == "file":
            matches.append(Path(item["local_path"]))
        elif item.get("type") == "directory" and name.startswith(item["path"] + "/"):
            matches.append(local_file(Path(item["local_path"]), name[len(item["path"]) + 1:]))
    if len(matches) != 1:
        raise BridgeError(f"artifact source has no unique verified file: {name}", "collection")
    return matches[0]


def collect_artifacts(transport, handle, case, directory, final, ensure, cache):
    root = local_file(directory, "artifacts" if final else "diagnostics/" + uuid.uuid4().hex)
    root.mkdir(parents=True, exist_ok=True)
    specs = case["artifacts"]
    before = parse_metadata(transport.run(metadata_script(handle, specs)).stdout, specs)
    temporary_root, claim = _begin_staging(directory, handle)
    pending, records = {}, []
    try:
        for item in specs:
            name, snapshot = item["path"], before[item["path"]]
            record = {**item, "status": "missing"}
            records.append(record)
            if snapshot["status"] == "error":
                record.update(status="error", errors=snapshot["errors"], error_code=snapshot["errors"][0]["code"], error="remote artifact scan failed")
                continue
            if snapshot["status"] != "present":
                old = cache.get(name)
                if final and old and old.get("status") == "verified":
                    path = _local_path(directory, old["local_path"])
                    members = old.get("members") if item.get("type") == "directory" else [
                        {"path": ".", "type": "file", "size": old["size"], "sha256": old["sha256"]}]
                    if _matches(path, item, {"members": members}, old):
                        record.update(old, local_path=str(path), remote_status="missing", availability="local_only")
                continue
            try:
                old = cache.get(name)
                candidate = _local_path(directory, old["local_path"]) if old else local_file(root, name)
                if final and _matches(candidate, item, snapshot, old):
                    pending[name] = {"path": candidate, "reused": True}
                    transport.stats["files_reused"] += sum(row["type"] == "file" for row in snapshot["members"])
                    continue
                if item.get("type", "file") == "directory":
                    fd, archive = tempfile.mkstemp(prefix=".download-", suffix=".tar", dir=temporary_root)
                    pending[name] = {"archive": Path(archive)}
                    with os.fdopen(fd, "wb") as output:
                        transport.run(safe_path_script(handle, name, "directory") +
                                      "tar -C " + shlex.quote(handle["remote_directory"] + "/work") + " -cf - -- " + shlex.quote(name) + "\n", output_file=output)
                    staging = temporary_root / (".download-dir-" + uuid.uuid4().hex)
                    pending[name]["staging"] = staging
                    actual = unpack_directory(archive, staging, name, item["limits"], ensure=ensure)
                    if final and actual != snapshot["members"]:
                        raise BridgeError("directory archive disagrees with source manifest", "collection")
                    pending[name]["path"] = staging / name
                else:
                    candidate.parent.mkdir(parents=True, exist_ok=True)
                    fd, temporary = tempfile.mkstemp(prefix=".download-", dir=temporary_root)
                    pending[name] = {"path": Path(temporary), "temporary": True}
                    with os.fdopen(fd, "wb") as output:
                        transport.run(safe_path_script(handle, name) + 'cat -- "$p"\n', output_file=output)
                    actual = {"path": ".", "type": "file", "size": Path(temporary).stat().st_size, "sha256": digest(Path(temporary))}
                    if final and [actual] != snapshot["members"]:
                        raise BridgeError("file data disagrees with source manifest", "collection")
                transport.stats["files_downloaded"] += sum(row["type"] == "file" for row in snapshot["members"])
            except (OSError, BridgeError) as exc:
                record.update(status="error", error=str(exc), error_code=getattr(exc, "code", "local_io"))
        after = parse_metadata(transport.run(metadata_script(handle, specs)).stdout, specs) if final else before
        ensure()
        for item, record in zip(specs, records):
            name, snapshot = item["path"], before[item["path"]]
            if name not in pending or record["status"] == "error":
                continue
            entry = pending[name]
            try:
                if final and snapshot != after[name]:
                    raise BridgeError("artifact changed during final collection", "collection")
                if entry.get("reused"):
                    destination = entry["path"]
                else:
                    if item.get("type", "file") == "directory":
                        destination = local_file(directory, "artifact-versions/" + uuid.uuid4().hex + "/" + name) if final else local_file(root, name)
                    else:
                        destination = local_file(root, name)
                        if destination.exists():
                            old = cache.get(name)
                            original_hash = snapshot["members"][0]["sha256"]
                            if not old or old.get("sha256") != original_hash:
                                destination = local_file(directory, "artifact-versions/" + uuid.uuid4().hex + "/" + name)
                            elif old:
                                destination = _local_path(directory, old["local_path"])
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    ensure()
                    os.replace(entry["path"], destination)
                members = snapshot["members"] if final else local_manifest(destination, item["limits"]) if item.get("type") == "directory" else [
                    {"path": ".", "type": "file", "size": destination.stat().st_size, "sha256": digest(destination)}]
                size = sum(row["size"] for row in members)
                sha = manifest_hash(members) if item.get("type") == "directory" else members[0]["sha256"]
                record.update(status="verified" if final else "diagnostic", local_path=str(destination), size=size, sha256=sha)
                if item.get("type") == "directory":
                    record["members"] = members
                    record["local_state"] = {m["path"]: file_state(local_file(destination, m["path"])) for m in members}
                else:
                    record["local_state"] = {".": file_state(destination)}
            except (OSError, BridgeError) as exc:
                record.update(status="error", error=str(exc), error_code=getattr(exc, "code", "local_io"))
        ensure()
        if final:
            for record in records:
                if record["status"] == "verified":
                    cache[record["path"]] = {**record, "local_path": Path(record["local_path"]).relative_to(directory).as_posix()}
            write_json(directory / "collection-cache.json", {**metadata("collection_cache", handle["run_id"], handle["input_fingerprint"]), "artifacts": cache})
        try:
            controls = _controls(transport, handle)
        except (BridgeError, OSError) as exc:
            controls = {"collection_diagnostics": {"error": str(exc), "error_code": getattr(exc, "code", "local_io")}}
        complete = final and all(row["status"] == "verified" or (row["status"] == "missing" and not row["required"]) for row in records)
        return {"status": "complete" if complete else "partial", "final": final, "artifacts": records}, controls
    finally:
        _remove_staging(directory, claim, handle)
