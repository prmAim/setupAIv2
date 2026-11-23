"""Unit and end-to-end tests for the golden-5 eval harness (ticket #24).

Every test builds a tiny fixture repository (baseline commit + a
``research/golden-5`` branch with ``eval/golden/1/...``) instead of touching
orbita, so the whole file runs offline in a few seconds.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import eval_harness as harness

GREETER = 'def greet(name: str) -> str:\n    return f"hi {name}"\n'
SHOUT = 'def shout(name: str) -> str:\n    return f"HI {name.upper()}"\n'
BASE_TEST = (
    "import unittest\n"
    "\n"
    "from tools.greeter import greet\n"
    "\n"
    "\n"
    "class BaseTests(unittest.TestCase):\n"
    "    def test_greet(self):\n"
    '        self.assertEqual(greet("a"), "hi a")\n'
)
HIDDEN_TEST = (
    "import unittest\n"
    "\n"
    "from tools.shout import shout\n"
    "\n"
    "\n"
    "class HiddenTests(unittest.TestCase):\n"
    "    def test_shout(self):\n"
    '        self.assertEqual(shout("a"), "HI A")\n'
)


def fixture_git(repo: Path, *args: str) -> str:
    outcome = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if outcome.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {outcome.stderr}")
    return outcome.stdout


def fixture_git_bytes(repo: Path, *args: str) -> bytes:
    outcome = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, check=False
    )
    if outcome.returncode != 0:
        raise AssertionError(outcome.stderr.decode("utf-8", errors="replace"))
    return outcome.stdout


def fixture_commit(repo: Path, message: str) -> str:
    fixture_git(repo, "-c", "user.name=fixture", "-c", "user.email=f@x", "add", "-A")
    fixture_git(
        repo,
        "-c",
        "user.name=fixture",
        "-c",
        "user.email=f@x",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-q",
        "-m",
        message,
    )
    return fixture_git(repo, "rev-parse", "HEAD").strip()


def build_fixture(root: Path, *, tamper: bool = False) -> dict[str, Path | str]:
    """Baseline commit, a golden-5 branch, and the harness path bundle."""
    source = root / "source"
    source.mkdir()
    fixture_git(source, "init", "-q", "-b", "main")
    (source / "tools").mkdir()
    (source / "tests").mkdir()
    (source / "tools" / "greeter.py").write_text(GREETER, encoding="utf-8")
    (source / "tests" / "test_base.py").write_text(BASE_TEST, encoding="utf-8")
    baseline = fixture_commit(source, "baseline")

    fixture_git(source, "checkout", "-q", "-b", "golden-work")
    (source / "tools" / "shout.py").write_text(SHOUT, encoding="utf-8")
    fixture_git(source, "add", "-A")
    gold_patch = fixture_git_bytes(source, "diff", "--cached", "--", "tools/shout.py")
    (source / "tests" / "test_hidden.py").write_text(HIDDEN_TEST, encoding="utf-8")
    fixture_git(source, "add", "-A")
    tests_patch = fixture_git_bytes(source, "diff", "--cached", "--", "tests/test_hidden.py")
    fixture_commit(source, "gold and hidden tests")

    spec = {
        "id": "fixture-task",
        "repo": "fixture/orbita",
        "baseline_sha": baseline,
        "merge_sha": fixture_git(source, "rev-parse", "HEAD").strip(),
        "language": "python",
        "gate": "fast",
        "expected_red": ["test_hidden.HiddenTests.test_shout"],
    }
    if tamper:
        spec["sha256"] = {"gold.patch": "0" * 64}
    base = source / "eval" / "golden" / "1"
    base.mkdir(parents=True)
    (base / "issue.md").write_text("Make shout() uppercase.\n", encoding="utf-8")
    (base / "gold.patch").write_bytes(gold_patch)
    (base / "tests.patch").write_bytes(tests_patch)
    (base / "task.json").write_text(json.dumps(spec, indent=2), encoding="utf-8")
    manifest = {
        "repo": "fixture/orbita",
        "tasks": [
            {
                "id": "fixture-task",
                "baseline_sha": baseline,
                "merge_sha": spec["merge_sha"],
                "language": "python",
                "gate": "fast",
            }
        ],
    }
    (source / "eval" / "golden" / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    fixture_commit(source, "golden set")
    fixture_git(source, "branch", "research/golden-5")
    fixture_git(source, "checkout", "-q", "main")
    return {
        "source": source,
        "baseline": baseline,
        "merge": spec["merge_sha"],
        "root": root,
        "paths": harness.HarnessPaths(
            source=source,
            orbita=source,
            ref="research/golden-5",
            work_root=root / "runs",
            evidence_root=root / "evidence",
            report_root=root / "reports",
            python=sys.executable,
            java_home=None,
        ),
    }


class EvalHarnessFixture(unittest.TestCase):
    tamper = False
    load_golden = True

    @classmethod
    def setUpClass(cls) -> None:
        cls._temp = tempfile.TemporaryDirectory()
        cls.fixture = build_fixture(Path(cls._temp.name), tamper=cls.tamper)
        cls.paths: harness.HarnessPaths = cls.fixture["paths"]  # type: ignore[assignment]
        cls.golden = (
            harness.GoldenSet(cls.paths.source, cls.paths.ref) if cls.load_golden else None
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temp.cleanup()


class GoldenSetTests(EvalHarnessFixture):
    def test_manifest_and_sha256(self) -> None:
        task = self.golden.tasks[0]
        self.assertEqual(task.task_id, "fixture-task")
        self.assertEqual(task.baseline_sha, self.fixture["baseline"])
        self.assertEqual(task.test_roots, ["tests"])
        self.assertIn("gold.patch", task.digests)
        digests_path = self.paths.report_root / "golden-digests.json"
        stored = self.golden.record_digests(digests_path)
        self.assertEqual(stored["tasks"]["1"], task.digests)

    def test_digest_drift_is_fatal(self) -> None:
        digests_path = self.paths.report_root / "drifted.json"
        harness.json_dump(digests_path, {"ref": "x", "tasks": {"1": {"gold.patch": "dead"}}})
        with self.assertRaises(harness.HarnessError):
            self.golden.record_digests(digests_path)

    def test_plan_hidden_targets(self) -> None:
        plan = harness.plan_hidden(self.golden.tasks[0])
        self.assertEqual(plan["python_patterns"], ["test_hidden.py"])
        self.assertEqual(plan["java_classes"], [])

    def test_java_classes_from_expected_red(self) -> None:
        task = harness.GoldenTask(
            number=9,
            task_id="java-task",
            path="eval/golden/9",
            language="java",
            gate="full",
            baseline_sha="0" * 40,
            merge_sha="1" * 40,
            issue=b"issue",
            gold_patch=b"gold",
            tests_patch=b"tests",
            spec={"expected_red": ["ru.orbita.broker.OrderSdkTlsTest (compile)"]},
            manifest_entry={},
            digests={},
        )
        self.assertEqual(
            harness.plan_hidden(task)["java_classes"],
            ["ru.orbita.broker.OrderSdkTlsTest"],
        )


class TamperedGoldenSetTests(EvalHarnessFixture):
    tamper = True
    load_golden = False

    def test_declared_sha256_mismatch_is_fatal(self) -> None:
        with self.assertRaises(harness.HarnessError):
            harness.GoldenSet(self.paths.source, self.paths.ref)


class SandboxIsolationTests(EvalHarnessFixture):
    def test_sandbox_has_one_commit_and_no_future_history(self) -> None:
        task = self.golden.tasks[0]
        dest = self.paths.work_root / "isolation"
        sandbox = harness.prepare_sandbox(
            source_repo=self.paths.orbita,
            baseline_sha=task.baseline_sha,
            dest=dest,
            issue=task.issue,
        )
        self.assertTrue(sandbox.proof["passed"])
        self.assertEqual(sandbox.proof["commits"], 1)
        self.assertEqual(sandbox.proof["rev_list_all"], 1)
        self.assertEqual(sandbox.proof["remotes"], "")
        self.assertEqual(sandbox.proof["refs"], ["refs/heads/main"])
        self.assertEqual(sandbox.baseline_tree, sandbox.source_tree)
        self.assertTrue(sandbox.issue_path.is_file())
        self.assertFalse((dest / "eval").exists())
        self.assertEqual(fixture_git(dest, "log", "--all", "--format=%H").split(), [
            sandbox.baseline_commit
        ])
        with self.assertRaises(AssertionError):
            fixture_git(dest, "cat-file", "-e", str(self.fixture["merge"]))
        with self.assertRaises(harness.HarnessError):
            harness.git(dest, "rev-parse", "HEAD~1")
        self.assertEqual(harness.git(dest, "remote", "-v"), "")


class ControlMatrixTests(EvalHarnessFixture):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        task = cls.golden.tasks[0]
        cls.cases = {
            mode: harness.run_control(
                task=task,
                mode=mode,
                paths=cls.paths,
                report_ts="unit",
            )
            for mode in ("gold", "noop", "cheat", "codex")
        }

    def test_agent_work_precedes_hidden_tests(self) -> None:
        case = self.cases["gold"]
        self.assertEqual(
            case["order"], ["isolate", "agent", "integrity", "hidden", "report"]
        )
        names = (Path(case["evidence"]) / "agent-diff-name-only.txt").read_text(
            encoding="utf-8"
        )
        self.assertIn("tools/shout.py", names)
        self.assertNotIn("tests/test_hidden.py", names)
        self.assertEqual(case["hidden_tests"]["tests_patch_exit"], 0)

    def test_gold_is_resolved(self) -> None:
        case = self.cases["gold"]
        self.assertEqual(case["verdict"], "resolved")
        self.assertEqual(case["hidden_tests"]["exit_code"], 0)
        self.assertFalse(case["test_integrity"]["test_intrusion"])
        self.assertEqual(case["test_integrity"]["verdict"], "pass")
        self.assertEqual(case["engine_calls"], 1)
        self.assertTrue(case["isolation"]["passed"])

    def test_noop_fails_without_breaking_harness(self) -> None:
        case = self.cases["noop"]
        self.assertEqual(case["verdict"], "fail")
        self.assertEqual(case["reason"], "hidden-tests")
        self.assertNotEqual(case["hidden_tests"]["exit_code"], 0)
        self.assertIsNone(case["harness_error"])
        self.assertTrue(case["isolation"]["passed"])

    def test_cheat_is_test_intrusion(self) -> None:
        case = self.cases["cheat"]
        self.assertEqual(case["verdict"], "fail")
        self.assertTrue(case["test_integrity"]["test_intrusion"])
        rules = {item["rule"] for item in case["test_integrity"]["findings"]}
        self.assertIn("test-file-modified", rules)
        self.assertEqual(case["hidden_tests"]["tests_patch_exit"], 0)

    def test_codex_mode_reports_not_run(self) -> None:
        case = self.cases["codex"]
        self.assertEqual(case["verdict"], "not_run")
        self.assertEqual(case["reason"], "engine:codex_disabled")
        self.assertIsNone(case["harness_error"])

    def test_control_matrix_matches_expectations(self) -> None:
        cases = [self.cases[mode] for mode in ("gold", "noop", "cheat")]
        self.assertEqual(harness.check_expectations(cases), [])

    def test_report_is_written(self) -> None:
        cases = [self.cases[mode] for mode in ("gold", "noop", "cheat")]
        digests = self.golden.record_digests(self.paths.report_root / "golden-digests.json")
        written = harness.write_report(
            paths=self.paths, report_ts="unit", cases=cases, digests=digests
        )
        payload = json.loads(Path(written["json"]).read_text(encoding="utf-8"))
        self.assertEqual(payload["mismatches"], [])
        self.assertEqual(len(payload["runs"]), 3)
        self.assertIn("History isolation", Path(written["markdown"]).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()