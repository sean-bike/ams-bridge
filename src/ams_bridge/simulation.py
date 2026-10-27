"""Blocking single-job orchestration and raw-data result assembly."""
import time
import uuid
import re
from pathlib import Path

from .common import BridgeError, now, write_json
from .config import case_spec, execution_config, finite, load_config
from .job_records import load_job
from .jobs import collect, status, submit
from .prepare import prepare
from .records import BRIDGE_VERSION
from .records import save_result
from .results import classify_logs
from .collection import resolve_file
from .transport import SSH
from .waveforms import current_result, query_spec, verified_artifact, waveforms
from .wave_query import decimal, factor


def diagnostic(exc, stage, source=None):
    return {"code": getattr(exc, "code", "local_io"), "stage": stage, "message": str(exc), "source": source}


def read_outputs(handle, collected, *, transport=None):
    """Read declarations separately so a bad optional source preserves good data."""
    handle, request, directory = load_job(handle)
    case = request["prepared"]["case"]
    observation, solver = collected["execution"], collected["solver"]
    state = observation["state"]
    simulation = ("success" if solver["status"] == "success" else "failed" if solver["status"] == "failed" else "unknown") if state == "completed" else state
    if simulation not in ("success", "failed", "cancelled", "timed_out", "unknown"):
        simulation = "unknown"
    errors, warnings = [], []
    warnings.extend({"code": w["code"], "stage": w["phase"], "message": w["text"], "source": w}
                    for w in solver.get("warnings", []))
    data = {"status": "not_requested", "required_complete": True, "analog": [], "digital": [], "sources": [], "missing": []}
    if simulation != "success":
        terminated = simulation in ("timed_out", "cancelled")
        if terminated:
            reason = observation.get("reason") or ("wall_timeout" if simulation == "timed_out" else "cancel_request")
            errors.append({"code": reason, "stage": "execution", "message": reason, "source": observation})
        proofs = sorted(solver.get("errors", []), key=lambda e: (
            e.get("phase") == "preprocess",
            bool(re.search(r'^\s*(?:xrun|xmelab)(?:\([^)]*\))?:\s*\*[EF],(?:VLGERR|ELBERR|SPCERR|AMSCBVLOG)\b', e["text"], re.I)),
            not bool(e.get("native_location"))))
        for proof in proofs:
            phase = proof.get("phase", "execution")
            license_message = bool(re.search("license", proof["text"], re.I))
            context = terminated and license_message
            (warnings if context else errors).append({"code": "native_log_context" if context else "license_unavailable" if license_message else phase + "_failed",
                "stage": phase, "message": proof["text"], "source": proof})
        if not terminated and not proofs:
            errors.append({"code": observation.get("error_code", "execution_" + simulation), "stage": "execution",
                           "message": observation.get("reason", solver["status"]), "source": None})
    for artifact in collected["collection"].get("artifacts", []):
        if artifact.get("remote_status") == "missing":
            warnings.append({"code": "remote_artifact_unavailable", "stage": "collection",
                             "message": "using the previously published local copy", "source": artifact["path"]})
        if artifact.get("status") != "verified":
            issue = {"code": artifact.get("error_code", "missing_artifact"), "stage": "collection",
                     "message": artifact.get("error", "artifact unavailable: " + artifact["path"]), "source": artifact["path"]}
            (errors if artifact["required"] else warnings).append(issue)
            data["missing"].append({"artifact": artifact["path"], "required": artifact["required"], **issue})
            if artifact["required"]:
                data["required_complete"] = False
    def missing_data(declaration, issue):
        (errors if declaration["required"] else warnings).append(issue)
        data["missing"].append({"id": declaration["id"], "required": declaration["required"], **issue})
        if declaration["required"]:
            data["required_complete"] = False

    for declaration in case.get("waveforms", []):
        try:
            q = query_spec({"operation": "read", **{k: v for k, v in declaration.items() if k not in ("id", "required", "coverage")}})
            reading = waveforms(handle, q, transport=transport)
            data["sources"].append({"id": declaration["id"], **{k: v for k, v in reading.items() if k != "signals"}})
            for issue in reading.get("errors", []):
                missing_data(declaration, issue)
            for signal in reading["signals"]:
                try:
                    if not reading["final"] or not signal["points"]:
                        raise BridgeError(signal["name"] + ": no final saved curve", "empty_waveform")
                    if "coverage" in declaration:
                        coverage = declaration["coverage"]
                        lo, hi = (decimal(t) * factor(signal) for t in signal["coverage"])
                        tolerance = decimal(coverage["atol_s"])
                        if lo > decimal(coverage["start_s"]) + tolerance or hi < decimal(coverage["stop_s"]) - tolerance:
                            raise BridgeError(signal["name"] + ": insufficient saved coverage", "out_of_range")
                    data[signal["kind"]].append({"source_id": declaration["id"], "analysis": declaration["analysis"], **signal})
                except BridgeError as exc:
                    missing_data(declaration, {**diagnostic(exc, "data", declaration["artifact"]), "signal": signal["name"]})
        except (BridgeError, OSError) as exc:
            missing_data(declaration, diagnostic(exc, "data", declaration["artifact"]))
    if case.get("waveforms") or data["missing"]:
        data["status"] = "partial" if data["missing"] and (data["analog"] or data["digital"]) else "failed" if data["missing"] else "complete"
    result = {"ok": simulation == "success" and data["required_complete"],
              "simulation": {"status": simulation, "exit_code": observation.get("exit_code"),
                             "reason": observation.get("reason"), "evidence": solver},
              "data": data, "errors": errors, "warnings": warnings,
              "job": {**handle, "record": str(directory / "job.json"), "prepared": request.get("prepared_directory"),
                      "resume": {"status": str(directory), "collect": str(directory)}},
              "metadata": {"bridge_version": BRIDGE_VERSION, "input_fingerprint": handle["input_fingerprint"],
                           "wall_timeout_s": request["execution"].get("wall_timeout_s"),
                           "solver_versions": solver.get("version_lines", []), "raw_result": str(directory / "result.json"),
                           "logs": [a.get("local_path") for a in collected["collection"].get("artifacts", []) if a["path"] in case["log_files"]]}}
    # Keep full curves in their sources and the returned value; persist only a summary.
    summary = {**result, "data": {**data, "analog": [], "digital": []}, "recorded_at": now()}
    write_json(directory / "simulation.json", summary)
    return result


