"""Explicit metadata-only job usage and directory cleanup previews."""
import os
import re
import shlex
from pathlib import Path
from .common import BridgeError, is_link, local_file, now
from .config import execution_config
from .job_records import load_job
from .records import metadata
from .remote import IDENTITY
from .transport import SSH

CATEGORIES = ("remote_work", "local_cache", "local_diagnostics")
RETAINED = ["final artifacts", "local derived conversions", "identity and request", "batch records", "remote control", "source inputs"]
LOCAL_TARGETS = {"local_cache": ("staging",), "local_diagnostics": ("diagnostics",)}


def _spec(raw, *, planning=False):
    allowed = {"schema_version", "execution", "local_root", "jobs", "categories"}
    if not isinstance(raw, dict) or raw.keys() - allowed or type(raw.get("schema_version")) is not int or raw["schema_version"] != 2:
        raise BridgeError("storage: expected version 2 and known fields")
    execution = execution_config(raw.get("execution"))
    value = raw.get("local_root")
    if not isinstance(value, str):
        raise BridgeError("storage.local_root: expected an explicit directory")
    root = Path(value).absolute()
    if root == Path(root.anchor) or not root.is_dir() or any(is_link(p) for p in (root, *root.parents)):
        raise BridgeError("storage.local_root: invalid directory", "unsafe_path")
    root = root.resolve()
    categories = raw.get("categories", [])
    if (not isinstance(categories, list) or any(not isinstance(c, str) or c not in CATEGORIES for c in categories) or
            len(set(categories)) != len(categories) or (planning and not categories)):
        raise BridgeError("select remote_work/local_cache/local_diagnostics")
    if not isinstance(raw.get("jobs"), list) or not raw["jobs"]:
        raise BridgeError("explicitly select job directories")
    loaded, ids = [], set()
    for name in raw["jobs"]:
        handle, request, directory = load_job(name)
        if directory == root or not directory.is_relative_to(root) or handle["run_id"] in ids:
            raise BridgeError("job outside local root or duplicate identity", "unsafe_path")
        if any(request["execution"].get(k) != execution.get(k) for k in ("ssh_host", "ssh_config_file", "remote_root")):
            raise BridgeError("storage connection disagrees with job", "identity_mismatch")
        ids.add(handle["run_id"])
        loaded.append((handle, request, directory))
    return {**raw, "execution": execution, "local_root": str(root), "categories": categories}, loaded


def _local_usage(path):
    size, files = 0, 0
    if is_link(path):
        raise BridgeError("linked target root refused", "unsafe_path")
    if path.exists():
        def failed(exc):
            raise exc
        for base, directories, names in os.walk(path, followlinks=False, onerror=failed):
            directories[:] = [d for d in directories if not is_link(Path(base) / d)]
            for name in names:
                item = Path(base) / name
                if not is_link(item) and item.is_file():
                    size += item.stat().st_size
                    files += 1
    return {"logical_bytes": size, "file_count": files}


def _remote_usage(handle, execution):
    script = _guard(handle, execution) + r'''
set -o pipefail
for name in work control derived; do
 p="$job/$name"
 test ! -L "$p" || exit 93
 printf '%s\t' "$name"
 if test -d "$p"; then
  find "$p" -type f -printf '%s\n' | awk '{bytes+=$1; count++} END {printf "%.0f\t%d\t1\n",bytes,count}'
 else printf '0\t0\t0\n'; fi
done
'''
    response = SSH(execution).run(script)
    rows = {}
    try:
        for line in response.stdout.decode().splitlines():
            name, size, count, exists = line.split("\t")
            rows[name] = {"logical_bytes": int(size), "file_count": int(count), "exists": exists == "1"}
    except (ValueError, UnicodeError) as exc:
        raise BridgeError("invalid storage inventory", "invalid_remote_record") from exc
    if set(rows) != {"work", "control", "derived"}:
        raise BridgeError("invalid storage inventory", "invalid_remote_record")
    return {"logical_bytes": sum(r["logical_bytes"] for r in rows.values()), "file_count": sum(r["file_count"] for r in rows.values()), "categories": rows}


