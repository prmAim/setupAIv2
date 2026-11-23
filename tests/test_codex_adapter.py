"""Codex adapter behaviour against local ``.cmd`` stubs; the real CLI is never run.

Stubs cover every branch the adapter must survive: a schema-valid answer, logs
around the JSON, a last-message file, garbage, silence, quota, auth, network,
an unrecognised non-zero exit, and a hang.  They also record the argv, the cwd,
whether stdin was closed and whether credentials leaked into the child env.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from codex_engine import (  # noqa: E402
    DEFAULT_TIMEOUT_S,
    OUTPUT_SCHEMA,
    CodexEngine,
    classify_failure,
    parse_reset_at,
    resolve_model,
)

VALID_JSON = (
    '{"files_changed":["backend/src/main/java/ru/orbita/demo/Stub.java"],'
    '"summary":"stub patch","tests_added":[],"notes":["one note"]}'
)

TRACE = """@echo off
echo argv=%*>>"%CODEX_STUB_LOG%"
echo cwd=%CD%>>"%CODEX_STUB_LOG%"
set "STDIN_LINE="
set /p STDIN_LINE=
if defined STDIN_LINE (echo stdin=present>>"%CODEX_STUB_LOG%") else (echo stdin=eof>>"%CODEX_STUB_LOG%")
if defined GITHUB_TOKEN echo token=leaked>>"%CODEX_STUB_LOG%"
if defined OPENAI_API_KEY echo token=leaked>>"%CODEX_STUB_LOG%"
"""

VALID_STUB = TRACE + 'echo starting up\n' + f'echo {VALID_JSON}\n' + 'echo done\nexit /b 0\n'

LAST_MESSAGE_STUB = TRACE + (
    'set "PREV="\n'
    'set "LAST="\n'
    ':loop\n'
    'if "%~1"=="" goto after\n'
    'if "%~1"=="--output-last-message" set "LAST=%~2"\n'
    'shift\n'
    'goto loop\n'
    ':after\n'
    'if defined LAST copy /y "%CODEX_STUB_PAYLOAD%" "%LAST%" >nul\n'
    'echo not json at all\n'
    'exit /b 0\n'
)

GARBAGE_STUB = TRACE + 'echo this is not json\nexit /b 0\n'

SILENT_STUB = TRACE + 'exit /b 0\n'

QUOTA_STUB = TRACE + (
    'echo ERROR: You have hit your usage limit. Please try again at 2026-11-23T18:04:00Z >&2\n'
    'exit /b 1\n'
)

QUOTA_CLOCK_STUB = TRACE + (
    'echo usage limit reached; try again at 11:04 PM >&2\n'
    'exit /b 1\n'
)

AUTH_STUB = TRACE + 'echo API error 401: not logged in >&2\nexit /b 1\n'

NETWORK_STUB = TRACE + (
    'echo 403 Forbidden: unsupported_country_region_territory >&2\n'
    'exit /b 1\n'
)

CLOUDFLARE_STUB = TRACE + (
    'echo Attention Required! blocked by Cloudflare >&2\n'
    'exit /b 1\n'
)

FAKE_SK = "sk-" + "a1b2c3d4e5f6g7h8" + "zz"
FAKE_PAT = "github_pat_" + "a1b2c3d4e5f6g7h8" + "zz"
FAKE_JWT = (
    "eyJhbGciOiJIUzI1NiJ9"
    + ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
    + ".abcdefghijklmnop"
)

FAIL_STUB = TRACE + (
    f"echo fatal: request rejected {FAKE_SK} {FAKE_PAT} {FAKE_JWT} >&2\n"
    "exit /b 3\n"
)
TIMEOUT_STUB = TRACE + (
    'ping -n 20 127.0.0.1 >nul\n'
    'echo {"files_changed":[],"summary":"too late"}\n'
    'exit /b 0\n'
)


class CodexAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.log = self.root / "codex-argv.log"
        self.done = self.root / "codex-done.txt"
        self.config = self.root / "codex-config.toml"

    def stub(self, name: str, body: str, *, preamble: bool = True) -> Path:
        path = self.root / name
        text = ("@echo off\n" if preamble else "") + body
        path.write_text(text, encoding="utf-8", newline="\r\n")
        return path

    def engine(
        self,
        stub: Path,
        *,
        timeout_s: int = 30,
        extra_env: dict[str, str] | None = None,
        model: str | None = None,
    ) -> CodexEngine:
        env = dict(os.environ)
        env.pop("CODEX_BIN", None)
        env.pop("CODEX_MODEL", None)
        env.pop("CODEX_SANDBOX", None)
        env.pop("CODEX_TIMEOUT_S", None)
        env.pop("CODEX_CLI_VERSION", None)
        env.update(
            {
                "CODEX_STUB_LOG": str(self.log),
                "CODEX_CONFIG_FILE": str(self.config),
                "NO_PROXY": "localhost,127.0.0.1",
                **(extra_env or {}),
            }
        )
        return CodexEngine(
            self.repo,
            binary=str(stub),
            model=model,
            timeout_s=timeout_s,
            cli_version="0.171.0",
            env=env,
        )

    def logged(self) -> str:
        return self.log.read_text(encoding="utf-8", errors="replace")

    def argv_line(self) -> str:
        return next(line for line in self.logged().splitlines() if line.startswith("argv="))

    def test_valid_json_returns_patch_report_and_pinned_argv(self) -> None:
        report = self.engine(self.stub("valid.cmd", VALID_STUB)).run("fix the thing")
        self.assertIsNone(report.engine_error)
        self.assertEqual(report.engine, "codex")
        self.assertEqual(report.cli_version, "0.171.0")
        self.assertEqual(
            report.files_changed, ["backend/src/main/java/ru/orbita/demo/Stub.java"]
        )
        self.assertEqual(report.summary, "stub patch")
        self.assertEqual(report.notes, ["one note"])
        self.assertEqual(report.tests_added, [])
        self.assertEqual(report.exit_code, 0)
        self.assertIsNotNone(report.duration_ms)
        logged = self.logged()
        argv = self.argv_line()
        self.assertIn("exec", argv)
        self.assertIn("--sandbox workspace-write", argv)
        self.assertIn("--output-schema", argv)
        self.assertIn("patch_report.schema.json", argv)
        self.assertIn("--output-last-message", argv)
        self.assertIn("--skip-git-repo-check", argv, "temp cwd is not a git repo")
        self.assertIn(f"cwd={self.repo}", logged)
        self.assertIn("stdin=eof", logged, "stdin must be closed, not inherited")
        self.assertNotIn("token=leaked", logged)
        self.assertEqual(OUTPUT_SCHEMA["required"], ["files_changed", "summary", "tests_added", "notes"])

    def test_git_worktree_does_not_pass_skip_git_repo_check(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        report = self.engine(self.stub("valid.cmd", VALID_STUB)).run("x")
        self.assertIsNone(report.engine_error)
        self.assertNotIn("--skip-git-repo-check", self.argv_line())

    def test_model_comes_from_config_toml(self) -> None:
        self.config.write_text('model = "gpt-6-luna"\n', encoding="utf-8")
        self.engine(self.stub("valid.cmd", VALID_STUB)).run("x")
        self.assertIn("--model gpt-6-luna", self.argv_line())

    def test_model_env_overrides_config(self) -> None:
        self.config.write_text('model = "from-config"\n', encoding="utf-8")
        engine = self.engine(
            self.stub("valid.cmd", VALID_STUB), extra_env={"CODEX_MODEL": "gpt-6-luna"}
        )
        engine.run("x")
        self.assertIn("--model gpt-6-luna", self.argv_line())

    def test_no_model_flag_without_config(self) -> None:
        self.engine(self.stub("valid.cmd", VALID_STUB)).run("x")
        self.assertNotIn("--model", self.argv_line())

    def test_last_message_file_wins_over_stdout(self) -> None:
        payload = self.root / "payload.json"
        payload.write_text(VALID_JSON, encoding="utf-8")
        stub = self.stub(
            "last.cmd",
            f'set "CODEX_STUB_PAYLOAD={payload}"\n' + LAST_MESSAGE_STUB,
            preamble=False,
        )
        report = self.engine(stub).run("x")
        self.assertIsNone(report.engine_error)
        self.assertEqual(report.summary, "stub patch")

    def test_invalid_json_is_a_structured_error(self) -> None:
        report = self.engine(self.stub("garbage.cmd", GARBAGE_STUB)).run("x")
        self.assertEqual(report.engine_error, "invalid_json")
        self.assertEqual(report.engine, "codex")
        self.assertEqual(report.files_changed, [])
        self.assertEqual(report.exit_code, 0)
        self.assertIn("JSON", report.error_detail.upper())

    def test_empty_output_is_reported_separately(self) -> None:
        report = self.engine(self.stub("silent.cmd", SILENT_STUB)).run("x")
        self.assertEqual(report.engine_error, "empty_output")
        self.assertEqual(report.exit_code, 0)
        self.assertEqual(report.files_changed, [])

    def test_quota_exhausted_parses_iso_reset_time(self) -> None:
        report = self.engine(self.stub("quota.cmd", QUOTA_STUB)).run("x")
        self.assertEqual(report.engine_error, "quota_exhausted")
        self.assertEqual(report.exit_code, 1)
        self.assertEqual(report.reset_at, "2026-11-23T18:04:00Z")
        self.assertEqual(report.reset_at_utc, "2026-11-23T18:04:00Z")
        self.assertIn("usage limit", report.error_detail)

    def test_quota_exhausted_parses_local_clock(self) -> None:
        report = self.engine(self.stub("quota-clock.cmd", QUOTA_CLOCK_STUB)).run("x")
        self.assertEqual(report.engine_error, "quota_exhausted")
        self.assertEqual(report.reset_at, "11:04 PM")
        self.assertIsNotNone(report.reset_at_utc)
        self.assertTrue(report.reset_at_utc.endswith("Z"))
        self.assertEqual(len(report.reset_at_utc), 20)

    def test_auth_expired(self) -> None:
        report = self.engine(self.stub("auth.cmd", AUTH_STUB)).run("x")
        self.assertEqual(report.engine_error, "auth_expired")
        self.assertEqual(report.exit_code, 1)

    def test_network_blocked_on_403_region_error(self) -> None:
        report = self.engine(self.stub("network.cmd", NETWORK_STUB)).run("x")
        self.assertEqual(report.engine_error, "network_blocked")
        self.assertEqual(report.exit_code, 1)

    def test_network_blocked_on_cloudflare(self) -> None:
        report = self.engine(self.stub("cloudflare.cmd", CLOUDFLARE_STUB)).run("x")
        self.assertEqual(report.engine_error, "network_blocked")

    def test_other_nonzero_exit_is_engine_failed_and_redacted(self) -> None:
        report = self.engine(self.stub("fail.cmd", FAIL_STUB)).run("x")
        self.assertEqual(report.engine_error, "engine_failed")
        self.assertEqual(report.exit_code, 3)
        self.assertLessEqual(len(report.error_detail), 2000)
        for secret in (FAKE_SK, FAKE_PAT, FAKE_JWT.split(".")[0]):
            self.assertNotIn(secret, report.error_detail)
        self.assertIn("[redacted]", report.error_detail)

    def test_timeout_kills_the_process_tree(self) -> None:
        started = time.monotonic()
        report = self.engine(self.stub("timeout.cmd", TIMEOUT_STUB), timeout_s=1).run("x")
        elapsed = time.monotonic() - started
        self.assertEqual(report.engine_error, "timeout")
        self.assertIsNone(report.exit_code)
        self.assertLess(elapsed, 20)
        time.sleep(2)
        self.assertFalse(self.done.exists(), "stub continued running after the timeout")

    def test_missing_binary_is_spawn_failed(self) -> None:
        missing = self.root / "does-not-exist.exe"
        report = self.engine(missing).run("x")
        self.assertEqual(report.engine_error, "spawn_failed")

    def test_env_is_scrubbed_of_credentials(self) -> None:
        report = self.engine(
            self.stub("valid.cmd", VALID_STUB),
            extra_env={
                "GITHUB_TOKEN": "ghp_" + "a1b2c3d4e5f6g7h8" + "zz",
                "OPENAI_API_KEY": FAKE_SK,
            },
        ).run("x")
        self.assertIsNone(report.engine_error)
        self.assertNotIn("token=leaked", self.logged())

    def test_defaults_follow_environment(self) -> None:
        engine = CodexEngine(
            self.repo,
            binary=str(self.repo / "codex.cmd"),
            env={"CODEX_TIMEOUT_S": "42", "CODEX_SANDBOX": "read-only"},
        )
        self.assertEqual(engine.timeout_s, 42)
        self.assertEqual(engine.sandbox, "read-only")
        with self.assertRaises(ValueError):
            CodexEngine(self.repo, env={"CODEX_SANDBOX": "yolo"})
        self.assertEqual(DEFAULT_TIMEOUT_S, 900)


class ClassificationTests(unittest.TestCase):
    def test_classification_precedence(self) -> None:
        self.assertEqual(classify_failure("usage limit; try again at 1:00 AM"), "quota_exhausted")
        self.assertEqual(classify_failure("HTTP 401: not logged in"), "auth_expired")
        self.assertEqual(classify_failure("403 unsupported_country_region_territory"), "network_blocked")
        self.assertIsNone(classify_failure("fatal: something else broke"))

    def test_parse_reset_at_iso_and_clock(self) -> None:
        self.assertEqual(
            parse_reset_at("reset at 2026-11-23T18:04:00+05:00"),
            ("2026-11-23T18:04:00+05:00", "2026-11-23T13:04:00Z"),
        )
        raw, utc = parse_reset_at("try again at 11:04 PM")
        self.assertEqual(raw, "11:04 PM")
        self.assertTrue(utc.endswith("Z"))
        self.assertEqual(parse_reset_at("no hint here"), (None, None))

    def test_model_resolution_reads_toml(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.toml"
            config.write_text('model = "gpt-6-luna"\n', encoding="utf-8")
            self.assertEqual(
                resolve_model({"CODEX_CONFIG_FILE": str(config)}), "gpt-6-luna"
            )
            self.assertEqual(
                resolve_model({"CODEX_CONFIG_FILE": str(config), "CODEX_MODEL": "x"}),
                "x",
            )
            self.assertIsNone(
                resolve_model({"CODEX_CONFIG_FILE": str(Path(temporary) / "missing.toml")})
            )


if __name__ == "__main__":
    unittest.main()