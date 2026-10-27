"""Read an ended job's PSFXL in place with Cadence srrutil.

Only index/signature bytes and selected native VCSV exports cross SSH. The native
reader handles its own database companions, including temporary symbolic links.
"""
import hashlib
import math
import re
import shlex
import tempfile
import time
import uuid
from pathlib import Path

from .artifacts import safe_path_script, unpack_directory
from .common import BridgeError, canonical, digest, file_state, local_file, read_json, relative, write_json
from .records import metadata, record_version
from .remote import work_available
from .transport import SSH, environment_command


def index_entries(text, signatures):
    if not text.startswith("HEADER\n") or not re.search(r"(?m)^END\s*$", text):
        raise BridgeError("PSFXL results index is incomplete", "parse_error")
    entries = []
    row = re.compile(r'(?m)^"([^"\n]+)"\s+"analysisInst"\s*\(\s*"([^"\n]+)"\s*"([^"\n]+)"\s*"([^"\n]+)"(.*?)(?=^"[^"\n]+"\s+"analysisInst"|^END\s*$)', re.S)
    for match in row.finditer(text):
        label, kind, filename, declared, rest = match.groups()
        filename = relative(filename, "PSF index dataFile")
        if kind != "tran" or declared != "PSF" or signatures.get(filename + ".dat") != "ngpsf":
            continue
        if not re.match(r'\s*""\s*\(\s*"time"\s*\)', rest):
            raise BridgeError("only single-sweep transient PSFXL is supported", "unsupported_format")
        logical = re.search(r'"dataset_logical_name"\s+"([^"\n]+)"', rest)
        dataset = logical[1] if logical else label
        entries.append({"name": dataset.removesuffix("-tran"), "index_name": label, "dataset": dataset,
                        "analysis_type": kind, "file": filename, "format": "psfxl", "status": "identified",
                        "evidence": {"declared_format": declared, "signature": "ngpsf companion"}})
    if not entries:
        raise BridgeError("no single transient PSFXL analysis identified in source index", "format_mismatch")
    return entries


def _selected(entries, q):
    from .waveforms import analysis_matches
    candidates = [e for e in entries if analysis_matches(e, q["analysis"])]
    if len(candidates) != 1:
        raise BridgeError("analysis name is missing or ambiguous", "missing_analysis")
    if (candidates[0].get("analysis_type", "tran") != "tran" or candidates[0].get("parent") or
            candidates[0].get("sweep_variables", ["time"]) != ["time"]):
        raise BridgeError("only single-sweep real transient PSFXL is supported", "unsupported_format")
    return candidates[0]


def signal_types(text):
    rows = re.findall(r"\[name:'([^']*)',\s+simName:'[^']*',\s+type:\(name:'([^']*)',\s*type:(\w+)\)\]", text)
    if not re.search(r"(?m)^#dataset .+ nbtype \d+", text) or len(rows) != len(re.findall(r"(?m)^\s*sig\[", text)):
        raise BridgeError("unsupported native signal type listing", "parse_error")
    return {name: {"quantity": quantity, "type": primitive} for name, quantity, primitive in rows}