def read_results(handle, *, transport=None):
    """Re-read one existing job; never prepare, submit or update its batch."""
    handle, request, directory = load_job(handle)
    previous = current_result(directory, handle)[0] if (directory / "result.json").exists() else None
    terminal = {"completed", "failed", "cancelled", "timed_out"}
    local_final = bool(previous and previous["collection"].get("final") and
                       previous["execution"].get("state") in terminal and
                       previous["execution"].get("reason") not in ("cleanup_incomplete", "unsupported_process_layout"))
    missing = []
    if local_final:
        records = previous["collection"].setdefault("artifacts", [])
        by_path = {a["path"]: a for a in records}
        for declaration in request["prepared"]["case"]["artifacts"]:
            item = by_path.get(declaration["path"])
            if item is None:
                item = {**declaration, "status": "missing"}
                records.append(item)
            try:
                verified_artifact(item, directory, {"bytes": float('inf')})
            except (BridgeError, OSError) as exc:
                missing.append((item, exc))
        collected = previous
    else:
        observation = status(handle, transport=transport)
        if observation["state"] not in terminal or observation.get("reason") in ("cleanup_incomplete", "unsupported_process_layout"):
            return {"ok": False, "simulation": {"status": observation["state"], "exit_code": observation.get("exit_code"),
                    "reason": observation.get("reason"), "last_observation": observation},
                    "data": {"status": "not_requested", "required_complete": False, "analog": [], "digital": [], "sources": [], "missing": []},
                    "errors": [{"code": observation.get("error_code", "execution_not_terminal"), "stage": "execution",
                                "message": "result reading requires a confirmed terminal job", "source": observation}],
                    "warnings": [], "job": handle, "metadata": {"bridge_version": BRIDGE_VERSION,
                    "wall_timeout_s": request["execution"].get("wall_timeout_s")}}
    if not local_final or missing:
        fresh = collect(handle, transport=transport)
        if fresh["collection"].get("final") or not local_final:
            collected = fresh
            if not local_final and collected["execution"].get("state") == "unknown":
                collected["execution"] = observation
        else:
            # A failed remote query must not erase already-confirmed execution.
            for item, exc in missing:
                item.update(status="error", error=str(exc), error_code=getattr(exc, "code", "missing_artifact"))
    texts = {}
    for name in request["prepared"]["case"]["log_files"]:
        try:
            texts[name] = resolve_file(collected["collection"]["artifacts"], name).read_text(encoding="utf-8", errors="replace")
        except (BridgeError, OSError):
            pass
    launch = collected.get("control_logs", {}).get("launch.log")
    if launch and not launch["truncated"]:
        texts["control/launch.log"] = launch["text"]
    if texts:
        collected["solver"] = classify_logs(texts, execution=collected["execution"])
    collected = save_result(directory, collected)
    return read_outputs(handle, collected, transport=transport)