def usage(spec):
    spec, loaded = _spec(spec)
    rows = []
    for handle, _, directory in loaded:
        row = {"run_id": handle["run_id"], "local_directory": str(directory), "remote_directory": handle["remote_directory"], "observed_at": now()}
        for side, action in (("local", lambda: _local_usage(directory)), ("remote", lambda: _remote_usage(handle, spec["execution"]))):
            try:
                row[side] = {"status": "complete", **action()}
            except (BridgeError, OSError) as exc:
                row[side] = {"status": "partial", "errors": [{"code": getattr(exc, "code", "local_io"), "message": str(exc)}]}
        rows.append(row)
    return {**metadata("storage_usage"), "status": "complete" if all(r[s]["status"] == "complete" for r in rows for s in ("local", "remote")) else "partial", "jobs": rows, "logical_bytes_are_not_allocated_bytes": True}


def _terminal_guard(handle, execution):
    return _guard(handle, execution) + r'''
test -s "$job/control/result" || exit 95
IFS=$'\t' read -r state rc reason < "$job/control/result"
case "$state" in completed|failed|cancelled|timed_out) ;; *) exit 95;; esac
case "$reason" in cleanup_incomplete|unsupported_process_layout) exit 95;; esac
for f in identity worker.identity observed_processes; do
 test -s "$job/control/$f" || { test "$f" = observed_processes && continue; exit 94; }
 while read -r p start boot extra; do
  test -n "$p" || continue
  [[ "$p" =~ ^[1-9][0-9]*$ && "$start" =~ ^[0-9]+$ && "$boot" =~ ^[0-9a-f-]{36}$ && -z "$extra" ]] || exit 94
  if same "$p" "$start" "$boot"; then exit 95; fi
 done < "$job/control/$f"
done
'''


def cleanup_plan(spec):
    spec, loaded = _spec(spec, planning=True)
    targets = []
    for handle, _, directory in loaded:
        try:
            response = SSH(spec["execution"]).run(_terminal_guard(handle, spec["execution"]), check=False)
            eligible = response.returncode == 0
            reason = None if eligible else "execution or job identity not confirmed ended"
        except (BridgeError, OSError) as exc:
            eligible, reason = False, str(exc)
        for category in spec["categories"]:
            names = (handle["remote_directory"] + "/work",) if category == "remote_work" else LOCAL_TARGETS[category]
            for name in names:
                target = {"run_id": handle["run_id"], "job": str(directory), "category": category, "path": name,
                          "eligible": eligible, "reason": reason, "logical_bytes": None}
                try:
                    if category == "remote_work":
                        target["logical_bytes"] = _remote_usage(handle, spec["execution"])["categories"]["work"]["logical_bytes"]
                        target["uncollected"] = "Collection is not confirmed. Deletion prevents further extraction from remote work."
                    else:
                        target["logical_bytes"] = _local_usage(local_file(directory, name))["logical_bytes"]
                except (BridgeError, OSError) as exc:
                    target.update(eligible=False, reason=str(exc))
                targets.append(target)
    return {**metadata("cleanup_plan"), "spec": spec, "targets": targets, "retained": RETAINED}

def _guard(handle, execution):
    job = handle["remote_directory"]
    if job != execution["remote_root"] + "/" + handle["run_id"] or not re.fullmatch(r"[0-9a-f]{32}", handle["run_id"]):
        raise BridgeError("remote cleanup identity/root mismatch", "identity_mismatch")
    return IDENTITY + f"\nset -eu\njob={shlex.quote(job)}\n" + r'''
cursor="$job/control"
while :; do
 test ! -L "$cursor" || { echo 'storage path contains a link' >&2; exit 93; }
 test "$cursor" != / || break
 cursor=${cursor%/*}; test -n "$cursor" || cursor=/
done
test "$(realpath -e -- "$job")" = "$job" || exit 93
test -d "$job/control" && test "$(realpath -e -- "$job/control")" = "$job/control" || exit 93
test -f "$job/control/input_fingerprint" && test ! -L "$job/control/input_fingerprint" && test "$(stat -c %h -- "$job/control/input_fingerprint")" = 1 || exit 94
''' + f'test "$(cat -- "$job/control/input_fingerprint")" = {shlex.quote(handle["input_fingerprint"])} || exit 94\n'


