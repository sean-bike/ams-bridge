"""Conservative solver log classification; no circuit measurements."""
import re
from datetime import datetime, timezone

LOG_RULES_VERSION = "2.4.0"


def native_progress(logs):
    """Observed phase in bounded log tails, independent of execution state."""
    result = {"stage": "unknown", "tool": "unknown", "source": None,
              "simulation_time": None, "last_log_updated_at": None,
              "basis": "observed bounded logs; not an execution state"}
    for log in sorted(logs, key=lambda item: item.get("mtime_epoch_s") or 0):
        if log.get("mtime_epoch_s") is not None:
            result["last_log_updated_at"] = datetime.fromtimestamp(log["mtime_epoch_s"], timezone.utc).isoformat()
        lines = list(enumerate(log.get("text", "").splitlines(), 1))
        starts = [i for i, line in lines if re.search(r'^\s*TOOL:\s*xrun.*Started on', line, re.I)]
        if starts:
            lines = [(i, line) for i, line in lines if i >= starts[-1]]
        for index, line in lines:
            source = {"path": log["path"], "text": line,
                      "line": None if log.get("truncated") else index,
                      "tail_line": index if log.get("truncated") else None}
            stage, tool = None, None
            if re.search(r'waiting\b.*licen[cs]e|licen[cs]e.*\bwaiting\b', line, re.I):
                stage, tool = "license_wait", "unknown"
            elif re.search(r'\bamsprimtool\b', line, re.I):
                stage, tool = "analog_interface", "amsprimtool"
            elif re.search(r'\bxmelab(?:\([^)]*\))?:|Elaborating the design hierarchy', line, re.I):
                stage, tool = "elaborate", "xmelab"
            elif re.search(r'^\s*(?:xmvlog|xmvhdl|amsspice)(?:\([^)]*\))?:|^\s*Compiling\b', line, re.I):
                stage = "compile"
                match = re.search(r'\b(xmvlog|xmvhdl|amsspice)\b', line, re.I)
                tool = match[1].lower() if match else "unknown"
            elif re.search(r'^\s*Transient Analysis\b|^\s*tran:\s*time\s*=', line, re.I):
                stage, tool = "transient", "spectre"
            elif re.search(r'^\s*xcelium>\s*run\b|^\s*xmsim:\s*running', line, re.I):
                stage, tool = "solve", "xmsim"
            if stage:
                result.update(stage=stage, tool=tool, source=source)
            sample = re.search(r'(?:^\s*tran:\s*time\s*=|\bRan until|\bSimulation (?:stopped|complete) via transient analysis stoptime at time|\bSimulation complete via \$finish\([012]\) at time)\s*([\d.eE+-]+)\s*(fs|ps|ns|us|ms|s)\b', line, re.I)
            if sample:
                result["simulation_time"] = {"value": float(sample[1]), "unit": sample[2].lower(), "source": source}
    return result