def native_source(handle, original, directory, q, transport=None, *, entries=None):
    """Return native analyses and selected real traces, reusing published exports."""
    from .waveforms import _regular, parse_local_many

    started = time.monotonic()
    execution = original["execution"]
    tool = q.get("converter", "srrutil")
    cache_path = local_file(directory, "waveform-cache.json")
    if cache_path.exists():
        _regular(cache_path)
    cache = read_json(cache_path) if cache_path.exists() else {**metadata("waveform_cache", handle["run_id"], handle["input_fingerprint"]), "entries": {}}
    record_version(cache, "waveform_cache")
    if cache["run_id"] != handle["run_id"] or cache["input_fingerprint"] != handle["input_fingerprint"]:
        raise BridgeError("waveform cache identity mismatch", "identity_mismatch")
    selection = {"artifact": q["artifact"], "converter": tool, "method": "srrutil-vcsv"}

    def remaining():
        value = q["limits"]["timeout_s"] - (time.monotonic() - started)
        if value <= 0:
            raise BridgeError("PSFXL operation exceeded time budget", "budget_exceeded")
        return value

    def decoded(record):
        if q["operation"] == "analyses":
            return record["analyses"], None, record
        entry = _selected(record["analyses"], q)
        if record.get("dataset") != entry["dataset"]:
            return None
        wanted = q["signals"] or record["signal_names"]
        names = [name for name in wanted if name in record["signal_names"]]
        if not set(names) <= record["exports"].keys():
            return None
        paths, size = [], 0
        for name in names:
            exported = record["exports"][name]
            path = local_file(directory, exported["path"])
            _regular(path)
            state = file_state(path)
            if state != exported["local_state"] and digest(path) != exported["sha256"]:
                raise BridgeError("native export changed locally", "identity_mismatch")
            size += path.stat().st_size
            if size > q["limits"]["bytes"]:
                raise BridgeError("native exports exceed byte budget", "budget_exceeded")
            paths.append(path)
        try:
            data = parse_local_many(paths, {**q["limits"], "timeout_s": remaining()}, q) if paths else {"signals": [], "decoded_points": 0}
        except BridgeError as exc:
            raise BridgeError(f"PSFXL parse: {exc}", exc.code) from exc
        signals = data["signals"]
        if [s["name"] for s in signals] != names:
            raise BridgeError("native export signal name mismatch", "identity_mismatch")
        return record["analyses"], {"analysis": entry["name"], "signals": signals, "adapter": "srrutil-vcsv",
                "decoded_points": data["decoded_points"], "precision": record["precision"],
                "unsupported_signals": {name: info["type"] for name, info in record.get("signal_types", {}).items() if info["type"] != "srrDouble"},
                "decode_scope": "native exact signal selection; original exported samples"}, record

    for item in cache["entries"].values():
        path = local_file(directory, item["path"])
        _regular(path)
        record = read_json(path)
        if record.get("selection") != selection:
            continue
        if (digest(path) != item["sha256"] or record.get("run_id") != handle["run_id"] or
                record.get("input_fingerprint") != handle["input_fingerprint"]):
            raise BridgeError("native export identity mismatch", "identity_mismatch")
        value = decoded(record)
        if value is not None:
            return value

    identifier = uuid.uuid4().hex
    local = local_file(directory, "derived/" + identifier)
    local.mkdir(parents=True)
    remote = handle["remote_directory"] + "/work/.derived/" + identifier
    control = handle["remote_directory"] + "/control"
    guard = (f'test "$(cat {shlex.quote(control + "/input_fingerprint")})" = {shlex.quote(handle["input_fingerprint"])} || exit 88\n'
             + work_available(control))
    source = handle["remote_directory"] + "/work/" + q["artifact"]
    t = transport or SSH(execution)
    record = {**metadata("conversion", handle["run_id"], handle["input_fingerprint"]), "selection": selection, "commands": []}
    stage = "metadata"

    def run(script, **kwargs):
        try:
            response = t.run(guard + script, check=False, timeout=remaining(), transfer=True, **kwargs)
            if response.returncode:
                code = {44: "missing_artifact", 45: "unsafe_path", 88: "identity_mismatch", 89: "remote_work_cleaned",
                        124: "budget_exceeded", 127: "dependency_unavailable", 153: "budget_exceeded"}.get(response.returncode, "conversion_failed")
                raise BridgeError(f"native PSFXL operation failed ({response.returncode}): " + response.stderr.decode("utf-8", "replace")[:2000], code)
            return response
        except BridgeError as exc:
            write_json(local / "conversion.json", {**record, "error": {"code": exc.code, "message": str(exc), "stage": stage}})
            raise BridgeError(f"PSFXL {stage}: {exc}; evidence: {local / 'conversion.json'}", exc.code) from exc

    lease_execution = {**execution, "control_timeout_s": min(execution["control_timeout_s"], remaining()),
                       "transfer_timeout_s": remaining()}
    with SSH(lease_execution).lease(control):
        inspect = (safe_path_script(handle, q["artifact"], "directory") + safe_path_script(handle, q["artifact"] + "/logFile") +
                   'test "$(stat -c %h -- "$p")" = 1 || exit 45\n' +
                   f'test "$(stat -c %s -- "$p")" -le {min(q["limits"]["bytes"], 1048576)} || exit 153\ncat -- "$p"\n' +
                   f'for f in {shlex.quote(source)}/*.dat; do\n test -f "$f" || continue\n test ! -L "$f" || exit 45\n' +
                   ' printf "\\000%s\\000" "${f##*/}"\n head -c 40 -- "$f"\ndone\n')
        if entries is None:
            parts = run(inspect).stdout.split(b"\x00")
            text = parts[0].decode("utf-8")
            # Binary signatures can themselves contain NULs: only the filename/magic
            # prefix is needed. No database tree traversal or full-source hash.
            signatures = {m[1].decode("utf-8"): "ngpsf" for m in re.finditer(rb'\x00([^\x00/]+\.dat)\x00\x00!ngpsf-', b"\x00".join(parts))}
            entries = index_entries(text, signatures)
        record = {**metadata("conversion", handle["run_id"], handle["input_fingerprint"]), "selection": selection,
                  "analyses": entries, "exports": {}, "signal_names": [], "commands": [],
                  "precision": "native VCSV; tested IC6.1.8-64b.83 writes 11 significant decimal digits; no bridge resampling",
                  "remote_directory": remote}
        if q["operation"] != "analyses":
            entry = _selected(entries, q)
            record["dataset"] = entry["dataset"]
            command = [tool, "-print-signal-type", "-dbase", source, "-dataset", entry["dataset"]]
            record["commands"].append(command)
            response = run(environment_command(execution, command))
            record["signal_types"] = signal_types(response.stdout.decode("utf-8"))
            names = [name for name, info in record["signal_types"].items() if info["type"] == "srrDouble"]
            record["signal_names"] = names
            make = (f'base={shlex.quote(handle["remote_directory"] + "/work/.derived")}\n'
                    'test ! -L "$base" && test "$(realpath -m -- "$base")" = "$base" || exit 45\n'
                    f'mkdir -p -- "$base"\nmkdir -- {shlex.quote(remote)}\ncd -- {shlex.quote(remote)}\n')
            commands = [shlex.join([tool, "-v"]) + " >tool.version 2>&1"]
            wanted = q["signals"] or names
            for index, name in enumerate(n for n in wanted if n in names):
                command = [tool, "-export", "-dbase", source, "-dataset", entry["dataset"], "-signal", name,
                           "-o", remote + "/" + str(index), "-vcsv"]
                record["commands"].append(command)
                commands.append(shlex.join(command) + " >>conversion.log 2>&1")
            runtime = "set -eu\nulimit -f " + str(math.ceil(q["limits"]["bytes"] / 1024)) + "\n" + "\n".join(commands)
            failure_log = f' || {{ rc=$?; tail -c 2000 -- {shlex.quote(remote + "/conversion.log")} >&2; exit "$rc"; }}\n'
            stage = "conversion"
            run(make + environment_command(execution, ["/bin/bash", "-c", runtime]) + failure_log)
            record["tool_version"] = run(f'head -c 4096 -- {shlex.quote(remote + "/tool.version")}').stdout.decode("utf-8", "replace")
            selected = [n for n in wanted if n in names]
            files = [str(index) + ".vcsv" for index in range(len(selected))]
            if files:
                stage = "transfer"
                manifest_limit = 256 * len(files) + 1
                limits = {"members": len(files) + 2, "file_bytes": max(q["limits"]["bytes"], manifest_limit),
                          "total_bytes": q["limits"]["bytes"] + manifest_limit}
                archive_limit = limits["total_bytes"] + limits["members"] * 8192 + 1048576
                script = f'cd -- {shlex.quote(remote)}\n: > manifest.tsv\ntotal=0\n'
                for filename in files:
                    script += (f'p={shlex.quote(remote + "/" + filename)}\n'
                         'test -e "$p" || { printf "native exporter did not create %s\\n" "$p" >&2; exit 46; }\n'
                         'test ! -L "$p" && test -f "$p" && test "$(stat -c %h -- "$p")" = 1 && test "$(realpath -e -- "$p")" = "$p" || exit 45\n'
                         'size=$(stat -c %s -- "$p"); total=$((total + size))\n'
                         f'test "$total" -le {q["limits"]["bytes"]} || exit 153\n'
                         'sha=$(sha256sum < "$p"); sha=${sha%% *}\n'
                         f'printf "%s\\t%s\\t%s\\n" {shlex.quote(filename)} "$size" "$sha" >> manifest.tsv\n')
                script += ("set -o pipefail\ntar --transform='s,^,exports/,' -cf - -- " + shlex.join(["manifest.tsv", *files]) +
                           f' | head -c {archive_limit + 1}\n')
                try:
                    with tempfile.TemporaryDirectory(prefix="psfxl-transfer-", dir=local) as temp:
                        archive = local_file(local, Path(temp).name + "/exports.tar")
                        with archive.open("wb") as stream:
                            run(script, output_file=stream)
                        received = unpack_directory(archive, local / "download", "exports", limits, ensure=remaining)
                    received = {member["path"]: member for member in received if member["type"] == "file"}
                    exported_root = local / "download/exports"
                    manifest = local_file(exported_root, "manifest.tsv")
                    if manifest.stat().st_size > manifest_limit:
                        raise BridgeError("native export manifest exceeds budget", "budget_exceeded")
                    rows = [line.split("\t") for line in manifest.read_text(encoding="ascii").splitlines()]
                    if (len(rows) != len(files) or any(len(row) != 3 for row in rows) or
                            {row[0] for row in rows} != set(files) or
                            {p.name for p in exported_root.iterdir()} != {"manifest.tsv", *files}):
                        raise BridgeError("native export batch is incomplete or contains unexpected members", "conversion_failed")
                    total_bytes = 0
                    for filename, size, sha in rows:
                        output = local_file(exported_root, filename)
                        if not size.isdecimal() or not re.fullmatch(r"[0-9a-f]{64}", sha):
                            raise BridgeError("invalid native export manifest", "identity_mismatch")
                        _regular(output)
                        total_bytes += output.stat().st_size
                        if total_bytes > q["limits"]["bytes"]:
                            raise BridgeError("native exports exceed total byte budget", "budget_exceeded")
                        if output.stat().st_size != int(size) or received[filename]["sha256"] != sha:
                            raise BridgeError("native export batch hash/size mismatch: " + filename, "identity_mismatch")
                        name = selected[files.index(filename)]
                        record["exports"][name] = {"path": output.relative_to(directory).as_posix(), "sha256": sha, "local_state": file_state(output)}
                except (BridgeError, OSError, ValueError) as exc:
                    code = getattr(exc, "code", "conversion_failed")
                    write_json(local / "conversion.json", {**record, "error": {"code": code, "message": str(exc), "stage": stage}})
                    raise BridgeError(f"PSFXL transfer: {exc}; evidence: {local / 'conversion.json'}", code) from exc

        stage = "parse"
        try:
            value = decoded(record)
        except BridgeError as exc:
            write_json(local / "conversion.json", {**record, "error": {"code": exc.code, "message": str(exc), "stage": stage}})
            raise BridgeError(f"{exc}; evidence: {local / 'conversion.json'}", exc.code) from exc
        record_path = local / "conversion.json"
        write_json(record_path, record)
        key = hashlib.sha256(canonical({**selection, "dataset": record.get("dataset"), "signals": list(record["exports"])})).hexdigest()
        cache["entries"][key] = {"path": record_path.relative_to(directory).as_posix(), "sha256": digest(record_path)}
        write_json(cache_path, cache)
        return value
