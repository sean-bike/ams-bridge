"""Durable job API. All state needed to resume lives in ordinary JSON files."""
from __future__ import annotations

import hashlib
import os
import posixpath
import re
import shlex
import tarfile
import tempfile
import time
import uuid
from pathlib import Path

from . import remote
from .job_records import load_job, save_handle
from .artifacts import safe_path_script
from .collection import Meter, collect_artifacts, load_cache, recover_staging, resolve_file
from .common import BridgeError, digest, local_file, now, operation_lock, read_json, write_json
from .config import case_spec, execution_config
from .prepare import Inputs, verify_prepared
from .results import classify_logs, native_progress
from .records import load_result, metadata, record_version, save_result
from .transport import SSH, environment_command, lease_timeout_s


def _transport(execution, transport):
    return transport if transport is not None else SSH(execution)


def _check_working_directory(case, execution):
    # The verified Xcelium 18.03 environment crashes in whitespace-containing work paths.
    for field, path in (("execution.remote_root", execution["remote_root"]),
                        ("case.working_directory", case["working_directory"])):
        if any(character.isspace() for character in path):
            raise BridgeError(f"{field}: whitespace is unsupported in the remote simulation working path: {path!r}")


def _probe_control(execution, transport):
    probe = r'''set -eu
test "${BASH_VERSINFO[0]}" -ge 4
for tool in setsid nohup timeout sha256sum tar ps realpath tail head wc grep flock stat find base64 cut; do command -v "$tool"; done
test -r /proc/sys/kernel/random/boot_id
timeout --version | head -n 1
setsid --version | head -n 1
'''
    probe += f"\ntest -w {shlex.quote(execution['remote_root'])} || test -w {shlex.quote(posixpath.dirname(execution['remote_root']))}\n"
    return transport.run(probe).stdout.decode("utf-8", "replace")


def _probe_solver(execution, transport):
    tools_probe = "set -eu; printf 'XRUN_PATH='; command -v " + shlex.quote(execution["xrun_path"]) + "; "
    tools_probe += shlex.join([execution["xrun_path"], "-version"]) + " 2>&1"
    probe = f"timeout --kill-after=2s {execution['control_timeout_s']}s " + environment_command(execution, ["/bin/bash", "-c", tools_probe])
    return transport.run(probe).stdout.decode("utf-8", "replace")


def validate(case, execution, *, transport=None):
    case, execution = case_spec(case), execution_config(execution)
    _check_working_directory(case, execution)
    inputs = Inputs(case)
    files = inputs.resolve()
    plan = {"arguments": [execution["xrun_path"], *case["arguments"]],
            "working_directory": "<remote_root>/<run_id>/work/" + case["working_directory"],
            "input_mapping": [{"source": str(Path(case["input_root"]) / p), "runtime": "work/" + p} for p in files],
            "environment_setup": execution.get("environment_setup"),
            "environment_names": list(execution["environment"]),
            "artifacts": case["artifacts"], "wall_timeout_s": execution["wall_timeout_s"],
            "transfer_timeout_s": execution["transfer_timeout_s"], "operation_lease_timeout_s": lease_timeout_s(execution)}
    plan["dependency_report"] = inputs.report
    if "waveforms" in case:
        plan["waveforms"] = {"declarations": case["waveforms"], "save_status": "requires_runtime_confirmation",
                             "control_inputs": [p for p in files if Path(p).suffix.lower() in (".scs", ".sv", ".v", ".vams", ".vh", ".tcl")]}
    report = {**metadata("probe"), "local": "passed", "remote": "not_checked", "plan": plan}
    try:
        t = _transport(execution, transport)
        control = _probe_control(execution, t)
        report["control_probe"] = {"status": "passed", "output": control}
        solver = _probe_solver(execution, t)
        report["solver_probe"] = {"status": "passed", "output": solver}
        report.update(remote="passed", probe_output=control + solver, observed_at=now())
    except BridgeError as exc:
        report.update(remote="blocked", error=str(exc), error_code=exc.code, observed_at=now())
    return report


def _external_check(case):
    script = "set -eu\n"
    for d in case["external_dependencies"]:
        if d.get("type") == "directory":
            script += f"test -d {shlex.quote(d['path'])} || {{ printf '%s\\n' {shlex.quote('external directory unavailable: ' + d['path'])} >&2; exit 78; }}\n"
            continue
        message = shlex.quote("external dependency fingerprint mismatch: " + d["path"])
        script += f"test \"$(sha256sum < {shlex.quote(d['path'])} | cut -d ' ' -f 1)\" = {shlex.quote(d['sha256'])} || {{ printf '%s\\n' {message} >&2; exit 78; }}\n"
    return script