def classify_logs(logs: dict[str, str], *, execution=None):
    evidence, errors, license_negotiation, phases, pipe_candidates = [], [], [], [], []
    warnings = []
    domains = {k: {"status": "insufficient_evidence", "evidence": []} for k in ("analog", "digital")}
    current_texts = []
    analog_mode, analog_mode_source = "unconfirmed", None
    for name, text in logs.items():
        lines = list(enumerate(text.splitlines(), 1))
        line_count = len(lines)
        starts = [i for i, line in lines if re.search(r"^\s*TOOL:\s*xrun.*Started on", line, re.I)]
        if starts:
            lines = [(i, line) for i, line in lines if i >= starts[-1]]
        current_texts.append("\n".join(line for _, line in lines))
        phase, phase_source = "unknown", None
        analog_start, analog_end, digital_start, digital_end, driver_end = None, None, None, None, None
        pending_license = []
        fallback = None
        ndb = None
        local_pipes = []
        first_run = next((i for i, line in lines if re.search(r'^\s*xcelium>\s*run\b', line, re.I)), None)
        for index, line in lines:
            source = {"path": name, "line": index, "text": line}
            enabled = re.fullmatch(r"\s*(Fast APS|APS) Enabled\.\s*", line)
            if enabled:
                analog_mode, analog_mode_source = enabled[1], source
            location = re.search(r'\(([^()]+),(\d+)\|(\d+)\)', line)
            if location:
                source["native_location"] = {"path": location[1], "line": int(location[2]), "column": int(location[3])}
            pipe = re.fullmatch(r'(.+):(\d+):(\d+):\s*fatal error: when writing output to\s*: Broken pipe\s*', line)
            if pipe:
                source["native_location"] = {"path": pipe[1], "line": int(pipe[2]), "column": int(pipe[3])}
            if re.search(r'^\s*Time for NDB Parsing:.*elapsed', line):
                ndb = source
            complete_line = index < line_count or text.endswith(("\n", "\r"))
            domain = ("analog" if re.search(r"spectre|amsspice|\*E,SPCERR", line, re.I) else
                      "digital" if re.search(r"\b(?:xmvlog|xmvhdl|xmelab|xmsim|xrun)\b", line, re.I) else "unknown")
            next_phase = None
            if re.search(r"\bxmvlog\b|\bxmvhdl\b|\bamsspice\b|\*E,SPCERR\b", line, re.I):
                next_phase = "compile"
            elif re.search(r"\bxmelab\b", line, re.I):
                next_phase = "elaborate"
            elif re.search(r"\bxmsim\b|^\s*xcelium>\s*run\b|Transient Analysis|Starting analog simulation engine", line, re.I):
                next_phase = "solve"
            if next_phase:
                phase, phase_source = next_phase, source
            if re.search(r"Starting analog simulation engine|^\s*Transient Analysis\b|^\s*Spectre.*Circuit Simulator", line, re.I):
                analog_start, analog_end = source, None
            if re.search(r"^\s*xcelium>\s*run\b|^\s*xmsim:\s*(?:running|simulation started)\b", line, re.I):
                digital_start, digital_end, driver_end, analog_end = source, None, None, None
            if complete_line and re.search(r"^\s*(?:Simulation complete via transient analysis stoptime at time\b|Simulation complete via \$finish\([012]\) at time\s+\d+(?:\.\d+)?\s+(?:FS|PS|NS|US|MS|S)\b|Ran until\s+\d+(?:\.\d+)?\s+(?:FS|PS|NS|US|MS|S)\b)", line, re.I) and digital_start:
                digital_end = source
            if complete_line and re.search(r"^\s*spectre completes with\s+0 errors\b", line, re.I) and analog_start:
                analog_end = source
            if complete_line and re.search(r"^\s*TOOL:\s+xrun.*Exiting", line, re.I) and digital_end:
                driver_end = source
            if re.search(r"\bSimulating in (?:non-)?AMS-SIE mode", line):
                fallback = source
            negotiation = re.search(r"^\s*Spectre_AMSD_Lk\s+\S+\s+- license checkout failed", line)
            if negotiation:
                pending_license.append({**source, "domain": domain, "phase": phase, "phase_source": phase_source})
            elif re.search(r"\*[EF],|ERROR\s*\(|FATAL\b|license.*(?:failed|denied|unavailable)|error.*license|spectre completes with\s+[1-9]\d* errors?\b|^\s*amsspice.*\*Error", line, re.I):
                item = {**source, "domain": domain, "phase": phase, "phase_source": phase_source}
                if pipe and name == "control/launch.log" and ndb and first_run and ndb["line"] < index < first_run:
                    item.update(phase="preprocess", phase_source=ndb)
                    local_pipes.append(item)
                errors.append(item)
        for item in pending_license:
            if analog_start and analog_end and item["line"] < analog_start["line"] < analog_end["line"]:
                license_negotiation.append({**item, "fallback": fallback, "completion": analog_end})
            else:
                errors.append({**item, "reason": "license checkout failure without subsequent confirmed analog completion"})
        if analog_end and digital_end:
            pipe_candidates.extend(item for item in local_pipes if item["line"] < digital_end["line"] and item["line"] < analog_end["line"])
        phases.append({"path": name, "phase": phase, "source": phase_source})
        for domain, proof in (("analog", [analog_start, analog_end]), ("digital", [digital_start, digital_end, driver_end])):
            if all(proof):
                domains[domain]["status"] = "success"
                domains[domain]["evidence"].extend(proof)
                evidence.extend(proof)
    combined = "\n".join(current_texts)
    other_errors = [e for e in errors if e not in pipe_candidates]
    if (execution and execution.get("state") == "completed" and type(execution.get("exit_code")) is int and execution["exit_code"] == 0
            and not other_errors and all(d["status"] == "success" for d in domains.values())):
        warnings = [{**e, "code": "native_preprocess_pipe_closed"} for e in pipe_candidates]
        errors = other_errors
    for domain in domains:
        domains[domain]["errors"] = [e for e in errors if e["domain"] == domain]
        if domains[domain]["errors"]:
            domains[domain]["status"] = "failed"
    status = "failed" if errors else "success" if all(d["status"] == "success" for d in domains.values()) else "insufficient_evidence"
    modes = re.findall(r"\bSimulating in ((?:non-)?AMS-SIE) mode", combined)
    mode = modes[-1] if modes and len(set(modes)) == 1 else "unconfirmed"
    solver_arguments = re.findall(r"(?m)^AMSD: Using spectre solver with arguments:\s*(.*)$", combined)
    arguments = solver_arguments[-1] if solver_arguments else None
    requested_threads = re.search(r"(?:^|\s)\+mt=(\d+)(?:\s|\.?$)", arguments or "")
    active_threads = re.findall(r"(?im)^\s*Multithreading (?:is )?enabled[^\n]*?\b(\d+) threads\b", combined)
    disabled = bool(re.search(r"(?im)^\s*Multithreading is disabled\b", combined))
    return {"status": status, "rules_version": LOG_RULES_VERSION, "domains": domains,
            "phase": errors[-1]["phase"] if errors else next((p["phase"] for p in reversed(phases) if p["source"]), "unknown"),
            "phase_sources": phases, "evidence": evidence, "errors": errors, "warnings": warnings, "license_negotiation": license_negotiation,
            "confirmed_settings": {"ams_mode": mode,
                                   "analog_mode": analog_mode, "analog_mode_source": analog_mode_source,
                                   "solver_arguments": arguments,
                                   "requested_threads": int(requested_threads[1]) if requested_threads else None,
                                   "multithreading": "disabled" if disabled else "enabled" if active_threads else "unconfirmed",
                                   "active_threads": int(active_threads[-1]) if active_threads and not disabled else None},
            "version_lines": [line for line in combined.splitlines()
                              if re.search(r"^\s*TOOL:\s*xrun|^\s*Version\s+\d+\.|^\s*(?:spectre|xrun)(?:\([^)]*\))?:\s*\d+\.", line, re.I)]}
