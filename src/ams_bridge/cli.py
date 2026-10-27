"""Thin CLI over the public Python API; stdout is always one JSON object."""
import argparse
import json
import sys
from pathlib import Path

from . import BridgeError, batch_cancel, batch_rerun, batch_resume, batch_start, batch_status, cancel, cleanup_apply, cleanup_plan, collect, import_unl, load_config, load_execution, logs, prepare, read_results, run_simulation, status, submit, usage, validate, waveforms
from .common import is_link, read_json, write_json


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise BridgeError(message)


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = Parser(prog="ams-bridge")
    commands = parser.add_subparsers(dest="operation", required=True)
    for operation in ("run", "validate", "prepare", "submit"):
        sub = commands.add_parser(operation)
        sub.add_argument("config")
        if operation == "prepare":
            sub.add_argument("--output", required=True)
        elif operation == "run":
            sub.add_argument("--wait-timeout-s", type=float)
        elif operation == "submit":
            sub.add_argument("--prepared", required=True)
            sub.add_argument("--job", help="resume the same job after an interrupted upload or uncertain submission")
    for operation in ("status", "cancel", "collect", "logs", "read-results"):
        sub = commands.add_parser(operation)
        sub.add_argument("job", help="job.json or its containing directory")
        if operation == "logs":
            sub.add_argument("--path")
            sub.add_argument("--limit-bytes", type=int, default=65536)
        elif operation == "status":
            sub.add_argument("--details", action="store_true")
    wave = commands.add_parser("waveforms")
    wave.add_argument("job")
    wave.add_argument("request")
    importer = commands.add_parser("import-unl")
    importer.add_argument("spec")
    importer.add_argument("--execution", required=True)
    importer.add_argument("--output", required=True)
    inventory = commands.add_parser("usage")
    inventory.add_argument("spec")
    cleanup = commands.add_parser("cleanup")
    cleanup_commands = cleanup.add_subparsers(dest="cleanup_operation", required=True)
    plan = cleanup_commands.add_parser("plan")
    plan.add_argument("spec")
    plan.add_argument("--output", required=True)
    apply = cleanup_commands.add_parser("apply")
    apply.add_argument("plan")
    batch = commands.add_parser("batch")
    batch_commands = batch.add_subparsers(dest="batch_operation", required=True)
    for operation in ("start", "status", "resume", "cancel", "rerun"):
        sub = batch_commands.add_parser(operation)
        if operation == "start":
            sub.add_argument("spec")
            sub.add_argument("--output", required=True)
        else:
            sub.add_argument("directory")
            if operation == "rerun":
                sub.add_argument("--entry", required=True)
    args = None
    try:
        args = parser.parse_args(argv)
        if args.operation == "cleanup" and args.cleanup_operation == "apply":
            result = cleanup_apply(read_json(args.plan))
        elif args.operation in ("usage", "cleanup"):
            spec = read_json(args.spec)
            spec["execution"] = load_execution(args.spec)
            base = Path(args.spec).absolute().parent
            if isinstance(spec.get("local_root"), str):
                spec["local_root"] = str((base / spec["local_root"]).absolute())
            if isinstance(spec.get("jobs"), list):
                spec["jobs"] = [str((base / p).absolute()) if isinstance(p, str) else p for p in spec["jobs"]]
            output = None
            if args.operation == "cleanup":
                output = Path(args.output).absolute()
                if output.exists() or any(is_link(p) for p in (output, *output.parents)):
                    raise BridgeError("cleanup plan output must be a new path without links")
                if any(output.is_relative_to(Path(p).absolute()) for p in spec.get("jobs", []) if isinstance(p, str)):
                    raise BridgeError("cleanup plan output must be outside selected job directories")
            result = usage(spec) if args.operation == "usage" else cleanup_plan(spec)
            if output is not None:
                write_json(output, result)
        elif args.operation == "batch":
            if args.batch_operation == "start":
                spec = read_json(args.spec)
                spec["execution"] = load_execution(args.spec)
                if isinstance(spec.get("entries"), list):
                    for entry in spec["entries"]:
                        if isinstance(entry, dict) and isinstance(entry.get("prepared"), str):
                            entry["prepared"] = str((Path(args.spec).resolve().parent / entry["prepared"]).absolute())
                result = batch_start(spec, args.output)
            elif args.batch_operation == "rerun":
                result = batch_rerun(args.directory, args.entry)
            else:
                result = {"status": batch_status, "resume": batch_resume, "cancel": batch_cancel}[args.batch_operation](args.directory)
        elif args.operation == "import-unl":
            result = import_unl(read_json(args.spec), load_execution(args.execution), args.output)
        elif args.operation == "run":
            result = run_simulation(args.config, **({"wait_timeout_s": args.wait_timeout_s} if args.wait_timeout_s is not None else {}))
        elif args.operation == "read-results":
            result = read_results(args.job)
        elif args.operation == "submit":
            result = submit(args.prepared, load_execution(args.config), job=args.job)
        elif args.operation in ("validate", "prepare"):
            config = load_config(args.config)
            if args.operation == "validate":
                result = validate(config["case"], config["execution"])
            elif args.operation == "prepare":
                result = prepare(config["case"], args.output)
        elif args.operation == "waveforms":
            result = waveforms(args.job, read_json(args.request))
        elif args.operation == "logs":
            result = logs(args.job, args.path, args.limit_bytes)
        elif args.operation == "status":
            result = status(args.job, details=args.details)
        else:
            result = {"cancel": cancel, "collect": collect}[args.operation](args.job)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        if args.operation == "usage":
            return 0 if result["status"] == "complete" else 3
        if args.operation == "waveforms" and result["status"] != "complete":
            return 2
        if args.operation == "cleanup":
            if args.cleanup_operation == "apply":
                codes = {r.get("error_code") for r in result["targets"]}
                return 0 if result["status"] == "completed" else 5 if codes & {"busy", "stale_plan"} else 3 if "communication" in codes else 4
            return 0 if all(t["eligible"] for t in result["targets"]) else 4
        if args.operation == "batch":
            if args.batch_operation in ("start", "resume", "rerun"):
                unavailable = result["state"] == "attention" or any(e["attempts"][-1].get("submission") == "failed" for e in result["entries"])
                return 3 if unavailable else 4 if not result["ok"] else 0
            if args.batch_operation == "cancel":
                requests = [v["request"] for v in result["cancel_request"]["results"].values()]
                return 5 if "busy" in requests else 3 if "unknown" in requests else 0
        if args.operation == "validate" and result["remote"] != "passed":
            return 3
        if args.operation == "submit" and result["submission"] != "confirmed":
            return 3
        if args.operation in ("run", "read-results") and not result["ok"]:
            return 4
        if args.operation == "collect" and (result["execution"]["state"] != "completed" or result["solver"]["status"] != "success" or result["collection"]["status"] != "complete"):
            return 4
        if args.operation == "cancel" and result["request"] == "busy":
            return 5
        return 0
    except (BridgeError, OSError) as exc:
        code = getattr(exc, "code", "local_io")
        error = {"error": str(exc), "code": code, "operation": getattr(args, "operation", "parse"), "message": str(exc)}
        if hasattr(exc, "details"):
            error["details"] = exc.details
        print(json.dumps(error, ensure_ascii=False))
        if getattr(args, "operation", None) in ("import-unl", "waveforms") and code in ("communication", "environment", "transfer_timeout", "control_timeout"):
            return 3
        return 5 if code in ("busy", "stale_plan") else 2


if __name__ == "__main__":
    sys.exit(main())
