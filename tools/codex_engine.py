"""Codex CLI adapter behind :class:`PatchReport`.

The adapter never raises for engine-side failures: invalid JSON, a non-zero
exit, and a timeout all come back as a ``PatchReport`` carrying
``engine_error`` plus redacted, bounded ``error_detail``. Tests drive it with
``.cmd`` stubs through ``CODEX_BIN``; the real CLI is never invoked here.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from patch_report import PatchReport
from process_util import command_for, process_env, redact, resolve_binary

OUTPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["files_changed", "summary"],
    "properties": {
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
        "cli_version": {"type": ["string", "null"]},
    },
}


class CodexProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeout_s: int = Field(default=300, ge=1, le=3600)
    sandbox: str = Field(
        default="workspace-write",
        pattern=r"^(read-only|workspace-write|danger-full-access)$",
    )
    cli_version: str | None = None


class _EnginePayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    files_changed: list[str]
    summary: str
    cli_version: str | None = None


def _kill_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        )
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


class CodexEngine:
    def __init__(
        self,
        repo: Path,
        *,
        binary: str | None = None,
        timeout_s: int = 300,
        sandbox: str = "workspace-write",
        cli_version: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.repo = Path(repo).resolve()
        self.env = process_env(env)
        self.binary = binary or resolve_binary("CODEX_BIN", "codex", self.env)
        self.timeout_s = timeout_s
        self.sandbox = sandbox
        self._cli_version = cli_version

    @property
    def cli_version(self) -> str | None:
        if self._cli_version:
            return self._cli_version
        from_env = (self.env.get("CODEX_CLI_VERSION") or "").strip()
        if from_env:
            return from_env
        for candidate in (
            self.env.get("CODEX_VERSION_FILE"),
            str(self.repo / ".codex-version"),
        ):
            if not candidate:
                continue
            path = Path(candidate)
            if path.is_file():
                value = path.read_text(encoding="utf-8", errors="replace").strip()
                if value:
                    return value
        return None

    def build_command(self, schema_path: Path, prompt: str) -> list[str]:
        return command_for(
            self.binary,
            "exec",
            "--sandbox",
            self.sandbox,
            "--output-schema",
            str(schema_path),
            prompt,
        )

    def run(self, prompt: str, *, task_id: str = "") -> PatchReport:
        version = self.cli_version
        with tempfile.TemporaryDirectory(prefix="codex-schema-") as directory:
            schema_path = Path(directory) / "patch_report.schema.json"
            schema_path.write_text(json.dumps(OUTPUT_SCHEMA, indent=2), encoding="utf-8")
            command = self.build_command(schema_path, prompt)
            started = time.perf_counter()
            popen_kwargs: dict[str, object] = {}
            if os.name != "nt":
                popen_kwargs["start_new_session"] = True
            try:
                process = subprocess.Popen(
                    command,
                    cwd=str(self.repo),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=self.env,
                    **popen_kwargs,
                )
            except OSError as error:
                return PatchReport(
                    engine="codex",
                    cli_version=version,
                    engine_error="spawn_failed",
                    error_detail=redact(f"{type(error).__name__}: {error}", env=self.env),
                )
            try:
                stdout, stderr = process.communicate(timeout=self.timeout_s)
            except subprocess.TimeoutExpired:
                _kill_tree(process.pid)
                stdout, stderr = process.communicate()
                return PatchReport(
                    engine="codex",
                    cli_version=version,
                    engine_error="timeout",
                    error_detail=redact(
                        f"timed out after {self.timeout_s}s; stderr: {stderr}", env=self.env
                    ),
                    exit_code=None,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )
            duration_ms = int((time.perf_counter() - started) * 1000)
            if process.returncode != 0:
                return PatchReport(
                    engine="codex",
                    cli_version=version,
                    engine_error="nonzero_exit",
                    error_detail=redact(stderr or stdout, env=self.env),
                    exit_code=process.returncode,
                    duration_ms=duration_ms,
                )
        try:
            payload = _EnginePayload.model_validate(json.loads((stdout or "").strip()))
        except (ValueError, ValidationError) as error:
            return PatchReport(
                engine="codex",
                cli_version=version,
                engine_error="invalid_json",
                error_detail=redact(f"invalid JSON: {error}", env=self.env),
                exit_code=0,
                duration_ms=duration_ms,
            )
        return PatchReport(
            files_changed=[item.replace("\\", "/") for item in payload.files_changed],
            summary=payload.summary,
            engine="codex",
            cli_version=version or payload.cli_version,
            exit_code=0,
            duration_ms=duration_ms,
        )