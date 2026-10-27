"""Adapters for bounded, local transient data; native coordinates stay intact."""
from __future__ import annotations

import contextlib
import csv
import importlib.metadata
import math
import re
import sys
from pathlib import Path

from .common import BridgeError, canonical, read_json, write_json
from .wave_query import coordinate, decimal

PARSER_VERSION = "2.5.0"


def probe(path):
    path = Path(path)
    with path.open("rb") as stream:
        head = stream.read(4096)
        stream.seek(max(0, path.stat().st_size - 12))
        tail = stream.read()
    return probe_bytes(head, tail, path)


def probe_bytes(head, tail, path="native source"):
    text = head.lstrip()
    if text.startswith(b";Version, 1, 0"):
        fmt = "vcsv"
    elif text.startswith(b"HEADER"):
        if not re.search(rb'(?m)^"PSFversion"\s+"[^"\r\n]+"', head):
            raise BridgeError(f"{path}: HEADER without a PSF version signature", "unknown_format")
        fmt = "psf_index" if re.search(rb'"analysisInst"\s+STRUCT\s*\(', head) else "psf_ascii"
    elif b"PSFversion" in head and b"\x00" in head:
        if b"Clarissa" not in tail:
            raise BridgeError(f"{path}: BINPSF signature has a missing/damaged Clarissa trailer", "parse_error")
        fmt = "binpsf"
    elif head[:4] == b"\xa1\x95\x11\xbf":
        fmt = "shm_member"
    elif re.match(rb"\$(?:date|version|comment|timescale|scope|var)\b", text):
        fmt = "vcd"
    elif b"PSFXL" in head:
        fmt = "psfxl"
    else:
        raise BridgeError(f"{path}: unrecognized waveform content", "unknown_format")
    return {"format": fmt, "head_hex": head[:64].hex(), "tail_hex": tail.hex()}


def detect(path):
    return probe(path)["format"]


class Budget:
    def __init__(self, limits, selection=None):
        self.limits = limits
        self.selection = selection or {}
        self.materialized = 0
        self.points = 0

    def point(self):
        self.points += 1
        if self.points > self.limits["points"]:
            raise BridgeError("waveform decoding exceeds point/byte budget", "budget_exceeded")

    def wanted(self, signal, time):
        if self.selection.get("operation") in ("signals", "analyses"):
            return False
        names = self.selection.get("signals", [])
        if names and signal["name"] not in names:
            return False
        window = self.selection.get("window")
        if window is not None:
            lo, hi = (coordinate(signal, t, self.selection.get("time_unit", "s")) for t in window)
            if decimal(time) < lo or decimal(time) > hi or (decimal(time) == hi and not self.selection.get("include_end", False)):
                return False
        self.materialized += 1
        return True


