"""Finite local admission over durable detached jobs; no remote scheduler."""
import re
import shutil
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from datetime import datetime, timedelta

from .common import BridgeError, digest, is_link, local_file, now, operation_lock, read_json, relative, write_json
from .config import execution_config
from .jobs import _check_working_directory, cancel, collect, status, submit
from .job_records import load_job
from .prepare import verify_prepared
from .records import load_result, metadata, record_version, save_result
from .simulation import read_outputs

ACTIVE = {"submitting", "running", "unknown"}
PHASES = ACTIVE | {"queued", "terminal", "cancelled_before_submit"}
TERMINAL = {"completed", "failed", "cancelled", "timed_out"}
UNCONFIRMED_CLEANUP = ("cleanup_incomplete", "unsupported_process_layout")


def _directory(value):
    path = Path(value).absolute()
    if any(is_link(p) for p in (path, *path.parents)):
        raise BridgeError("batch path contains a symbolic link / junction", "unsafe_path")
    return path.resolve()


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value):
        raise BridgeError("batch entry id: expected a portable identifier")
    relative("prepared/" + value)


def _attempt(run_id, execution, created_at, replaces=None):
    return {"run_id": run_id, "created_at": created_at, "job_directory": str(Path(execution["local_output_root"]) / run_id),
            "phase": "queued", **({"replaces_run_id": replaces} if replaces else {})}


def batch_start(spec, directory):
    if (not isinstance(spec, dict) or set(spec) != {"schema_version", "execution", "concurrency", "entries"} or
            type(spec["schema_version"]) is not int or spec["schema_version"] != 2 or
            type(spec["concurrency"]) is not int or spec["concurrency"] < 1 or not isinstance(spec["entries"], list) or not spec["entries"]):
        raise BridgeError("batch spec: expected version 2, one execution, positive integer concurrency and nonempty entries")
    execution = execution_config(spec["execution"])
    _directory(execution["local_output_root"])
    originals, identifiers = [], set()
    for entry in spec["entries"]:
        if not isinstance(entry, dict) or set(entry) != {"id", "prepared"} or not isinstance(entry["prepared"], str):
            raise BridgeError("batch entries: expected id and prepared path")
        _identifier(entry["id"])
        if entry["id"].casefold() in identifiers:
            raise BridgeError("batch entries: duplicate/case-alias id")
        identifiers.add(entry["id"].casefold())
        source = _directory(entry["prepared"])
        local_file(source, "prepared.json")
        record = verify_prepared(source)
        _check_working_directory(record["case"], execution)
        originals.append((entry, source, record))
    directory = _directory(directory)
    if directory.exists():
        raise BridgeError("batch output directory already exists; use resume")
    directory.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".batch-", dir=directory.parent))
    try:
        batch_id, created = uuid.uuid4().hex, now()
        declarations, entries = [], []
        for entry, source, record in originals:
            name = "prepared/" + entry["id"]
            target = stage / name
            target.mkdir(parents=True)
            shutil.copyfile(local_file(source, "prepared.json"), target / "prepared.json")
            for item in record["files"]:
                output = local_file(target / "inputs", item["path"])
                output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(local_file(source / "inputs", item["path"]), output)
            local_file(target / "inputs", record["case"]["working_directory"]).mkdir(parents=True, exist_ok=True)
            if verify_prepared(target)["fingerprint"] != record["fingerprint"]:
                raise BridgeError("batch snapshot changed while copying")
            run = uuid.uuid4().hex
            declarations.append({"id": entry["id"], "prepared": name, "origin": str(source), "fingerprint": record["fingerprint"],
                                 "prepared_sha256": digest(target / "prepared.json"), "initial_run_id": run})
            entries.append({"id": entry["id"], "attempts": [_attempt(run, execution, created)]})
        request = {**metadata("batch_request"), "batch_id": batch_id, "execution": execution,
                   "concurrency": spec["concurrency"], "entries": declarations}
        write_json(stage / "batch-request.json", request)
        batch = {**metadata("batch", source={"request": {"path": "batch-request.json", "sha256": digest(stage / "batch-request.json")}}),
                 "batch_id": batch_id, "updated_at": created, "entries": entries}
        write_json(stage / "batch.json", batch)
        stage.rename(directory)
    except BaseException:
        if stage.exists():
            if stage.resolve().parent != directory.parent.resolve() or not stage.name.startswith(".batch-"):
                raise BridgeError("batch staging cleanup escaped its owned parent", "unsafe_path")
            shutil.rmtree(stage)
        raise
    return batch_resume(directory)


