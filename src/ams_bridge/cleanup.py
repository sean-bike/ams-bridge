
"""Explicit directory deletion with job identity and operation exclusion."""
import shutil

from .common import BridgeError, is_link, local_file, now, operation_lock
from .records import record_version
from .storage import LOCAL_TARGETS, _spec, _terminal_guard
from .transport import SSH


def cleanup_apply(plan):
    record_version(plan, "cleanup_plan")
    spec, loaded = _spec(plan.get("spec"), planning=True)
    jobs = {h["run_id"]: (h, d) for h, _, d in loaded}
    targets = plan.get("targets")
    if not isinstance(targets, list):
        raise BridgeError("cleanup targets must be an array")
    for target in targets:
        if not isinstance(target, dict) or target.get("run_id") not in jobs or target.get("category") not in spec["categories"]:
            raise BridgeError("target not in selected jobs/categories", "unsafe_path")
        handle, directory = jobs[target["run_id"]]
        expected = (handle["remote_directory"] + "/work",) if target["category"] == "remote_work" else LOCAL_TARGETS[target["category"]]
        if target.get("job") != str(directory) or target.get("path") not in expected:
            raise BridgeError("target escapes job/category", "unsafe_path")
    outcomes = []
    for target in targets:
        handle, directory = jobs[target["run_id"]]
        outcome = {**target, "status": "error"}
        try:
            if not target.get("eligible"):
                raise BridgeError("preview did not admit target", "ineligible")
            with operation_lock(directory):
                t = SSH(spec["execution"])
                with t.lease(handle["remote_directory"] + "/control"):
                    script = _terminal_guard(handle, spec["execution"])
                    if target["category"] == "remote_work":
                        script += 'p="$job/work"\ntest ! -L "$p" || exit 93\n'
                        script += 'if test ! -e "$p"; then printf already_absent; else\n'
                        script += 'test -d "$p" && test "$(realpath -e -- "$p")" = "$job/work" || exit 93\n'
                        script += 'rm -rf -- "$p" || exit 96\ntest ! -e "$p" || exit 96\nprintf deleted\nfi\n'
                    response = t.run(script, check=False, transfer=target["category"] == "remote_work")
                    if response.returncode:
                        code = {93: "unsafe_path", 94: "identity_mismatch", 95: "execution_not_terminal", 96: "partial_delete"}.get(response.returncode, "remote_error")
                        raise BridgeError("cleanup rejected or incomplete: " + str(response.returncode), code)
                    if target["category"] == "remote_work":
                        outcome["status"] = response.stdout.decode().strip()
                        if outcome["status"] not in ("deleted", "already_absent"):
                            raise BridgeError("cleanup response not confirmed", "invalid_remote_record")
                    else:
                        path = local_file(directory, target["path"])
                        if is_link(path):
                            raise BridgeError("linked target root refused", "unsafe_path")
                        if not path.exists():
                            outcome["status"] = "already_absent"
                        else:
                            if path.resolve().parent != directory.resolve():
                                raise BridgeError("target escaped job", "unsafe_path")
                            shutil.rmtree(path)
                            outcome["status"] = "deleted"
        except (BridgeError, OSError) as exc:
            outcome.update(status="partial" if getattr(exc, "code", None) == "partial_delete" else "error",
                           error=str(exc), error_code=getattr(exc, "code", "local_io"))
        outcomes.append(outcome)
    return {"status": "completed" if all(t["status"] in ("deleted", "already_absent") for t in outcomes) else "partial", "targets": outcomes, "observed_at": now()}