def ascii_psf(path, budget):
    try:
        from psf_utils import PSF
    except ImportError as exc:
        raise BridgeError("PSF ASCII reader requires ams-bridge[waveforms]", "dependency_unavailable") from exc
    text = path.read_text(encoding="utf-8")
    if not re.search(r"(?m)^END\s*$", text):
        raise BridgeError("PSF ASCII lacks END; incomplete data", "parse_error")
    if not re.search(r"(?ms)^TRACE\s*\n.+?^VALUE\s*$", text):
        raise BridgeError("PSF has no TRACE curves", "empty_waveform")
    parsed = PSF(path, use_cache=False, update_cache=False)
    if len(parsed.sweeps or []) != 1 or parsed.get_sweep().name != "time" or parsed.meta.get("analysis type") != "tran":
        raise BridgeError("only single-sweep real transient PSF is supported", "unsupported_format")
    sweep = parsed.get_sweep()
    if sweep.units != "s":
        raise BridgeError("PSF time units must be explicitly seconds", "unsupported_format")
    signals = []
    for signal in parsed.all_signals():
        if not signal.units or "complex" in signal.type.kind:
            raise BridgeError(f"{signal.name}: missing units or unsupported complex data", "unsupported_format")
        if len(signal.ordinate) != len(sweep.abscissa):
            raise BridgeError(f"{signal.name}: incomplete sweep", "parse_error")
        points = []
        previous = -math.inf
        for order, (time, value) in enumerate(zip(sweep.abscissa, signal.ordinate)):
            time, value = float(time), float(value)
            if not math.isfinite(time) or not math.isfinite(value) or time < previous:
                raise BridgeError(f"{signal.name}: non-finite or unordered point {order}", "parse_error")
            previous = time
            budget.point()
            if budget.wanted({"name": signal.name, "kind": "analog", "time_unit": "s"}, time):
                points.append({"time": time, "value": value, "order": order})
        if not len(sweep.abscissa):
            raise BridgeError(f"{signal.name}: no points", "empty_waveform")
        signals.append({"name": signal.name, "kind": "analog", "unit": signal.units,
                        "time_unit": "s", "points": points, "coverage": [float(sweep.abscissa[0]), float(sweep.abscissa[-1])], "source_point_count": len(sweep.abscissa)})
    if not signals:
        raise BridgeError("PSF has no signal curves", "empty_waveform")
    return {"analysis": parsed.meta.get("analysis name"), "signals": signals,
            "adapter": "psf-utils", "adapter_version": importlib.metadata.version("psf-utils"),
            "source_metadata": parsed.meta, "decode_scope": "full PSF library decode; selected point objects", "budget_scope": "input bytes, all source points, returned JSON bytes"}


def vcsv_data(path, budget):
    """The single real trace written by srrutil -export -vcsv (no resampling)."""
    with path.open(encoding="utf-8", newline="") as stream:
        header = [stream.readline().rstrip("\r\n") for _ in range(6)]
        if (header[0] != ";Version, 1, 0" or not header[1].startswith(";") or not header[1][1:] or
                header[2:4] != [";X, Y", ";Re, Re"] or any(not h.startswith(";") for h in header[4:6])):
            raise BridgeError("unsupported native VCSV header/trace type", "unsupported_format")
        units = next(csv.reader([header[4].removeprefix(";")], skipinitialspace=True))
        if len(units) != 2 or units[0].strip() != "s":
            raise BridgeError("VCSV requires one real transient trace with a seconds axis", "unsupported_format")
        signal = {"name": header[1][1:], "kind": "analog", "unit": units[1].strip(),
                  "time_unit": "s", "points": [], "source_point_count": 0, "coverage": None}
        previous = -math.inf
        for row in csv.reader(stream, skipinitialspace=True):
            if len(row) != 2:
                raise BridgeError("native VCSV requires time,value rows", "parse_error")
            t, value = map(float, row)
            if not math.isfinite(t) or not math.isfinite(value) or t < previous:
                raise BridgeError("native VCSV contains non-finite or unordered samples", "parse_error")
            previous = t
            budget.point()
            order = signal["source_point_count"]
            signal["source_point_count"] += 1
            if signal["coverage"] is None:
                signal["coverage"] = [t, t]
            signal["coverage"][1] = t
            if budget.wanted(signal, t):
                signal["points"].append({"time": t, "value": value, "order": order})
        if signal["coverage"] is None:
            raise BridgeError("native VCSV has no samples", "empty_waveform")
    return {"analysis": "tran", "signals": [signal], "adapter": "srrutil-vcsv",
            "adapter_version": "1.0", "source_metadata": {"header": header},
            "decode_scope": "selected native trace; full source sample scan",
            "budget_scope": "export bytes, all selected source points, returned JSON bytes"}