def _items(batch, request):
    for entry, declaration in zip(batch["entries"], request["entries"]):
        for attempt in entry["attempts"]:
            yield entry, declaration, attempt


def _request(directory, request, declaration, attempt, *, create=False):
    path = _directory(attempt["job_directory"])
    snapshot = local_file(directory, declaration["prepared"])
    prepared = verify_prepared(snapshot)
    expected = {"run_id": attempt["run_id"], "input_fingerprint": declaration["fingerprint"],
                "prepared": prepared, "execution": request["execution"], "created_at": attempt["created_at"],
                "source": {"batch": {"batch_id": request["batch_id"], "entry_id": declaration["id"]}}}
    file = local_file(path, "request.json")
    if not file.exists():
        if not create:
            return
        if path.exists() and any(path.iterdir()):
            raise BridgeError("reserved job directory has unrelated contents", "identity_mismatch")
        path.mkdir(parents=True, exist_ok=True)
        value = {**metadata("request", attempt["run_id"], declaration["fingerprint"],
                             {"batch": {"batch_id": request["batch_id"], "entry_id": declaration["id"]}}),
                 **expected, "created_at": attempt["created_at"], "prepared_directory": str(snapshot)}
        write_json(file, value)
    actual = read_json(file)
    record_version(actual, "request")
    if any(actual.get(k) != v for k, v in expected.items()):
        raise BridgeError("batch job request differs from its durable intent", "identity_mismatch")


def _load(directory, *, refresh_results=False):
    directory = _directory(directory)
    batch = read_json(local_file(directory, "batch.json"))
    if record_version(batch, "batch") != 2:
        raise BridgeError("batch: supported schema_version is 2")
    request_path = local_file(directory, "batch-request.json")
    if batch["source"].get("request") != {"path": "batch-request.json", "sha256": digest(request_path)}:
        raise BridgeError("batch declaration hash mismatch", "identity_mismatch")
    request = read_json(request_path)
    if record_version(request, "batch_request") != 2 or not re.fullmatch(r"[0-9a-f]{32}", str(request.get("batch_id", ""))) or batch.get("batch_id") != request["batch_id"]:
        raise BridgeError("batch identity mismatch", "identity_mismatch")
    if (type(request.get("concurrency")) is not int or request["concurrency"] <= 0 or
            not isinstance(request.get("entries"), list) or not request["entries"] or
            not isinstance(batch.get("entries"), list) or len(batch["entries"]) != len(request["entries"])):
        raise BridgeError("batch has invalid concurrency/entries")
    execution = execution_config(request.get("execution"), check_files=False)
    if execution != request["execution"]:
        raise BridgeError("batch execution is not canonical")
    ids, runs = set(), set()
    for entry, declaration in zip(batch["entries"], request["entries"]):
        if not isinstance(entry, dict) or not isinstance(declaration, dict):
            raise BridgeError("batch entry/declaration must be an object")
        _identifier(declaration.get("id"))
        ident = declaration["id"]
        if ident.casefold() in ids or entry.get("id") != ident or declaration.get("prepared") != "prepared/" + ident:
            raise BridgeError("batch entry identity/snapshot mismatch")
        ids.add(ident.casefold())
        snapshot = local_file(directory, declaration["prepared"])
        if digest(local_file(snapshot, "prepared.json")) != declaration.get("prepared_sha256") or verify_prepared(snapshot)["fingerprint"] != declaration.get("fingerprint"):
            raise BridgeError("batch snapshot fingerprint mismatch", "identity_mismatch")
        attempts = entry.get("attempts")
        if not isinstance(attempts, list) or not attempts:
            raise BridgeError("batch entry has no attempts")
        previous = None
        for attempt in attempts:
            if not isinstance(attempt, dict) or not re.fullmatch(r"[0-9a-f]{32}", str(attempt.get("run_id", ""))):
                raise BridgeError("invalid batch attempt identity")
            run = attempt["run_id"]
            if (run in runs or not isinstance(attempt.get("phase"), str) or attempt["phase"] not in PHASES or not isinstance(attempt.get("created_at"), str) or
                    attempt.get("job_directory") != str(Path(execution["local_output_root"]) / run) or
                    (previous is None and (run != declaration.get("initial_run_id") or "replaces_run_id" in attempt)) or
                    (previous is not None and attempt.get("replaces_run_id") != previous)):
                raise BridgeError("batch attempt differs from its declaration/history", "identity_mismatch")
            try:
                if datetime.fromisoformat(attempt["created_at"]).utcoffset() != timedelta(0):
                    raise ValueError()
            except ValueError as exc:
                raise BridgeError("batch attempt created_at must be UTC") from exc
            runs.add(run); previous = run
            if "execution" in attempt:
                observation = attempt["execution"]
                if (not isinstance(observation, dict) or not isinstance(observation.get("state"), str) or
                        observation["state"] not in TERMINAL | {"running", "unknown"} or observation.get("communication") not in ("reachable", "unreachable") or
                        any(k in observation and not isinstance(observation[k], str) for k in ("reason", "phase", "observed_at"))):
                    raise BridgeError("malformed batch execution observation")
            if attempt["phase"] == "terminal" and (attempt.get("execution", {}).get("state") not in TERMINAL or
                    attempt["execution"].get("communication") != "reachable" or attempt["execution"].get("reason") in UNCONFIRMED_CLEANUP):
                raise BridgeError("batch terminal phase has no confirmed terminal observation")
            if "submission" in attempt and (not isinstance(attempt["submission"], str) or attempt["submission"] not in ("pending", "confirmed", "unknown", "failed")):
                raise BridgeError("malformed batch submission state")
            if ("summary" in attempt and (not isinstance(attempt["summary"], dict) or "result" not in attempt)) or any(
                    k in attempt and not isinstance(attempt[k], str) for k in ("error", "error_code")):
                raise BridgeError("malformed batch attempt summary/error")
            _request(directory, request, declaration, attempt)
            job = Path(attempt["job_directory"])
            if (job / "job.json").exists():
                handle, _, _ = load_job(job)
                if handle["run_id"] != run or handle["input_fingerprint"] != declaration["fingerprint"]:
                    raise BridgeError("batch job identity mismatch", "identity_mismatch")
            if "result" in attempt:
                ref = attempt["result"]
                if not isinstance(ref, dict) or not isinstance(ref.get("path"), str):
                    raise BridgeError("malformed batch result reference")
                if ref["path"] != "result.json":
                    raise BridgeError("batch result must reference its current result", "unsafe_path")
                if not re.fullmatch(r"[0-9a-f]{64}", str(ref.get("sha256", ""))):
                    raise BridgeError("malformed batch result hash", "identity_mismatch")
                result_path = local_file(job, ref["path"])
                changed = digest(result_path) != ref.get("sha256")
                if changed and not refresh_results:
                    raise BridgeError("batch result reference hash mismatch", "identity_mismatch")
                result = load_result(result_path)
                if (result["run_id"] != run or result["input_fingerprint"] != declaration["fingerprint"] or
                        (not changed and attempt.get("summary") != _summary(result))):
                    raise BridgeError("batch result identity/summary mismatch", "identity_mismatch")
    _cancelled(directory, request, runs)
    return directory, batch, request