def run_simulation(config, *, wait_timeout_s=None, transport=None):
    """Run once. Client wait expiry retains the original job for explicit recovery."""
    handle, stage, wait, directory, observation, execution = None, "input", None, None, None, None
    try:
        config = load_config(config) if isinstance(config, (str, Path)) else config
        if not isinstance(config, dict) or set(config) != {"case", "execution"}:
            raise BridgeError("configuration: expected case and execution")
        case, execution = case_spec(config["case"]), execution_config(config["execution"])
        if not any(w["required"] for w in case.get("waveforms", [])):
            raise BridgeError("run requires at least one required waveform; use submit/collect for logs only")
        wait = wait_timeout_s if wait_timeout_s is not None else 300
        finite(wait, "wait_timeout_s", True)
        if wait <= 0:
            raise BridgeError("wait_timeout_s must be positive")
        stage = "prepare"
        directory = Path(execution["local_output_root"]) / ("prepared-" + uuid.uuid4().hex)
        prepare(case, directory)
        stage = "submission"
        handle = submit(directory, execution, transport=transport)
        stage = "wait"
        deadline = time.monotonic() + wait
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BridgeError("client wait budget expired; remote wall timeout is unchanged", "wait_timeout")
            poll_transport = transport or SSH({**execution, "control_timeout_s": min(execution["control_timeout_s"], remaining),
                                               "connect_timeout_s": min(execution["connect_timeout_s"], remaining)})
            observation = status(handle, transport=poll_transport)
            if observation.get("communication") == "unreachable":
                if time.monotonic() >= deadline:
                    raise BridgeError("client wait budget expired during status query; original job retained", "wait_timeout")
                raise BridgeError(observation.get("reason", "SSH unavailable while waiting"), observation.get("error_code", "communication"))
            if observation["state"] == "unknown" or observation.get("reason") in ("cleanup_incomplete", "unsupported_process_layout"):
                raise BridgeError("execution not confirmed; query the original job before continuing", "execution_unknown")
            if observation["state"] in ("completed", "failed", "cancelled", "timed_out"):
                break
            if time.monotonic() >= deadline:
                raise BridgeError("client wait budget expired; remote wall timeout is unchanged", "wait_timeout")
            time.sleep(min(.2, max(0, deadline - time.monotonic())))
        stage = "collection"
        result = read_outputs(handle, collect(handle, transport=transport), transport=transport)
        result["metadata"]["wait_timeout_s"] = wait
        return result
    except (BridgeError, OSError) as exc:
        last = observation or {}
        known = last.get("state") if last.get("communication", "reachable") == "reachable" else "unknown"
        return {"ok": False, "simulation": {"status": known if known in ("running", "failed", "cancelled", "timed_out") else "unknown",
                                            "exit_code": last.get("exit_code"), "reason": last.get("reason"), "last_observation": last},
                "data": {"status": "failed", "required_complete": False, "analog": [], "digital": [], "sources": [], "missing": []},
                "errors": [diagnostic(exc, stage)], "warnings": [], "job": handle,
                "metadata": {"bridge_version": BRIDGE_VERSION, "wait_timeout_s": wait,
                             "wall_timeout_s": execution.get("wall_timeout_s") if execution is not None else None,
                             "prepared": str(directory) if directory is not None else None}}