def vcd_data(path, budget):
    try:
        from vcd.reader import TokenKind as K, tokenize
    except ImportError as exc:
        raise BridgeError("VCD reader requires ams-bridge[waveforms]", "dependency_unavailable") from exc
    with path.open("rb") as stream:
        header = stream.read(65536)
    timescale = re.search(rb"\$timescale\s+([^$]+)\$end", header)
    if timescale and not re.fullmatch(rb"(?:1|10|100)\s*(?:s|ms|us|ns|ps|fs)\s*", timescale[1]):
        raise BridgeError("VCD nonstandard/fractional timescale is unsupported; preserve native data", "unsupported_format")
    if re.search(rb"\$var\s+(?:real|realtime|string|event)\b", header):
        raise BridgeError("VCD real/realtime/string/event variables are unsupported", "unsupported_format")
    scope, signals, by_id = [], [], {}
    tick, scale, defined, ordinal, end_tick = 0, None, False, 0, None
    with path.open("rb") as stream:
        for token in tokenize(stream):
            kind = token.kind
            if kind is K.TIMESCALE:
                if scale is not None or defined:
                    raise BridgeError("VCD duplicate/late timescale", "parse_error")
                t = token.timescale
                scale = {"magnitude": t.magnitude, "unit": t.unit.value}
            elif kind is K.SCOPE:
                if defined:
                    raise BridgeError("VCD late scope", "parse_error")
                scope.append(token.scope.ident)
            elif kind is K.UPSCOPE:
                if not scope or defined:
                    raise BridgeError("VCD unbalanced scope", "parse_error")
                scope.pop()
            elif kind is K.VAR:
                v = token.var
                if defined or v.type_.value in ("real", "realtime", "string", "event"):
                    raise BridgeError(f"VCD {v.reference}: unsupported variable type {v.type_.value}", "unsupported_format")
                if v.size <= 0 or v.size > budget.limits["bytes"]:
                    raise BridgeError("VCD width exceeds byte budget", "budget_exceeded")
                name = ".".join([*scope, v.reference])
                if v.size == 1 and isinstance(v.bit_index, int):
                    name += f"[{v.bit_index}]"
                if any(s["name"] == name for s in signals):
                    raise BridgeError(f"VCD duplicate signal name: {name}", "parse_error")
                signal = {"name": name, "kind": "digital", "unit": "logic", "time_unit": "tick",
                          "width": v.size, "range": v.bit_index, "id_code": v.id_code, "points": [], "source_point_count": 0, "first_tick": None}
                aliases = by_id.setdefault(v.id_code, [])
                if aliases and aliases[0]["width"] != v.size:
                    raise BridgeError("VCD alias widths disagree", "parse_error")
                aliases.append(signal)
                signals.append(signal)
            elif kind is K.ENDDEFINITIONS:
                if defined or scope:
                    raise BridgeError("VCD unbalanced/duplicate definitions", "parse_error")
                defined = True
            elif kind is K.CHANGE_TIME:
                if not defined or token.time_change < tick:
                    raise BridgeError("VCD unordered time or missing definitions", "parse_error")
                tick = token.time_change
                end_tick = tick
            elif kind in (K.CHANGE_SCALAR, K.CHANGE_VECTOR):
                if not defined:
                    raise BridgeError("VCD values before definitions", "parse_error")
                change = token.scalar_change if kind is K.CHANGE_SCALAR else token.vector_change
                aliases = by_id.get(change.id_code)
                if not aliases:
                    raise BridgeError(f"VCD undeclared id: {change.id_code}", "parse_error")
                width = aliases[0]["width"]
                value = format(change.value, "b") if type(change.value) is int else str(change.value).upper()
                if not re.fullmatch("[01XZ]+", value) or len(value) > width or (kind is K.CHANGE_SCALAR and width != 1):
                    raise BridgeError("VCD invalid four-state vector/width", "parse_error")
                value = value.rjust(width, value[0] if value[0] in "XZ" else "0")
                for signal in aliases:
                    budget.point()
                    signal["source_point_count"] += 1
                    if signal["first_tick"] is None:
                        signal["first_tick"] = tick
                    if budget.wanted({**signal, "timescale": scale}, tick):
                        signal["points"].append({"time": tick, "value": value, "order": ordinal,
                                                 "source_line": token.span.start.line})
                ordinal += 1
                end_tick = tick if end_tick is None else max(end_tick, tick)
            elif kind in (K.CHANGE_REAL, K.CHANGE_STRING):
                raise BridgeError("VCD real/string changes are unsupported", "unsupported_format")
    if not defined or scale is None:
        raise BridgeError("VCD requires complete definitions and explicit timescale", "parse_error")
    if not signals or not any(s["source_point_count"] for s in signals):
        raise BridgeError("VCD has no value changes", "empty_waveform")
    for signal in signals:
        signal.update(timescale=scale, coverage=[signal.pop("first_tick"), end_tick] if signal["source_point_count"] else None)
        signal["aliases"] = [s["name"] for s in by_id[signal["id_code"]] if s is not signal]
    return {"analysis": "tran", "signals": signals, "adapter": "pyvcd",
            "adapter_version": importlib.metadata.version("pyvcd"),
            "decode_scope": "full VCD token scan; selected event objects", "budget_scope": "input bytes, all source events including aliases, returned JSON bytes", "source_metadata": {"end_tick": end_tick}}


