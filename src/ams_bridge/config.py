"""Configuration validation shared by the CLI and Python API."""
from __future__ import annotations

import copy
import math
import re
from pathlib import Path, PurePosixPath

from .common import BridgeError, is_link, read_json, relative


def finite(value, field, nonnegative=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise BridgeError(f"{field}: expected a finite number")
    if nonnegative and value < 0:
        raise BridgeError(f"{field}: expected a nonnegative number")


def strings(value, field, nonempty=False):
    if not isinstance(value, list) or any(not isinstance(x, str) or not x or "\x00" in x for x in value):
        raise BridgeError(f"{field}: expected an array of nonempty strings")
    if nonempty and not value:
        raise BridgeError(f"{field}: must not be empty")


def case_spec(raw: dict, *, check_files=True) -> dict:
    if not isinstance(raw, dict):
        raise BridgeError("case: expected an object")
    c = copy.deepcopy(raw)
    allowed = {"schema_version", "name", "input_root", "working_directory", "arguments", "entry_files",
               "additional_inputs", "dependency_mode", "external_dependencies", "log_files", "artifacts", "waveforms"}
    if c.keys() - allowed:
        raise BridgeError(f"case: unknown fields: {sorted(c.keys() - allowed)}")
    if type(c.get("schema_version")) is not int or c["schema_version"] != 2:
        raise BridgeError("case.schema_version: supported version is 2")
    if not isinstance(c.get("name"), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", c["name"]):
        raise BridgeError("case.name: expected a portable identifier, at most 64 characters")
    if (not isinstance(c.get("input_root"), str) or not c["input_root"] or "\x00" in c["input_root"] or
            (check_files and not Path(c["input_root"]).is_dir())):
        raise BridgeError("case.input_root: directory does not exist")
    root = Path(c["input_root"]).absolute()
    if check_files and any(is_link(p) for p in (root, *root.parents)):
        raise BridgeError("case.input_root: symbolic link / junction is not supported")
    if check_files:
        c["input_root"] = str(root.resolve())
    c["working_directory"] = relative(c.get("working_directory", "."), "case.working_directory")
    if check_files and not (Path(c["input_root"]) / c["working_directory"]).is_dir():
        raise BridgeError("case.working_directory: directory does not exist")
    for key in ("arguments", "entry_files", "additional_inputs", "log_files"):
        c.setdefault(key, [])
        strings(c[key], f"case.{key}", nonempty=key in ("arguments", "entry_files"))
        if key != "arguments":
            c[key] = [relative(x, f"case.{key}") for x in c[key]]
    c.setdefault("dependency_mode", "explicit")
    if c["dependency_mode"] not in ("explicit", "resolve"):
        raise BridgeError("case.dependency_mode: expected explicit or resolve")
    c.setdefault("external_dependencies", [])
    if not isinstance(c["external_dependencies"], list):
        raise BridgeError("case.external_dependencies: expected an array")
    for i, dependency in enumerate(c["external_dependencies"]):
        field = f"case.external_dependencies[{i}]"
        if not isinstance(dependency, dict):
            raise BridgeError(f"{field}: expected an object")
        kind = dependency.get("type", "file")
        fields = {"path", "purpose"} | ({"sha256"} if kind == "file" else set())
        if kind not in ("file", "directory") or set(dependency) - (fields | {"type"}) or not fields.issubset(dependency):
            raise BridgeError(f"{field}: expected file path/purpose/sha256 or directory path/purpose")
        path = dependency["path"]
        if not isinstance(path, str) or not path.startswith("/") or any(x in path for x in "\x00\n\r") or ".." in PurePosixPath(path).parts:
            raise BridgeError(f"{field}.path: expected an absolute Linux dependency path")
        if kind == "file" and (not isinstance(dependency["sha256"], str) or not re.fullmatch("[0-9a-f]{64}", dependency["sha256"])):
            raise BridgeError(f"{field}.sha256: expected a lowercase SHA-256")
        if not isinstance(dependency["purpose"], str) or not dependency["purpose"]:
            raise BridgeError(f"{field}.purpose: expected a nonempty string")
    c.setdefault("artifacts", [])
    if not isinstance(c["artifacts"], list):
        raise BridgeError("case.artifacts: expected an array")
    artifacts = set()
    for item in c["artifacts"]:
        allowed_artifact = {"path", "required"} | ({"type", "limits"} if c["schema_version"] == 2 else set())
        if (not isinstance(item, dict) or not {"path", "required"}.issubset(item) or set(item) - allowed_artifact or
                type(item["required"]) is not bool):
            raise BridgeError("case.artifacts: each item needs path and boolean required")
        item["path"] = relative(item["path"], "case.artifacts.path")
        if item["path"] == "." or item["path"].casefold() in artifacts:
            raise BridgeError("case.artifacts: duplicate or invalid file path")
        artifacts.add(item["path"].casefold())
        if c["schema_version"] == 2:
            item.setdefault("type", "file")
            if item["type"] not in ("file", "directory"):
                raise BridgeError("case.artifacts.type: expected file or directory")
            if item["type"] == "directory":
                item.setdefault("limits", {})
                if not isinstance(item["limits"], dict) or set(item["limits"]) - {"members", "file_bytes", "total_bytes"}:
                    raise BridgeError("case.artifacts.limits: invalid directory budget fields")
                for key, default in (("members", 10000), ("file_bytes", 536870912), ("total_bytes", 1073741824)):
                    item["limits"].setdefault(key, default)
                    if type(item["limits"][key]) is not int or item["limits"][key] <= 0:
                        raise BridgeError(f"case.artifacts.limits.{key}: expected a positive integer")
            elif "limits" in item:
                raise BridgeError("case.artifacts.limits: directory budgets require type=directory")
    for first in artifacts:
        if any(other != first and other.startswith(first + "/") for other in artifacts):
            raise BridgeError("case.artifacts: overlapping file/directory paths")
    if "waveforms" in c:
        from .waveforms import limits_spec, query_spec
        if not isinstance(c["waveforms"], list):
            raise BridgeError("case.waveforms: expected an array")
        identifiers = set()
        for wave in c["waveforms"]:
            fields = {"id", "artifact", "format", "analysis", "signals", "required", "limits", "converter", "window", "coverage"}
            if not isinstance(wave, dict) or set(wave) - fields or not {"id", "artifact", "analysis", "signals", "required"}.issubset(wave):
                raise BridgeError("case.waveforms: invalid declaration fields")
            if (not isinstance(wave["id"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", wave["id"]) or
                    wave["id"] in identifiers or type(wave["required"]) is not bool or not wave["signals"]):
                raise BridgeError("case.waveforms: invalid/duplicate id, required or empty signals")
            identifiers.add(wave["id"])
            wave["artifact"] = relative(wave["artifact"], "case.waveforms.artifact")
            wave["limits"] = limits_spec(wave.get("limits"))
            wave.setdefault("format", "auto")
            query_spec({"operation": "read", **{k: v for k, v in wave.items() if k not in ("id", "required", "coverage")}})
            if "coverage" in wave:
                coverage = wave["coverage"]
                if not isinstance(coverage, dict) or set(coverage) != {"start_s", "stop_s", "atol_s"}:
                    raise BridgeError("case.waveforms.coverage: expected start_s, stop_s, atol_s")
                for key in coverage:
                    finite(coverage[key], "case.waveforms.coverage." + key, True)
                if coverage["start_s"] > coverage["stop_s"]:
                    raise BridgeError("case.waveforms.coverage: reversed interval")
    return c


def execution_config(raw: dict, *, check_files=True) -> dict:
    if not isinstance(raw, dict):
        raise BridgeError("execution: expected an object")
    e = copy.deepcopy(raw)
    allowed = {"ssh_host", "remote_root", "local_output_root", "xrun_path", "environment",
               "environment_setup", "wall_timeout_s", "connect_timeout_s", "control_timeout_s", "transfer_timeout_s", "ssh_config_file"}
    if e.keys() - allowed:
        raise BridgeError(f"execution: unknown fields: {sorted(e.keys() - allowed)}")
    host = e.get("ssh_host")
    if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@-]*", host):
        raise BridgeError("execution.ssh_host: expected an SSH alias or user@host")
    root = e.get("remote_root")
    if not isinstance(root, str) or not root.startswith("/") or str(PurePosixPath(root)) == "/" or any(x in root for x in "\x00\n\r") or ".." in PurePosixPath(root).parts:
        raise BridgeError("execution.remote_root: expected a non-root absolute Linux directory")
    e["remote_root"] = str(PurePosixPath(root))
    for key in ("xrun_path", "local_output_root"):
        if not isinstance(e.get(key), str) or not e[key] or "\x00" in e[key]:
            raise BridgeError(f"execution.{key}: expected a nonempty path")
    if check_files:
        e["local_output_root"] = str(Path(e["local_output_root"]).resolve())
    if e.get("ssh_config_file") is not None:
        if not isinstance(e["ssh_config_file"], str) or not e["ssh_config_file"] or "\x00" in e["ssh_config_file"]:
            raise BridgeError("execution.ssh_config_file: expected a nonempty path")
        if check_files and not Path(e["ssh_config_file"]).is_file():
            raise BridgeError("execution.ssh_config_file: SSH configuration file does not exist")
        if check_files:
            e["ssh_config_file"] = str(Path(e["ssh_config_file"]).resolve())
    for key, default in (("wall_timeout_s", None), ("connect_timeout_s", 8), ("control_timeout_s", 20), ("transfer_timeout_s", 300)):
        e.setdefault(key, default)
        if key == "wall_timeout_s" and e[key] is None:
            continue
        finite(e[key], f"execution.{key}", True)
        if e[key] <= 0:
            raise BridgeError(f"execution.{key}: must be positive")
    e.setdefault("environment", {})
    if not isinstance(e["environment"], dict) or any(not isinstance(k, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k) or
            not isinstance(v, str) or "\x00" in v for k, v in e["environment"].items()):
        raise BridgeError("execution.environment: expected valid variable names and string values")
    if "AMS_BRIDGE_JOB_ID" in e["environment"]:
        raise BridgeError("execution.environment.AMS_BRIDGE_JOB_ID: reserved for job identity")
    setup = e.get("environment_setup")
    if setup is not None:
        if not isinstance(setup, dict) or set(setup) != {"shell", "path"} or setup["shell"] not in ("/bin/bash", "/bin/sh", "/bin/csh", "/bin/tcsh"):
            raise BridgeError("execution.environment_setup: expected supported shell and path")
        if not isinstance(setup["path"], str) or not setup["path"].startswith("/") or any(x in setup["path"] for x in "\x00\n\r"):
            raise BridgeError("execution.environment_setup.path: expected an absolute path")
    return e


def _execution_paths(raw, base):
    if not isinstance(raw, dict):
        raise BridgeError("execution: expected an object")
    raw = copy.deepcopy(raw)
    for key in ("local_output_root", "ssh_config_file"):
        if isinstance(raw.get(key), str):
            raw[key] = str((base / raw[key]).resolve())
    return execution_config(raw)


def load_execution(path) -> dict:
    path = Path(path).resolve()
    data = read_json(path)
    return _execution_paths(data.get("execution"), path.parent)


def load_config(path) -> dict:
    path = Path(path).resolve()
    data = read_json(path)
    if set(data) != {"case", "execution"} or not isinstance(data["case"], dict) or not isinstance(data["execution"], dict):
        raise BridgeError("configuration: expected case and execution objects")
    if isinstance(data["case"].get("input_root"), str):
        data["case"]["input_root"] = str((path.parent / data["case"]["input_root"]).absolute())
    return {"case": case_spec(data["case"]), "execution": _execution_paths(data["execution"], path.parent)}
