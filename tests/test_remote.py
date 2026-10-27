"""Optional SSH smoke tests: submit/collect, cancellation and timeout."""
import json
import os
import shlex
import tempfile
import subprocess
import sys
import time
import unittest
from pathlib import Path
from datetime import datetime
from unittest.mock import patch
from ams_bridge import cancel, collect, load_config, prepare, status, submit
from ams_bridge.transport import SSH
from ams_bridge.common import digest


def succeeded(result):
    return result["execution"]["state"] == "completed" and result["solver"]["status"] == "success" and result["collection"]["status"] == "complete"

@unittest.skipUnless(os.environ.get("AMS_BRIDGE_TEST_CONFIG"), "set AMS_BRIDGE_TEST_CONFIG for SSH smoke tests")
class RemoteJobs(unittest.TestCase):
    def setUp(self):
        solver_probe = patch("ams_bridge.jobs._probe_solver", return_value="test workload; Cadence probe disabled in control tests\n")
        solver_probe.start()
        self.addCleanup(solver_probe.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "source"
        self.root.mkdir()
        config = load_config(os.environ["AMS_BRIDGE_TEST_CONFIG"])
        self.execution = dict(config["execution"], xrun_path="/bin/bash", wall_timeout_s=6)
        self.t = SSH(self.execution)
        self.case = {"schema_version": 2, "name": "control_test", "input_root": str(self.root),
                     "working_directory": ".", "arguments": ["workload.sh"], "entry_files": ["workload.sh"],
                     "dependency_mode": "explicit", "log_files": ["xrun.log"],
                     "artifacts": [{"path": "xrun.log", "required": True}, {"path": "m.csv", "required": True},
                                   {"path": "pid.txt", "required": False}]}
        self.handles = []
        self.addCleanup(self.stop_remaining)

    def stop_remaining(self):
        for h in self.handles:
            cancel(h)

    def launch(self, prefix="", transport=None):
        text = "echo $$ > pid.txt\n" + prefix + "\nprintf 'name,value,unit\\nv,1,V\\n' > m.csv\nprintf 'Starting analog simulation engine...\\nxcelium> run\\nSimulation complete via transient analysis stoptime at time 80 NS\\nspectre completes with 0 errors\\nTOOL: xrun Exiting on today\\n' > xrun.log\n"
        (self.root / "workload.sh").write_text(text, encoding="utf-8", newline="\n")
        prepared = self.base / ("prepared-" + str(len(self.handles)))
        prepare(self.case, prepared)
        original_hash = digest(self.root / "workload.sh")
        handle = submit(prepared, self.execution, transport=transport)
        self.assertEqual(original_hash, digest(self.root / "workload.sh"))
        self.handles.append(handle)
        return handle

    def finish(self, handle, seconds=15):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            observation = status(handle)
            if observation["state"] not in ("running", "unknown"):
                return observation
            time.sleep(.15)
        self.fail(f"job failed to finish: {observation}")

    def launch_waiting(self, prefix=""):
        self.execution["wall_timeout_s"] = 180
        return self.launch(prefix + "\nwhile test ! -f release; do sleep .2; done")

    def release(self, handle):
        self.t.run("touch -- " + shlex.quote(handle["remote_directory"] + "/work/release"))

    def assert_remote_exit_bound(self, handle, *, cancelled=False):
        control = shlex.quote(handle["remote_directory"] + "/control")
        command = "stat -c %Y -- cancel.request" if cancelled else "cat started_at"
        rows = self.t.run(f"set -eu\ncd -- {control}\n{command}\ncat finished_at\n").stdout.decode().splitlines()
        start = float(rows[0]) if cancelled else datetime.fromisoformat(rows[0]).timestamp()
        end = datetime.fromisoformat(rows[1]).timestamp()
        # Both endpoints round down to seconds. Adding one gives a conservative
        # remote elapsed upper bound, excluding later SSH observation overhead.
        upper = end + 1 - start - (0 if cancelled else self.execution["wall_timeout_s"])
        self.assertGreaterEqual(upper, 0)
        self.assertLessEqual(upper, 5)
        return {"start": rows[0], "finished_at": rows[1], "overshoot_upper_bound_s": upper}

    def test_success_resume_and_repeated_collection(self):
        handle = self.launch("sleep .5")
        self.assertEqual(handle["submission"], "confirmed", handle)
        fresh = subprocess.run([sys.executable, "-m", "ams_bridge", "status", handle["local_directory"]], capture_output=True, text=True)
        self.assertEqual(fresh.returncode, 0, fresh.stderr)
        self.assertEqual(json.loads(fresh.stdout)["run_id"], handle["run_id"])
        self.assertEqual(self.finish(Path(handle["local_directory"]) / "job.json")["state"], "completed")
        result = collect(handle)
        self.assertTrue(succeeded(result), result)
        self.assertEqual(collect(handle)["collection"], result["collection"])
        self.assertEqual(cancel(handle)["request"], "already_terminal")

    def test_cancel_is_request_and_does_not_cancel_other_job(self):
        one = self.launch_waiting()
        two = self.launch_waiting()
        self.assertEqual(cancel(one)["request"], "requested")
        self.assertEqual(self.finish(one)["state"], "cancelled")
        timing = self.assert_remote_exit_bound(one, cancelled=True)
        self.assertEqual(status(two)["state"], "running")
        self.release(two)
        self.assertEqual(self.finish(two)["state"], "completed")
        from ams_bridge.common import now, write_json
        write_json(Path(os.environ["AMS_BRIDGE_TEST_EVIDENCE_DIR"]) / ("cancel-timing.json"), {"recorded_at": now(), "run_id": one["run_id"],
                   "timing": timing, "unrelated_run_id": two["run_id"], "unrelated_survived": True, "passed": True})

    def test_disconnected_timeout_ignores_term_and_cleans_grandchild(self):
        self.execution["wall_timeout_s"] = .8
        handle = self.launch("trap '' TERM USR1\n(sleep 60 & wait) &\nwait")
        # No local polling or timer participates in enforcing the deadline.
        time.sleep(4)
        result = self.finish(handle)
        self.assertEqual(result["state"], "timed_out", result)
        timing = self.assert_remote_exit_bound(handle)
        data = self.t.run("cat " + shlex.quote(handle["remote_directory"] + "/work/pid.txt")).stdout.decode().strip()
        check = self.t.run(f"test ! -d /proc/{int(data)} || test \"$(ps -o stat= -p {int(data)})\" = Z", check=False)
        self.assertEqual(check.returncode, 0)
        self.assertFalse(succeeded(collect(handle)))
        from ams_bridge.common import now, write_json
        write_json(Path(os.environ["AMS_BRIDGE_TEST_EVIDENCE_DIR"]) / ("timeout-timing.json"), {"recorded_at": now(), "run_id": handle["run_id"],
                   "timing": timing, "state": result["state"], "pid_observed_gone_or_zombie": int(data), "passed": True})
