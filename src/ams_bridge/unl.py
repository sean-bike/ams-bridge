"""Read-only capture and explicit relocation of the observed Cadence UNL export."""
from __future__ import annotations

import base64
import copy
import hashlib
import math
import posixpath
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path, PurePosixPath

from .common import BridgeError, digest, is_link, relative, write_json
from .config import case_spec, execution_config
from .native_audit import FLAGS, VALUES, FILES, OUTPUTS, argument_tokens, compilefile
from .prepare import Inputs
from .records import metadata
from .source_audit import file_calls
from .transport import SSH


def absolute(value, field):
    if (not isinstance(value, str) or not value.startswith("/") or value == "/" or
            any(c in value for c in "\x00\n\r\\$*?") or ".." in PurePosixPath(value).parts):
        raise BridgeError(f"{field}: expected a non-root absolute Linux path")
    return posixpath.normpath(value)


def import_spec(raw):
    required = {"schema_version", "source_directory", "invoke_directory", "include_directories", "external_roots", "case"}
    if not isinstance(raw, dict) or not required.issubset(raw) or set(raw) - required - {"stop_s", "limits", "database_directory"}:
        raise BridgeError("UNL spec: invalid fields")
    spec = copy.deepcopy(raw)
    if type(spec["schema_version"]) is not int or spec["schema_version"] != 2:
        raise BridgeError("UNL spec: supported schema_version is 2")
    for key in ("source_directory", "invoke_directory"):
        spec[key] = absolute(spec[key], key)
    for key in ("include_directories", "external_roots"):
        if not isinstance(spec[key], list):
            raise BridgeError(f"{key}: expected an array")
        spec[key] = [absolute(p, key) for p in spec[key]]
    if "stop_s" in spec and (isinstance(spec["stop_s"], bool) or not isinstance(spec["stop_s"], (int, float)) or
                             not math.isfinite(spec["stop_s"]) or spec["stop_s"] <= 0):
        raise BridgeError("stop_s: expected a positive finite number")
    if "database_directory" in spec:
        value = spec["database_directory"]
        if not isinstance(value, str) or value.startswith("/") or any(c in value for c in "$*?\x00\\\n\r"):
            raise BridgeError("database_directory: expected a static job-local path from netlist/")
        if relative(posixpath.normpath(posixpath.join("netlist", value))) in (".", "netlist", "sources", "originals"):
            raise BridgeError("database_directory: cannot be an input root")
    limits = spec.setdefault("limits", {})
    if not isinstance(limits, dict) or set(limits) - {"files", "file_bytes", "total_bytes", "timeout_s"}:
        raise BridgeError("UNL limits: invalid fields")
    for key, default in (("files", 1024), ("file_bytes", 16777216), ("total_bytes", 67108864), ("timeout_s", 120)):
        limits.setdefault(key, default)
        value = limits[key]
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0 or
                (key != "timeout_s" and type(value) is not int)):
            raise BridgeError(f"UNL limits.{key}: expected a positive {'number' if key == 'timeout_s' else 'integer'}")
    if not isinstance(spec["case"], dict) or set(spec["case"]) - {"name", "artifacts", "log_files", "waveforms"}:
        raise BridgeError("UNL case: expected name/artifacts/log_files/waveforms")
    case_spec({**spec["case"], "schema_version": 2, "input_root": "<import>", "working_directory": "netlist",
               "arguments": ["-64bit", "-f", "xrunArgs"], "entry_files": ["netlist/xrunArgs"]}, check_files=False)
    return spec


