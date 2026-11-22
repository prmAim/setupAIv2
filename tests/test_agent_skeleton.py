"""Hermetic tests for the tracer-bullet prototype: throwaway git repo, no network."""

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
import agent_skeleton as skeleton

EXISTING_TEST = '''import unittest


def expected_total():
    return 2


class ExistingTests(unittest.TestCase):
    def test_addition(self):
        self.assertEqual(1 + 1, 2)

    def test_truth(self):
        self.assertTrue(True)
'''

GOOD_FUNCTION = '''"""Harmless helper used by the tracer-bullet prototype run."""


def ping(name: str = "target") -> str:
    """Return a deterministic greeting without side effects."""
    return f"pong:{name}"
'''

GOOD_TEST = '''import unittest

from tools.smoke_ping import ping


class SmokePingTests(unittest.TestCase):
    def test_ping_is_deterministic(self):
        self.assertEqual(ping("tracer"), "pong:tracer")
'''


class GraphFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "target"
        (self.repo / "tests").mkdir(parents=True)
        (self.repo / "tools").mkdir()
        (self.repo / "tests" / "test_existing.py").write_text(EXISTING_TEST, encoding="utf-8")
        (self.repo / "README.md").write_text("fixture\n", encoding="utf-8")
        (self.repo / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
        self.git("init", "-q")
        self.git("config", "user.email", "t13@example.invalid")
        self.git("config", "user.name", "t13 fixture")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "fixture")
        self.sha = self.git("rev-parse", "HEAD").strip()
        self.evidence = self.root / "evidence"
        self.state_db = self.root / "state" / "graph.sqlite"

    def git(self, *args, check=True):
        return subprocess.run(
            ["git", *args], cwd=self.repo, capture_output=True, text=True, check=check
        ).stdout

    def build_task(
        self,
        *,
        patch,
        fix_patches=None,
        forbidden_paths=None,
        test_roots=None,
        task_id="t13-fixture",
    ):
        payload = {
            "task_id": task_id,
            "repo": str(self.repo),
            "baseline_sha": self.sha,
            "patch": patch,
            "fix_patches": fix_patches or [],
            "lint_command": [sys.executable, "-m", "compileall", "-q", "tests", "tools"],
            "test_command": [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"],
        }
        if forbidden_paths is not None:
            payload["forbidden_paths"] = forbidden_paths
        if test_roots is not None:
            payload["test_roots"] = test_roots
        return skeleton.TaskSpec.model_validate(payload)

    def run_graph(self, task, **kwargs):
        settings = skeleton.SkeletonSettings(
            _env_file=None,
            tracing=False,
            evidence_root=self.evidence,
            state_db=self.state_db,
        )
        return skeleton.run_graph(task=task, settings=settings, **kwargs)

    def report(self, task_id="t13-fixture"):
        path = self.evidence / task_id / "report.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def bundle(self, task_id="t13-fixture"):
        return self.evidence / task_id

class GateTests(GraphFixture):
    def test_good_patch_passes_all_gates(self):
        task = self.build_task(
            patch={
                "tools/smoke_ping.py": GOOD_FUNCTION,
                "tests/test_smoke_ping.py": GOOD_TEST,
            }
        )
        report = self.run_graph(task)
        self.assertEqual(report["verdict"], "pass")
        self.assertEqual(report["engine"], "fake")
        self.assertIsNone(report["cli_version"])
        self.assertEqual(report["baseline_sha"], self.sha)
        self.assertEqual(report["attempts"], 0)
        self.assertEqual(report["evidence_writes"], 1)
        gates = report["gates"]
        self.assertEqual(sorted(gates), ["diff-scope", "lint", "test-integrity", "tests"])
        for name, gate in gates.items():
            self.assertEqual(gate["exit_code"], 0, name)
            self.assertTrue(gate["passed"], name)
        bundle = self.bundle()
        for name in (
            "patch.diff",
            "patch.sha256",
            "diff-name-only.txt",
            "tests.log",
            "report.json",
            "gate-diff-scope.json",
            "gate-lint.json",
            "gate-tests.json",
            "gate-test-integrity.json",
        ):
            self.assertTrue((bundle / name).is_file(), name)
        patch_bytes = (bundle / "patch.diff").read_bytes()
        digest = skeleton.sha256_bytes(patch_bytes)
        self.assertEqual((bundle / "patch.sha256").read_text(encoding="utf-8").strip(), digest)
        self.assertEqual(report["patch_sha256"], digest)
        self.assertIn("+def ping", patch_bytes.decode("utf-8"))
        names = sorted((bundle / "diff-name-only.txt").read_text(encoding="utf-8").split())
        self.assertEqual(names, ["tests/test_smoke_ping.py", "tools/smoke_ping.py"])

    def test_test_integrity_blocks_modified_existing_test(self):
        modified = EXISTING_TEST.replace("self.assertEqual(1 + 1, 2)", "self.assertEqual(1 + 1, 3)")
        task = self.build_task(patch={"tests/test_existing.py": modified})
        report = self.run_graph(task)
        self.assertEqual(report["verdict"], "escalated")
        gate = report["gates"]["test-integrity"]
        self.assertNotEqual(gate["exit_code"], 0)
        self.assertFalse(gate["passed"])
        self.assertEqual(gate["rule"], "test-body-modified")
        self.assertIn("test-body-modified: tests/test_existing.py", gate["detail"])
        self.assertEqual(
            gate["findings"],
            [
                {
                    "file": "tests/test_existing.py",
                    "rule": "test-body-modified",
                    "evidence": "test ExistingTests.test_addition",
                }
            ],
        )
        self.assertEqual(report["gates"]["tests"]["command"], ["not-run"])

    def test_test_integrity_blocks_modified_helper(self):
        modified = EXISTING_TEST.replace("return 2", "return 3")
        task = self.build_task(patch={"tests/test_existing.py": modified})
        report = self.run_graph(task)
        self.assertEqual(report["verdict"], "escalated")
        gate = report["gates"]["test-integrity"]
        self.assertFalse(gate["passed"])
        self.assertEqual(gate["rule"], "helper-modified")
        self.assertIn("helper-modified: tests/test_existing.py", gate["detail"])

    def test_test_integrity_blocks_deleted_test(self):
        deleted = EXISTING_TEST.replace(
            "\n    def test_truth(self):\n        self.assertTrue(True)\n", ""
        )
        self.assertNotEqual(deleted, EXISTING_TEST)
        task = self.build_task(patch={"tests/test_existing.py": deleted})
        report = self.run_graph(task)
        self.assertEqual(report["verdict"], "escalated")
        gate = report["gates"]["test-integrity"]
        self.assertFalse(gate["passed"])
        self.assertEqual(gate["rule"], "test-removed")
        self.assertIn("test-removed: tests/test_existing.py", gate["detail"])

    def test_adding_new_test_to_existing_file_is_allowed(self):
        extended = EXISTING_TEST + (
            "\n    def test_added_later(self):\n        self.assertTrue(True)\n"
        )
        task = self.build_task(patch={"tests/test_existing.py": extended})
        report = self.run_graph(task)
        gate = report["gates"]["test-integrity"]
        self.assertEqual(gate["exit_code"], 0, gate["detail"])
        self.assertTrue(gate["passed"], gate["detail"])
        self.assertIsNone(gate["rule"])
        self.assertEqual(gate["findings"], [])
        self.assertEqual(report["verdict"], "pass")
        self.assertEqual(report["gates"]["tests"]["exit_code"], 0)

    def test_diff_scope_blocks_github_workflow(self):
        task = self.build_task(
            patch={".github/workflows/ci.yml": "name: t13\non: [push]\njobs: {}\n"}
        )
        report = self.run_graph(task)
        self.assertEqual(report["verdict"], "escalated")
        gate = report["gates"]["diff-scope"]
        self.assertNotEqual(gate["exit_code"], 0)
        self.assertIn(".github/", gate["detail"])
        self.assertEqual(report["gates"]["test-integrity"]["exit_code"], 0)

    def test_test_integrity_blocks_skip_marker_in_new_file(self):
        skipped = (
            "import unittest\n\n\n"
            "class SkippedTests(unittest.TestCase):\n"
            '    @unittest.skip("later")\n'
            "    def test_skipped(self):\n"
            "        pass\n"
        )
        task = self.build_task(patch={"tests/test_skipped.py": skipped})
        report = self.run_graph(task)
        self.assertEqual(report["verdict"], "escalated")
        self.assertIn("skip/xfail marker", report["gates"]["test-integrity"]["detail"])

    def test_fix_loop_applies_declared_fix_patch(self):
        failing = (
            "import unittest\n\n\n"
            "class FailingTests(unittest.TestCase):\n"
            "    def test_fails_first(self):\n"
            "        self.assertEqual(1, 2)\n"
        )
        passing = failing.replace("self.assertEqual(1, 2)", "self.assertEqual(1, 1)")
        task = self.build_task(
            patch={"tests/test_fixloop.py": failing},
            fix_patches=[{"tests/test_fixloop.py": passing}],
        )
        report = self.run_graph(task)
        self.assertEqual(report["verdict"], "pass")
        self.assertEqual(report["attempts"], 1)
        self.assertEqual(report["fix_history"], ["fix patch 1"])
        self.assertEqual(report["gates"]["tests"]["exit_code"], 0)

class ResumeTests(GraphFixture):
    def run_process(self, spec, *extra, thread="resume-thread"):
        command = [
            sys.executable,
            str(TOOLS / "agent_skeleton.py"),
            "run",
            "--task",
            str(spec),
            "--thread",
            thread,
            "--evidence-root",
            str(self.evidence),
            "--state-db",
            str(self.state_db),
            "--no-tracing",
            *extra,
        ]
        return command

    def test_resume_after_kill_completes_without_duplicate_evidence(self):
        task = self.build_task(
            patch={
                "tools/smoke_ping.py": GOOD_FUNCTION,
                "tests/test_smoke_ping.py": GOOD_TEST,
            },
            task_id="t13-resume",
        )
        spec = self.root / "task-resume.json"
        spec.write_text(task.model_dump_json(indent=2), encoding="utf-8")
        bundle = self.bundle("t13-resume")
        log_path = self.root / "first-run.log"
        with log_path.open("w", encoding="utf-8") as log:
            first = subprocess.Popen(
                self.run_process(spec, "--sleep-in-test", "45"),
                cwd=str(self.repo),
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            marker = bundle / ".test-node-started"
            deadline = time.monotonic() + 120
            while not marker.is_file() and time.monotonic() < deadline and first.poll() is None:
                time.sleep(0.2)
            self.assertTrue(
                marker.is_file(),
                "first process never reached the test node: "
                + log_path.read_text(encoding="utf-8", errors="replace")[-800:],
            )
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(first.pid)],
                capture_output=True,
                text=True,
                check=False,
            )
            first.wait(timeout=60)
        self.assertFalse(
            (bundle / "report.json").exists(), "evidence must not exist after the kill"
        )
        resumed = subprocess.run(
            self.run_process(spec, "--resume"),
            cwd=str(self.repo),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        report = self.report("t13-resume")
        self.assertEqual(report["verdict"], "pass")
        self.assertEqual(report["evidence_writes"], 1)
        self.assertEqual(report["attempts"], 0)
        self.assertEqual(report["gates"]["tests"]["exit_code"], 0)
        self.assertIn("test", report["resume"]["next"])
        self.assertGreaterEqual(report["resume"]["checkpoints_before"], 4)
        self.assertEqual(len(list(bundle.glob("gate-tests.json"))), 1)
        self.assertFalse((bundle / ".test-node-started").exists())
        written = sorted(
            path.name for path in bundle.glob("*.json") if path.name.startswith("gate-")
        )
        self.assertEqual(len(written), len(set(written)))


if __name__ == "__main__":
    unittest.main()