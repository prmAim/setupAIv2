"""Gate behaviour that graph-level fixtures cannot cover: Java sources and parse safety."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from test_integrity import compare

JAVA_PATH = "backend/src/test/java/ru/orbita/demo/TradePolicyTest.java"
JAVA_TEST = """package ru.orbita.demo;

import org.junit.jupiter.api.Test;

class TradePolicyTest {
  @Test
  void priceStepCannotExceedTheHalfPercent() {
    assertThat(TradePolicy.limit("101")).isEqualByComparingTo("101.5");
  }

  @Test
  void wholeConfirmedRangeMustFit() {
    assertThat(TradePolicy.limit("99")).isEqualByComparingTo("99.5");
  }
}
"""


class TempRepo:
    def __init__(self, root: Path) -> None:
        self.root = root

    @classmethod
    def create(cls, parent: Path, files: dict[str, str]) -> tuple["TempRepo", str]:
        root = parent / "repo"
        root.mkdir()
        repo = cls(root)
        repo.git("init", "-q")
        repo.git("config", "user.email", "t15@example.invalid")
        repo.git("config", "user.name", "t15 fixture")
        repo.git("config", "commit.gpgsign", "false")
        repo.git("config", "core.autocrlf", "false")
        for rel, text in files.items():
            repo.write(rel, text)
        repo.git("add", "-A")
        repo.git("commit", "-q", "-m", "baseline")
        sha = repo.git("rev-parse", "HEAD").strip()
        return repo, sha

    def git(self, *args: str) -> str:
        completed = subprocess.run(
            ["git", *args], cwd=self.root, capture_output=True, text=True, check=True
        )
        return completed.stdout

    def write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")


class JavaGateTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)

    def repo(self, files: dict[str, str] | None = None) -> tuple[TempRepo, str]:
        return TempRepo.create(self.tmp, files or {JAVA_PATH: JAVA_TEST})

    def rules(self, result) -> set[str]:
        return {finding.rule for finding in result.findings}

    def test_added_disabled_annotation_is_denied(self) -> None:
        repo, sha = self.repo()
        repo.write(
            JAVA_PATH,
            JAVA_TEST.replace(
                "  @Test\n  void wholeConfirmedRangeMustFit",
                "  @Disabled\n  @Test\n  void wholeConfirmedRangeMustFit",
            ),
        )
        result = compare(repo.root, sha, [JAVA_PATH])
        self.assertEqual(result.exit_code, 1, result.summary)
        self.assertIn("skip-marker-added", self.rules(result))
        self.assertEqual([item.file for item in result.findings], [JAVA_PATH])

    def test_new_test_method_in_existing_java_file_is_allowed(self) -> None:
        repo, sha = self.repo()
        repo.write(
            JAVA_PATH,
            JAVA_TEST.replace(
                "  @Test\n  void wholeConfirmedRangeMustFit",
                "  @Test\n  void newAddedCase() {}\n\n  @Test\n  void wholeConfirmedRangeMustFit",
            ),
        )
        result = compare(repo.root, sha, [JAVA_PATH])
        self.assertEqual(result.exit_code, 0, result.summary)
        self.assertEqual(result.findings, [])

    def test_unparseable_file_is_denied(self) -> None:
        repo, sha = self.repo()
        repo.write(JAVA_PATH, JAVA_TEST.replace("class TradePolicyTest {", "class TradePolicyTest "))
        result = compare(repo.root, sha, [JAVA_PATH])
        self.assertEqual(result.exit_code, 1, result.summary)
        self.assertIn("parse-error", self.rules(result))


if __name__ == "__main__":
    unittest.main()