def _cancelled(directory, request, runs=None):
    path = local_file(directory, "cancel.json")
    if not path.exists():
        return set()
    value = read_json(path)
    if (record_version(value, "batch_cancel") != 2 or value.get("batch_id") != request["batch_id"] or
            not isinstance(value.get("run_ids"), list) or any(not isinstance(r, str) or not re.fullmatch(r"[0-9a-f]{32}", r) for r in value["run_ids"])):
        raise BridgeError("invalid batch cancellation intent")
    selected = set(value["run_ids"])
    if len(selected) != len(value["run_ids"]) or (runs is not None and not selected.issubset(runs)):
        raise BridgeError("batch cancellation includes unknown attempts")
    return selected


def _save(directory, batch):
    batch["updated_at"] = now()
    write_json(local_file(directory, "batch.json"), batch)


def _view(directory, batch, request):
    attempts = [a for _, _, a in _items(batch, request)]
    slots = sum(a["phase"] in ACTIVE for a in attempts)
    queued = any(a["phase"] == "queued" for a in attempts)
    running = any(a["phase"] in ("running", "submitting") for a in attempts)
    state = "running" if running else "attention" if slots or queued else "finished"
    pending = any(a["phase"] == "terminal" and a.get("data_state") in (None, "pending", "processing") for a in attempts)
    if pending:
        state = "running"
    ok = all(e["attempts"][-1].get("summary", {}).get("ok") is True and
             e["attempts"][-1]["phase"] == "terminal" and not e["attempts"][-1].get("error") for e in batch["entries"])
    entries = []
    for entry, declaration in zip(batch["entries"], request["entries"]):
        projected = []
        for attempt in entry["attempts"]:
            summary = attempt.get("summary", {"simulation": attempt.get("execution", {}).get("state", "unknown" if attempt["phase"] in ACTIVE else "not_started"),
                                               "data": "not_requested", "required_complete": False, "ok": False})
            projected.append({**attempt, "summary": summary})
        entries.append({**entry, "attempts": projected, "input_fingerprint": declaration["fingerprint"], "prepared": declaration["prepared"]})
    return {**batch, "entries": entries, "execution": request["execution"], "concurrency": request["concurrency"], "observed_at": now(),
            "state": state, "ok": state == "finished" and ok, "slots_in_use": slots,
            "cancel_requested": bool(_cancelled(directory, request))}


