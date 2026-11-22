"""Tracer-bullet prototype: edit engines, gates, idempotent publish, Langfuse spans."""

from __future__ import annotations

import argparse
import contextlib
import difflib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Literal, TypedDict

from langfuse import Langfuse, propagate_attributes
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]
TOOLS = Path(__file__).resolve().parent
WORK_ROOT = Path(r"C:\Git\_wf")
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import test_integrity as ast_integrity  # noqa: E402  (tools dir is on sys.path above)
from codex_engine import CodexEngine, CodexProfile  # noqa: E402
from diff_scope import DEFAULT_PROFILE, DiffScopeRules, RuleHit  # noqa: E402
from diff_scope import evaluate as evaluate_diff_scope  # noqa: E402
from diff_scope import resolve_rules  # noqa: E402
from patch_report import PatchReport  # noqa: E402
from publish import GitHubClient, PublishConfig, Publisher  # noqa: E402

ENGINE = "fake"
MAX_FIX_ATTEMPTS = 3
DEFAULT_FORBIDDEN_PATHS = [
    ".github/",
    "db/migration/",
    ".env*",
    "uv.lock",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "poetry.lock",
    "Cargo.lock",
    "gradle.lockfile",
]
DEFAULT_TEST_ROOTS = ["tests"]
LINT_COMMAND = ["uv", "run", "--locked", "ruff", "check"]
TEST_COMMAND = ["uv", "run", "--locked", "python", "-m", "unittest", "discover", "-s", "tests", "-v"]
SKIP_MARKERS = re.compile(
    r"(@unittest\.skip|@skip\b|pytest\.mark\.(skip|xfail)|self\.skipTest\(|raise\s+unittest\.SkipTest)",
)


class SkeletonError(RuntimeError):
    """A deterministic, non-secret failure raised by a graph node."""


class Plan(BaseModel):
    task_id: str
    steps: list[str]
    strategy: Literal["fake-patch", "codex-patch"] = "fake-patch"
    expected_files: list[str]
    fix_attempts_allowed: int = 0


class GateResult(BaseModel):
    name: str
    command: list[str]
    exit_code: int
    duration_ms: int
    passed: bool
    detail: str = ""
    evidence: str | None = None
    rule: str | None = None
    findings: list[dict[str, str]] = Field(default_factory=list)


class TaskSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,63}$")
    repo: Path
    baseline_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    patch: dict[str, str]
    fix_patches: list[dict[str, str]] = Field(default_factory=list)
    forbidden_paths: list[str] | None = None
    diff_scope_profile: str = DEFAULT_PROFILE
    diff_scope_rules: DiffScopeRules | None = None
    test_roots: list[str] = Field(default_factory=lambda: list(DEFAULT_TEST_ROOTS))
    lint_command: list[str] = Field(default_factory=lambda: list(LINT_COMMAND))
    test_command: list[str] = Field(default_factory=lambda: list(TEST_COMMAND))
    engine: Literal["fake", "codex"] = "fake"
    codex: CodexProfile = Field(default_factory=CodexProfile)
    publish: PublishConfig | None = None
    notes: str = ""

    @classmethod
    def load(cls, path: Path) -> TaskSpec:
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def scoped_rules(self) -> DiffScopeRules:
        if self.diff_scope_rules is not None:
            return self.diff_scope_rules
        return resolve_rules(profile=self.diff_scope_profile, explicit=self.forbidden_paths)


class SkeletonState(TypedDict, total=False):
    task_id: str
    repo: str
    baseline_sha: str
    plan: dict[str, Any] | None
    diff: str
    patch_report: dict[str, Any] | None
    gate_results: dict[str, dict[str, Any]]
    attempts: int
    engine_calls: int
    evidence_paths: list[str]
    trace_id: str
    session_id: str
    previous_trace_id: str | None
    changed_files: list[str]
    verdict: str
    summary: str
    fix_history: list[str]
    publish: dict[str, Any]


class SkeletonSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
        populate_by_name=True,
    )

    langfuse_base_url: Literal["http://localhost:3000", "http://127.0.0.1:3000"] = (
        "http://localhost:3000"
    )
    langfuse_public_key: SecretStr = SecretStr("")
    langfuse_secret_key: SecretStr = SecretStr("")
    langfuse_environment: str = Field(default="development", validation_alias="APP_ENV")
    evidence_root: Path = Field(
        default=WORK_ROOT / "t13-evidence", validation_alias="T13_EVIDENCE_ROOT"
    )
    state_db: Path = Field(
        default=WORK_ROOT / "t13-state" / "t13.sqlite", validation_alias="T13_STATE_DB"
    )
    tracing: bool = Field(default=True, validation_alias="T13_TRACING")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def file_digest(path: Path) -> str:
    return sha256_bytes(path.read_bytes().replace(b"\r\n", b"\n"))


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def json_dump(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-c", "core.quotepath=false", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise SkeletonError(
            f"git {' '.join(args)} failed with exit {completed.returncode}: "
            f"{completed.stderr.strip()[:400]}"
        )
    return completed.stdout


def safe_relative(repo: Path, relative: str) -> Path:
    candidate = (repo / relative).resolve()
    if candidate != repo.resolve() and repo.resolve() not in candidate.parents:
        raise SkeletonError(f"patch path escapes the target repository: {relative}")
    return candidate


