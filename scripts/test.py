"""Run unittest and save a machine-readable record; skips are never passes."""
import argparse
import importlib.metadata
import os
import sys
import unittest
from collections import Counter
from pathlib import Path

from ams_bridge.common import digest, now, write_json
from ams_bridge import __version__


class RecordedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cases = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.cases.append({"test": test.id(), "status": "passed"})

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.cases.append({"test": test.id(), "status": "skipped", "reason": reason})

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.cases.append({"test": test.id(), "status": "failed", "reason": self._exc_info_to_string(err, test)})

    def addError(self, test, err):
        super().addError(test, err)
        self.cases.append({"test": test.id(), "status": "error", "reason": self._exc_info_to_string(err, test)})

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self.cases.append({"test": subtest.id(), "status": "failed" if issubclass(err[0], test.failureException) else "error",
                               "reason": self._exc_info_to_string(err, test)})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="runs/test-results.json")
    parser.add_argument("--pattern", default="test_*.py")
    parser.add_argument("--match", action="append", help="unittest name wildcard; may be repeated")
    args = parser.parse_args()
    os.environ.setdefault("AMS_BRIDGE_TEST_EVIDENCE_DIR", str(Path(str(Path(args.output).with_suffix("")) + "-artifacts").resolve()))
    project = Path(__file__).resolve().parents[1]
    def snapshot():
        paths = [*(project / "src/ams_bridge").glob("*.py"), *(project / "tests").glob("*.py"),
                 *(project / "scripts").glob("*.py"), project / "pyproject.toml"]
        return {p.relative_to(project).as_posix(): digest(p) for p in sorted(paths)}
    hashes = snapshot()
    loader = unittest.TestLoader()
    loader.testNamePatterns = args.match
    suite = loader.discover(str(project / "tests"), pattern=args.pattern)
    result = unittest.TextTestRunner(verbosity=2, resultclass=RecordedResult).run(suite)
    unchanged = snapshot() == hashes
    dependencies = {}
    for name in ("psf-utils", "pyvcd"):
        try:
            dependencies[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            dependencies[name] = None
    write_json(args.output, {"recorded_at": now(), "bridge_version": __version__, "python": sys.version, "platform": sys.platform,
                             "ssh_integration_enabled": bool(os.environ.get("AMS_BRIDGE_TEST_CONFIG")),
                             "evidence_directory": os.environ["AMS_BRIDGE_TEST_EVIDENCE_DIR"],
                             "pattern": args.pattern,
                             "match": args.match,
                             "tests_run": result.testsRun, "successful": result.wasSuccessful() and unchanged,
                             "source_sha256": hashes, "sources_unchanged": unchanged,
                             "optional_dependencies": dependencies,
                             "counts": dict(Counter(case["status"] for case in result.cases)), "cases": result.cases})
    return 0 if result.wasSuccessful() and unchanged else 1


if __name__ == "__main__":
    raise SystemExit(main())