def _cancel_attempt(attempt):
    try:
        value = cancel(attempt["job_directory"])
        attempt["cancel_request"] = {"request": value["request"], "observed_at": now()}
    except (BridgeError, OSError) as exc:
        attempt["cancel_request"] = {"request": "unknown", "error": str(exc), "observed_at": now()}


def _submit_attempt(directory, batch, request, declaration, attempt):
    try:
        _request(directory, request, declaration, attempt, create=True)
        handle = submit(local_file(directory, declaration["prepared"]), request["execution"], job=attempt["job_directory"])
        if handle["run_id"] != attempt["run_id"]:
            raise BridgeError("submission returned a different run identity", "identity_mismatch")
        attempt["submission"] = handle["submission"]
        attempt["phase"] = "unknown" if handle["submission"] == "unknown" else "running"
    except (BridgeError, OSError) as exc:
        attempt.update(phase="unknown", error=str(exc), error_code=getattr(exc, "code", "local_io"))
    _save(directory, batch)


def _poll(attempt):
    try:
        if not (Path(attempt["job_directory"]) / "job.json").exists():
            attempt.update(phase="unknown", error="local handle not yet persisted")
            return None
        observation = status(attempt["job_directory"])
        attempt["execution"] = {k: observation[k] for k in ("state", "communication", "phase", "reason", "exit_code", "observed_at") if k in observation}
        terminal = observation["communication"] == "reachable" and observation["state"] in TERMINAL and observation.get("reason") not in UNCONFIRMED_CLEANUP
        if observation["communication"] == "reachable" and observation["state"] in TERMINAL | {"running"} and observation.get("phase") != "prepare":
            attempt["submission"] = "confirmed"
        attempt["phase"] = "terminal" if terminal else "running" if observation["communication"] == "reachable" and observation["state"] == "running" else "unknown"
        return observation
    except (BridgeError, OSError) as exc:
        if attempt["phase"] != "terminal":
            attempt["phase"] = "unknown"
        attempt.update(error=str(exc), error_code=getattr(exc, "code", "local_io"))
        return None


def _summary(result):
    return {"simulation": result["simulation"]["status"], "data": result["data"]["status"],
            "required_complete": result["data"]["required_complete"], "ok": result["ok"]}


def _collect_data(job):
    collected = collect(job)
    result = read_outputs(job, collected)
    handle, _, directory = load_job(job)
    collected.update(simulation=result["simulation"], data={k: v for k, v in result["data"].items() if k not in ("analog", "digital")}, ok=result["ok"])
    saved = save_result(directory, collected)
    return {"path": "result.json", "sha256": digest(directory / "result.json")}, _summary(saved)


def _run(directory, batch, request):
    # One bounded data worker; only the coordinator writes batch.json.
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="ams-data") as worker:
        return _coordinate(directory, batch, request, worker)


