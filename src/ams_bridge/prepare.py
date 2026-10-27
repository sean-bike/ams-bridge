"""Freeze inputs without rewriting native files or executing them."""
from __future__ import annotations

import hashlib
import posixpath
import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath

from .common import BridgeError, canonical, digest, local_file, read_json, relative, write_json
from .config import case_spec
from .source_audit import file_calls, spectre_outputs
from .records import metadata, record_version, validate_files
from .native_audit import FLAGS, VALUES, FILES, OUTPUTS, NESTED, argument_tokens, binding, compilefile, nested_arguments, tcl


def uncomment(text: str) -> str:
    # Keep quoted paths and line numbers intact while removing C/HDL comments.
    pattern = r'"(?:\\.|[^"\\])*"|/\*.*?\*/|//[^\n]*'
    return re.sub(pattern, lambda m: m[0] if m[0].startswith('"') else " " + "\n" * m[0].count("\n"), text, flags=re.S)


class Inputs:
    def __init__(self, case):
        self.case = case
        self.root = Path(case["input_root"])
        self.cwd = case["working_directory"]
        self.files = set()
        self.scanned = set()
        self.defer_sources = False
        self.stack = []
        self.include_dirs = [self.cwd]
        self.library_files = {}
        self.report = []
        self.externals = {d["path"] for d in case["external_dependencies"]}
        self.external_directories = [d["path"].rstrip("/") for d in case["external_dependencies"] if d.get("type") == "directory"]
        self.outputs = {a["path"] for a in case["artifacts"]} | set(case["log_files"])
        self.output_locations = set(self.outputs)
        for name in case["entry_files"]:
            if not local_file(self.root, name).is_file():
                raise BridgeError(f"entry_files: expected a regular file: {name}")
        for name in case["entry_files"] + case["additional_inputs"]:
            self.add_declared(name)
        if self.outputs & self.files:
            raise BridgeError(f"output files must not be uploaded: {sorted(self.outputs & self.files)}")

    def add_declared(self, name):
        path = local_file(self.root, name)
        if not path.exists():
            raise BridgeError(f"input does not exist: {name}")
        if path.is_dir():
            for item in sorted(path.iterdir()):
                self.add_declared(item.relative_to(self.root).as_posix())
        elif path.is_file():
            if path.suffix.lower() in (".log", ".psf", ".shm") or "xcelium.d" in path.parts:
                raise BridgeError(f"old simulation product is not an input: {name}")
            self.files.add(name)
        else:
            raise BridgeError(f"not a regular input file: {name}")

    def text(self, name):
        try:
            return uncomment(local_file(self.root, name).read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            raise BridgeError(f"{name}: cannot scan as UTF-8: {exc}") from exc

    def path(self, value, base, origin, output=False):
        if any(x in value for x in ("$", "*", "?", "\x00", "\\")):
            raise BridgeError(f"{origin}: dynamic or non-POSIX path is unsupported: {value}" + ("; use dependency_mode=explicit" if self.case["dependency_mode"] == "resolve" else ""))
        if value.startswith("/"):
            normalized = posixpath.normpath(value)
            if not output and (normalized in self.externals or any(normalized.startswith(d + "/") for d in self.external_directories)):
                return None
            raise BridgeError(f"{origin}: undeclared external input or absolute output: {value}")
        return relative(posixpath.normpath(posixpath.join(base, value)), origin)

    def reference(self, value, base, origin, **details):
        name = self.path(value, base, origin)
        self.report.append({"origin": origin, "raw": value, "base": base, "resolved": name or value,
                            "classification": "external" if name is None else "input", "result": "accepted", **details})
        if name is None:
            return
        if not local_file(self.root, name).is_file():
            raise BridgeError(f"{origin}: missing dependency: {name}")
        if name in self.outputs:
            raise BridgeError(f"{origin}: dependency overlaps an output: {name}")
        if self.case["dependency_mode"] == "explicit" and name not in self.files:
            raise BridgeError(f"{origin}: add undeclared dependency to additional_inputs: {name}")
        self.files.add(name)
        if not self.defer_sources:
            self.scan(name)

    def output(self, value, base, origin):
        name = self.path(value, base, origin, output=True)
        if name == ".":
            raise BridgeError(f"{origin}: output must name a file or subdirectory")
        self.output_locations.add(name)
        self.report.append({"origin": origin, "raw": value, "base": base, "resolved": name,
                            "classification": "output", "result": "accepted"})

    def library_output(self, value, origin):
        # Xrun maps a bare -makelib name under its native library container.
        # It need not collide with an input source directory of the same name.
        self.path(value.rsplit(":", 1)[0], self.cwd, origin, output=True)
        self.report.append({"origin": origin, "raw": value, "classification": "native_library_output", "result": "native_resolution"})

    def arguments(self, tokens, base, origin):
        i = 0
        while i < len(tokens):
            token = tokens[i]
            where = f"{origin}: argument[{i}]"
            if token == "-amsbind":
                self.reference(".binding.cfg", self.cwd, where)
                self.reference(".amsbind.scs", self.cwd, where)
                i += 1
                continue
            if token in FLAGS:
                i += 1
                continue
            if token in VALUES | FILES | OUTPUTS | NESTED | {"-f", "-F", "-incdir", "-modelincdir", "-y", "-reflib", "-amscompilefile"}:
                if i + 1 >= len(tokens):
                    raise BridgeError(f"{where}: missing value for {token}")
                value = tokens[i + 1]
                if token == "-amscompilefile":
                    entry = compilefile(value, where)
                    self.reference(entry["file"], base, where, native_attributes={k: v for k, v in entry.items() if k != "file"})
                elif token in NESTED:
                    nested_arguments(token, value, where, lambda p, w: self.output(p, self.cwd, w))
                elif token in OUTPUTS:
                    if token == "-makelib":
                        self.library_output(value, where)
                    else:
                        self.output(value, self.cwd, where)
                elif token in ("-incdir", "-modelincdir", "-y"):
                    self.add_incdir(value, base, where)
                    if token == "-y":
                        name = self.path(value, base, where)
                        if name is not None:
                            self.add_declared(name)
                elif token == "-reflib":
                    value = value.rsplit(":", 1)[0]
                    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.$]*", value) and not (self.root / base / value).exists():
                        self.report.append({"origin": where, "raw": value, "classification": "logical_library", "result": "native_resolution"})
                    else:
                        name = self.path(value, base, where)
                        if name is not None:
                            self.add_declared(name)
                elif token in FILES:
                    file_base = self.cwd if token == "-input" else base
                    if token in ("-cdslib", "-hdlvar"):
                        name = self.path(value, file_base, where)
                        if name is None:
                            raise BridgeError(f"{where}: library configuration must be packaged for auditing")
                        self.library_files[name] = token
                    self.reference(value, file_base, where)
                elif token in ("-f", "-F"):
                    name = self.path(value, self.cwd if token == "-f" else base, where)
                    if name is None:
                        raise BridgeError(f"{where}: file lists must be packaged locally")
                    if name in self.stack:
                        raise BridgeError(f"{where}: include cycle: {' -> '.join(self.stack + [name])}")
                    if self.case["dependency_mode"] == "explicit" and name not in self.files:
                        raise BridgeError(f"{where}: undeclared file list: {name}")
                    self.files.add(name)
                    self.stack.append(name)
                    text = self.text(name).replace("\\\n", " ")
                    try:
                        nested = argument_tokens(text)
                    except ValueError as exc:
                        raise BridgeError(f"{name}: {exc}") from exc
                    self.arguments(nested, self.cwd if token == "-f" else posixpath.dirname(name) or ".", name)
                    self.stack.pop()
                i += 2
                continue
            if token.startswith("+incdir+"):
                for value in token[len("+incdir+"):].split("+"):
                    self.add_incdir(value, base, where)
            elif token.startswith("+"):
                pass  # Native plusargs remain ordered and are not shell expressions.
            elif token.startswith("-"):
                raise BridgeError(f"{where}: dependency syntax for {token} is unknown; use dependency_mode=explicit")
            else:
                self.reference(token, base, where)
            i += 1

    def add_incdir(self, value, base, origin):
        name = self.path(value, base, origin)
        if name is None:
            self.report.append({"origin": origin, "raw": value, "classification": "external_directory", "result": "accepted"})
            return
        if not local_file(self.root, name).is_dir():
            raise BridgeError(f"{origin}: local include directory does not exist: {value}")
        if name not in self.include_dirs:
            self.include_dirs.append(name)
        self.report.append({"origin": origin, "raw": value, "base": base, "resolved": name,
                            "classification": "include_directory", "result": "accepted"})

    def scan(self, name):
        if name in self.stack:
            raise BridgeError(f"{name}: include cycle: {' -> '.join(self.stack + [name])}")
        if name in self.scanned:
            return
        self.scanned.add(name)
        if name in self.library_files:
            for number, line in enumerate(self.text(name).splitlines(), 1):
                content = line.split("#", 1)[0].strip()
                if not content or content.startswith("--"):
                    continue
                if self.library_files[name] == "-hdlvar" and re.fullmatch(r"DEFINE\s+WORK\s+[A-Za-z_][A-Za-z0-9_]*", content):
                    continue
                parts = argument_tokens(content)
                if self.library_files[name] == "-cdslib" and len(parts) == 3 and parts[0].upper() == "DEFINE":
                    value = parts[2]
                    mapped = self.path(value, posixpath.dirname(name) or ".", f"{name}:{number}")
                    if mapped is not None and (self.root / mapped).is_dir():
                        self.add_declared(mapped)
                    elif mapped is not None:
                        self.output(value, posixpath.dirname(name) or ".", f"{name}:{number}")
                    self.report.append({"origin": f"{name}:{number}", "raw": value, "classification": "library_mapping", "result": "accepted"})
                    continue
                if self.library_files[name] == "-cdslib" and len(parts) == 2 and parts[0].upper() in ("INCLUDE", "SOFTINCLUDE"):
                    if parts[0].upper() == "SOFTINCLUDE" and not (self.root / posixpath.dirname(name) / parts[1]).exists() and not parts[1].startswith("/"):
                        continue
                    nested = self.path(parts[1], posixpath.dirname(name) or ".", name)
                    if nested is not None:
                        self.library_files[nested] = "-cdslib"
                    self.reference(parts[1], posixpath.dirname(name) or ".", name)
                    continue
                raise BridgeError(f"{name}:{number}: cannot resolve library mapping; use dependency_mode=explicit")
            return
        if PurePosixPath(name).name == ".binding.cfg":
            binding(self.text(name), name)
            return
        if PurePosixPath(name).name == ".amsbind.scs":
            content = self.text(name)
            match = re.fullmatch(r"\s*amsd\s*\{([\s\S]*)\}\s*", content)
            if not match:
                raise BridgeError(f"{name}: unsupported AMS binding block; use dependency_mode=explicit")
            binding(match[1], name)
            return
        suffix = PurePosixPath(name).suffix.lower()
        if suffix not in (".v", ".sv", ".vh", ".svh", ".va", ".vams", ".scs", ".tcl"):
            return
        text = self.text(name)
        if suffix == ".tcl":
            tcl(text, name, lambda p, w: self.output(p, self.cwd, w))
            return
        guard = re.fullmatch(r"\s*`ifndef\s+(\w+)\s*\n\s*`define\s+\1\s*\n([\s\S]*)\n\s*`endif\s*", text)
        guard_line = next((n for n, line in enumerate(text.splitlines(), 1) if line.strip()), None) if guard else None
        self.stack.append(name)
        if suffix == ".scs":
            for value, line_number in spectre_outputs(text, name, self.stack):
                self.output(value, self.cwd, f"{name}:{line_number} (reference chain: {' -> '.join(self.stack)})")
        elif suffix != ".tcl":
            for value, writing, line_number in file_calls(text, name, self.stack):
                where = f"{name}:{line_number} (reference chain: {' -> '.join(self.stack)})"
                if writing:
                    self.output(value, self.cwd, where)
                else:
                    self.reference(value, self.cwd, where)
        for number, line in enumerate(text.splitlines(), 1):
            where = f"{name}:{number}"
            if suffix == ".scs":
                m = re.match(r'\s*(?:include|ahdl_include)\s+"([^"\n]+)"(?:\s+section\s*=\s*(\w+))?(?:\s+amsd_subckt_bind\s*=\s*yes)?\s*$', line)
                if m:
                    self.reference(m[1], posixpath.dirname(name) or ".", where, **({"section": m[2]} if m[2] else {}))
                elif re.match(r"\s*(?:include|ahdl_include)\b", line):
                    raise BridgeError(f"{where}: unsupported Spectre include syntax; use dependency_mode=explicit with all inputs declared")
                continue
            if self.case["dependency_mode"] == "resolve" and number != guard_line and re.search(r"`(?:ifdef|ifndef|elsif|else)\b", line):
                raise BridgeError(f"{where}: conditional preprocessing requires explicit dependencies; use dependency_mode=explicit")
            include = re.search(r'`include\s+"([^"\n]+)"', line)
            if include:
                value = include[1]
                if value.startswith("/"):
                    self.reference(value, self.cwd, where)
                else:
                    candidates = [self.path(value, d, where) for d in self.include_dirs]
                    found = next((p for p in candidates if local_file(self.root, p).is_file()), None)
                    if not found:
                        raise BridgeError(f"{where}: include not found in declared search order: {value}")
                    self.reference(found, ".", where)
            elif "`include" in line and self.case["dependency_mode"] == "resolve":
                raise BridgeError(f"{where}: dynamic include requires explicit dependencies; use dependency_mode=explicit")
        self.stack.pop()

    def resolve(self):
        # Include directories must be known before scanning source includes.
        # Source references are queued until the full ordered file-list traversal has completed.
        if self.case["dependency_mode"] == "explicit":
            # Native files and unknown option values belong to the solver, not this resolver.
            self.report.append({"classification": "explicit_native_inputs", "result": "user_declared"})
            for i, token in enumerate(self.case["arguments"][:-1]):
                if token in OUTPUTS:
                    value = self.case["arguments"][i + 1]
                    if token == "-makelib":
                        self.library_output(value, f"arguments[{i}]")
                    else:
                        self.output(value, self.cwd, f"arguments[{i}]")
        else:
            self.defer_sources = True
            self.arguments(self.case["arguments"], self.cwd, "arguments")
            self.defer_sources = False
            for name in sorted(self.files.copy()):
                self.scan(name)
        folded = {}
        for name in sorted(self.files):
            if name.casefold() in folded and folded[name.casefold()] != name:
                raise BridgeError(f"case-insensitive path collision: {folded[name.casefold()]} and {name}")
            folded[name.casefold()] = name
            for out in self.output_locations:
                if name == out or name.startswith(out + "/"):
                    raise BridgeError(f"input overlaps writable output: {name}")
        return sorted(self.files)


