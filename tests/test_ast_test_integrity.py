"""Exercise the AST test integrity gate on copies of real orbita test files."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from test_integrity import compare


ORBITA = Path(os.environ.get("ORBITA_BASELINE", r"C:\Git\_wf\orbita-baseline"))

PY_CONFIG = "tests/test_configuration.py"
PY_WORKFLOW = "tests/test_workflow.py"
JAVA_TRADE = "backend/src/test/java/ru/orbita/copy/TradePolicyTest.java"
JAVA_LEDGER = "backend/src/test/java/ru/orbita/broker/InterfaceDemoLedgerIT.java"


class TempRepo:
    def __init__(self, root: Path) -> None:
        self.root = root

    @classmethod
    def create(cls, parent: Path, files: dict[str, bytes]) -> tuple["TempRepo", str]:
        root = parent / "repo"
        root.mkdir()
        repo = cls(root)
        repo.git("init", "-q")
        repo.git("config", "user.email", "test@example.com")
        repo.git("config", "user.name", "Test")
        repo.git("config", "commit.gpgsign", "false")
        repo.git("config", "core.autocrlf", "false")
        for rel, data in files.items():
            repo.write(rel, data)
        repo.git("add", "-A")
        repo.git("commit", "-q", "-m", "baseline")
        sha = repo.git("rev-parse", "HEAD").stdout.decode().strip()
        return repo, sha

    def git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=self.root, capture_output=True, check=True
        )

    def write(self, rel: str, data: bytes) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def read(self, rel: str) -> bytes:
        return (self.root / rel).read_bytes()


@unittest.skipUnless(ORBITA.is_dir(), f"orbita baseline not found at {ORBITA}")
class AstTestIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)

    def real(self, rel: str) -> bytes:
        return (ORBITA / rel).read_bytes()

    def make_repo(self, files: dict[str, bytes]) -> tuple[TempRepo, str]:
        return TempRepo.create(self.tmp, files)

    def assert_allowed(self, result) -> None:
        self.assertEqual(result.exit_code, 0, result.summary)
        self.assertEqual(result.findings, [])

    def assert_denied(self, result, rule: str) -> None:
        self.assertEqual(result.exit_code, 1, result.summary)
        self.assertIn(rule, {finding.rule for finding in result.findings})

    def test_new_test_file_is_allowed(self) -> None:
        repo, sha = self.make_repo({PY_WORKFLOW: self.real(PY_WORKFLOW)})
        repo.write(
            "tests/test_added.py",
            b"import unittest\n\n\n"
            b"class AddedTests(unittest.TestCase):\n"
            b"    def test_added(self):\n"
            b"        self.assertEqual(1 + 1, 2)\n",
        )
        repo.write(
            "backend/src/test/java/ru/orbita/copy/AddedPolicyTest.java",
            b"package ru.orbita.copy;\n\n"
            b"import org.junit.jupiter.api.Test;\n\n"
            b"class AddedPolicyTest {\n"
            b"  @Test\n"
            b"  void addedTest() {}\n"
            b"}\n",
        )
        result = compare(repo.root, sha)
        self.assert_allowed(result)

    def test_modified_assert_is_denied(self) -> None:
        python = self.real(PY_CONFIG)
        java = self.real(JAVA_TRADE)
        repo, sha = self.make_repo({PY_CONFIG: python, JAVA_TRADE: java})
        changed_python = python.replace(
            b'"https://api.openai.com/v1"', b'"https://api.openai.com/v2"'
        )
        changed_java = java.replace(
            b'.isEqualByComparingTo("101.5")', b'.isEqualByComparingTo("102")'
        )
        self.assertNotEqual(changed_python, python)
        self.assertNotEqual(changed_java, java)
        repo.write(PY_CONFIG, changed_python)
        repo.write(JAVA_TRADE, changed_java)
        result = compare(repo.root, sha, [PY_CONFIG, JAVA_TRADE])
        self.assert_denied(result, "test-body-modified")
        self.assertEqual({finding.file for finding in result.findings}, {PY_CONFIG, JAVA_TRADE})

    def test_deleted_test_is_denied(self) -> None:
        python = self.real(PY_WORKFLOW)
        java = self.real(JAVA_TRADE)
        repo, sha = self.make_repo({PY_WORKFLOW: python, JAVA_TRADE: java})
        cut = python.index(b"    def test_blank_model_response_is_rejected")
        java_method = (
            b'  @Test\n'
            b'  void priceStepCannotExceedTheHalfPercentDeterioration() {\n'
            b'    assertThat(TradePolicy.limit(BUY, new BigDecimal("101"), new BigDecimal("0.1")))\n'
            b'        .isEqualByComparingTo("101.5");\n'
            b'    assertThat(TradePolicy.limit(SELL, new BigDecimal("101"), new BigDecimal("0.1")))\n'
            b'        .isEqualByComparingTo("100.5");\n'
            b'  }\n'
        )
        self.assertIn(java_method, java)
        repo.write(PY_WORKFLOW, python[:cut])
        repo.write(JAVA_TRADE, java.replace(java_method, b""))
        result = compare(repo.root, sha, [PY_WORKFLOW, JAVA_TRADE])
        self.assert_denied(result, "test-removed")
        self.assertEqual(len(result.findings), 2, result.findings)

    def test_added_skip_marker_is_denied(self) -> None:
        python = self.real(PY_CONFIG)
        java = self.real(JAVA_TRADE)
        repo, sha = self.make_repo({PY_CONFIG: python, JAVA_TRADE: java})
        python_marker = b'    def test_trace_timeout_is_reported'
        self.assertIn(python_marker, python)
        repo.write(
            PY_CONFIG,
            python.replace(
                python_marker,
                b'    @unittest.skip("disabled")\n' + python_marker,
            ),
        )
        java_import = b"import org.junit.jupiter.api.Test;"
        java_test = b"  @Test\n  void wholeConfirmedAgeRangeMustFitWithinSixtySeconds() {"
        self.assertIn(java_import, java)
        self.assertIn(java_test, java)
        repo.write(
            JAVA_TRADE,
            java.replace(
                java_import,
                b"import org.junit.jupiter.api.Disabled;\n" + java_import,
            ).replace(java_test, b"  @Disabled\n" + java_test),
        )
        result = compare(repo.root, sha, [PY_CONFIG, JAVA_TRADE])
        self.assert_denied(result, "skip-marker-added")
        self.assertEqual(len(result.findings), 2, result.findings)

    def test_reformatting_is_allowed(self) -> None:
        python = self.real(PY_CONFIG)
        java = self.real(JAVA_TRADE)
        repo, sha = self.make_repo({PY_CONFIG: python, JAVA_TRADE: java})
        python_target = (
            b'self.assertEqual(args["base_url"], "https://api.openai.com/v1")'
        )
        self.assertIn(python_target, python)
        reformatted_python = python.replace(
            python_target,
            b'self.assertEqual( args[ "base_url" ] ,  "https://api.openai.com/v1" )',
        ).replace(b"import os\n", b"import os\n\n# formatting comment\n")
        java_target = b"    Instant execution = Instant.parse"
        self.assertIn(java_target, java)
        reformatted_java = java.replace(
            java_target, b"\n\n      // formatting comment\n      Instant   execution=Instant.parse"
        ).replace(b"    assertThat(", b"    assertThat(\n        ")
        repo.write(PY_CONFIG, reformatted_python)
        repo.write(JAVA_TRADE, reformatted_java)
        result = compare(repo.root, sha, [PY_CONFIG, JAVA_TRADE])
        self.assert_allowed(result)

    def test_added_import_is_allowed(self) -> None:
        python = self.real(PY_WORKFLOW)
        java = self.real(JAVA_TRADE)
        repo, sha = self.make_repo({PY_WORKFLOW: python, JAVA_TRADE: java})
        repo.write(PY_WORKFLOW, b"import json\n" + python)
        repo.write(JAVA_TRADE, java.replace(b"package ru.orbita.copy;\n", b"package ru.orbita.copy;\n\nimport java.util.List;\n", 1))
        result = compare(repo.root, sha, [PY_WORKFLOW, JAVA_TRADE])
        self.assert_allowed(result)

    def test_removed_import_is_denied(self) -> None:
        python = self.real(PY_CONFIG)
        java = self.real(JAVA_TRADE)
        repo, sha = self.make_repo({PY_CONFIG: python, JAVA_TRADE: java})
        repo.write(PY_CONFIG, python.replace(b"import os\n", b"", 1))
        repo.write(JAVA_TRADE, java.replace(b"import java.math.BigDecimal;\n", b"", 1))
        result = compare(repo.root, sha, [PY_CONFIG, JAVA_TRADE])
        self.assert_denied(result, "import-removed")
        self.assertEqual(len(result.findings), 2, result.findings)

    def test_modified_helper_is_denied(self) -> None:
        python = self.real(PY_CONFIG)
        java = self.real(JAVA_LEDGER)
        repo, sha = self.make_repo({PY_CONFIG: python, JAVA_LEDGER: java})
        python_target = b"with patch.dict(os.environ, {}, clear=True):"
        java_target = b'database().execute("drop schema if exists orbita_demo_preview cascade");'
        self.assertIn(python_target, python)
        self.assertIn(java_target, java)
        repo.write(
            PY_CONFIG,
            python.replace(
                python_target, b'with patch.dict(os.environ, {"EXTRA": "1"}, clear=True):'
            ),
        )
        repo.write(
            JAVA_LEDGER,
            java.replace(
                java_target,
                b'database().execute("drop schema if exists orbita_demo_preview cascade;");',
            ),
        )
        result = compare(repo.root, sha, [PY_CONFIG, JAVA_LEDGER])
        self.assert_denied(result, "helper-modified")
        self.assertEqual(len(result.findings), 2, result.findings)

    def test_real_git_show_against_orbita_baseline(self) -> None:
        sha = subprocess.run(
            ["git", "-C", str(ORBITA), "rev-parse", "HEAD"],
            capture_output=True,
            check=True,
        ).stdout.decode().strip()
        result = compare(ORBITA, sha, [PY_CONFIG, JAVA_TRADE, JAVA_LEDGER])
        self.assert_allowed(result)
        self.assertIn("3 file(s)", result.summary)


if __name__ == "__main__":
    unittest.main()
