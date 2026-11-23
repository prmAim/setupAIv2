"""Codex CLI adapter behind :class:`PatchReport`.

The adapter never raises for engine-side failures: invalid JSON, an empty
answer, a non-zero exit, and a timeout all come back as a ``PatchReport``
carrying ``engine_error`` plus redacted, bounded ``error_detail``.

Two rules make the adapter safe for metrics:

* only *task* failures are errors the agent owns; quota, auth, network,
  timeout and spawn failures are environment failures
  (:data:`patch_report.INFRASTRUCTURE_ERRORS`) and must be counted separately;
* nothing that reaches evidence is unredacted, and stderr is capped at 2000
  characters (``process_util.STDERR_LIMIT``).

Tests drive it with ``.cmd`` stubs through ``CODEX_BIN``; the real CLI is
never invoked by the test suite.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import tempfile
import time
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from patch_report import PatchReport
from process_util import (
    SECRET_ENV_MARKERS,
    command_for,
    process_env,
    redact,
    resolve_binary,
)

DEFAULT_CODEX_BIN = r"C:\Git\_wf\codex-pinned\node_modules\.bin\codex.cmd"
DEFAULT_TIMEOUT_S = 900
DEFAULT_SANDBOX = "workspace-write"
SANDBOX_PATTERN = re.compile(r"^(read-only|workspace-write|danger-full-access)$")
SCHEMA_NAME = "patch_report.schema.json"
LAST_MESSAGE_NAME = "last-message.json"

OUTPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["files_changed", "summary", "tests_added", "notes"],
    "properties": {
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
        "tests_added": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "array", "items": {"type": "string"}},
    },
}

QUOTA_PATTERN = re.compile(r"(?i)\b(?:usage limit|try again at|rate limit)\b")
AUTH_PATTERN = re.compile(r"(?i)(?:\b401\b|not logged in|unauthenticated)")
NETWORK_PATTERN = re.compile(
    r"(?i)(?:\b403\b|unsupported_country_region_territory|cloudflare"
    r"|network error|connection (?:reset|refused)|getaddrinfo|\bENOTFOUND\b)"
)
ISO_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
)
TRY_AGAIN_AT = re.compile(r"(?i)try again at\s+([^\r\n]+)")
RESET_AT = re.compile(r"(?i)\breset(?:s|_at)?\s*(?:at|:)?\s*([^\r\n]+)")
CLOCK = re.compile(r"(?i)\b(\d{1,2}):(\d{2})\s*(am|pm)?\b")

_VERSION_CACHE: dict[str, str | None] = {}

class CodexProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeout_s: int = Field(default=DEFAULT_TIMEOUT_S, ge=1, le=3600)
    sandbox: str = Field(default=DEFAULT_SANDBOX, pattern=SANDBOX_PATTERN.pattern)
    cli_version: str | None = None

class _EnginePayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    files_changed: list[str] = Field(default_factory=list)
    summary: str = ""
    tests_added: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    cli_version: str | None = None

def engine_env(extra: dict[str, Any] | None = None) -> dict[str, str]:
    """Child environment without credentials: subscription auth lives in CODEX_HOME."""
    merged = process_env(extra)
    for key in list(merged):
        if any(marker in key.upper() for marker in SECRET_ENV_MARKERS):
            merged.pop(key, None)
    return merged

def _config_path(env: dict[str, str]) -> Path:
    override = (env.get("CODEX_CONFIG_FILE") or "").strip()
    if override:
        return Path(override)
    return Path(env.get("CODEX_HOME") or Path.home() / ".codex") / "config.toml"

def resolve_model(env: dict[str, str]) -> str | None:
    """``CODEX_MODEL`` wins, then ``model =`` from the Codex config.toml."""
    explicit = (env.get("CODEX_MODEL") or "").strip()
    if explicit:
        return explicit
    path = _config_path(env)
    try:
        with path.open("rb") as handle:
            config = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    model = config.get("model")
    return str(model).strip() if isinstance(model, str) and model.strip() else None

def _extract_version(text: str) -> str | None:
    match = re.search(r"\b(\d+\.\d+\.\d+)\b", text or "")
    return match.group(1) if match else None

def probe_cli_version(binary: str, env: dict[str, str], *, timeout_s: int = 60) -> str | None:
    """Ask the pinned CLI for its version once per binary; never calls the model."""
    if binary in _VERSION_CACHE:
        return _VERSION_CACHE[binary]
    try:
        completed = subprocess.run(
            command_for(binary, "--version"),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            env=env,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        version = None
    else:
        version = _extract_version(f"{completed.stdout or ''}\n{completed.stderr or ''}")
    _VERSION_CACHE[binary] = version
    return version

def _to_utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _parse_iso(raw: str) -> str | None:
    candidate = raw.strip().replace(" ", "T")
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    if re.search(r"[+-]\d{2}$", candidate):
        candidate = f"{candidate}:00"
    try:
        moment = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return _to_utc(moment)

def parse_reset_at(text: str) -> tuple[str | None, str | None]:
    """Return the raw reset hint and, when possible, the same moment in UTC.

    Codex reports quota windows both as an ISO timestamp and as a local clock
    time ("try again at 11:04 PM").  The clock form is interpreted in the host
    timezone, picking the next occurrence, so a reset that already passed today
    lands tomorrow instead of in the past.
    """
    if not text:
        return None, None
    iso = ISO_TIMESTAMP.search(text)
    if iso:
        raw = iso.group(0)
        return raw, _parse_iso(raw)
    for pattern in (TRY_AGAIN_AT, RESET_AT):
        match = pattern.search(text)
        if not match:
            continue
        raw = match.group(1).strip().strip(" .;,\"'`)]}")
        clock = CLOCK.search(raw)
        if not clock:
            return raw or None, None
        hour = int(clock.group(1))
        minute = int(clock.group(2))
        meridiem = (clock.group(3) or "").lower()
        if meridiem == "pm" and hour < 12:
            hour += 12
        if meridiem == "am" and hour == 12:
            hour = 0
        now = datetime.now().astimezone()
        moment = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if moment <= now:
            moment += timedelta(days=1)
        return raw or None, _to_utc(moment)
    return None, None

def classify_failure(text: str) -> str | None:
    """Map CLI output onto the three environment failures, else ``None``."""
    if not text:
        return None
    if QUOTA_PATTERN.search(text):
        return "quota_exhausted"
    if AUTH_PATTERN.search(text):
        return "auth_expired"
    if NETWORK_PATTERN.search(text):
        return "network_blocked"
    return None

def _last_json_object(text: str) -> dict[str, Any] | None:
    """Return the last JSON object in ``text`` (Codex may print logs around it)."""
    window = text[-200_000:]
    decoder = json.JSONDecoder()
    best: tuple[int, dict[str, Any]] | None = None
    for index, character in enumerate(window):
        if character != "{":
            continue
        try:
            value, end = decoder.raw_decode(window, index)
        except ValueError:
            continue
        if isinstance(value, dict) and (best is None or end > best[0]):
            best = (end, value)
    return best[1] if best else None

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
        model: str | None = None,
        timeout_s: int | None = None,
        sandbox: str | None = None,
        cli_version: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.repo = Path(repo).resolve()
        self.env = engine_env(env)
        self.binary = binary or resolve_binary("CODEX_BIN", DEFAULT_CODEX_BIN, self.env)
        self.timeout_s = timeout_s if timeout_s is not None else self._env_timeout()
        self.sandbox = sandbox or (self.env.get("CODEX_SANDBOX") or DEFAULT_SANDBOX).strip()
        if not SANDBOX_PATTERN.match(self.sandbox):
            raise ValueError(f"unsupported CODEX_SANDBOX: {self.sandbox!r}")
        self.model = model if model is not None else resolve_model(self.env)
        self._cli_version = cli_version

    def _env_timeout(self) -> int:
        raw = (self.env.get("CODEX_TIMEOUT_S") or "").strip()
        try:
            value = int(raw)
        except ValueError:
            return DEFAULT_TIMEOUT_S
        return value if 1 <= value <= 3600 else DEFAULT_TIMEOUT_S

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
        self._cli_version = probe_cli_version(self.binary, self.env)
        return self._cli_version

    def _is_git_worktree(self) -> bool:
        for directory in (self.repo, *self.repo.parents):
            if (directory / ".git").exists():
                return True
        return False

    def build_command(
        self,
        schema_path: Path,
        prompt: str,
        last_message_path: Path | None = None,
    ) -> list[str]:
        if last_message_path is None:
            last_message_path = Path(schema_path).with_name(LAST_MESSAGE_NAME)
        args = [
            "exec",
            "--sandbox",
            self.sandbox,
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(last_message_path),
        ]
        if self.model:
            args.extend(["--model", self.model])
        if not self._is_git_worktree():
            args.append("--skip-git-repo-check")
        args.append(prompt)
        return command_for(self.binary, *args)

    def _failure(
        self,
        *,
        stdout: str,
        stderr: str,
        exit_code: int | None,
        duration_ms: int,
        version: str | None,
        default_error: str,
    ) -> PatchReport:
        combined = f"{stderr}\n{stdout}".strip()
        error = classify_failure(combined) or default_error
        reset_at = reset_at_utc = None
        if error == "quota_exhausted":
            reset_at, reset_at_utc = parse_reset_at(combined)
        return PatchReport(
            engine="codex",
            cli_version=version,
            engine_error=error,
            error_detail=redact(combined, env=self.env),
            reset_at=reset_at,
            reset_at_utc=reset_at_utc,
            exit_code=exit_code,
            duration_ms=duration_ms,
        )

    def run(self, prompt: str, *, task_id: str = "") -> PatchReport:
        version = self.cli_version
        started = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="codex-run-") as directory:
            temporary = Path(directory)
            schema_path = temporary / SCHEMA_NAME
            schema_path.write_text(json.dumps(OUTPUT_SCHEMA, indent=2), encoding="utf-8")
            last_message_path = temporary / LAST_MESSAGE_NAME
            command = self.build_command(schema_path, prompt, last_message_path)
            popen_kwargs: dict[str, object] = {}
            if os.name != "nt":
                popen_kwargs["start_new_session"] = True
            try:
                process = subprocess.Popen(
                    command,
                    cwd=str(self.repo),
                    stdin=subprocess.DEVNULL,
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
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )
            try:
                stdout, stderr = process.communicate(timeout=self.timeout_s)
            except subprocess.TimeoutExpired:
                _kill_tree(process.pid)
                try:
                    stdout, stderr = process.communicate(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    stdout, stderr = "", ""
                return self._failure(
                    stdout=stdout or "",
                    stderr=stderr or f"timed out after {self.timeout_s}s",
                    exit_code=None,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                    version=version,
                    default_error="timeout",
                )
            duration_ms = int((time.perf_counter() - started) * 1000)
            stdout = stdout or ""
            stderr = stderr or ""
            if process.returncode != 0:
                return self._failure(
                    stdout=stdout,
                    stderr=stderr,
                    exit_code=process.returncode,
                    duration_ms=duration_ms,
                    version=version,
                    default_error="engine_failed",
                )
            answer = ""
            if last_message_path.is_file():
                answer = last_message_path.read_text(encoding="utf-8", errors="replace").strip()
            payload = _last_json_object(answer) or _last_json_object(stdout)
            if payload is None:
                if not (answer.strip() or stdout.strip() or stderr.strip()):
                    return PatchReport(
                        engine="codex",
                        cli_version=version,
                        engine_error="empty_output",
                        error_detail="engine exited 0 without any output",
                        exit_code=0,
                        duration_ms=duration_ms,
                    )
                return PatchReport(
                    engine="codex",
                    cli_version=version,
                    engine_error="invalid_json",
                    error_detail=redact(
                        f"no JSON object in engine output; tail: {answer or stdout}",
                        env=self.env,
                    ),
                    exit_code=0,
                    duration_ms=duration_ms,
                )
        try:
            parsed = _EnginePayload.model_validate(payload)
        except ValidationError as error:
            return PatchReport(
                engine="codex",
                cli_version=version,
                engine_error="invalid_json",
                error_detail=redact(
                    f"JSON does not match the patch report schema: {error}", env=self.env
                ),
                exit_code=0,
                duration_ms=duration_ms,
            )
        return PatchReport(
            files_changed=[item.replace("\\", "/") for item in parsed.files_changed],
            summary=parsed.summary,
            tests_added=[item.replace("\\", "/") for item in parsed.tests_added],
            notes=list(parsed.notes),
            engine="codex",
            cli_version=version or parsed.cli_version,
            exit_code=0,
            duration_ms=duration_ms,
        )