class RemoteSources:
    def __init__(self, spec, execution, transport=None):
        self.limits = spec["limits"]
        self.transport = transport or SSH(execution)
        self.started = time.monotonic()
        self.cache = {}
        self.total = 0

    def remaining(self):
        remaining = self.limits["timeout_s"] - (time.monotonic() - self.started)
        if remaining <= 0:
            raise BridgeError("UNL import exceeded time budget", "budget_exceeded")
        return remaining

    def read(self, path, *, required=True):
        self.remaining()
        if path in self.cache:
            return self.cache[path]
        if len(self.cache) >= self.limits["files"]:
            raise BridgeError("UNL input count exceeds budget", "budget_exceeded")
        code = f"set -eu\np={shlex.quote(path)}\n" + r'''
test -f "$p" || exit 44
canonical=$(realpath -e -- "$p")
size=$(stat -Lc %s -- "$p")
''' + f"test \"$size\" -le {min(self.limits['file_bytes'], self.limits['total_bytes'] - self.total)} || exit 89\n" + r'''
before=$(head -c "$((size + 1))" -- "$p" | sha256sum | cut -d ' ' -f 1)
printf 'AMS_UNL_SOURCE_1\000%s\000%s\000%s\000' "$canonical" "$size" "$before"
head -c "$size" -- "$p" | base64 -w0
printf '\000'
test "$(realpath -e -- "$p")" = "$canonical" && test "$(stat -Lc %s -- "$p")" = "$size" &&
test "$(head -c "$((size + 1))" -- "$p" | sha256sum | cut -d ' ' -f 1)" = "$before" || exit 88
'''
        try:
            result = self.transport.run(code, check=False, timeout=self.remaining(), transfer=True)
        except BridgeError as exc:
            if (isinstance(exc.__cause__, subprocess.TimeoutExpired) or exc.code in ("transfer_timeout", "control_timeout")) and time.monotonic() - self.started >= self.limits["timeout_s"]:
                raise BridgeError("UNL import exceeded overall time budget while reading " + path, "budget_exceeded") from exc
            raise
        if result.returncode == 44:
            if not required:
                return None
            raise BridgeError(f"UNL dependency missing: {path}", "missing_input")
        if result.returncode:
            if result.returncode == 124:
                self.remaining()
                raise BridgeError("UNL source transfer timed out: " + path, "transfer_timeout")
            raise BridgeError(f"UNL source read failed ({result.returncode}): {path}",
                              "budget_exceeded" if result.returncode == 89 else "identity_mismatch" if result.returncode == 88 else "communication")
        parts = result.stdout.split(b"\x00")
        try:
            if len(parts) != 6 or parts[0] != b"AMS_UNL_SOURCE_1" or parts[-1] != b"":
                raise ValueError("wire fields")
            canonical, size, sha = parts[1].decode("utf-8"), int(parts[2]), parts[3].decode("ascii")
            absolute(canonical, "canonical source")
            if not 0 <= size <= self.limits["file_bytes"] or not re.fullmatch(r"[0-9a-f]{64}", sha):
                raise ValueError("metadata")
            if len(parts[4]) > 4 * ((self.limits["file_bytes"] + 2) // 3):
                raise ValueError("payload")
            data = base64.b64decode(parts[4], validate=True)
            if len(data) != size or hashlib.sha256(data).hexdigest() != sha:
                raise ValueError("hash/size")
        except (BridgeError, ValueError, UnicodeError) as exc:
            raise BridgeError(f"invalid UNL source response: {path}", "invalid_remote_record") from exc
        self.total += size
        if self.total > self.limits["total_bytes"]:
            raise BridgeError("UNL source bytes exceed budget", "budget_exceeded")
        self.cache[path] = data, {"source": path, "canonical_source": canonical, "sha256": sha, "size": size}
        return self.cache[path]


def mask_comments(text):
    pattern = r'"(?:\\.|[^"\\])*"|/\*.*?\*/|//[^\n]*'
    return re.sub(pattern, lambda m: m[0] if m[0].startswith('"') else re.sub(r"[^\r\n]", " ", m[0]), text, flags=re.S)


def native_quote(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"' if re.search(r'[\s"#\\]', value) else value


class Importer:
    def __init__(self, spec, loader, stage):
        self.spec, self.loader, self.stage = spec, loader, stage
        self.source = spec["source_directory"]
        self.files, self.external, self.active, self.modes = {}, {}, [], {}
        self.report = {**metadata("unl_import"), "adapter_version": "2.0.0", "spec": spec,
                       "files": [], "dependencies": [], "edits": [],
                       "external_assurance": "all literal includes hashed, including unselected sections; no model evaluation"}
        self.include_dirs = [self.source]

    def path(self, raw, base):
        value = raw.replace("${IC_INVOKE_DIR}", self.spec["invoke_directory"])
        if any(c in value for c in "$*?\x00\\\n\r"):
            raise BridgeError(f"unsupported dynamic UNL path: {raw}")
        return posixpath.normpath(value if value.startswith("/") else posixpath.join(base, value))

    def destination(self, source):
        if posixpath.dirname(source) == self.source:
            return relative("netlist/" + posixpath.basename(source))
        return relative("sources/" + hashlib.sha256(source.encode()).hexdigest()[:16] + "_" + posixpath.basename(source))

    def edit(self, origin, old, new, reason, **position):
        if old != new:
            self.report["edits"].append({"origin": origin, "old": old, "new": new, "reason": reason, **position})
        return new

    def reference(self, raw, base, origin, *, mode="source", child_base=None, target_base="netlist", external=False, resolved_path=None, **details):
        try:
            path = resolved_path or self.path(raw, base)
            destination = self.visit(path, mode=mode, argument_base=child_base, force_external=external)
        except BridgeError as exc:
            self.report["dependencies"].append({"origin": origin, "raw": raw, "base": base,
                                                "classification": "external" if external else "input", "result": "rejected", "reason": str(exc)})
            raise BridgeError(f"{origin}: {exc}", exc.code) from exc
        self.report["dependencies"].append({"origin": origin, "raw": raw, "base": base, "resolved": path,
                                            "classification": "external" if destination.startswith("/") else "input", "result": "accepted", **details})
        if external:
            return raw
        replacement = destination if destination.startswith("/") else posixpath.relpath(destination, target_base)
        return self.edit(origin, raw, replacement, "relocate referenced input")

    def output(self, value, origin):
        if value.startswith("/") or any(c in value for c in "$*?\x00\\\n\r"):
            raise BridgeError(f"{origin}: unsupported absolute/dynamic output: {value}")
        resolved = relative(posixpath.normpath(posixpath.join("netlist", value)), origin)
        self.report["dependencies"].append({"origin": origin, "raw": value, "base": "netlist",
                                            "resolved": resolved, "classification": "output", "result": "accepted"})
        return value

    def arguments(self, text, path, base, destination):
        try:
            tokens = argument_tokens(mask_comments(text))
        except ValueError as exc:
            raise BridgeError(f"{path}: malformed file list: {exc}") from exc
        target_base = "netlist" if base == self.source else posixpath.dirname(destination)
        result, index = [], 0
        while index < len(tokens):
            token = tokens[index]
            where = f"{path}: argument[{index}]"
            result.append(token)
            if token in FLAGS:
                index += 1
                continue
            if token == "-amsbind":
                for name in (".binding.cfg", ".amsbind.scs"):
                    self.reference(name, self.source, where)
                index += 1
                continue
            if token in VALUES | FILES | OUTPUTS | {"-f", "-F", "-incdir", "-modelincdir", "-amscompilefile", "-spectre_args", "-xmsimargs"}:
                if index + 1 >= len(tokens):
                    raise BridgeError(f"{where}: missing option value")
                value = tokens[index + 1]
                if token in FILES | {"-f", "-F"}:
                    invocation_relative = token in ("-f", "-input")
                    child = self.path(value, self.source if invocation_relative else base)
                    child_base = (self.source if token == "-f" else posixpath.dirname(child)) if token in ("-f", "-F") else None
                    value = self.reference(value, self.source if invocation_relative else base, where,
                                           mode="arguments" if token in ("-f", "-F") else "source", child_base=child_base,
                                           target_base="netlist" if invocation_relative else target_base)
                elif token == "-amscompilefile":
                    fields = compilefile(value, where)
                    fields["file"] = self.reference(fields["file"], base, where, target_base=target_base)
                    value = " ".join(k + ":" + v for k, v in fields.items())
                elif token in ("-incdir", "-modelincdir"):
                    resolved = self.path(value, base)
                    if resolved not in self.include_dirs:
                        self.include_dirs.append(resolved)
                    mapped = "netlist" if resolved == self.source else "sources"
                    value = self.edit(where, value, posixpath.relpath(mapped, target_base), "explicit references replace broad native search root")
                elif token in OUTPUTS:
                    self.output(value, where)
                elif token in ("-spectre_args", "-xmsimargs"):
                    try:
                        nested = argument_tokens(value)
                    except ValueError as exc:
                        raise BridgeError(f"{where}: malformed nested {token}: {exc}") from exc
                    for i, item in enumerate(nested):
                        if item == "-ahdllibdir" and i + 1 < len(nested):
                            nested[i + 1] = self.edit(where, nested[i + 1], "../cache/ahdl", "isolate writable AHDL cache")
                    value = " ".join(native_quote(v) for v in nested)
                result.append(value)
                index += 2
                continue
            if token.startswith("+incdir+"):
                raise BridgeError(f"{where}: use the verified -incdir form for UNL import")
            if token.startswith("+"):
                index += 1
                continue
            if token.startswith("-"):
                raise BridgeError(f"{where}: unsupported option: {token}")
            result[-1] = self.reference(token, base, where, target_base=target_base)
            index += 1
        return "\n".join(native_quote(t) for t in result) + "\n"

    def source_text(self, text, path, destination, external):
        suffix = PurePosixPath(path).suffix.lower()
        if suffix == ".tcl":
            pattern = r'(?m)^[ \t]*if\s*\{\s*\[info exists ::env\(AMS_RESULTS_DIR\)\s*\]\s*\}\s*\{\s*set AMS_RESULTS_DIR \$env\(AMS_RESULTS_DIR\)\s*\}\s*else\s*\{\s*set AMS_RESULTS_DIR "([^"\n]+)"\s*\}\s*$'
            conditional = re.search(pattern, text)
            if conditional:
                fallback = self.output(self.spec.get("database_directory", conditional[1]), path)
                self.edit(path, conditional[1], fallback, "explicit native database destination")
                replacement = "# AMS_RESULTS_DIR pinned by UNL import\n"
                self.edit(path, conditional[0], replacement, "freeze generated AMS_RESULTS_DIR fallback; do not inherit external output environment")
                text = text[:conditional.start()] + replacement + text[conditional.end():]
                database = r'(?m)^([ \t]*database\s+-open\s+\w+\s+-into\s+)(\$\{AMS_RESULTS_DIR\})(\s+-default[ \t]*\r?$)'
                def substitute(match):
                    value = self.edit(path, match[2], native_quote(fallback), "replace generated database variable at its audited output position")
                    return match[1] + value + match[3]
                text, count = re.subn(database, substitute, text)
                if count != 1:
                    raise BridgeError(f"{path}: expected one generated database output reference")
            return text
        if suffix not in (".scs", ".va", ".vams", ".v", ".sv", ".vh", ".svh"):
            return text
        masked = mask_comments(text)
        edits = []
        pattern = r'(?m)^\s*(?:`include|include|ahdl_include)\s+"([^"\n]+)"'
        for statement in re.finditer(r"(?m)^[ \t]*(?:`include|include|ahdl_include)\b[^\r\n]*", masked):
            if not re.match(pattern.removeprefix("(?m)"), statement[0]):
                line = text.count("\n", 0, statement.start()) + 1
                raise BridgeError(f"{path}:{line}: unsupported nonliteral include syntax")
        for match in re.finditer(pattern, masked):
            raw = match[1]
            line = text.count("\n", 0, match.start()) + 1
            base = posixpath.dirname(path) if suffix == ".scs" else self.source
            resolved = self.path(raw, base)
            if not raw.startswith("/") and self.loader.read(resolved, required=False) is None:
                for directory in [*self.include_dirs, *self.spec["include_directories"]]:
                    candidate = self.path(raw, directory)
                    if self.loader.read(candidate, required=False) is not None:
                        base, resolved = directory, candidate
                        break
            section = re.match(r"\s+section\s*=\s*(\w+)", masked[match.end():])
            replacement = self.reference(raw, base, f"{path}:{line}", external=external, resolved_path=resolved,
                                         target_base=posixpath.dirname(destination) if suffix == ".scs" else "netlist",
                                         **({"section": section[1]} if section else {}))
            if not external:
                edits.append((match.start(1), match.end(1), self.edit(f"{path}:{line}", raw, replacement, "relocate literal include")))
        if not external and suffix != ".scs":
            for raw, writing, line, start, count in file_calls(masked, path, locations=True):
                if writing:
                    self.output(raw, f"{path}:{line}")
                else:
                    replacement = self.reference(raw, self.source, f"{path}:{line}")
                    edits.append((start, start + count, replacement))
        if not external and path == self.source + "/amsControlSpectre.scs" and "stop_s" in self.spec:
            matches = list(re.finditer(r"\btran\s+tran\s+stop\s*=\s*([^\s\\]+)", masked))
            if len(matches) != 1:
                raise BridgeError("expected one generated transient stop for explicit clipping")
            match = matches[0]
            original = re.fullmatch(r"([\d.eE+-]+)([fpnum]?)", match[1])
            scales = {"": 1, "f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6, "m": 1e-3}
            try:
                duration = float(original[1]) * scales[original[2]] if original else 0
            except ValueError:
                duration = 0
            if not math.isfinite(duration) or self.spec["stop_s"] > duration:
                raise BridgeError("stop_s must crop an explicitly understood original transient duration")
            value = format(self.spec["stop_s"], ".17g")
            edits.append((match.start(1), match.end(1), self.edit(path, match[1], value, "explicit transient stop crop; checker parameters unchanged")))
        for start, end, replacement in sorted(edits, reverse=True):
            text = text[:start] + replacement + text[end:]
        return text

    def visit(self, path, *, mode="source", argument_base=None, force_external=False):
        self.loader.remaining()
        data, source = self.loader.read(path)
        canonical = source["canonical_source"]
        external = force_external or any(path == root or canonical == root or path.startswith(root + "/") or canonical.startswith(root + "/") for root in self.spec["external_roots"])
        try:
            text = data.decode("utf-8")
        except UnicodeError as exc:
            raise BridgeError(f"{path}: source cannot be audited as UTF-8", "parse_error") from exc
        if external:
            if canonical in self.external:
                return canonical
            self.external[canonical] = {"path": canonical, "sha256": source["sha256"], "purpose": "shared read-only model; literal includes pinned"}
            self.source_text(text, path, self.destination(path), True)
            return canonical
        destination = self.destination(path)
        context = (mode, argument_base)
        if path in self.active:
            raise BridgeError("UNL reference cycle: " + " -> ".join([*self.active, path]))
        if path in self.modes:
            if self.modes[path] != context:
                raise BridgeError(f"UNL input used with ambiguous file-list bases: {path}")
            return destination
        self.modes[path] = context
        self.active.append(path)
        adapted = self.arguments(text, path, argument_base or self.source, destination) if mode == "arguments" else self.source_text(text, path, destination, False)
        self.active.pop()
        output = self.stage / "inputs" / destination
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(adapted.encode("utf-8"))
        original = "originals/" + hashlib.sha256(path.encode()).hexdigest() + ".bin"
        original_path = self.stage / "inputs" / original
        original_path.parent.mkdir(exist_ok=True)
        original_path.write_bytes(data)
        self.files[path] = destination
        self.report["files"].append({**source, "path": destination, "original": original,
                                     "adapted_sha256": digest(output), "changed": data != adapted.encode("utf-8")})
        return destination


def import_unl(spec, execution, directory, *, transport=None):
    spec, execution = import_spec(spec), execution_config(execution)
    target = Path(directory).resolve()
    if target.exists():
        raise BridgeError(f"UNL import destination already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".import-", dir=target.parent))
    adapter = None
    try:
        loader = RemoteSources(spec, execution, transport)
        try:
            header = loader.read(spec["source_directory"] + "/netlist.vams")[0].decode("utf-8")
        except UnicodeError as exc:
            raise BridgeError("UNL netlist header is not UTF-8", "parse_error") from exc
        ic = re.search(r"IC subversion:\s*(\S+)", header)
        xcelium = re.search(r"Xcelium version:\s*(\S+)", header)
        if not ic or not xcelium or (ic[1], xcelium[1]) != ("IC6.1.8-64b.83", "18.03-s001"):
            raise BridgeError("UNL exporter version is not the verified IC618/Xcelium1803 combination", "unsupported_format")
        adapter = Importer(spec, loader, stage)
        adapter.report["exporter"] = {"ic_subversion": ic[1], "xcelium": xcelium[1]}
        adapter.visit(spec["source_directory"] + "/xrunArgs", mode="arguments", argument_base=spec["source_directory"])
        (stage / "inputs/sources").mkdir(exist_ok=True)
        report_path = stage / "inputs/import-report.json"
        write_json(report_path, adapter.report)
        additional = sorted([*(p.relative_to(stage / "inputs").as_posix() for p in (stage / "inputs").rglob("*") if p.is_file())])
        case = {**spec["case"], "schema_version": 2, "input_root": str(stage / "inputs"), "working_directory": "netlist",
                "arguments": ["-64bit", "-f", "xrunArgs"], "entry_files": ["netlist/xrunArgs"],
                "additional_inputs": additional, "dependency_mode": "explicit", "external_dependencies": list(adapter.external.values())}
        case = case_spec(case)
        inputs = Inputs(case)
        inputs.resolve()
        adapter.report["frozen_dependency_report"] = inputs.report
        adapter.report["external_dependencies"] = case["external_dependencies"]
        adapter.report["source_bytes"] = loader.total
        adapter.report["elapsed_s"] = time.monotonic() - loader.started
        write_json(report_path, adapter.report)
        case["input_root"] = str(target / "inputs")
        write_json(stage / "config.json", {"case": case, "execution": execution})
        stage.rename(target)
        return {**metadata("unl_import"), "import_directory": str(target), "configuration": str(target / "config.json"),
                "report": str(target / "inputs/import-report.json"), "exporter": adapter.report["exporter"],
                "input_files": len(adapter.files), "external_files": len(adapter.external), "edits": len(adapter.report["edits"])}
    except BaseException as exc:
        if isinstance(exc, BridgeError):
            exc.details = {"dependencies": adapter.report["dependencies"] if adapter else [],
                           "rejection": {"code": exc.code, "message": str(exc), "source_directory": spec["source_directory"]}}
        if stage.exists():
            if is_link(stage) or stage.resolve().parent != target.parent or not stage.name.startswith(".import-"):
                raise BridgeError("UNL staging identity changed; cleanup refused", "unsafe_path")
            shutil.rmtree(stage)
        raise
