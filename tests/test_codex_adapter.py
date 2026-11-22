"""Codex adapter behaviour against local ``.cmd`` stubs; the real CLI is never run."""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from codex_engine import OUTPUT_SCHEMA, CodexEngine  # noqa: E402

VALID_STUB = """@echo off
echo argv=%*>>"%CODEX_STUB_LOG%"
echo cwd=%CD%>>"%CODEX_STUB_LOG%"
echo {"files_changed":["backend/src/main/java/ru/orbita/demo/Stub.java"],"summary":"stub patch"}
exit /b 0
"""

GARBAGE_STUB = """@echo off
echo this is not json
exit /b 0
"""

TIMEOUT_STUB = """@echo off
ping -n 12 127.0.0.1 >nul
echo {"files_changed":[],"summary":"too late"}
exit /b 0
"""

FAIL_STUB = """@echo off
echo fatal: request rejected sk-abcdefgh12345678 >&2
exit /b 3
"""


class CodexAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.log = self.root / "codex-argv.log"
        self.done = self.root / "codex-done.txt"

    def stub(self, name: str, body: str) -> Path:
        path = self.root / name
        path.write_text(body, encoding="utf-8", newline="\r\n")
        return path

    def engine(self, stub: Path, *, timeout_s: int = 30) -> CodexEngine:
        env = dict(os.environ)
        env.update(
            {
                "CODEX_STUB_LOG": str(self.log),
                "CODEX_STUB_DONE": str(self.done),
                "NO_PROXY": "localhost,127.0.0.1",
            }
        )
        return CodexEngine(
            self.repo,
            binary=str(stub),
            timeout_s=timeout_s,
            cli_version="stub-0.1.0",
            env=env,
        )

    def test_valid_json_returns_patch_report_and_pinned_argv(self) -> None:
        report = self.engine(self.stub("valid.cmd", VALID_STUB)).run("fix the thing", task_id="t16")
        self.assertIsNone(report.engine_error)
        self.assertEqual(report.engine, "codex")
        self.assertEqual(report.cli_version, "stub-0.1.0")
        self.assertEqual(
            report.files_changed, ["backend/src/main/java/ru/orbita/demo/Stub.java"]
        )
        self.assertEqual(report.summary, "stub patch")
        self.assertEqual(report.exit_code, 0)
        self.assertIsNotNone(report.duration_ms)
        logged = self.log.read_text(encoding="utf-8", errors="replace")
        argv_line = next(line for line in logged.splitlines() if line.startswith("argv="))
        self.assertIn("exec", argv_line)
        self.assertIn("--sandbox workspace-write", argv_line)
        self.assertIn("--output-schema", argv_line)
        self.assertIn("patch_report.schema.json", argv_line)
        self.assertIn(f"cwd={self.repo}", logged)
        self.assertEqual(OUTPUT_SCHEMA["required"], ["files_changed", "summary"])

    def test_invalid_json_is_a_structured_error(self) -> None:
        report = self.engine(self.stub("garbage.cmd", GARBAGE_STUB)).run("x", task_id="t16")
        self.assertEqual(report.engine_error, "invalid_json")
        self.assertEqual(report.engine, "codex")
        self.assertEqual(report.files_changed, [])
        self.assertEqual(report.exit_code, 0)
        self.assertIn("JSON", report.error_detail.upper())

    def test_timeout_kills_the_process_tree(self) -> None:
        started = time.monotonic()
        report = self.engine(self.stub("timeout.cmd", TIMEOUT_STUB), timeout_s=1).run(
            "x", task_id="t16"
        )
        elapsed = time.monotonic() - started
        self.assertEqual(report.engine_error, "timeout")
        self.assertLess(elapsed, 15)
        time.sleep(2)
        self.assertFalse(self.done.exists(), "stub continued running after the timeout")

    def test_nonzero_exit_trims_and_redacts_stderr(self) -> None:
        report = self.engine(self.stub("fail.cmd", FAIL_STUB)).run("x", task_id="t16")
        self.assertEqual(report.engine_error, "nonzero_exit")
        self.assertEqual(report.exit_code, 3)
        self.assertLessEqual(len(report.error_detail), 2000)
        self.assertNotIn("sk-abcdefgh12345678", report.error_detail)
        self.assertIn("[redacted]", report.error_detail)


if __name__ == "__main__":
    unittest.main()