"""Input snapshots, dependency declarations, isolation and CLI preparation."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from ams_bridge import BridgeError, prepare, verify_prepared
from ams_bridge.cli import main
from ams_bridge.config import case_spec, execution_config, load_config


class Inputs(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "case"
        self.root.mkdir()
        self.write("top.sv", 'module top; initial begin $finish; end endmodule\n')
        self.case = {"schema_version": 2, "name": "test", "input_root": str(self.root),
                     "working_directory": ".", "arguments": ["-sv", "top.sv"], "entry_files": ["top.sv"],
                     "dependency_mode": "resolve", "artifacts": [{"path": "xrun.log", "required": True}],
                     "log_files": ["xrun.log"]}

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def freeze(self):
        return prepare(self.case, self.base / "frozen")

    def test_snapshot_survives_source_changes_and_source_removal(self):
        record = self.freeze()
        expected = (self.base / "frozen/inputs/top.sv").read_bytes()
        self.write("top.sv", "changed")
        self.assertEqual((self.base / "frozen/inputs/top.sv").read_bytes(), expected)
        (self.root / "top.sv").unlink()
        self.assertEqual(verify_prepared(self.base / "frozen")["fingerprint"], record["fingerprint"])

    def test_corrupted_snapshot_rejected(self):
        self.freeze()
        (self.base / "frozen/inputs/top.sv").write_text("corrupt")
        with self.assertRaisesRegex(BridgeError, "fingerprint mismatch"):
            verify_prepared(self.base / "frozen")

    def test_f_uses_invocation_base_with_spaces_comments_and_continuation(self):
        self.write("lists/args.f", '# comment\n"source space/top.sv" // comment\n\\\n +define+NAME=1\n')
        self.write("source space/top.sv", "module a; endmodule")
        self.case.update(arguments=["-f", "lists/args.f"], entry_files=["lists/args.f"])
        record = self.freeze()
        self.assertIn("source space/top.sv", [x["path"] for x in record["files"]])
        self.assertEqual(record["case"]["arguments"], self.case["arguments"])

    def test_filelist_and_include_cycles_show_chain(self):
        self.write("loop.f", "-f loop.f")
        self.case.update(arguments=["-f", "loop.f"], entry_files=["loop.f"])
        with self.assertRaisesRegex(BridgeError, "cycle.*loop.f"):
            self.freeze()

    def test_explicit_dependency_list_is_user_declared(self):
        self.write("top.sv", '`include "extra.svh"')
        self.write("extra.svh", "wire a;")
        self.case["dependency_mode"] = "explicit"
        self.assertEqual([f["path"] for f in self.freeze()["files"]], ["top.sv"])

    def test_output_paths_in_arguments_and_source(self):
        for option in (["-log", "/old/run.log"], ["-xmlibdirname", "../shared"]):
            with self.subTest(option=option):
                self.case["arguments"] = ["top.sv", *option]
                with self.assertRaises(BridgeError):
                    self.freeze()
        self.case["arguments"] = ["top.sv"]
        self.write("top.sv", 'initial fd = $fopen("/old/measurements.csv", "w");')
        with self.assertRaisesRegex(BridgeError, "absolute output"):
            self.freeze()

    def test_validation_rejects_invalid_contracts(self):
        for change in ({"schema_version": True}, {"schema_version": 99}, {"entry_files": []},
                       {"working_directory": "../other"}, {"unknown": True}):
            with self.subTest(change=change), self.assertRaises(BridgeError):
                case_spec({**self.case, **change})
        e = {"ssh_host": "simulation-server", "remote_root": "/tmp/ams", "local_output_root": str(self.base / "runs"), "xrun_path": "xrun"}
        for change in ({"wall_timeout_s": 0}, {"wall_timeout_s": float("nan")}, {"wall_timeout_s": True},
                       {"ssh_host": "-oProxyCommand=bad"}, {"environment": {"A;bad": "value"}}):
            with self.subTest(change=change), self.assertRaises(BridgeError):
                execution_config({**e, **change})

    def test_cli_prepare_and_config_relative_paths(self):
        e = {"ssh_host": "simulation-server", "remote_root": "/tmp/ams", "local_output_root": "runs", "xrun_path": "xrun"}
        c = dict(self.case, input_root="case")
        path = self.base / "config.json"
        path.write_text(json.dumps({"case": c, "execution": e}))
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = main(["prepare", str(path), "--output", str(self.base / "frozen")])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stream.getvalue())["case"]["input_root"], str(self.root.resolve()))
        self.assertEqual(load_config(path)["execution"]["local_output_root"], str((self.base / "runs").resolve()))
