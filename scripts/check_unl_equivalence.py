"""Verify the predeclared startup equivalence of imported and original sources."""
import argparse
import csv
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ams_bridge.collection import resolve_file
from ams_bridge.common import BridgeError, digest, read_json, write_json, now
from ams_bridge.jobs import _load, _external_check
from ams_bridge.transport import SSH
from ams_bridge.waveforms import current_result


def table(path):
    with Path(path).open(newline="", encoding="utf-8") as stream:
        rows = list(csv.reader(stream, strict=True))
    if not rows or not rows[0] or len(set(rows[0])) != len(rows[0]):
        raise BridgeError(f"{path}: invalid CSV header")
    header, repeated, data = rows[0], 0, []
    for row in rows[1:]:
        if row == header:
            repeated += 1
        elif len(row) != len(header):
            raise BridgeError(f"{path}: invalid CSV width")
        else:
            data.append(dict(zip(header, row)))
    return header, repeated, data


def number(value):
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise BridgeError("invalid comparison number") from exc
    if not result.is_finite():
        raise BridgeError("non-finite comparison value")
    return result


def compare_tables(actual, reference, rule, declaration):
    ah, ar, aa = table(actual)
    bh, br, bb = table(reference)
    failures = []
    if ah != bh or ar != br or len(aa) != len(bb) or (rule["require_nonempty"] and not aa):
        failures.append("header/repeated-header/count mismatch or empty data")
    maximum = {}
    for index, (a, b) in enumerate(zip(aa, bb), 1):
        for column in ah:
            if column == rule["time_column"] or column in rule.get("numeric_columns", []):
                tolerance = (declaration["time_s_atol"] if column == "time_s" else
                             declaration["time_ps_atol"] if column == "time_ps" else declaration["analog_phase_ps_atol"])
                error = abs(number(a[column]) - number(b[column]))
                maximum[column] = max(maximum.get(column, Decimal(0)), error)
                same = error <= number(str(tolerance))
            else:
                same = a[column] == b[column]
            if not same:
                failures.append(f"row {index} {column}: {a[column]} != {b[column]}")
    return {"passed": not failures, "rows": len(aa), "reference_rows": len(bb), "repeated_headers": ar,
            "maximum_error": {k: str(v) for k, v in maximum.items()}, "failures": failures,
            "unknown_rows_compared": sum(any(v.lower() in ("x", "z") for v in row.values()) for row in aa)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("import_directory")
    parser.add_argument("job")
    parser.add_argument("reference_job")
    parser.add_argument("--declaration", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    root = Path(args.import_directory)
    report = read_json(root / "inputs/import-report.json")
    declaration = read_json(args.declaration)
    handle, request, directory = _load(args.job)
    rh, rr, rd = _load(args.reference_job)
    actual, result_path = current_result(directory, handle)
    reference, reference_path = current_result(rd, rh)
    selected = read_json(rd / "reference.json")
    checks = []
    def check(name, passed, **details):
        checks.append({"name": name, "passed": bool(passed), **details})
    for name, result in (("imported", actual), ("reference", reference)):
        check(name + ":execution", result["execution"]["state"] == "completed" and result["solver"]["status"] == "success" and
              result["collection"]["status"] == "complete" and result["collection"].get("final") is True)
    pin_map = {e["path"]: e["sha256"] for e in selected["external_dependencies"]}
    source_checks = []
    for entry in report["files"]:
        original = root / "inputs" / entry["original"]
        adapted = root / "inputs" / entry["path"]
        pinned = pin_map.get(entry["source"], pin_map.get(entry["canonical_source"]))
        passed = pinned == entry["sha256"] == digest(original) and digest(adapted) == entry["adapted_sha256"]
        if not entry["changed"]:
            passed = passed and digest(adapted) == pinned
        source_checks.append({"source": entry["source"], "passed": passed, "changed": entry["changed"]})
    check("all original sources match independent reference pins", all(e["passed"] for e in source_checks), files=source_checks)
    check("external model pins", all(pin_map.get(e["path"]) == e["sha256"] for e in report["external_dependencies"]),
          models=report["external_dependencies"])
    # Before/after pin checks concern actual original paths, not captured substitutions.
    try:
        SSH(rr["execution"]).run(_external_check(rr["prepared"]["case"]))
        check("original source pins still unchanged after both runs", True)
    except BridgeError as exc:
        check("original source pins still unchanged after both runs", False, error=str(exc))
    for entry in report["files"]:
        old = (root / "inputs" / entry["original"]).read_text(encoding="utf-8")
        new = (root / "inputs" / entry["path"]).read_text(encoding="utf-8")
        name = Path(entry["path"]).name
        if entry["source"].endswith(("checker.f", "coefficient_overrides.f")):
            pattern = r"(?m)^\s*-defparam\s+([^\s]+)"
            check(name + ":ordered defparams", re.findall(pattern, old) == re.findall(pattern, new))
        if name == "amsControlSpectre.scs":
            normalize = lambda text: re.sub(r"\bstop\s*=\s*[^\s\\]+", "stop=<declared>", text)
            check("unchanged native solver controls except explicit stop", normalize(old) == normalize(new))
        if name == "spiceModels.scs":
            normalize = lambda text: re.sub(r'(?m)(^\s*include\s+")[^"\n]+', r'\1<path>', text)
            check("unchanged model sections and binding flags", normalize(old) == normalize(new))
        if name == "probe.tcl":
            pattern = r"(?m)^probe[^\n\r]*"
            check("unchanged ordered native probes", re.findall(pattern, old) == re.findall(pattern, new))
        if name == "xrunArgs":
            match = lambda text: re.findall(r"-amsformat\s+([^\s]+)", text)
            check("unchanged native waveform format", match(old) == match(new) == ["psfxl_all"])
    check("same actual solver settings", actual["solver"]["confirmed_settings"] == reference["solver"]["confirmed_settings"],
          imported=actual["solver"]["confirmed_settings"], reference=reference["solver"]["confirmed_settings"])
    tables = {}
    for name, rule in declaration["tables"].items():
        try:
            a = resolve_file(actual["collection"]["artifacts"], "netlist/" + name)
            b = resolve_file(reference["collection"]["artifacts"], "netlist/" + name)
            outcome = compare_tables(a, b, rule, declaration)
        except (OSError, ValueError, BridgeError, csv.Error) as exc:
            outcome = {"passed": False, "error": str(exc)}
        tables[name] = outcome
        check(name + ":equivalence", outcome["passed"])
    output = {"recorded_at": now(), "run_id": handle["run_id"], "reference_run_id": rh["run_id"],
              "declaration_sha256": digest(Path(args.declaration)), "import_report_sha256": digest(root / "inputs/import-report.json"),
              "result_sha256": digest(result_path), "reference_result_sha256": digest(reference_path),
              "reference_method_sha256": digest(rd / "reference.json"), "checks": checks, "tables": tables,
              "passed": all(c["passed"] for c in checks), "scope": declaration["scope"]}
    write_json(args.output, output)
    print({"passed": output["passed"], "tables": tables, "failed_checks": [c["name"] for c in checks if not c["passed"]]})
    return 0 if output["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
