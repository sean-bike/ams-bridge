"""Read explicitly collected waveforms, retaining source and conversion evidence."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from .artifacts import local_manifest, safe_path_script
from .job_records import load_job
from .common import BridgeError, canonical, digest, file_state, is_link, local_file, operation_lock, read_json, relative, write_json
from .records import RESULT_RULES_VERSION, load_result, metadata, record_version
from .remote import work_available
from .transport import SSH, environment_command
from .wave_data import PARSER_VERSION, detect, probe
from .wave_query import decimal, selected_points


def limits_spec(raw=None):
    if not isinstance(raw, (dict, type(None))):
        raise BridgeError("waveform limits: expected an object")
    limits = dict(raw or {})
    if set(limits) - {"points", "bytes", "timeout_s"}:
        raise BridgeError("waveform limits: expected points, bytes, timeout_s")
    for key, default in (("points", 1000000), ("bytes", 268435456), ("timeout_s", 30)):
        limits.setdefault(key, default)
        value = limits[key]
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0 or
                (key != "timeout_s" and type(value) is not int)):
            raise BridgeError(f"waveform limits.{key}: expected a positive {'number' if key == 'timeout_s' else 'integer'}")
    return limits


def query_spec(raw):
    if not isinstance(raw, dict):
        raise BridgeError("waveform request: expected an object")
    q = dict(raw)
    if set(q) - {"operation", "artifact", "format", "analysis", "signals", "limits", "window",
                 "time_unit", "converter", "diagnostic", "include_end"}:
        raise BridgeError("waveform request: unknown fields")
    if q.get("operation") not in ("analyses", "signals", "read"):
        raise BridgeError("waveform operation: expected analyses/signals/read")
    q["artifact"] = relative(q.get("artifact"), "waveform.artifact")
    q.setdefault("format", "auto")
    if q["format"] not in ("auto", "psf_ascii", "binpsf", "vcd", "shm", "psfxl"):
        raise BridgeError("waveform.format: unsupported declaration", "unsupported_format")
    q["limits"] = limits_spec(q.get("limits"))
    q.setdefault("signals", [])
    if (not isinstance(q["signals"], list) or any(not isinstance(s, str) or not s or any(c in s for c in "\x00\n\r") for s in q["signals"]) or
            len(set(q["signals"])) != len(q["signals"]) or (q["operation"] in ("read",) and not q["signals"])):
        raise BridgeError("waveform.signals: expected unique exact names")
    if q["operation"] != "analyses" and (not isinstance(q.get("analysis"), str) or not q["analysis"]):
        raise BridgeError("waveform.analysis: required exact analysis name")
    q.setdefault("time_unit", "s")
    q.setdefault("diagnostic", False)
    if q["time_unit"] not in ("s", "tick") or type(q["diagnostic"]) is not bool:
        raise BridgeError("invalid time_unit/diagnostic")
    if "window" in q:
        if not isinstance(q["window"], list) or len(q["window"]) != 2 or not 0 <= decimal(q["window"][0]) <= decimal(q["window"][1]):
            raise BridgeError("waveform.window: expected ordered nonnegative endpoints")
    if "include_end" in q and (type(q["include_end"]) is not bool or "window" not in q or q["operation"] not in ("read",)):
        raise BridgeError("waveform.include_end: expected a boolean with an explicit read window")
    if "window" in q and q["operation"] in ("read",):
        q.setdefault("include_end", False)
    if "converter" in q and (not isinstance(q["converter"], str) or not q["converter"] or any(c in q["converter"] for c in "\x00\n\r")):
        raise BridgeError("waveform.converter: expected a native executable path")
    return q


def current_result(directory, handle):
    path = local_file(directory, "result.json")
    result = load_result(path)
    if result["run_id"] != handle["run_id"] or result["input_fingerprint"] != handle["input_fingerprint"]:
        raise BridgeError("waveform result identity mismatch", "identity_mismatch")
    return result, path


def analysis_matches(entry, name):
    dataset = entry.get("dataset")
    return (entry.get("name") in (None, name) or name in (entry.get("index_name"), dataset) or
            bool(dataset and dataset.removesuffix("-tran") == name))


def _regular(path):
    info = path.stat()
    if not path.is_file() or info.st_nlink != 1:
        raise BridgeError("waveform source/cache is linked or not a regular file", "unsafe_path")
    return info


def _output_directory(directory, name):
    path = local_file(directory, name)
    if path.exists() and not path.is_dir():
        raise BridgeError(f"waveform output is not a directory: {path}", "unsafe_path")
    return path


def verified_artifact(record, directory, limits):
    if record.get("status") not in ("verified", "diagnostic") or not isinstance(record.get("local_path"), str):
        raise BridgeError("waveform artifact has not been collected completely", "missing_artifact")
    path = Path(record["local_path"])
    if any(is_link(p) for p in (path, *path.parents)):
        raise BridgeError("waveform source contains a link", "unsafe_path")
    path = path.resolve()
    if not path.is_absolute() or not path.is_relative_to(directory):
        raise BridgeError("artifact path is outside job directory", "unsafe_path")
    path = local_file(directory, path.relative_to(directory).as_posix())
    if record.get("type", "file") == "directory":
        expected = record.get("members", [])
        state = {m["path"]: file_state(local_file(path, m["path"])) for m in expected}
        if state != record.get("local_state"):
            actual = local_manifest(path, record["limits"])
            if actual != expected:
                raise BridgeError("waveform directory no longer matches archive manifest", "identity_mismatch")
        else:
            actual = expected
        members = [m for m in actual if m["type"] == "file"]
    else:
        info = _regular(path)
        sha = record["sha256"] if record.get("local_state") == {".": file_state(path)} else digest(path)
        members = [{"path": ".", "type": "file", "size": info.st_size, "sha256": sha}]
        if members[0]["size"] != record.get("size") or members[0]["sha256"] != record.get("sha256"):
            raise BridgeError("waveform file no longer matches archived hash", "identity_mismatch")
    if sum(m["size"] for m in members) > limits["bytes"]:
        raise BridgeError("waveform source exceeds byte budget", "budget_exceeded")
    return path, members


def analysis_files(path, members, *, deadline=None, inspect=None):
    if path.is_file():
        detected = probe(path)
        if detected["format"] == "psf_index":
            raise BridgeError(f"{path}: declare a directory artifact containing this index and its companions", "missing_companion")
        return [{"name": None, "file": ".", "format": detected["format"], "status": "identified",
                 "evidence": {"declared_format": None, "probe": detected, "members": members}}]
    index = path / "logFile"
    if not index.is_file():
        raise BridgeError(f"{path}: waveform directory lacks results index logFile", "missing_companion")
    if detect(index) != "psf_index":
        raise BridgeError("logFile is not a supported results index", "parse_error")
    try:
        text = index.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise BridgeError(f"{index}: results index is not supported UTF-8 text", "parse_error") from exc
    sections = re.fullmatch(r"(?ms)(.*?)^VALUE[ \t]*\n(.*?)^END\s*", text)
    schema = (r'"analysisInst"\s+STRUCT\s*\(\s*"analysisType"\s+STRING\s*\*\s*'
              r'"dataFile"\s+STRING\s*\*\s*"format"\s+STRING\s*\*\s*"parent"\s+STRING\s*\*\s*'
              r'"sweepVariable"\s+ARRAY\s*\(\s*\*\s*\)\s+STRING\s*\*\s*"description"\s+STRING\s*\*\s*\)\s*')
    if not sections or not re.search(schema + r"\Z", sections[1]):
        raise BridgeError(f"{index}: malformed or unsupported analysisInst schema/VALUE/END", "parse_error")
    string = r'"(?:[^"\\\r\n]|\\.)*"'
    property_value = r"(?:" + string + r"|[+-]?\d+(?:\.\d*)?(?:[eE][+-]?\d+)?)"
    row = re.compile(r"\s*(" + string + r')\s+"analysisInst"\s*\(\s*(' + string + r")\s*(" + string +
                     r")\s*(" + string + r")\s*(" + string + r")\s*\(\s*((?:" + string + r"\s*)*)\)\s*" + string +
                     r"\s*\)\s*(?:PROP\s*\(\s*(?:" + string + r"\s+" + property_value + r"\s*)*\)\s*)?")
    data, offset, labels = sections[2], 0, set()
    line = text.count("\n", 0, sections.start(2)) + 1
    by_name = {m["path"]: m for m in members}
    index_hash = digest(index)
    version = re.search(r'(?m)^"version"\s+(' + string + r")", sections[1])
    try:
        index_version = json.loads(version[1]) if version else None
    except ValueError as exc:
        raise BridgeError(f"{index}: invalid quoted version", "parse_error") from exc
    entries = []
    # Only the observed six-field drlLog layout is supported; consume every row.
    while offset < len(data):
        if deadline is not None and time.monotonic() >= deadline:
            raise BridgeError(f"{index}: analysis discovery exceeded time budget", "budget_exceeded")
        match = row.match(data, offset)
        if not match:
            if not data[offset:].strip():
                break
            raise BridgeError(f"{index}:{line}: malformed or unsupported analysisInst row", "parse_error")
        source_line = line
        line += data.count("\n", offset, match.end())
        try:
            label, kind, name, declared, parent = [json.loads(v) for v in match.groups()[:5]]
            variables = [json.loads(v) for v in re.findall(string, match[6])]
        except ValueError as exc:
            raise BridgeError(f"{index}:{line}: invalid quoted index value", "parse_error") from exc
        offset = match.end()
        if label in labels:
            raise BridgeError(f"{index}:{line}: duplicate analysis label {label}", "parse_error")
        labels.add(label)
        suffix = "-" + kind + ("_psf" if label.endswith("_psf") else "")
        evidence = {"declared_format": declared, "index": {"path": "logFile", "sha256": index_hash, "line": source_line,
                     "version": index_version}, "members": []}
        entry = {"name": label.removesuffix(suffix), "index_name": label, "analysis_type": kind,
                 "parent": parent, "sweep_variables": variables,
                 "file": name, "format": None, "status": "identified", "evidence": evidence}
        logical = re.search(r'"dataset_logical_name"\s+("(?:[^"\\]|\\.)*")', match[0])
        if logical:
            entry["dataset"] = json.loads(logical[1])
        try:
            name = relative(name, "index dataFile"); entry["file"] = name
            if inspect is not None:
                entry.update(inspect(entry))
                entries.append(entry)
                continue
            if name not in by_name:
                raise BridgeError(f"index companion missing: {name}", "missing_companion")
            evidence["members"].append(by_name[name]); evidence["probe"] = probe(local_file(path, name))
            fmt = evidence["probe"]["format"]
            companion_data = name + ".dat"
            if fmt == "binpsf" and companion_data in by_name:
                with local_file(path, companion_data).open("rb") as stream:
                    if stream.read(9) == b"\x00!ngpsf-0":
                        fmt = "psfxl"
            if declared == "SST2":
                companion = str(Path(name).with_suffix(".dsn")).replace("\\", "/")
                if companion not in by_name:
                    raise BridgeError(f"SHM/SST2 companion missing: {companion}", "missing_companion")
                evidence["members"].append(by_name[companion])
                try:
                    evidence["companion_probe"] = probe(local_file(path, companion))
                except BridgeError as exc:
                    raise BridgeError(f"SHM/SST2 companion {companion}: {exc}", "format_mismatch") from exc
                if fmt != "shm_member" or evidence["companion_probe"]["format"] != "shm_member":
                    raise BridgeError("SHM/SST2 trn/dsn signature mismatch", "format_mismatch")
                fmt = "shm"
            elif declared not in ("PSF", "SST2"):
                raise BridgeError(f"unsupported results-index format: {declared}", "unsupported_format")
            elif fmt not in ("psf_ascii", "binpsf", "psfxl"):
                raise BridgeError(f"index format conflict: {declared}/{fmt}", "format_mismatch")
            entry["format"] = fmt
        except BridgeError as exc:
            entry.update(status="error", error_code=exc.code, error=f"{index}:{source_line} [{label}]: {exc}")
        entries.append(entry)
    if not entries:
        raise BridgeError("results index has no recognized analyses", "empty_waveform")
    return entries


def parse_local(path, fmt, limits, selection=None):
    return _parse_request({"path": str(path), "format": fmt, "limits": limits, "selection": selection})


def parse_local_many(paths, limits, selection=None):
    return _parse_request({"paths": [str(path) for path in paths], "limits": limits, "selection": selection})


def _parse_request(request):
    limits = request["limits"]
    with tempfile.TemporaryDirectory(prefix="ams-wave-") as temporary:
        root = Path(temporary)
        write_json(root / "request.json", request)
        try:
            with (root / "stderr").open("wb") as stderr:
                completed = subprocess.run([sys.executable, "-m", "ams_bridge.wave_data", str(root / "request.json"), str(root / "result.json")],
                                           stdout=subprocess.DEVNULL, stderr=stderr, timeout=limits["timeout_s"],
                                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
        except subprocess.TimeoutExpired as exc:
            raise BridgeError("waveform parser exceeded time budget", "budget_exceeded") from exc
        result = root / "result.json"
        if completed.returncode or not result.is_file():
            raise BridgeError("waveform parser process failed", "parse_error")
        if result.stat().st_size > limits["bytes"]:
            raise BridgeError("waveform parser output exceeds byte budget", "budget_exceeded")
        response = read_json(result)
        if "error" in response:
            raise BridgeError(f"{request.get('path', 'selected VCSV files')}: {response['error']}", response["code"])
        return response["data"]


def native_window(window):
    # simvisdbutil 18.03 rejects scientific notation in -range.
    return ":".join(format(decimal(x) * decimal("1e9"), "f") for x in window) + "ns"


def _conversion_command(tool, fmt, source, output, q):
    return ([tool, "-f", "%.17g", "-i", source, "-o", output] if fmt == "binpsf" else
            [tool, "-nolog", "-vcd", "-range", native_window(q["window"]),
             *[x for signal in q["signals"] for x in ("-signal", signal)], "-output", output, source])


def _convert(handle, execution, directory, artifact, entry, members, q, transport, *, offline=False):
    started = time.monotonic()
    def remaining():
        value = q["limits"]["timeout_s"] - (time.monotonic() - started)
        if value <= 0:
            raise BridgeError("native conversion exceeded time budget", "budget_exceeded")
        return value
    _output_directory(directory, "derived")
    fmt = entry["format"]
    tool = q.get("converter", "psf" if fmt == "binpsf" else "simvisdbutil")
    if fmt == "shm" and (not q["signals"] or "window" not in q or q["time_unit"] != "s"):
        raise BridgeError("SHM conversion requires exact signals and an explicit seconds window", "unsupported_format")
    identity = {k: v for k, v in entry.items() if k in ("name", "file", "format", "index_name", "analysis_type")}
    source_name = artifact if entry["file"] == "." else artifact + "/" + entry["file"]
    source_remote = handle["remote_directory"] + "/work/" + source_name
    extension = "ascii" if fmt == "binpsf" else "vcd"
    selection = {"format": fmt, "artifact": artifact, "entry": identity, "members": members, "converter": tool,
                 "signals": q["signals"] if fmt == "shm" else [], "window": q.get("window") if fmt == "shm" else None,
                 "time_unit": q["time_unit"] if fmt == "shm" else "s"}
    key = hashlib.sha256(canonical(selection)).hexdigest()
    cache_path = local_file(directory, "waveform-cache.json")
    if cache_path.exists():
        _regular(cache_path)
    cache = read_json(cache_path) if cache_path.exists() else {**metadata("waveform_cache", handle["run_id"], handle["input_fingerprint"]), "entries": {}}
    record_version(cache, "waveform_cache")
    if cache["run_id"] != handle["run_id"] or cache["input_fingerprint"] != handle["input_fingerprint"] or not isinstance(cache.get("entries"), dict):
        raise BridgeError("waveform cache identity/entries mismatch", "identity_mismatch")
    if key in cache["entries"]:
        ref = cache["entries"][key]
        if (not isinstance(ref, dict) or not isinstance(ref.get("path"), str) or
                not re.fullmatch(r"derived/[0-9a-f]{32}/conversion\.json", ref["path"]) or
                not re.fullmatch(r"[0-9a-f]{64}", str(ref.get("sha256", "")))):
            raise BridgeError("malformed conversion cache reference", "identity_mismatch")
        record_path = local_file(directory, ref["path"])
        _regular(record_path)
        if digest(record_path) != ref["sha256"]:
            raise BridgeError("conversion record hash mismatch", "identity_mismatch")
        record = read_json(record_path)
        record_version(record, "conversion")
        remote = record.get("remote_directory")
        if remote not in (handle["remote_directory"] + prefix + record_path.parent.name for prefix in ("/derived/", "/work/.derived/")):
            raise BridgeError("conversion directory is outside its job", "identity_mismatch")
        expected_command = _conversion_command(tool, fmt, source_remote, remote + "/wave." + extension, q)
        if (record["run_id"] != handle["run_id"] or record["input_fingerprint"] != handle["input_fingerprint"] or
                not isinstance(record.get("output"), dict) or not isinstance(record["output"].get("path"), str) or
                not re.fullmatch(r"[0-9a-f]{64}", str(record["output"].get("sha256", ""))) or
                type(record["output"].get("size")) is not int or record["output"]["size"] < 0):
            raise BridgeError("malformed conversion identity/output", "identity_mismatch")
        if (record.get("remote_directory") != remote or record.get("command") != expected_command or
                type(record.get("returncode")) is not int or record["returncode"] != 0 or
                isinstance(record.get("elapsed_s"), bool) or not isinstance(record.get("elapsed_s"), (int, float)) or
                not math.isfinite(record["elapsed_s"]) or record["elapsed_s"] < 0 or
                not isinstance(record.get("tool_evidence"), list) or len(record["tool_evidence"]) != 3 or
                any(not isinstance(v, str) for v in record["tool_evidence"]) or not record["tool_evidence"][0].strip()):
            raise BridgeError("conversion command/tool/exit provenance mismatch", "identity_mismatch")
        if ("remote_output" in record and (not isinstance(record["remote_output"], dict) or
                type(record["remote_output"].get("size")) is not int or
                record["remote_output"] != {"size": record["output"]["size"], "sha256": record["output"]["sha256"]})) or (
                "parser_version" in record and (not isinstance(record["parser_version"], str) or not record["parser_version"])):
            raise BridgeError("conversion parser/native output receipt mismatch", "identity_mismatch")
        if "output_integrity" in record and (not isinstance(record["output_integrity"], dict) or record["output_integrity"].get("status") != "verified"):
            raise BridgeError("conversion output validation did not pass", "identity_mismatch")
        if fmt == "shm" and (not isinstance(record.get("time_semantics"), dict) or
                            record["time_semantics"].get("within_tick_source_order_preserved") is not False):
            raise BridgeError("conversion time semantics mismatch", "identity_mismatch")
        output = local_file(directory, record["output"]["path"])
        info = _regular(output)
        expected = record_path.parent / ("wave.ascii" if fmt == "binpsf" else "wave.vcd")
        if (output != expected or record.get("selection") != selection or info.st_size != record["output"]["size"] or
                (file_state(output) != record["output"].get("local_state") and digest(output) != record["output"]["sha256"])):
            raise BridgeError("conversion source/output mismatch", "identity_mismatch")
        if info.st_size > q["limits"]["bytes"]:
            raise BridgeError("cached conversion exceeds current budget", "budget_exceeded")
        if record["output"].get("local_state") != file_state(output):
            record["output"]["local_state"] = file_state(output)
            write_json(record_path, record)
            cache["entries"][key]["sha256"] = digest(record_path)
            write_json(cache_path, cache)
        return output, record
    if offline:
        raise BridgeError("offline reading requires an existing verified local conversion", "offline_conversion_required")
    t = transport or SSH(execution)
    identifier = uuid.uuid4().hex
    local_dir = local_file(directory, "derived/" + identifier)
    local_dir.mkdir(parents=True)
    remote_dir = handle["remote_directory"] + "/work/.derived/" + identifier
    output_remote = remote_dir + "/wave." + extension
    command = _conversion_command(tool, fmt, source_remote, output_remote, q)
    proof = safe_path_script(handle, source_name) if not members else ""
    for member in members:
        name = artifact if member["path"] == "." else artifact + "/" + member["path"]
        proof += safe_path_script(handle, name)
        proof += f'test "$(stat -c %h -- "$p")" = 1 && test "$(sha256sum < "$p" | cut -d \' \' -f 1)" = {shlex.quote(member["sha256"])} || exit 88\n'
    budget = remaining()
    lease_execution = {**execution, "control_timeout_s": min(execution.get("control_timeout_s", 20), budget),
                       "connect_timeout_s": min(execution.get("connect_timeout_s", 8), budget), "transfer_timeout_s": budget}
    with SSH(lease_execution).lease(handle["remote_directory"] + "/control"):
        guard = (f'test "$(cat {shlex.quote(handle["remote_directory"] + "/control/input_fingerprint")})" = {shlex.quote(handle["input_fingerprint"])} || exit 88\n' + work_available(handle["remote_directory"] + "/control") + proof)
        make = f"base={shlex.quote(handle['remote_directory'] + '/work/.derived')}\ntest ! -L \"$base\" && test \"$(realpath -m -- \"$base\")\" = \"$base\"\nmkdir -p -- \"$base\"\nmkdir -- {shlex.quote(remote_dir)}\ncd -- {shlex.quote(remote_dir)}\n"
        runtime = (f"ulimit -f {math.ceil(q['limits']['bytes'] / 1024)}\n"
                   f"command -v {shlex.quote(tool)} >tool.path || exit 127\n"
                   + shlex.join([tool, "-v" if fmt == "binpsf" else "-version", *([] if fmt == "binpsf" else ["-nolog"])]) + " >tool.version 2>&1\n"
                   + shlex.join(command) + " >conversion.log 2>&1\nrc=$?\nprintf '%s\\n' \"$rc\" >conversion.exit\nexit \"$rc\"\n")
        try:
            response = t.run("set -eu\n" + guard + make + environment_command(execution, ["/bin/bash", "-c", runtime]),
                             timeout=remaining(), transfer=True, check=False)
        except BridgeError as exc:
            if time.monotonic() - started >= q["limits"]["timeout_s"]:
                raise BridgeError("native conversion exceeded time budget", "budget_exceeded") from exc
            raise
        record = {**metadata("conversion", handle["run_id"], handle["input_fingerprint"]), "selection": selection,
                  "command": command, "remote_directory": remote_dir, "returncode": response.returncode,
                  "elapsed_s": time.monotonic() - started, "parser_version": PARSER_VERSION,
                  "detection": entry.get("evidence")}
        if fmt == "shm":
            record["time_semantics"] = {"within_tick_source_order_preserved": False,
                                        "axis": "converted VCD integer ticks and declared timescale",
                                        "note": "native conversion may quantize timestamps and reorder simultaneous signals; do not assume original event sequence"}
        details = t.run(f"cd -- {shlex.quote(remote_dir)} || exit 1\nfor f in tool.path tool.version conversion.log; do printf '\\000'; head -c 65536 -- \"$f\" 2>/dev/null || :; done\n",
                        timeout=min(execution.get("control_timeout_s", 20), remaining()), check=False)
        record["tool_evidence"] = details.stdout.decode("utf-8", "replace").split("\x00")[1:]
        write_json(local_dir / "conversion.json", record)
        if response.returncode:
            evidence = "\n".join(record["tool_evidence"])
            code = ("identity_mismatch" if response.returncode == 88 else
                    "remote_work_cleaned" if response.returncode == 89 else
                    "remote_work_cleanup_partial" if response.returncode == 90 else
                    "dependency_unavailable" if response.returncode == 127 else
                    "budget_exceeded" if response.returncode in (124, 153) else
                    "empty_waveform" if "No traces available" in evidence else "conversion_failed")
            raise BridgeError(f"native conversion failed ({response.returncode}); evidence: {local_dir / 'conversion.json'}",
                              code)
        output = local_dir / ("wave." + extension)
        output_guard = "p=" + shlex.quote(output_remote) + r'''
cursor="$p"
while :; do
 test ! -L "$cursor" || exit 94
 test "$cursor" != / || break
 cursor=${cursor%/*}; test -n "$cursor" || cursor=/
done
test -f "$p" && test "$(realpath -e -- "$p")" = "$p" && test "$(stat -c %h -- "$p")" = 1 || exit 94
''' + f'test "$(stat -c %s -- "$p")" -le {q["limits"]["bytes"]} || exit 153\n'
        download = (guard + output_guard + 'size=$(stat -c %s -- "$p")\nsha=$(sha256sum < "$p" | cut -d \' \' -f 1)\n' +
                    f'head -c {q["limits"]["bytes"] + 1} -- "$p" || exit 91\n' + output_guard +
                    'test "$(stat -c %s -- "$p")" = "$size" && test "$(sha256sum < "$p" | cut -d \' \' -f 1)" = "$sha" || exit 88\n' +
                    'printf \'AMS_BRIDGE_OUTPUT\\t%s\\t%s\\n\' "$size" "$sha" >&2\n')
        try:
            timeout = remaining()
            with output.open("wb") as stream:
                fetched = t.run(download, output_file=stream, check=False, timeout=timeout, transfer=True)
            if fetched.returncode:
                code = {88: "identity_mismatch", 94: "unsafe_path", 153: "budget_exceeded"}.get(fetched.returncode, "conversion_failed")
                raise BridgeError(f"converted output rejected ({fetched.returncode}): {output_remote}", code)
            receipt = re.findall(rb"(?m)^AMS_BRIDGE_OUTPUT\t([0-9]+)\t([0-9a-f]{64})\r?$", fetched.stderr)
            if len(receipt) != 1 or output.stat().st_size != int(receipt[0][0]) or digest(output) != receipt[0][1].decode("ascii"):
                raise BridgeError("converted download does not match native output receipt", "identity_mismatch")
        except BridgeError as exc:
            record["output_integrity"] = {"status": "failed", "error_code": exc.code, "error": str(exc)}
            write_json(local_dir / "conversion.json", record)
            raise
        if output.stat().st_size > q["limits"]["bytes"]:
            raise BridgeError("converted output exceeds byte budget", "budget_exceeded")
        record["output"] = {"path": output.relative_to(directory).as_posix(), "sha256": digest(output), "size": output.stat().st_size}
        record["output"]["local_state"] = file_state(output)
        record["remote_output"] = {"sha256": receipt[0][1].decode("ascii"), "size": int(receipt[0][0])}
        record["output_integrity"] = {"status": "verified"}
        write_json(local_dir / "conversion.json", record)
    cache["entries"][key] = {"path": (local_dir / "conversion.json").relative_to(directory).as_posix(), "sha256": digest(local_dir / "conversion.json")}
    write_json(cache_path, cache)
    return output, record


def waveforms(handle, request, *, transport=None):
    q = query_spec(request)
    handle, original, directory = load_job(handle)
    with operation_lock(directory):
        started = time.monotonic()
        try:
            return _waveforms(handle, original, directory, q, transport)
        except BridgeError as exc:
            if exc.code in ("communication", "transfer_timeout", "control_timeout") and time.monotonic() - started >= q["limits"]["timeout_s"]:
                raise BridgeError("waveform operation exceeded time budget: " + str(exc), "budget_exceeded") from exc
            raise


def _waveforms(handle, original, directory, q, transport):
    _output_directory(directory, "waveform-reads")
    started = time.monotonic()
    result, result_path = current_result(directory, handle)
    records = result["collection"].get("artifacts", [])
    record = next((a for a in records if a["path"] == q["artifact"]), None)
    native = q["format"] == "psfxl" or record is None or record.get("status") not in ("verified", "diagnostic")
    final = result["collection"].get("final") is True and (native or record.get("status") == "verified")
    if not final and not q["diagnostic"]:
        raise BridgeError("only diagnostic queries may open a non-final snapshot", "diagnostic_only")
    if native:
        if not final:
            raise BridgeError("native database export requires an ended simulation", "diagnostic_only")
        if q["format"] == "psfxl":
            from .native_psfxl import native_source
            entries, data, conversion = native_source(handle, original, directory, q, transport)
        else:
            from .remote_waveforms import remote_source
            entries, data, conversion = remote_source(handle, original, directory, q, transport)
        path, members = q["artifact"], []
    else:
        path, members = verified_artifact(record, directory, q["limits"])
        entries = analysis_files(path, members, deadline=started + q["limits"]["timeout_s"])
        if q["format"] == "auto" and any(e.get("format") == "psfxl" and (q["operation"] == "analyses" or analysis_matches(e, q["analysis"])) for e in entries):
            from .native_psfxl import native_source
            entries, data, conversion = native_source(handle, original, directory, q, transport)
            native = True
    source = {"result": {"path": result_path.relative_to(directory).as_posix(), "sha256": digest(result_path)},
              "artifact": q["artifact"], "members": members}
    output = {**metadata("waveform_read", handle["run_id"], handle["input_fingerprint"], source),
              "request": q, "parser_version": PARSER_VERSION, "rules_version": RESULT_RULES_VERSION, "final": final, "status": "complete"}
    if native:
        output["conversion"] = conversion
    if q["operation"] == "analyses":
        if q["format"] != "auto" and any(e["status"] == "identified" and e["format"] != q["format"] for e in entries):
            raise BridgeError("declared waveform format conflicts with analysis content", "format_mismatch")
        output["analyses"] = entries
        if any(e["status"] == "error" for e in entries):
            output["status"] = "partial"
    else:
        candidates = [e for e in entries if analysis_matches(e, q["analysis"])]
        declared = [e for e in candidates if e["format"] == q["format"]]
        if declared:
            candidates = declared
        if len(candidates) != 1:
            raise BridgeError("analysis name is missing or ambiguous", "missing_analysis")
        entry = candidates[0]
        if entry["status"] == "error":
            raise BridgeError(entry["error"], entry["error_code"])
        if entry.get("analysis_type", "tran") != "tran":
            raise BridgeError(f"{path}: analysis type {entry['analysis_type']} is not supported for numeric reads", "unsupported_format")
        fmt = entry["format"]
        if q["format"] != "auto" and q["format"] != fmt:
            raise BridgeError(f"declared {q['format']}, detected {fmt}", "format_mismatch")
        output["detected_format"] = fmt
        output["analysis"] = entry
        input_path = path if native or entry["file"] == "." else local_file(path, entry["file"])
        if not native and fmt in ("binpsf", "shm"):
            if not final:
                raise BridgeError("native database snapshots cannot be converted during execution", "diagnostic_only")
            remaining = q["limits"]["timeout_s"] - (time.monotonic() - started)
            if remaining <= 0:
                raise BridgeError("waveform operation exceeded time budget", "budget_exceeded")
            conversion_query = {**q, "limits": {**q["limits"], "timeout_s": remaining}}
            try:
                input_path, conversion = _convert(handle, original["execution"], directory, q["artifact"], entry, members, conversion_query, transport)
            except BridgeError as exc:
                if exc.code == "communication" and time.monotonic() - started >= q["limits"]["timeout_s"]:
                    raise BridgeError("waveform conversion exceeded time budget", "budget_exceeded") from exc
                raise
            output["conversion"] = conversion
            fmt = "psf_ascii" if fmt == "binpsf" else "vcd"
        elif not native and fmt not in ("psf_ascii", "vcd"):
            raise BridgeError(f"waveform format {fmt} is unsupported", "unsupported_format")
        remaining = q["limits"]["timeout_s"] - (time.monotonic() - started)
        if remaining <= 0:
            raise BridgeError("waveform time budget exceeded", "budget_exceeded")
        if not native:
            data = parse_local(input_path, fmt, {**q["limits"], "timeout_s": remaining}, q)
        if data["analysis"] != (entry["name"] or q["analysis"]):
            raise BridgeError(f"{input_path}: analysis header conflicts with selected index/analysis", "missing_analysis")
        by_name = {s["name"]: s for s in data["signals"]}
        missing = set(q["signals"]) - set(by_name)
        if missing:
            output["status"] = "partial" if set(q["signals"]) & by_name.keys() else "failed"
            unsupported = data.get("unsupported_signals", {})
            output["errors"] = [{"code": "unsupported_format" if name in unsupported else "missing_signal", "stage": "data", "signal": name,
                                 "message": "unsupported native type: " + unsupported[name] if name in unsupported else "missing exact signal: " + name,
                                 "source": q["artifact"]} for name in sorted(missing)]
        selected = [by_name[n] for n in q["signals"] if n in by_name] if q["signals"] else list(by_name.values())
        output["reader"] = {k: v for k, v in data.items() if k != "signals"}
        output["signal_metadata"] = [{k: v for k, v in s.items() if k not in ("points", "aliases")} for s in selected]
        if q["operation"] == "signals":
            output["signals"] = [{k: v for k, v in s.items() if k != "points"} | {"point_count": s["source_point_count"]} for s in selected]
        elif q["operation"] == "read":
            output["signals"] = []
            for signal in selected:
                points = selected_points(signal, q.get("window"), q["time_unit"], include_end=q.get("include_end", False))
                value = {**signal, "points": points, "point_count": len(points),
                         "returned_range": [points[0]["time"], points[-1]["time"]] if points else None}
                if signal["kind"] == "digital":
                    value["event_count"] = sum("boundary_state_at" not in p for p in points)
                output["signals"].append(value)
    output["elapsed_s"] = time.monotonic() - started
    if output["elapsed_s"] > q["limits"]["timeout_s"] or len(canonical(output)) > q["limits"]["bytes"]:
        raise BridgeError("waveform operation exceeds time/output budget", "budget_exceeded")
    write_json(local_file(directory, "waveform-reads/" + uuid.uuid4().hex + ".json"), output)
    return output


def read_source(path, request):
    """Read an explicit local ASCII PSF/VCD source, without a job or SSH."""
    if not isinstance(request, dict):
        raise BridgeError("local waveform request must be an object")
    q = query_spec({"artifact": Path(path).name, **request})
    path = Path(path).absolute()
    if any(is_link(p) for p in (path, *path.parents)):
        raise BridgeError("local waveform source contains a link", "unsafe_path")
    path = path.resolve()
    _regular(path)
    fmt = detect(path)
    if q["format"] != "auto" and q["format"] != fmt:
        raise BridgeError("declared format conflicts with local data", "format_mismatch")
    data = parse_local(path, fmt, q["limits"], q)
    if q["operation"] == "analyses":
        return {"analysis": data["analysis"], "format": fmt}
    if data["analysis"] != q["analysis"]:
        raise BridgeError("analysis does not match local source", "missing_analysis")
    by_name = {s["name"]: s for s in data["signals"]}
    missing = set(q["signals"]) - by_name.keys()
    selected = [by_name[n] for n in q["signals"] if n in by_name] if q["signals"] else data["signals"]
    output = {"status": "complete", "source": str(path), "format": fmt, "analysis": data["analysis"],
              "reader": {k: v for k, v in data.items() if k != "signals"}, "signals": []}
    if missing:
        output["status"] = "partial" if selected else "failed"
        output["errors"] = [{"code": "missing_signal", "stage": "data", "signal": name,
                             "message": "missing exact signal: " + name, "source": str(path)} for name in sorted(missing)]
    for signal in selected:
        if q["operation"] == "signals":
            output["signals"].append({k: v for k, v in signal.items() if k != "points"})
        else:
            output["signals"].append({**signal, "points": selected_points(signal, q.get("window"), q["time_unit"], include_end=q.get("include_end", False))})
    return output