def is_forbidden(path: str, forbidden: list[str]) -> str | None:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    name = normalized.rsplit("/", 1)[-1]
    for pattern in forbidden:
        if pattern.endswith("/") and normalized.startswith(pattern):
            return pattern
        if pattern.endswith("*") and name.startswith(pattern[:-1]):
            return pattern
        if not pattern.endswith(("/", "*")) and (name == pattern or normalized == pattern):
            return pattern
    return None


class FakeEngine:
    """Deterministic patch applier: no network, no model, no subprocess."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo

    def apply(self, patch: dict[str, str], *, summary: str) -> PatchReport:
        if not patch:
            raise SkeletonError("fake engine received an empty patch")
        written = []
        for relative, content in sorted(patch.items()):
            target = safe_relative(self.repo, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
            cache = target.parent / "__pycache__"
            if cache.is_dir():
                shutil.rmtree(cache, ignore_errors=True)
            written.append(relative.replace("\\", "/"))
        return PatchReport(files_changed=written, summary=summary, cli_version=None)
class GitTool:
    """Module-level git with one tool span per command."""

    def __init__(self, repo: Path, recorder: "TraceRecorder", task_id: str) -> None:
        self.repo = repo
        self.recorder = recorder
        self.task_id = task_id

    def __call__(self, *args: str) -> str:
        command = ["git", *args]
        started = time.perf_counter()
        completed = subprocess.run(
            ["git", "-c", "core.quotepath=false", "-C", str(self.repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        duration_ms = int((time.perf_counter() - started) * 1000)
        self.recorder.tool(
            name=f"git {args[0] if args else ''}".strip(),
            cmd=command,
            exit_code=completed.returncode,
            duration_ms=duration_ms,
            metadata={"task_id": self.task_id},
        )
        if completed.returncode != 0:
            raise SkeletonError(
                f"git {' '.join(args)} failed with exit {completed.returncode}: "
                f"{completed.stderr.strip()[:400]}"
            )
        return completed.stdout


def build_patch_text(
    repo: Path,
    baseline_sha: str,
    files: list[str],
    *,
    git_runner: Callable[..., str] | None = None,
) -> str:
    runner = git_runner or (lambda *args: git(repo, *args))
    chunks: list[str] = []
    for relative in sorted(files):
        try:
            old = runner("show", f"{baseline_sha}:{relative}")
        except SkeletonError:
            old = ""
        current = safe_relative(repo, relative)
        if not current.is_file():
            new = ""
        else:
            try:
                new = current.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
        if old == new:
            continue
        from_label = f"a/{relative}" if old else "/dev/null"
        text = "".join(
            difflib.unified_diff(
                old.splitlines(keepends=True),
                new.splitlines(keepends=True),
                fromfile=from_label,
                tofile=f"b/{relative}",
            )
        )
        if text and not text.endswith("\n"):
            text += "\n"
        chunks.append(text)
    return "".join(chunks)


def changed_files(
    repo: Path,
    baseline_sha: str,
    *,
    git_runner: Callable[..., str] | None = None,
) -> list[str]:
    runner = git_runner or (lambda *args: git(repo, *args))
    tracked = [line for line in runner("diff", "--name-only", baseline_sha).splitlines() if line]
    untracked = [
        line for line in runner("ls-files", "--others", "--exclude-standard").splitlines() if line
    ]
    return sorted({line.replace("\\", "/") for line in tracked + untracked})


class TraceRecorder:
    """Root span, node spans, engine generations, tool spans, session linkage."""

    def __init__(
        self,
        settings: SkeletonSettings,
        *,
        client_factory: Callable[[SkeletonSettings], Any] | None = None,
    ) -> None:
        self.note = "disabled"
        self.client: Any = None
        self.trace_id = ""
        self.session_id = ""
        self.previous_trace_id: str | None = None
        self.root: Any = None
        self.span_names: set[str] = set()
        self.emitted: list[dict[str, Any]] = []
        os.environ["NO_PROXY"] = "localhost,127.0.0.1"
        os.environ["no_proxy"] = "localhost,127.0.0.1"
        public = settings.langfuse_public_key.get_secret_value().strip()
        secret = settings.langfuse_secret_key.get_secret_value().strip()
        if not settings.tracing:
            self.note = "disabled: tracing is off for this run"
        elif client_factory is not None:
            self.client = client_factory(settings)
            self.note = "enabled: recording double"
        elif not public or not secret:
            self.note = "disabled: Langfuse keys are not configured"
        else:
            self.client = Langfuse(
                public_key=public,
                secret_key=secret,
                base_url=settings.langfuse_base_url,
                environment=settings.langfuse_environment,
                timeout=10,
                flush_at=1,
            )
            self.note = "enabled"

    @contextlib.contextmanager
    def session(self, session_id: str) -> Iterator[None]:
        self.session_id = session_id
        if self.client is None:
            yield
            return
        with propagate_attributes(session_id=session_id):
            yield

    def _emit(self, *, name: str, as_type: str, metadata: dict[str, Any]) -> None:
        self.span_names.add(name)
        self.emitted.append(
            {
                "name": name,
                "type": as_type.upper(),
                "trace_id": self.trace_id,
                "session_id": self.session_id,
                "metadata": metadata,
            }
        )

    def start(
        self,
        *,
        name: str,
        metadata: dict[str, Any],
        input: Any,
        session_id: str | None = None,
        previous_trace_id: str | None = None,
        trace_id: str | None = None,
    ) -> None:
        if session_id:
            self.session_id = session_id
        self.previous_trace_id = previous_trace_id
        seed = f"{metadata['task_id']}:{metadata['baseline_sha']}:{metadata.get('engine')}"
        seed = f"{seed}:{self.session_id}:{previous_trace_id}"
        if self.client is None:
            self.trace_id = trace_id or sha256_text(seed)[:32]
            return
        self.trace_id = self.client.create_trace_id()
        span_metadata = {
            **metadata,
            "session_id": self.session_id,
            "previous_trace_id": self.previous_trace_id,
        }
        self.root = self.client.start_observation(
            name=name,
            as_type="agent",
            trace_context={"trace_id": self.trace_id},
            input=input,
            metadata=span_metadata,
        )
        self._emit(name=name, as_type="agent", metadata=span_metadata)

    def node(self, name: str, *, metadata: dict[str, Any], input: Any) -> Any:
        if self.client is None or self.root is None:
            return None
        span = self.client.start_observation(
            name=name,
            as_type="chain",
            trace_context={"trace_id": self.trace_id, "parent_span_id": self.root.id},
            input=input,
            metadata={**metadata, "node": name},
        )
        self._emit(name=name, as_type="chain", metadata={**metadata, "node": name})
        return span

    def generation(
        self,
        *,
        name: str,
        metadata: dict[str, Any],
        input: Any,
        output_bytes: int,
    ) -> None:
        payload = {**metadata, "output_bytes": int(output_bytes)}
        if self.client is not None and self.root is not None:
            span = self.client.start_observation(
                name=name,
                as_type="generation",
                trace_context={"trace_id": self.trace_id, "parent_span_id": self.root.id},
                input=input,
                metadata=payload,
            )
            self.close(span, {"output_bytes": int(output_bytes)})
        self._emit(name=name, as_type="generation", metadata=payload)

    def tool(
        self,
        *,
        name: str,
        cmd: list[str],
        exit_code: int,
        duration_ms: int,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        payload = {
            "cmd": list(cmd),
            "exit": int(exit_code),
            "duration_ms": int(duration_ms),
            **(metadata or {}),
        }
        if self.client is not None and self.root is not None:
            span = self.client.start_observation(
                name=name,
                as_type="tool",
                trace_context={"trace_id": self.trace_id, "parent_span_id": self.root.id},
                metadata=payload,
            )
            self.close(span, {"exit": int(exit_code), "duration_ms": int(duration_ms)})
        self._emit(name=name, as_type="tool", metadata=payload)

    def close(self, span: Any, output: Any, *, error: str | None = None) -> None:
        if span is None:
            return
        if error is None:
            span.update(output=output)
        else:
            span.update(output={"error": error}, level="ERROR", status_message=error)
        span.end()

    def finish(self, output: dict[str, Any]) -> None:
        self.close(self.root, output)

    def read_back(self, *, environment: str | None = None, deadline_s: float = 30.0) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "status": self.note,
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "previous_trace_id": self.previous_trace_id,
        }
        if self.client is None:
            return summary
        if not hasattr(self.client, "api"):
            return {**summary, "status": "local-recorder", **self._summarise(self.emitted)}
        self.client.flush()
        deadline = time.monotonic() + deadline_s
        observations: list[Any] = []
        while True:
            kwargs: dict[str, Any] = {
                "session_id": self.session_id,
                "fields": "core,basic,io,model,metadata",
                "limit": 200,
            }
            if environment:
                kwargs["environment"] = environment
            observations = self.client.api.observations.get_many(**kwargs).data
            seen = {getattr(item, "name", "") for item in observations}
            if self.span_names.issubset(seen) or time.monotonic() >= deadline:
                break
            time.sleep(2)
        records = []
        for item in observations:
            data = item.model_dump() if hasattr(item, "model_dump") else {}
            records.append(
                {
                    "name": data.get("name") or getattr(item, "name", ""),
                    "type": str(data.get("type") or getattr(item, "type", "")),
                    "trace_id": data.get("trace_id") or getattr(item, "trace_id", ""),
                    "session_id": data.get("session_id") or getattr(item, "session_id", ""),
                    "metadata": data.get("metadata"),
                }
            )
        return {
            **summary,
            "status": "read-back",
            "trace_url": self.client.get_trace_url(trace_id=self.trace_id),
            "observations": len(records),
            "expected_observations": len(self.span_names),
            **self._summarise(records),
        }

    @staticmethod
    def _summarise(records: list[dict[str, Any]]) -> dict[str, Any]:
        traces: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"session_ids": set(), "types": set(), "names": []}
        )
        for item in records:
            trace = traces[item["trace_id"]]
            if item.get("session_id"):
                trace["session_ids"].add(item["session_id"])
            trace["types"].add(item["type"])
            trace["names"].append(item["name"])
        return {
            "trace_ids": sorted(traces),
            "traces": {
                key: {
                    "session_ids": sorted(value["session_ids"]),
                    "types": sorted(value["types"]),
                    "names": sorted(set(value["names"])),
                }
                for key, value in sorted(traces.items())
            },
        }

    def shutdown(self) -> None:
        if self.client is not None and hasattr(self.client, "shutdown"):
            self.client.shutdown()

class Runtime:
    """Shared, per-run context for graph nodes (task, settings, recorder)."""

    def __init__(
        self,
        task: TaskSpec,
        settings: SkeletonSettings,
        recorder: TraceRecorder,
        *,
        sleep_in_test: float = 0.0,
    ) -> None:
        self.task = task
        self.settings = settings
        self.recorder = recorder
        self.sleep_in_test = sleep_in_test
        self.repo = task.repo.resolve()
        self.evidence = settings.evidence_root / task.task_id
        self.rules = task.scoped_rules()
        self.engine_name = task.engine
        if task.engine == "codex":
            self.engine: FakeEngine | CodexEngine = CodexEngine(
                self.repo,
                timeout_s=task.codex.timeout_s,
                sandbox=task.codex.sandbox,
                cli_version=task.codex.cli_version,
            )
        else:
            self.engine = FakeEngine(self.repo)
        self.git = GitTool(self.repo, recorder, task.task_id)
        self.metadata = {
            "task_id": task.task_id,
            "repo": str(self.repo),
            "baseline_sha": task.baseline_sha,
            "engine": task.engine,
            "cli_version": task.codex.cli_version,
        }

    def record_gate(self, result: GateResult) -> dict[str, Any]:
        path = self.evidence / f"gate-{result.name}.json"
        json_dump(path, result.model_dump(mode="json"))
        return {**result.model_dump(mode="json"), "evidence": str(path)}

    def not_run(self, name: str, reason: str) -> dict[str, Any]:
        return self.record_gate(
            GateResult(
                name=name,
                command=["not-run"],
                exit_code=1,
                duration_ms=0,
                passed=False,
                detail=reason,
            )
        )

    def run_gate(
        self,
        name: str,
        command: list[str],
        *,
        log_name: str | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        completed = subprocess.run(
            command,
            cwd=self.repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=1800,
        )
        duration_ms = int((time.perf_counter() - started) * 1000)
        output = (completed.stdout or "") + (completed.stderr or "")
        if log_name:
            (self.evidence / log_name).write_text(output, encoding="utf-8", newline="\n")
        self.recorder.tool(
            name=name,
            cmd=list(command),
            exit_code=completed.returncode,
            duration_ms=duration_ms,
            metadata={"task_id": self.task.task_id},
        )
        tail = "\n".join(line for line in output.splitlines() if line.strip())[-1200:]
        result = GateResult(
            name=name,
            command=command,
            exit_code=completed.returncode,
            duration_ms=duration_ms,
            passed=completed.returncode == 0,
            detail=tail,
        )
        return self.record_gate(result)

    def test_paths(self) -> list[str]:
        """Test files under ``test_roots`` in the baseline and the working tree."""
        paths: set[str] = set()
        for root in self.task.test_roots:
            normalized = root.replace("\\", "/").strip("/")
            try:
                listing = self.git(
                    "ls-tree",
                    "-r",
                    "--name-only",
                    self.task.baseline_sha,
                    "--",
                    normalized,
                )
            except SkeletonError:
                listing = ""
            paths.update(
                line.replace("\\", "/") for line in listing.splitlines() if line.strip()
            )
            base = safe_relative(self.repo, normalized)
            if base.is_dir():
                for path in base.rglob("*"):
                    if path.is_file() and "__pycache__" not in path.parts:
                        paths.add(path.relative_to(self.repo).as_posix())
        return sorted(item for item in paths if item.endswith((".py", ".java")))

    def test_integrity(self) -> dict[str, Any]:
        started = time.perf_counter()
        paths = self.test_paths()
        comparison = ast_integrity.compare(self.repo, self.task.baseline_sha, paths)
        findings = [
            {"file": item.file, "rule": item.rule, "evidence": item.evidence}
            for item in comparison.findings
        ]
        baseline_set: set[str] = set()
        for root in self.task.test_roots:
            listing = self.git(
                "ls-tree", "-r", "--name-only", self.task.baseline_sha, "--", root
            )
            baseline_set.update(
                line.replace("\\", "/") for line in listing.splitlines() if line.strip()
            )
        for relative in paths:
            if relative in baseline_set or not relative.endswith(".py"):
                continue
            target = self.repo / relative
            if not target.is_file():
                continue
            marker = SKIP_MARKERS.search(target.read_text(encoding="utf-8", errors="replace"))
            if marker:
                findings.append(
                    {
                        "file": relative,
                        "rule": "skip-marker-added",
                        "evidence": f"skip/xfail marker in new test file: {marker.group(0)}",
                    }
                )
        findings.sort(key=lambda item: (item["file"], item["rule"], item["evidence"]))
        rules = sorted({item["rule"] for item in findings})
        if findings:
            detail = "; ".join(
                f"{item['rule']}: {item['file']}"
                + (f" ({item['evidence']})" if item["evidence"] else "")
                for item in findings
            )
        else:
            detail = f"{comparison.summary}; new test files carry no skip/xfail markers"
        exit_code = 1 if findings else comparison.exit_code
        result = GateResult(
            name="test-integrity",
            command=["ast", "git-show", self.task.baseline_sha, *self.task.test_roots],
            exit_code=exit_code,
            duration_ms=int((time.perf_counter() - started) * 1000),
            passed=exit_code == 0,
            detail=detail[:2000],
            rule=", ".join(rules) if rules else None,
            findings=findings,
        )
        recorded = self.record_gate(result)
        self.recorder.tool(
            name="ast-test-integrity",
            cmd=result.command,
            exit_code=exit_code,
            duration_ms=result.duration_ms,
            metadata={"task_id": self.task.task_id},
        )
        return recorded

    def diff_scope(self) -> dict[str, Any]:
        started = time.perf_counter()
        files = changed_files(self.repo, self.task.baseline_sha, git_runner=self.git)
        hits: list[RuleHit] = evaluate_diff_scope(self.rules, files)
        detail = (
            "; ".join(f"{hit.path}: {hit.reason}" for hit in hits)
            if hits
            else f"{len(files)} changed files, none outside the allowed scope"
        )
        rules = sorted({hit.rule for hit in hits})
        result = GateResult(
            name="diff-scope",
            command=["git", "diff", "--name-only", self.task.baseline_sha],
            exit_code=1 if hits else 0,
            duration_ms=int((time.perf_counter() - started) * 1000),
            passed=not hits,
            detail=detail[:2000],
            rule=", ".join(rules) if rules else None,
            findings=[hit.model_dump() for hit in hits],
        )
        return self.record_gate(result)

    def apply_patch(self, patch: dict[str, str], summary: str) -> dict[str, Any]:
        prompt = json.dumps({"summary": summary, "files": sorted(patch)}, sort_keys=True)
        prompt_sha = sha256_text(prompt)
        started = time.perf_counter()
        if isinstance(self.engine, CodexEngine):
            report = self.engine.run(prompt, task_id=self.task.task_id)
        else:
            report = self.engine.apply(patch, summary=summary)
        duration_ms = int((time.perf_counter() - started) * 1000)
        self.recorder.generation(
            name=f"engine:{self.task.task_id}:{len(self.recorder.emitted)}",
            metadata={
                "engine": report.engine,
                "cli_version": report.cli_version or "none",
                "prompt_sha256": prompt_sha,
                "task_id": self.task.task_id,
                "duration_ms": duration_ms,
            },
            input={"prompt_sha256": prompt_sha, "files": sorted(patch)},
            output_bytes=len(report.model_dump_json().encode("utf-8")),
        )
        files = sorted(
            set(changed_files(self.repo, self.task.baseline_sha, git_runner=self.git))
            | set(report.files_changed)
        )
        return {
            "patch_report": report.model_dump(mode="json"),
            "diff": build_patch_text(
                self.repo, self.task.baseline_sha, files, git_runner=self.git
            ),
            "changed_files": files,
        }

    def publisher(self, config: PublishConfig) -> Publisher:
        def on_tool(command: list[str], exit_code: int, duration_ms: int) -> None:
            self.recorder.tool(
                name="git publish",
                cmd=command,
                exit_code=exit_code,
                duration_ms=duration_ms,
                metadata={"task_id": self.task.task_id},
            )

        return Publisher(self.repo, config, GitHubClient(), on_tool=on_tool)

def build_graph(
    task: TaskSpec,
    settings: SkeletonSettings,
    recorder: TraceRecorder,
    *,
    checkpointer: Any = None,
    sleep_in_test: float = 0.0,
):
    runtime = Runtime(task, settings, recorder, sleep_in_test=sleep_in_test)
    evidence = runtime.evidence
    metadata = runtime.metadata
    publish_enabled = task.publish is not None

    def traced(
        name: str,
        summarize: Callable[[dict[str, Any]], Any],
        payload: Callable[[SkeletonState], Any],
    ):
        def wrap(node: Callable[[SkeletonState], dict[str, Any]]):
            def invoke(state: SkeletonState) -> dict[str, Any]:
                span = recorder.node(name, metadata=metadata, input=payload(state))
                try:
                    update = node(state)
                except Exception as error:
                    recorder.close(span, None, error=f"{type(error).__name__}: {error}")
                    raise
                recorder.close(span, summarize(update))
                return update

            return invoke

        return wrap

    def ingest(state: SkeletonState) -> dict[str, Any]:
        repo = runtime.repo
        if not (repo / ".git").exists():
            raise SkeletonError(f"target repository is not a git checkout: {repo}")
        head = runtime.git("rev-parse", "HEAD").strip()
        if head != task.baseline_sha:
            raise SkeletonError(
                f"baseline mismatch: HEAD {head[:12]} != task baseline {task.baseline_sha[:12]}"
            )
        evidence.mkdir(parents=True, exist_ok=True)
        return {
            "task_id": task.task_id,
            "repo": str(repo),
            "baseline_sha": task.baseline_sha,
            "attempts": state.get("attempts", 0),
            "engine_calls": state.get("engine_calls", 0),
            "gate_results": state.get("gate_results", {}),
            "evidence_paths": state.get("evidence_paths", []),
            "plan": None,
            "diff": "",
            "patch_report": None,
            "changed_files": [],
            "verdict": "",
            "summary": "",
            "fix_history": [],
            "publish": state.get("publish", {}),
        }

    def plan(state: SkeletonState) -> dict[str, Any]:
        steps = ["ingest", "plan", f"edit({task.engine})", "fast_gate", "test", "gates", "evidence"]
        if publish_enabled:
            steps.extend(["push", "open_pr"])
        report = Plan(
            task_id=task.task_id,
            steps=steps,
            strategy="codex-patch" if task.engine == "codex" else "fake-patch",
            expected_files=sorted(path.replace("\\", "/") for path in task.patch),
            fix_attempts_allowed=min(MAX_FIX_ATTEMPTS, len(task.fix_patches)),
        )
        return {"plan": report.model_dump(mode="json")}

    def edit(state: SkeletonState) -> dict[str, Any]:
        update = runtime.apply_patch(
            task.patch, f"{runtime.engine_name} engine applied {len(task.patch)} file(s)"
        )
        results = dict(state.get("gate_results", {}))
        report = update["patch_report"]
        if report.get("engine_error"):
            results["engine"] = runtime.record_gate(
                GateResult(
                    name="engine",
                    command=["engine", str(report.get("engine"))],
                    exit_code=int(report.get("exit_code") or 1),
                    duration_ms=int(report.get("duration_ms") or 0),
                    passed=False,
                    detail=(report.get("error_detail") or report["engine_error"])[:2000],
                    rule=str(report["engine_error"]),
                )
            )
        return {
            **update,
            "gate_results": results,
            "engine_calls": int(state.get("engine_calls", 0)) + 1,
        }

    def fast_gate(state: SkeletonState) -> dict[str, Any]:
        results = dict(state.get("gate_results", {}))
        for result in (runtime.test_integrity(), runtime.diff_scope()):
            results[result["name"]] = result
        return {"gate_results": results}

    def run_tests(state: SkeletonState) -> dict[str, Any]:
        if runtime.sleep_in_test:
            (evidence / ".test-node-started").write_text(
                str(os.getpid()), encoding="utf-8", newline="\n"
            )
            time.sleep(runtime.sleep_in_test)
        result = runtime.run_gate("tests", list(task.test_command), log_name="tests.log")
        results = dict(state.get("gate_results", {}))
        results["tests"] = result
        return {"gate_results": results}

    def fix(state: SkeletonState) -> dict[str, Any]:
        attempts = int(state.get("attempts", 0))
        history = list(state.get("fix_history") or [])
        allowed = min(MAX_FIX_ATTEMPTS, len(task.fix_patches))
        if attempts >= allowed:
            return {
                "attempts": attempts,
                "fix_history": [*history, "fix cap reached; no further engine call"],
            }
        update = runtime.apply_patch(
            task.fix_patches[attempts],
            f"{runtime.engine_name} engine applied fix patch {attempts + 1}",
        )
        return {
            **update,
            "attempts": attempts + 1,
            "engine_calls": int(state.get("engine_calls", 0)) + 1,
            "fix_history": [*history, f"fix patch {attempts + 1}"],
        }

    def gates(state: SkeletonState) -> dict[str, Any]:
        results = dict(state.get("gate_results", {}))
        for result in (runtime.test_integrity(), runtime.diff_scope()):
            results[result["name"]] = result
        results["lint"] = runtime.run_gate("lint", list(task.lint_command), log_name="lint.log")
        verdict = "pass" if all(item["passed"] for item in results.values()) else "fail"
        return {"gate_results": results, "verdict": verdict}

    def escalate(state: SkeletonState) -> dict[str, Any]:
        failed = sorted(
            name for name, item in state.get("gate_results", {}).items() if not item["passed"]
        )
        return {
            "verdict": "escalated",
            "summary": (
                "stopped before a green run; failed gates: "
                + (", ".join(failed) if failed else "unknown")
            ),
        }

    def push(state: SkeletonState) -> dict[str, Any]:
        result: dict[str, Any] = {"status": "skipped"}
        if task.publish is None:
            result["reason"] = "task has no publish config"
        elif state.get("verdict") != "pass":
            result["reason"] = f"verdict is {state.get('verdict') or 'missing'}"
        else:
            result = runtime.publisher(task.publish).push()
        return {"publish": {**(state.get("publish") or {}), "push": result}}

    def open_pr(state: SkeletonState) -> dict[str, Any]:
        result: dict[str, Any] = {"status": "skipped"}
        if task.publish is None:
            result["reason"] = "task has no publish config"
        elif state.get("verdict") != "pass":
            result["reason"] = f"verdict is {state.get('verdict') or 'missing'}"
        else:
            result = runtime.publisher(task.publish).open_pr()
        return {"publish": {**(state.get("publish") or {}), "open_pr": result}}

    def evidence_node(state: SkeletonState) -> dict[str, Any]:
        previous = evidence / "report.json"
        writes = 1
        if previous.is_file():
            try:
                stored = json.loads(previous.read_text(encoding="utf-8"))
                writes = int(stored.get("evidence_writes", 0)) + 1
            except (ValueError, TypeError):
                writes = 1
        marker = evidence / ".test-node-started"
        if marker.exists():
            marker.unlink()
        diff_text = state.get("diff", "")
        (evidence / "patch.diff").write_text(diff_text, encoding="utf-8", newline="\n")
        (evidence / "patch.sha256").write_text(
            sha256_text(diff_text) + "\n", encoding="utf-8", newline="\n"
        )
        files = sorted(set(state.get("changed_files") or []))
        (evidence / "diff-name-only.txt").write_text(
            "\n".join(files) + "\n", encoding="utf-8", newline="\n"
        )
        gate_results = dict(state.get("gate_results", {}))
        for name in ("test-integrity", "diff-scope", "lint", "tests"):
            if name not in gate_results:
                gate_results[name] = runtime.not_run(name, "gate not executed before escalation")
        bundle = sorted(
            [path.name for path in evidence.glob("gate-*.json")]
            + ["patch.diff", "patch.sha256", "diff-name-only.txt", "tests.log", "report.json"]
        )
        bundle = [name for name in bundle if (evidence / name).exists()]
        patch_report = state.get("patch_report") or {}
        scope_gate = gate_results.get("diff-scope") or {}
        scope_verdict = "pass" if scope_gate.get("passed") else "deny"
        report = {
            "task_id": task.task_id,
            "repo": str(runtime.repo),
            "baseline_sha": task.baseline_sha,
            "engine": patch_report.get("engine") or runtime.engine_name,
            "cli_version": patch_report.get("cli_version"),
            "verdict": state.get("verdict") or "fail",
            "diff_scope_verdict": scope_verdict,
            "attempts": int(state.get("attempts", 0)),
            "engine_calls": int(state.get("engine_calls", 0)),
            "trace_id": recorder.trace_id,
            "previous_trace_id": recorder.previous_trace_id,
            "session_id": recorder.session_id,
            "tracing": recorder.note,
            "plan": state.get("plan"),
            "patch_report": state.get("patch_report"),
            "patch_sha256": sha256_text(diff_text),
            "changed_files": files,
            "gates": {name: item for name, item in sorted(gate_results.items())},
            "summary": state.get("summary", ""),
            "fix_history": state.get("fix_history") or [],
            "publish": state.get("publish") or {},
            "evidence_paths": [str(evidence / name) for name in bundle],
            "evidence_writes": writes,
        }
        json_dump(previous, report)
        return {"evidence_paths": report["evidence_paths"], "verdict": report["verdict"]}

    def after_edit(state: SkeletonState) -> str:
        report = state.get("patch_report") or {}
        return "escalate" if report.get("engine_error") else "fast_gate"

    def after_fast_gate(state: SkeletonState) -> str:
        gates_state = state.get("gate_results", {})
        if gates_state.get("test-integrity", {}).get("passed", False) and gates_state.get(
            "diff-scope", {}
        ).get("passed", False):
            return "test"
        return "escalate"

    def after_test(state: SkeletonState) -> str:
        if state.get("gate_results", {}).get("tests", {}).get("passed", False):
            return "gates"
        allowed = min(MAX_FIX_ATTEMPTS, len(task.fix_patches))
        if int(state.get("attempts", 0)) < allowed:
            return "fix"
        return "escalate"

    def after_evidence(state: SkeletonState) -> str:
        if publish_enabled and state.get("verdict") == "pass":
            return "push"
        return "end"

    nodes = {
        "ingest": traced(
            "ingest",
            lambda update: {"repo": update["repo"], "baseline_sha": update["baseline_sha"]},
            lambda state: {"task_id": task.task_id},
        )(ingest),
        "plan": traced(
            "plan",
            lambda update: update["plan"],
            lambda state: {"patch": sorted(task.patch)},
        )(plan),
        "edit": traced(
            "edit",
            lambda update: update["patch_report"],
            lambda state: {"patch": sorted(task.patch)},
        )(edit),
        "fast_gate": traced(
            "fast_gate",
            lambda update: {
                name: item["exit_code"] for name, item in update["gate_results"].items()
            },
            lambda state: {"gates": ["test-integrity", "diff-scope"]},
        )(fast_gate),
        "test": traced(
            "test",
            lambda update: update["gate_results"]["tests"]["exit_code"],
            lambda state: {"command": task.test_command},
        )(run_tests),
        "fix": traced(
            "fix",
            lambda update: {"attempts": update["attempts"]},
            lambda state: {"attempts": state.get("attempts", 0)},
        )(fix),
        "gates": traced(
            "gates",
            lambda update: {
                name: item["exit_code"] for name, item in update["gate_results"].items()
            },
            lambda state: {"gates": ["test-integrity", "diff-scope", "lint"]},
        )(gates),
        "escalate": traced(
            "escalate",
            lambda update: update["verdict"],
            lambda state: {"attempts": state.get("attempts", 0)},
        )(escalate),
        "evidence": traced(
            "evidence",
            lambda update: {"files": len(update["evidence_paths"])},
            lambda state: {"verdict": state.get("verdict", "")},
        )(evidence_node),
        "push": traced(
            "push",
            lambda update: update["publish"].get("push"),
            lambda state: {"verdict": state.get("verdict", "")},
        )(push),
        "open_pr": traced(
            "open_pr",
            lambda update: update["publish"].get("open_pr"),
            lambda state: {"verdict": state.get("verdict", "")},
        )(open_pr),
    }
    builder = StateGraph(SkeletonState)
    for name, node in nodes.items():
        builder.add_node(name, node)
    builder.add_edge(START, "ingest")
    builder.add_edge("ingest", "plan")
    builder.add_edge("plan", "edit")
    builder.add_conditional_edges(
        "edit", after_edit, {"fast_gate": "fast_gate", "escalate": "escalate"}
    )
    builder.add_conditional_edges(
        "fast_gate", after_fast_gate, {"test": "test", "escalate": "escalate"}
    )
    builder.add_conditional_edges(
        "test", after_test, {"gates": "gates", "fix": "fix", "escalate": "escalate"}
    )
    builder.add_edge("fix", "test")
    builder.add_edge("gates", "evidence")
    builder.add_edge("escalate", "evidence")
    builder.add_conditional_edges("evidence", after_evidence, {"push": "push", "end": END})
    builder.add_edge("push", "open_pr")
    builder.add_edge("open_pr", END)
    return builder.compile(checkpointer=checkpointer)

def finalize_report(report_path: Path, updates: dict[str, Any]) -> dict[str, Any]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report.update(updates)
    json_dump(report_path, report)
    return report


def run_graph(
    *,
    task: TaskSpec,
    settings: SkeletonSettings,
    thread_id: str | None = None,
    resume: bool = False,
    sleep_in_test: float = 0.0,
    recorder: TraceRecorder | None = None,
) -> dict[str, Any]:
    settings.state_db.parent.mkdir(parents=True, exist_ok=True)
    recorder = recorder or TraceRecorder(settings)
    metadata = {
        "task_id": task.task_id,
        "repo": str(task.repo.resolve()),
        "baseline_sha": task.baseline_sha,
        "engine": task.engine,
        "cli_version": task.codex.cli_version,
    }
    connection = sqlite3.connect(settings.state_db, check_same_thread=False)
    config = {"configurable": {"thread_id": thread_id or task.task_id}, "recursion_limit": 60}
    resume_info: dict[str, Any] = {}
    try:
        saver = SqliteSaver(connection)
        saver.setup()
        app = build_graph(
            task, settings, recorder, checkpointer=saver, sleep_in_test=sleep_in_test
        )
        snapshot = app.get_state(config) if resume else None
        if resume:
            resume_info = {
                "checkpoints_before": sum(1 for _ in app.get_state_history(config)),
                "next": list(snapshot.next),
                "step": (snapshot.metadata or {}).get("step"),
                "has_values": bool(snapshot.values),
            }
        previous_trace = (snapshot.values.get("trace_id") if snapshot else None) or None
        with recorder.session(task.task_id):
            recorder.start(
                name=f"tracer-bullet:{task.task_id}",
                metadata=metadata,
                input={"thread_id": thread_id or task.task_id, "patch": sorted(task.patch)},
                session_id=task.task_id,
                previous_trace_id=previous_trace,
            )
            with tracing_context(enabled=False):
                if resume:
                    app.invoke(None, config)
                else:
                    app.invoke(
                        {
                            "trace_id": recorder.trace_id,
                            "session_id": task.task_id,
                            "previous_trace_id": None,
                            "attempts": 0,
                            "engine_calls": 0,
                            "gate_results": {},
                            "evidence_paths": [],
                            "verdict": "",
                            "fix_history": [],
                            "publish": {},
                        },
                        config,
                    )
            values = app.get_state(config).values
            report_path = settings.evidence_root / task.task_id / "report.json"
            recorder.finish(
                {
                    "verdict": values.get("verdict"),
                    "attempts": values.get("attempts"),
                    "engine_calls": values.get("engine_calls"),
                    "gates": {
                        name: item["exit_code"]
                        for name, item in (values.get("gate_results") or {}).items()
                    },
                }
            )
        trace_info = recorder.read_back(environment=settings.langfuse_environment)
        return finalize_report(
            report_path,
            {
                "trace_id": recorder.trace_id,
                "previous_trace_id": previous_trace,
                "session_id": task.task_id,
                "trace": trace_info,
                "publish": values.get("publish") or {},
                "checkpoints": sum(1 for _ in app.get_state_history(config)),
                "resume": resume_info or None,
            },
        )
    finally:
        recorder.shutdown()
        connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="execute the tracer-bullet graph")
    run.add_argument("--task", type=Path, required=True)
    run.add_argument("--thread", default=None)
    run.add_argument("--resume", action="store_true")
    run.add_argument("--sleep-in-test", type=float, default=0.0)
    run.add_argument("--evidence-root", type=Path, default=None)
    run.add_argument("--state-db", type=Path, default=None)
    run.add_argument("--env-file", type=Path, default=None)
    run.add_argument("--no-tracing", action="store_true")
    args = parser.parse_args(argv)
    task = TaskSpec.load(args.task)
    overrides: dict[str, Any] = {}
    if args.evidence_root is not None:
        overrides["evidence_root"] = args.evidence_root
    if args.state_db is not None:
        overrides["state_db"] = args.state_db
    if args.env_file is not None:
        overrides["_env_file"] = args.env_file
    if args.no_tracing:
        overrides["tracing"] = False
    settings = SkeletonSettings(**overrides)
    report = run_graph(
        task=task,
        settings=settings,
        thread_id=args.thread,
        resume=args.resume,
        sleep_in_test=args.sleep_in_test,
    )
    print(
        json.dumps(
            {
                "verdict": report.get("verdict"),
                "attempts": report.get("attempts"),
                "engine_calls": report.get("engine_calls"),
                "trace_id": report.get("trace_id"),
                "trace_ids": (report.get("trace") or {}).get("trace_ids"),
                "session_id": report.get("session_id"),
                "evidence": str(settings.evidence_root / task.task_id),
                "gates": {
                    name: item["exit_code"] for name, item in (report.get("gates") or {}).items()
                },
            },
            ensure_ascii=False,
        )
    )
    return 0 if report.get("verdict") == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())