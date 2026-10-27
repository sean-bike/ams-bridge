"""Solver evidence and high-level run behavior; all inputs are synthetic."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ams_bridge import BridgeError, prepare, run_simulation
from ams_bridge.common import digest, read_json, write_json
from ams_bridge.records import metadata, RESULT_RULES_VERSION
from ams_bridge.results import classify_logs, native_progress
from ams_bridge.simulation import read_outputs


FINISH = ("Starting analog simulation engine...\nxcelium> run\n"
          "Simulation complete via $finish(1) at time 65 NS + 0\n"
          "spectre completes with 0 errors\nTOOL: xrun Exiting\n")


def native_logs(label):
    completion = ("Simulation complete via $finish(1) at time 65 NS + 0\n" if label == "finish"
                  else "Simulation complete via transient analysis stoptime at time 65 NS\n")
    launch = ("Spectre_AMSD_Lk 16.10 - license checkout failed\n"
              "Time for NDB Parsing: elapsed 1 s\n"
              "input.scs:1:0: fatal error: when writing output to : Broken pipe\n"
              "Starting analog simulation engine...\nxcelium> run\n"
              "Transient Analysis tran\n" + completion + "spectre completes with 0 errors\n")
    xrun = "Spectre_AMSD_Lk 16.10 - license checkout failed\n" + FINISH
    return {"xrun.log": xrun, "control/launch.log": launch}


class NativeLogs(unittest.TestCase):
    def test_no_mode_license_negotiation_requires_later_same_run_completion(self):
        logs = native_logs("finish")
        result = classify_logs({"xrun.log": logs["xrun.log"]})
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["confirmed_settings"]["ams_mode"], "unconfirmed")
        self.assertTrue(result["license_negotiation"])
        prefix = "Spectre_AMSD_Lk 16.10 - license checkout failed\n"
        for text in [prefix, prefix + FINISH.split("spectre completes")[0],
                     prefix + FINISH + "xmsim: *E,NOLIC: license unavailable\n",
                     FINISH + "TOOL: xrun Started on tomorrow\n" + prefix]:
            self.assertNotEqual(classify_logs({"xrun.log": text})["status"], "success")

    def test_finish_levels_and_native_error_priority(self):
        for level in (0, 1, 2):
            text = FINISH.replace("$finish(1)", f"$finish({level})")
            result = classify_logs({"xrun.log": text})
            self.assertEqual(result["status"], "success", result)
            progress = native_progress([{"path": "xrun.log", "text": text, "truncated": False}])
            self.assertEqual(progress["simulation_time"]["value"], 65)
            self.assertEqual(progress["simulation_time"]["unit"], "ns")
        for text in [FINISH.replace("$finish(1)", "$finish(3)"),
                     "Simulation complete via $finish(1) at time 65 NS + 0\n",
                     FINISH + "xmsim: *E,ASRTST: assertion failed\n",
                     FINISH + "FATAL: runtime error\n"]:
            self.assertNotEqual(classify_logs({"xrun.log": text})["status"], "success")

    def test_known_pipe_warning_needs_complete_logs_and_confirmed_exit(self):
        for label in ("finish", "stoptime"):
            logs = native_logs(label)
            result = classify_logs(logs, execution={"state": "completed", "exit_code": 0})
            self.assertEqual(result["status"], "success", result["errors"])
            warning = result["warnings"][0]
            self.assertEqual(warning["code"], "native_preprocess_pipe_closed")
            self.assertEqual(warning["phase"], "preprocess")
            self.assertEqual(warning["path"], "control/launch.log")
            self.assertIn("fatal error", warning["text"])
            self.assertEqual(warning["native_location"]["line"], 1)
            self.assertEqual(warning["native_location"]["column"], 0)

    def test_pipe_is_not_a_general_ignore_rule(self):
        original = native_logs("stoptime")
        for observation in [None, {"state": "completed", "exit_code": 1},
                            {"state": "timed_out", "exit_code": 124}, {"state": "cancelled", "exit_code": 143}]:
            self.assertNotEqual(classify_logs(original, execution=observation)["status"], "success")
        for launch in [original["control/launch.log"].replace("Time for NDB Parsing:", "No parsing evidence:"),
                       original["control/launch.log"].replace("when writing output to : Broken pipe", "invalid C syntax"),
                       original["control/launch.log"] + "xmvlog: *E,FAIL: genuine compiler error\n",
                       original["control/launch.log"].split("spectre completes")[0],
                       original["control/launch.log"].replace("fatal error: when writing output", "fatal error: other output")]:
            logs = {**original, "control/launch.log": launch}
            self.assertNotEqual(classify_logs(logs, execution={"state": "completed", "exit_code": 0})["status"], "success")
        pipe = "input.scs:1:0: fatal error: when writing output to : Broken pipe\n"
        logs = {**original, "control/launch.log": "Time for NDB Parsing: elapsed 1 s\nxcelium> run\n" + pipe + FINISH}
        self.assertNotEqual(classify_logs(logs, execution={"state": "completed", "exit_code": 0})["status"], "success")

VCD = '''$timescale 1 ps $end
$scope module tb $end
$var wire 4 ! bus [3:0] $end
$var wire 4 ! alias [0:3] $end
$var reg 1 # d $end
$upscope $end
$enddefinitions $end
#0
$dumpvars
b0 !
0#
$end
#5
b10xz !
1#
0#
#6
bz !
#10
b1111 !
#20
'''

class Simulation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def job(self, text=VCD):
        inputs = self.root / "input"
        inputs.mkdir()
        (inputs / "top.sv").write_text("module top; endmodule")
        case = {"schema_version": 2, "name": "wave", "input_root": str(inputs), "arguments": ["top.sv"],
                "entry_files": ["top.sv"], "dependency_mode": "resolve", "artifacts": [{"path": "wave.data", "required": True}]}
        prepared = prepare(case, self.root / "prepared")
        directory = self.root / "job"
        directory.mkdir()
        run = "a" * 32
        fp = prepared["fingerprint"]
        handle = {**metadata("job", run, fp), "local_directory": str(directory), "remote_directory": "/tmp/waves/" + run,
                  "ssh_host": "unused", "submission": "confirmed"}
        request = {**metadata("request", run, fp), "prepared": prepared,
                   "execution": {"ssh_host": "unused", "remote_root": "/tmp/waves", "local_output_root": str(self.root),
                                 "xrun_path": "xrun", "environment": {}}}
        path = directory / "artifacts/wave.data"
        path.parent.mkdir()
        path.write_text(text)
        result = {**metadata("result", run, fp), "execution": {"state": "completed"}, "solver": {"status": "insufficient_evidence"},
                  "rules_version": RESULT_RULES_VERSION,
                  "collection": {"status": "complete", "final": True, "artifacts": [
                      {"path": "wave.data", "status": "verified", "local_path": str(path), "size": path.stat().st_size, "sha256": digest(path)}]}}
        write_json(directory / "job.json", handle)
        write_json(directory / "request.json", request)
        write_json(directory / "result.json", result)
        return directory, path

    def fixture(self):
        directory, source = self.job()
        request = read_json(directory / "request.json")
        request["prepared"]["case"]["waveforms"] = [{"id": "bus", "artifact": "wave.data", "analysis": "tran", "signals": ["tb.bus"], "required": True}]
        write_json(directory / "request.json", request)
        result = read_json(directory / "result.json")
        result["solver"]["status"] = "success"
        write_json(directory / "result.json", result)
        return directory, source, request, result

    def test_run_once_returns_raw_data_without_metrics(self):
        directory, _, request, collected = self.fixture()
        handle = read_json(directory / "job.json")
        config = {"case": request["prepared"]["case"], "execution": request["execution"]}
        with patch("ams_bridge.simulation.prepare"), patch("ams_bridge.simulation.submit", return_value=handle) as submit, patch("ams_bridge.simulation.status", return_value={"state": "completed"}), patch("ams_bridge.simulation.collect", return_value=collected):
            result = run_simulation(config)
        self.assertTrue(result["ok"], result)
        submit.assert_called_once()
        self.assertEqual(result["data"]["digital"][0]["points"][1]["value"], "10XZ")
        self.assertEqual(result["simulation"]["status"], "success")
        for key in ("checks", "validation", "passed", "measurements"):
            self.assertNotIn(key, json.dumps(result))
        self.assertFalse((directory / "evaluations").exists())
        # User post-processing cannot alter bridge execution success.
        user_spec_passes = False
        self.assertFalse(user_spec_passes)
        self.assertTrue(result["ok"])

    def test_unknown_and_client_expiry_keep_original_identity(self):
        directory, _, request, _ = self.fixture()
        handle = read_json(directory / "job.json")
        for observation, expected in (({"state": "unknown"}, "execution_unknown"), ({"state": "running"}, "wait_timeout")):
            with self.subTest(expected=expected), patch("ams_bridge.simulation.prepare"), patch("ams_bridge.simulation.submit", return_value=handle) as submit, patch("ams_bridge.simulation.status", return_value=observation), patch("ams_bridge.simulation.collect", side_effect=AssertionError("unconfirmed run collected")):
                result = run_simulation({"case": request["prepared"]["case"], "execution": request["execution"]}, wait_timeout_s=.001)
            self.assertFalse(result["ok"])
            self.assertEqual(result["job"]["run_id"], handle["run_id"])
            self.assertEqual(result["errors"][0]["code"], expected)
            self.assertEqual(result["simulation"]["status"], observation["state"])
            submit.assert_called_once()

    def test_required_missing_and_short_curves_preserve_valid_data(self):
        directory, _, request, collected = self.fixture()
        base = request["prepared"]["case"]["waveforms"][0]
        request["prepared"]["case"]["waveforms"].extend([
            {**base, "id": "missing", "signals": ["not_saved"]},
            {**base, "id": "short", "coverage": {"start_s": 0, "stop_s": 30e-12, "atol_s": 0}}])
        write_json(directory / "request.json", request)
        result = read_outputs(directory, collected)
        self.assertFalse(result["ok"])
        self.assertEqual(result["data"]["status"], "partial")
        self.assertEqual(len(result["data"]["digital"]), 1)
        self.assertEqual([e["code"] for e in result["errors"]], ["missing_signal", "out_of_range"])
        request["prepared"]["case"]["waveforms"][-1]["required"] = False
        request["prepared"]["case"]["waveforms"][-2]["required"] = False
        write_json(directory / "request.json", request)
        optional = read_outputs(directory, collected)
        self.assertTrue(optional["ok"])
        self.assertEqual(len(optional["warnings"]), 2)

    def test_solver_warning_preserved_and_missing_data_still_fails(self):
        from unittest.mock import patch
        from ams_bridge import BridgeError
        directory, _, _, collected = self.fixture()
        collected["execution"].update(state="completed", exit_code=0)
        collected["solver"] = classify_logs(native_logs("stoptime"), execution=collected["execution"])
        result = read_outputs(directory, collected)
        self.assertTrue(result["ok"])
        self.assertTrue(any(w["code"] == "native_preprocess_pipe_closed" for w in result["warnings"]))
        with patch("ams_bridge.simulation.waveforms", side_effect=BridgeError("missing signal", "missing_signal")):
            result = read_outputs(directory, collected)
        self.assertEqual(result["simulation"]["status"], "success")
        self.assertFalse(result["ok"])
        self.assertFalse(result["data"]["required_complete"])