def _coordinate(directory, batch, request, worker):
    execution_config(request["execution"])
    recover = {a["run_id"] for _, _, a in _items(batch, request) if a["phase"] in ACTIVE}
    retried = set()
    future, processing = None, None
    for _, _, attempt in _items(batch, request):
        if attempt["phase"] == "terminal":
            attempt["data_state"] = "pending"
            attempt.pop("summary", None)
            attempt.pop("result", None)
    while True:
        if future is not None and future.done():
            try:
                processing["result"], processing["summary"] = future.result()
                processing["data_state"] = "complete"
                processing.pop("error", None)
                processing.pop("error_code", None)
            except (BridgeError, OSError) as exc:
                processing.update(data_state="failed", error=str(exc), error_code=getattr(exc, "code", "local_io"))
                processing.pop("result", None)
                processing.pop("summary", None)
            future, processing = None, None

        cancelled = _cancelled(directory, request)
        for _, declaration, attempt in _items(batch, request):
            if attempt["phase"] == "queued" and attempt["run_id"] in cancelled:
                attempt["phase"] = "cancelled_before_submit"
            if attempt["phase"] in ACTIVE:
                if attempt["run_id"] in cancelled and attempt.get("cancel_request", {}).get("request") not in ("requested", "already_terminal"):
                    _cancel_attempt(attempt)
                observation = _poll(attempt)
                if attempt["phase"] == "terminal":
                    attempt["data_state"] = "pending"
                    retried.add(attempt["run_id"])
                retry = attempt["run_id"] in recover and attempt["run_id"] not in retried and attempt["phase"] == "unknown"
                if retry and (not (Path(attempt["job_directory"]) / "job.json").exists() or
                              observation is not None and observation["communication"] == "reachable" and observation.get("reason") in ("remote_record_missing", "launch_not_confirmed")):
                    retried.add(attempt["run_id"])
                    _submit_attempt(directory, batch, request, declaration, attempt)
        if future is None:
            processing = next((a for _, _, a in _items(batch, request) if a["phase"] == "terminal" and a.get("data_state") == "pending"), None)
            if processing is not None:
                processing["data_state"] = "processing"
                future = worker.submit(_collect_data, processing["job_directory"])
        _save(directory, batch)
        dispatch_busy = False
        for _, declaration, attempt in _items(batch, request):
            if attempt["phase"] != "queued":
                continue
            try:
                with operation_lock(directory, ".dispatch.lock"):
                    if attempt["run_id"] in _cancelled(directory, request):
                        attempt["phase"] = "cancelled_before_submit"
                        _save(directory, batch)
                        continue
                    if sum(a["phase"] in ACTIVE for _, _, a in _items(batch, request)) >= request["concurrency"]:
                        break
                    attempt.update(phase="submitting", admitted_at=now())
                    _save(directory, batch)
            except BridgeError as exc:
                if exc.code == "busy":
                    dispatch_busy = True
                    break
                raise
            _submit_attempt(directory, batch, request, declaration, attempt)
            if attempt["run_id"] in _cancelled(directory, request):
                _cancel_attempt(attempt)
                _save(directory, batch)
        _save(directory, batch)
        view = _view(directory, batch, request)
        if not dispatch_busy and view["state"] in ("finished", "attention"):
            return view
        time.sleep(.2)


def batch_resume(directory):
    directory, _, _ = _load(directory, refresh_results=True)
    with operation_lock(directory):
        directory, batch, request = _load(directory, refresh_results=True)
        return _run(directory, batch, request)


def batch_status(directory):
    directory, batch, request = _load(directory)
    execution_config(request["execution"])
    for _, _, attempt in _items(batch, request):
        if attempt["phase"] in ACTIVE:
            _poll(attempt)
    return _view(directory, batch, request)


def batch_cancel(directory):
    directory, _, _ = _load(directory)
    with operation_lock(directory, ".dispatch.lock"):
        directory, batch, request = _load(directory)
        selected = [a["run_id"] for _, _, a in _items(batch, request)]
        value = {**metadata("batch_cancel"), "batch_id": request["batch_id"], "run_ids": selected, "results": {}}
        write_json(local_file(directory, "cancel.json"), value)
    for _, _, attempt in _items(batch, request):
        if attempt["phase"] in ACTIVE and (Path(attempt["job_directory"]) / "job.json").exists():
            _cancel_attempt(attempt)
            value["results"][attempt["run_id"]] = attempt["cancel_request"]
    # Retain the intent if a concurrent request expanded its selected attempts.
    with operation_lock(directory, ".dispatch.lock"):
        latest = read_json(local_file(directory, "cancel.json"))
        if latest["created_at"] == value["created_at"]:
            write_json(local_file(directory, "cancel.json"), value)
    return {**_view(directory, batch, request), "cancel_request": value}


def batch_rerun(directory, entry_id):
    directory, _, _ = _load(directory)
    with operation_lock(directory):
        directory, batch, request = _load(directory)
        entry = next((e for e in batch["entries"] if e["id"] == entry_id), None)
        if entry is None:
            raise BridgeError("batch rerun: unknown entry id")
        with operation_lock(directory, ".dispatch.lock"):
            previous = entry["attempts"][-1]["run_id"]
            entry["attempts"].append(_attempt(uuid.uuid4().hex, request["execution"], now(), previous))
            _save(directory, batch)
        return _run(directory, batch, request)