def fingerprint(case, files):
    return hashlib.sha256(canonical({"case": case, "files": files})).hexdigest()


def prepare(case: dict, destination) -> dict:
    case = case_spec(case)
    target = Path(destination).resolve()
    if target.exists():
        raise BridgeError(f"prepared directory already exists: {target}")
    root = Path(case["input_root"])
    if target.is_relative_to(root):
        raise BridgeError("prepared directory must be outside input_root")
    inputs = Inputs(case)
    names = inputs.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".prepare-", dir=target.parent))
    try:
        records = []
        for name in names:
            source = local_file(root, name)
            before = digest(source)
            copy = stage / "inputs" / name
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, copy)
            if before != digest(copy) or before != digest(source):
                raise BridgeError(f"input changed while freezing: {name}")
            records.append({"path": name, "source": str(source), "sha256": before,
                            "size": copy.stat().st_size})
        (stage / "inputs" / case["working_directory"]).mkdir(parents=True, exist_ok=True)
        input_fingerprint = fingerprint(case, records)
        record = {**metadata("prepared", input_fingerprint=input_fingerprint), "case": case, "files": records,
                  "fingerprint": input_fingerprint,
                  "dependency_report": inputs.report,
                  "dependency_assurance": "user_declared" if case["dependency_mode"] == "explicit" else "resolved_subset"}
        write_json(stage / "prepared.json", record)
        stage.rename(target)
    except BaseException:
        shutil.rmtree(stage)
        raise
    return {"prepared_directory": str(target), **record}