def submit(prepared_directory, execution, *, job=None, transport=None):
    if job is not None:
        if not isinstance(job, dict):
            path = Path(job)
            job_file = path / "job.json" if path.is_dir() else path
            if not job_file.exists() and job_file.name == "job.json" and (job_file.parent / "request.json").is_file():
                _recover_handle(job_file.parent, prepared_directory, execution)
        _, _, directory = _load(job)
        with operation_lock(directory):
            return _submit(prepared_directory, execution, job=job, transport=transport)
    return _submit(prepared_directory, execution, transport=transport)


def _recover_handle(directory, prepared_directory, execution):
    with operation_lock(directory):
        if (directory / "job.json").exists():
            return
        prepared = verify_prepared(prepared_directory)
        request = read_json(directory / "request.json")
        if record_version(request, "request") != 2 or not isinstance(request["run_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", request["run_id"]):
            raise BridgeError("request-only recovery requires a V2 run identity")
        original = execution_config(request.get("execution"))
        if request["input_fingerprint"] != prepared["fingerprint"] or original != execution_config(execution):
            raise BridgeError("request-only recovery requires the original prepared fingerprint and execution configuration")
        handle = {**metadata("job", request["run_id"], prepared["fingerprint"],
                              {"request_recovery": {"path": "request.json", "sha256": digest(directory / "request.json")}}),
                  "ssh_host": original["ssh_host"], "local_directory": str(directory.resolve()),
                  "remote_directory": original["remote_root"] + "/" + request["run_id"],
                  "created_at": request["created_at"], "submission": "pending", "recovery": "request_only"}
        _load(handle)
        write_json(directory / "job.json", handle)


def _submit(prepared_directory, execution, *, job=None, transport=None):
    prepared_directory = Path(prepared_directory).resolve()
    prepared = verify_prepared(prepared_directory)
    execution = execution_config(execution)
    _check_working_directory(prepared["case"], execution)
    t = _transport(execution, transport)
    if job is not None:
        handle, request, directory = _load(job)
        if handle["input_fingerprint"] != prepared["fingerprint"] or execution != execution_config(request["execution"]):
            raise BridgeError("resume requires the original prepared fingerprint and execution configuration")
        observation = status(handle, transport=t)
        if observation["communication"] != "reachable":
            return handle
        if observation["state"] == "running" or observation["phase"] == "finished":
            handle["submission"] = "confirmed"
            save_handle(directory, handle)
            return handle
    else:
        run_id = uuid.uuid4().hex
        directory = Path(execution["local_output_root"]) / run_id
        directory.mkdir(parents=True, exist_ok=False)
        handle = {**metadata("job", run_id, prepared["fingerprint"]), "ssh_host": execution["ssh_host"],
                  "remote_directory": execution["remote_root"] + "/" + run_id, "local_directory": str(directory),
                  "input_fingerprint": prepared["fingerprint"], "created_at": now(), "submission": "pending"}
        write_json(directory / "request.json", {**metadata("request", run_id, prepared["fingerprint"]),
                   "prepared": prepared, "prepared_directory": str(prepared_directory), "execution": execution})
    remote_directory = handle["remote_directory"]
    if handle.get("error"):
        write_json(directory / ("submit-error-" + uuid.uuid4().hex + ".json"), handle)
        handle.pop("error", None)
        handle.pop("error_code", None)
    save_handle(directory, handle)
    launching = False
    try:
        establish = f"set -eu\numask 077\nmkdir -p -- {shlex.quote(execution['remote_root'])}\n"
        create = (f"mkdir -- {shlex.quote(remote_directory)}\n"
                  f"mkdir -- {shlex.quote(remote_directory + '/control')} {shlex.quote(remote_directory + '/work')}\n"
                  f"printf %s {shlex.quote(prepared['fingerprint'])} > {shlex.quote(remote_directory + '/control/input_fingerprint')}\n")
        establish += f"if test ! -e {shlex.quote(remote_directory)}; then\n{create}fi\n" if job is not None else create
        establish += f"test ! -L {shlex.quote(remote_directory)}\n"
        establish += f"test \"$(cat {shlex.quote(remote_directory + '/control/input_fingerprint')})\" = {shlex.quote(prepared['fingerprint'])}\n"
        t.run(establish + _external_check(prepared["case"]))
        probe = {**metadata("probe", handle["run_id"], handle["input_fingerprint"]), "remote": "passed"}
        try:
            probe["control_probe"] = {"output": _probe_control(execution, t)}
            probe["solver_probe"] = {"output": _probe_solver(execution, t)}
        except BridgeError as exc:
            probe.update(remote="blocked", error=str(exc), error_code=exc.code)
            write_json(directory / "probe.json", probe)
            raise BridgeError(str(exc), "communication" if exc.code == "communication" else "environment") from exc
        write_json(directory / "probe.json", probe)
        runtime = ("cd -- " + shlex.quote(remote_directory + "/work/" + prepared["case"]["working_directory"]) + " || exit 72\n" +
                   "export AMS_BRIDGE_JOB_ID=" + shlex.quote(handle["run_id"]) + "\n" +
                   "{ printf 'WORKING_DIRECTORY=%s\\n' \"$PWD\"; printf 'XRUN_PATH='; command -v " + shlex.quote(execution["xrun_path"]) +
                   "; printf 'STANDALONE_SPECTRE_PATH='; command -v spectre || :; printf '\\nPATH=%s\\n' \"$PATH\"; } > " +
                   shlex.quote(remote_directory + "/control/runtime_environment.txt") + "\nexec " +
                   shlex.join([execution["xrun_path"], *prepared["case"]["arguments"]]))
        solver = environment_command(execution, ["/bin/bash", "-c", runtime])
        code = remote.supervisor(remote_directory + "/control", remote_directory + "/work/" + prepared["case"]["working_directory"], solver, execution["wall_timeout_s"])
        with tempfile.TemporaryFile() as archive:
            with tarfile.open(fileobj=archive, mode="w") as tar:
                for item in prepared["files"]:
                    tar.add(prepared_directory / "inputs" / item["path"], arcname=item["path"], recursive=False)
            archive.seek(0)
            archive_hash = hashlib.file_digest(archive, "sha256").hexdigest()
            archive.seek(0)
            upload = remote_directory + "/control/upload.tar.part"
            staging = f"set -eu\numask 077\ncd {shlex.quote(remote_directory + '/control')}\n"
            staging += "test ! -e launch.claim || { echo 'launch already claimed; use status, never re-execute' >&2; exit 76; }\n"
            staging += "printf uploading > transfer_stage.tmp; mv transfer_stage.tmp transfer_stage\n"
            staging += f"cat > {shlex.quote(upload)}\n"
            staging += "printf verifying > transfer_stage.tmp; mv transfer_stage.tmp transfer_stage\n"
            staging += f"test \"$(sha256sum < {shlex.quote(upload)} | cut -d ' ' -f 1)\" = {shlex.quote(archive_hash)} || {{ echo 'upload archive fingerprint mismatch; resume with original handle' >&2; exit 79; }}\n"
            staging += f"cd {shlex.quote(remote_directory + '/work')}\ntar -xf {shlex.quote(upload)}\nrm -- {shlex.quote(upload)}\n"
            staging += f"mkdir -p -- {shlex.quote(prepared['case']['working_directory'])}\n"
            for item in prepared["files"]:
                staging += f"test \"$(sha256sum < {shlex.quote(item['path'])} | cut -d ' ' -f 1)\" = {shlex.quote(item['sha256'])}\n"
            staging += _external_check(prepared["case"])
            staging += f"printf ready > {shlex.quote(remote_directory + '/control/transfer_stage.tmp')}\nmv {shlex.quote(remote_directory + '/control/transfer_stage.tmp')} {shlex.quote(remote_directory + '/control/transfer_stage')}\n"
            staging += remote.launch(remote_directory + "/control", code)
            command = shlex.join(["flock", "-n", "-o", remote_directory + "/control/operation.lock", "bash", "-c", staging])
            # -o prevents a detached solver from retaining the transfer lock descriptor.
            write_json(directory / "launch.json", {**metadata("launch", handle["run_id"], handle["input_fingerprint"]), "command": command, "recorded_at": now()})
            launching = True
            started = time.monotonic()
            upload_record = {**metadata("upload", handle["run_id"], handle["input_fingerprint"]),
                             "bytes_attempted": archive.seek(0, os.SEEK_END), "timeout_s": execution["transfer_timeout_s"], "confirmed": False}
            archive.seek(0)
            try:
                t.run(command, input_file=archive, transfer=True)
                upload_record["confirmed"] = True
            finally:
                upload_record["elapsed_s"] = time.monotonic() - started
                write_json(directory / "upload.json", upload_record)
        handle["submission"] = "confirmed"
    except BridgeError as exc:
        handle.update(submission="unknown" if launching or exc.code == "communication" else "failed",
                      error=str(exc), error_code=exc.code)
    save_handle(directory, handle)
    return handle


def _load(handle):
    return load_job(handle)


def _parse_status(data):
    try:
        lines = data.decode("ascii").strip("\r\n").splitlines()
        if len(lines) != 1:
            raise BridgeError("remote status record is malformed", "invalid_remote_record")
        parts = lines[0].split("\t")
        if (len(parts) == 4 and parts[0] == "terminal" and parts[1] in ("completed", "failed", "cancelled", "timed_out") and
                re.fullmatch(r"\d{1,3}", parts[2]) and int(parts[2]) <= 255 and re.fullmatch(r"[a-z_]+", parts[3]) and
                (parts[1] != "completed" or int(parts[2]) == 0)):
            return {"state": parts[1], "exit_code": int(parts[2]), "reason": parts[3], "phase": "finished"}
        if parts == ["running"]:
            return {"state": "running", "phase": "running"}
        if len(parts) == 2 and parts[0] == "unknown" and re.fullmatch(r"[a-z_]+", parts[1]):
            return {"reason": parts[1]}
    except UnicodeError:
        pass
    raise BridgeError("remote status record is malformed", "invalid_remote_record")


def status(handle, *, transport=None, details=False):
    handle, request, directory = _load(handle)
    result = {**metadata("status", handle["run_id"], handle["input_fingerprint"]), "communication": "reachable", "state": "unknown",
              "phase": "unknown", "observed_at": now()}
    try:
        response = _transport(request["execution"], transport).run(remote.query(handle["remote_directory"] + "/control"), check=False)
        if response.returncode == 74:
            result["reason"] = "remote_record_missing"
        else:
            if response.returncode:
                raise BridgeError("remote status command failed", "invalid_remote_record")
            result.update(_parse_status(response.stdout))
        if result["state"] == "unknown" and handle["submission"] == "failed":
            result.update(state="failed", phase="prepare", reason=handle.get("error", "submission_failed"))
    except BridgeError as exc:
        if exc.code == "invalid_remote_record":
            result.update(reason=str(exc), error_code=exc.code)
            write_json(directory / "status.json", result)
            return result
        result.update(communication="unreachable", reason=str(exc), error_code=exc.code)
    if details:
        result["logs"] = []
        for name in request["prepared"]["case"]["log_files"]:
            try:
                result["logs"].append(logs(handle, name, transport=transport))
            except BridgeError as exc:
                result["logs"].append({"path": name, "text": "", "truncated": False,
                                       "error": str(exc), "error_code": exc.code, "observed_at": now()})
        result["native_progress"] = native_progress(result["logs"])
        if result["state"] == "running" and result["communication"] == "reachable":
            try:
                response = _transport(request["execution"], transport).run(remote.processes(handle["remote_directory"] + "/control"))
                observed = [{"pid": int(m[1]), "tool": m[2]} for m in re.finditer(rb'(?m)^\s*(\d+)\s+(\S+)\s*$', response.stdout)]
                for item in observed:
                    item["tool"] = item["tool"].decode("utf-8", "replace")
                result["native_progress"]["processes"] = observed
                result["native_progress"]["basis"] = "observed bounded logs and verified worker session; not an execution state"
                priorities = {"amsprimtool": 5, "spectre": 4, "xmvlog": 3, "xmvhdl": 3, "xmelab": 2, "xmsim": 1, "xrun": 0}
                candidates = [item for item in observed if item["tool"] in priorities]
                if candidates:
                    tool = max(candidates, key=lambda item: priorities[item["tool"]])
                    source = {**tool, "path": "control/worker.identity", "method": "verified worker session ps --sid", "observed_at": now()}
                    progress = result["native_progress"]
                    progress.update(tool=tool["tool"], tool_source=source)
                    phase = {"amsprimtool": "analog_interface", "xmvlog": "compile", "xmvhdl": "compile", "xmelab": "elaborate"}.get(tool["tool"])
                    if phase:
                        progress.update(stage=phase, source=source)
            except BridgeError as exc:
                result["native_progress"]["process_error"] = {"code": exc.code, "message": str(exc)}
    write_json(directory / "status.json", result)
    return result


def cancel(handle, *, transport=None):
    handle, request, _ = _load(handle)
    t = _transport(request["execution"], transport)
    try:
        response = t.run(remote.request_cancel(handle["remote_directory"] + "/control"))
        outcome = response.stdout.decode().strip()
    except BridgeError as exc:
        return {"request": "unknown", "error": str(exc), "status": status(handle, transport=t)}
    return {"request": outcome, "status": status(handle, transport=t)}


def _safe_remote_file(handle, name):
    return safe_path_script(handle, name)


def logs(handle, path=None, limit_bytes=65536, *, transport=None):
    handle, request, _ = _load(handle)
    case = request["prepared"]["case"]
    if type(limit_bytes) is not int or not 0 < limit_bytes <= 1024 * 1024:
        raise BridgeError("limit_bytes: expected 1..1048576")
    path = path or (case["log_files"][0] if case["log_files"] else None)
    if path not in case["log_files"]:
        raise BridgeError("logs: path must be declared in log_files")
    response = _transport(request["execution"], transport).run(
        _safe_remote_file(handle, path) + 'printf "AMS_BRIDGE_LOG_MTIME\\t%s\\n" "$(stat -c %Y -- "$p")" >&2\n' + f'tail -c {limit_bytes + 1} -- "$p"\n')
    data = response.stdout
    stamp = re.search(rb'(?m)^AMS_BRIDGE_LOG_MTIME\t(\d+)$', response.stderr)
    return {"path": path, "text": data[-limit_bytes:].decode("utf-8", "replace"),
            "truncated": len(data) > limit_bytes, "observed_at": now(),
            "mtime_epoch_s": int(stamp[1]) if stamp else None}


def collect(handle, *, transport=None):
    checked, _, directory = _load(handle)
    with operation_lock(directory):
        previous = local_file(directory, "result.json")
        if previous.exists():
            load_result(previous)
        recover_staging(directory, checked)
        load_cache(directory, checked)
        return _collect(handle, transport=transport)


def _collect(handle, *, transport=None):
    handle, request, directory = _load(handle)
    execution = execution_config(request["execution"])
    t = Meter(_transport(execution, transport), execution)
    observation = status(handle, transport=t)
    final = False
    case = request["prepared"]["case"]
    cache = load_cache(directory, handle)
    controls = {}
    try:
        if observation["communication"] != "reachable":
            raise BridgeError("collection unavailable: remote job is unreachable", "communication")
        t.stats["control_calls"] += 1
        with SSH(execution).lease(handle["remote_directory"] + "/control") as ensure:
            # Recheck after admission: cleanup may have started after status().
            observation = status(handle, transport=t)
            if observation["communication"] != "reachable":
                raise BridgeError("collection admission status could not be confirmed", "communication")
            final = (observation["phase"] == "finished" and observation["state"] in ("completed", "failed", "cancelled", "timed_out")
                     and observation.get("reason") not in ("cleanup_incomplete", "unsupported_process_layout"))
            collection, controls = collect_artifacts(t, handle, case, directory, final, ensure, cache)
    except (BridgeError, OSError) as exc:
        if getattr(exc, "code", None) == "busy":
            raise
        collection = {"status": "partial", "final": final, "artifacts": [
            {**item, "status": "error", "error": str(exc), "error_code": getattr(exc, "code", "local_io")} for item in case["artifacts"]]}
    records = collection["artifacts"]
    texts = {}
    for name in case["log_files"]:
        try:
            texts[name] = resolve_file(records, name).read_text(encoding="utf-8", errors="replace")
        except (OSError, BridgeError):
            continue
    if "launch.log" in controls and final and not controls["launch.log"]["truncated"]:
        texts["control/launch.log"] = controls["launch.log"]["text"]
    solver = classify_logs(texts, execution=observation)
    result = {"run_id": handle["run_id"], "input_fingerprint": handle["input_fingerprint"],
              "observed_at": now(), "execution": observation, "solver": solver, "control_logs": controls,
              "collection": collection, "transfer": t.summary()}
    return save_result(directory, result)
