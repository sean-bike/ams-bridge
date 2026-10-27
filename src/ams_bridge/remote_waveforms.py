"""Open job/work waveform sources without making an original archive mandatory."""
import base64
import hashlib
import shlex
import time
import uuid

from .artifacts import safe_path_script
from .common import BridgeError, canonical, digest, file_state, local_file, read_json, write_json
from .records import metadata, record_version
from .remote import work_available
from .transport import SSH
from .wave_data import probe_bytes


def remote_source(handle, original, directory, q, transport=None):
    from .waveforms import _convert, _regular, analysis_files, analysis_matches, parse_local
    from .native_psfxl import native_source

    started = time.monotonic()
    execution = original["execution"]
    cache_path = local_file(directory, "waveform-cache.json")
    if cache_path.exists():
        _regular(cache_path)
    cache = read_json(cache_path) if cache_path.exists() else {**metadata("waveform_cache", handle["run_id"], handle["input_fingerprint"]), "entries": {}}
    record_version(cache, "waveform_cache")
    if cache["run_id"] != handle["run_id"] or cache["input_fingerprint"] != handle["input_fingerprint"]:
        raise BridgeError("waveform cache identity mismatch", "identity_mismatch")
    selection = {"method": "native-source", "artifact": q["artifact"]}
    record = None
    for ref in cache["entries"].values():
        path = local_file(directory, ref["path"])
        _regular(path)
        candidate = read_json(path)
        if candidate.get("selection", {}).get("artifact") != q["artifact"]:
            continue
        if (candidate.get("selection", {}).get("method") == "srrutil-vcsv" and q["format"] in ("auto", "psfxl") and
                (q["operation"] == "analyses" or any(analysis_matches(e, q["analysis"]) for e in candidate["analyses"]))):
            return native_source(handle, original, directory, q, transport)
        if candidate.get("selection") == selection:
            if (digest(path) != ref["sha256"] or candidate.get("run_id") != handle["run_id"] or
                    candidate.get("input_fingerprint") != handle["input_fingerprint"]):
                raise BridgeError("waveform source metadata identity mismatch", "identity_mismatch")
            record = candidate
            break

    def remaining():
        value = q["limits"]["timeout_s"] - (time.monotonic() - started)
        if value <= 0:
            raise BridgeError("waveform source operation exceeded time budget", "budget_exceeded")
        return value

    t = transport or SSH(execution)
    control = handle["remote_directory"] + "/control"
    guard = (f'test "$(cat {shlex.quote(control + "/input_fingerprint")})" = {shlex.quote(handle["input_fingerprint"])} || exit 88\n' + work_available(control))

    def run(script, **kwargs):
        response = t.run(guard + script, check=False, timeout=remaining(), transfer=True, **kwargs)
        if response.returncode:
            code = {44: "missing_artifact", 45: "unsafe_path", 88: "identity_mismatch", 89: "remote_work_cleaned", 124: "budget_exceeded", 153: "budget_exceeded"}.get(response.returncode, "conversion_failed")
            raise BridgeError(f"waveform source unavailable ({response.returncode}): " + response.stderr.decode("utf-8", "replace")[:2000], code)
        return response

    if record is None:
        identifier = uuid.uuid4().hex
        local = local_file(directory, "derived/" + identifier)
        local.mkdir(parents=True)
        lease_execution = {**execution, "control_timeout_s": min(execution["control_timeout_s"], remaining()), "transfer_timeout_s": remaining()}
        with SSH(lease_execution).lease(control):
            peek = (safe_path_script(handle, q["artifact"], "any") + 'if test -f "$p"; then\n'
                    'test "$(stat -c %h -- "$p")" = 1 || exit 45\n'
                    'printf "file\\000"\nhead -c 4096 -- "$p" | base64 -w0\nprintf "\\000"\ntail -c 12 -- "$p" | base64 -w0\n'
                    'else\nprintf "directory\\000"\n' + safe_path_script(handle, q["artifact"] + "/logFile") +
                    'test "$(stat -c %h -- "$p")" = 1 || exit 45\n' +
                    f'test "$(stat -c %s -- "$p")" -le {min(1048576, q["limits"]["bytes"])} || exit 153\ncat -- "$p"\nfi\n')
            response = run(peek).stdout.split(b"\x00")
            if response[0] == b"file":
                detected = probe_bytes(base64.b64decode(response[1]), base64.b64decode(response[2]))
                entries = [{"name": None, "file": ".", "format": detected["format"], "status": "identified", "evidence": {"probe": detected}}]
            else:
                (local / "logFile").write_bytes(response[1])
                entries = analysis_files(local, [], inspect=lambda entry: {"format": entry["evidence"]["declared_format"]})
                script = ""
                for entry in entries:
                    script += (safe_path_script(handle, q["artifact"] + "/" + entry["file"]) +
                               'head -c 4096 -- "$p" | base64 -w0\nprintf "\\000"\ntail -c 12 -- "$p" | base64 -w0\nprintf "\\000"\n'
                               'if test -f "$p.dat"; then test ! -L "$p.dat" || exit 45\nhead -c 40 -- "$p.dat" | base64 -w0; fi\nprintf "\\000"\n')
                probes = run(script).stdout.split(b"\x00")
                for index, entry in enumerate(entries):
                    head, tail, companion = (base64.b64decode(v) for v in probes[index * 3:index * 3 + 3])
                    detected = probe_bytes(head, tail)
                    fmt = detected["format"]
                    if entry["format"] == "SST2" and fmt == "shm_member":
                        fmt = "shm"
                    elif fmt == "binpsf" and companion.startswith(b"\x00!ngpsf-"):
                        fmt = "psfxl"
                        entry.setdefault("dataset", entry["index_name"])
                        entry["name"] = entry["dataset"].removesuffix("-tran")
                    entry["format"] = fmt
                    entry["evidence"]["probe"] = detected
            record = {**metadata("conversion", handle["run_id"], handle["input_fingerprint"]),
                      "selection": selection, "analyses": entries}
            if len(entries) == 1 and entries[0]["format"] in ("psf_ascii", "vcd"):
                name = q["artifact"] if entries[0]["file"] == "." else q["artifact"] + "/" + entries[0]["file"]
                output = local / "wave.source"
                script = (safe_path_script(handle, name) + f'test "$(stat -c %s -- "$p")" -le {q["limits"]["bytes"]} || exit 153\n'
                          'test "$(stat -c %h -- "$p")" = 1 || exit 45\n'
                          'sha=$(sha256sum < "$p"); sha=${sha%% *}\ncat -- "$p"\nprintf "%s\\n" "$sha" >&2\n')
                with output.open("wb") as stream:
                    fetched = run(script, output_file=stream)
                if digest(output) != fetched.stderr.decode("ascii").strip():
                    raise BridgeError("waveform source transfer hash mismatch", "identity_mismatch")
                record["output"] = {"path": output.relative_to(directory).as_posix(), "sha256": digest(output), "local_state": file_state(output)}
        record_path = local / "conversion.json"
        write_json(record_path, record)
        key = hashlib.sha256(canonical(selection)).hexdigest()
        cache["entries"][key] = {"path": record_path.relative_to(directory).as_posix(), "sha256": digest(record_path)}
        write_json(cache_path, cache)
    entries = record["analyses"]
    psfxl = [entry for entry in entries if entry["format"] == "psfxl"]
    query = {**q, "limits": {**q["limits"], "timeout_s": remaining()}}
    if psfxl and q["format"] in ("auto", "psfxl") and (q["operation"] == "analyses" or any(analysis_matches(e, q["analysis"]) for e in psfxl)):
        return native_source(handle, original, directory, query, transport, entries=psfxl)
    if q["operation"] == "analyses":
        return entries, None, record
    candidates = [entry for entry in entries if analysis_matches(entry, q["analysis"])]
    declared = [entry for entry in candidates if entry["format"] == q["format"]]
    if declared:
        candidates = declared
    if len(candidates) != 1:
        raise BridgeError("analysis name is missing or ambiguous", "missing_analysis")
    entry = candidates[0]
    fmt = entry["format"]
    if q["format"] not in ("auto", fmt):
        raise BridgeError(f"declared {q['format']}, detected {fmt}", "format_mismatch")
    if entry.get("analysis_type", "tran") != "tran":
        raise BridgeError("only real transient numeric reads are supported", "unsupported_format")
    if fmt in ("binpsf", "shm"):
        path, conversion = _convert(handle, execution, directory, q["artifact"], entry, [], query, transport)
        fmt = "psf_ascii" if fmt == "binpsf" else "vcd"
    elif "output" in record:
        output = record["output"]
        path = local_file(directory, output["path"])
        _regular(path)
        if file_state(path) != output["local_state"] and digest(path) != output["sha256"]:
            raise BridgeError("local waveform source changed", "identity_mismatch")
        conversion = record
    else:
        raise BridgeError("source format requires a supported native export", "unsupported_format")
    data = parse_local(path, fmt, {**q["limits"], "timeout_s": remaining()}, q)
    return entries, data, conversion