def decode(path, fmt, limits, selection=None):
    path = Path(path)
    if path.stat().st_size > limits["bytes"]:
        raise BridgeError("waveform input exceeds byte budget", "budget_exceeded")
    actual = detect(path)
    if fmt != actual:
        raise BridgeError(f"declared {fmt}, detected {actual}", "format_mismatch")
    budget = Budget(limits, selection)
    result = {"psf_ascii": ascii_psf, "vcd": vcd_data, "vcsv": vcsv_data}.get(fmt)
    if result is None:
        raise BridgeError(f"{fmt}: conversion required", "unsupported_format")
    data = result(path, budget)
    data["signals"] = [s for s in data["signals"] if not budget.selection.get("signals") or s["name"] in budget.selection["signals"]]
    data.update(parser_version=PARSER_VERSION, decoded_points=budget.points, materialized_points=budget.materialized)
    if len(canonical(data)) > limits["bytes"]:
        raise BridgeError("decoded waveform exceeds byte budget", "budget_exceeded")
    return data


def decode_many(paths, limits, selection=None):
    """Selected native VCSV exports share one request budget and process."""
    budget = Budget(limits, selection)
    signals, size = [], 0
    for name in paths:
        path = Path(name)
        try:
            size += path.stat().st_size
            if size > limits["bytes"]:
                raise BridgeError("native exports exceed total input byte budget", "budget_exceeded")
            if detect(path) != "vcsv":
                raise BridgeError("selected native export is not VCSV", "format_mismatch")
            signals.extend(vcsv_data(path, budget)["signals"])
        except (BridgeError, OSError, ValueError) as exc:
            raise BridgeError(f"{path}: {exc}", getattr(exc, "code", "parse_error")) from exc
    signals = [s for s in signals if not budget.selection.get("signals") or s["name"] in budget.selection["signals"]]
    data = {"analysis": "tran", "signals": signals, "adapter": "srrutil-vcsv",
            "parser_version": PARSER_VERSION, "decoded_points": budget.points,
            "materialized_points": budget.materialized,
            "decode_scope": "selected native traces; full source sample scans in one process",
            "budget_scope": "combined export bytes, all selected source points, returned JSON bytes"}
    if len(canonical(data)) > limits["bytes"]:
        raise BridgeError("decoded waveform exceeds total byte budget", "budget_exceeded")
    return data


def main():
    request = read_json(sys.argv[1])
    try:
        # Third party warnings must not corrupt the JSON protocol or write caches.
        with contextlib.redirect_stdout(sys.stderr):
            data = (decode_many(request["paths"], request["limits"], request.get("selection")) if "paths" in request else
                    decode(request["path"], request["format"], request["limits"], request.get("selection")))
        result = {"data": data}
    except (BridgeError, OSError, ValueError) as exc:
        result = {"error": str(exc), "code": getattr(exc, "code", "parse_error")}
    write_json(sys.argv[2], result)


if __name__ == "__main__":
    main()