def verify_prepared(directory) -> dict:
    directory = Path(directory).resolve()
    record = read_json(directory / "prepared.json")
    record_version(record, "prepared")
    validate_files(record.get("files"))
    if (not isinstance(record.get("case"), dict) or
            not isinstance(record.get("files"), list) or
            record.get("fingerprint") != fingerprint(record["case"], record["files"])):
        raise BridgeError("prepared manifest fingerprint mismatch")
    if record["input_fingerprint"] != record["fingerprint"]:
        raise BridgeError("prepared metadata fingerprint mismatch")
    names = set()
    for item in record["files"]:
        if (not isinstance(item, dict) or set(item) != {"path", "source", "sha256", "size"} or
                not isinstance(item["path"], str) or item["path"] in names):
            raise BridgeError("prepared manifest has an invalid or duplicate file record")
        names.add(item["path"])
        path = local_file(directory / "inputs", item["path"])
        if not path.is_file() or digest(path) != item["sha256"] or path.stat().st_size != item["size"]:
            raise BridgeError(f"prepared input fingerprint mismatch: {item['path']}")
    frozen_case = dict(record["case"], input_root=str(directory / "inputs"))
    frozen_case = case_spec(frozen_case)
    inputs = Inputs(frozen_case)
    if inputs.resolve() != sorted(x["path"] for x in record["files"]):
        raise BridgeError("prepared file set changed")
    if "dependency_report" in record and record["dependency_report"] != inputs.report:
        raise BridgeError("prepared dependency report changed")
    return record
