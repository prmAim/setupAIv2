"""Tracer-bullet prototype: fake edit engine, gates, evidence bundle, Langfuse spans."""

from __future__ import annotations

import argparse
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
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, TypedDict

from langfuse import Langfuse
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]
WORK_ROOT = Path(r"C:\Git\_wf")

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


class PatchReport(BaseModel):
    files_changed: list[str]
    summary: str
    engine: Literal["fake"] = ENGINE
    cli_version: str | None = None


class Plan(BaseModel):
    task_id: str
    steps: list[str]
    strategy: Literal["fake-patch"] = "fake-patch"
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


class TaskSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,63}$")
    repo: Path
    baseline_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    patch: dict[str, str]
    fix_patches: list[dict[str, str]] = Field(default_factory=list)
    forbidden_paths: list[str] = Field(default_factory=lambda: list(DEFAULT_FORBIDDEN_PATHS))
    test_roots: list[str] = Field(default_factory=lambda: list(DEFAULT_TEST_ROOTS))
    lint_command: list[str] = Field(default_factory=lambda: list(LINT_COMMAND))
    test_command: list[str] = Field(default_factory=lambda: list(TEST_COMMAND))
    notes: str = ""

    @classmethod
    def load(cls, path: Path) -> TaskSpec:
        return cls.model_validate_json(path.read_text(encoding="utf-8"))


class SkeletonState(TypedDict, total=False):
    task_id: str
    repo: str
    baseline_sha: str
    plan: dict[str, Any] | None
    diff: str
    patch_report: dict[str, Any] | None
    gate_results: dict[str, dict[str, Any]]
    attempts: int
    evidence_paths: list[str]
    trace_id: str
    changed_files: list[str]
    verdict: str
    summary: str
    fix_history: list[str]


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


def build_patch_text(repo: Path, baseline_sha: str, files: list[str]) -> str:
    chunks: list[str] = []
    for relative in sorted(files):
        try:
            old = git(repo, "show", f"{baseline_sha}:{relative}")
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


def changed_files(repo: Path, baseline_sha: str) -> list[str]:
    tracked = [line for line in git(repo, "diff", "--name-only", baseline_sha).splitlines() if line]
    untracked = [
        line
        for line in git(repo, "ls-files", "--others", "--exclude-standard").splitlines()
        if line
    ]
    return sorted({line.replace("\\", "/") for line in tracked + untracked})


class TraceRecorder:
    """Root span plus one child span per graph node, readable back by trace id."""

    def __init__(self, settings: SkeletonSettings) -> None:
        self.note = "disabled"
        self.client: Langfuse | None = None
        self.trace_id = ""
        self.root: Any = None
        self.span_names: set[str] = set()
        public = settings.langfuse_public_key.get_secret_value().strip()
        secret = settings.langfuse_secret_key.get_secret_value().strip()
        if not settings.tracing:
            self.note = "disabled: tracing is off for this run"
        elif not public or not secret:
            self.note = "disabled: Langfuse keys are not configured"
        else:
            os.environ["NO_PROXY"] = "localhost,127.0.0.1"
            os.environ["no_proxy"] = "localhost,127.0.0.1"
            self.client = Langfuse(
                public_key=public,
                secret_key=secret,
                base_url=settings.langfuse_base_url,
                environment=settings.langfuse_environment,
                timeout=10,
                flush_at=1,
            )
            self.note = "enabled"

    def start(
        self, *, name: str, metadata: dict[str, Any], input: Any, trace_id: str | None = None
    ) -> None:
        seed = f"{metadata['task_id']}:{metadata['baseline_sha']}:{ENGINE}"
        if self.client is None:
            self.trace_id = trace_id or sha256_text(seed)[:32]
            return
        self.trace_id = trace_id or self.client.create_trace_id()
        self.root = self.client.start_observation(
            name=name,
            as_type="agent",
            trace_context={"trace_id": self.trace_id},
            input=input,
            metadata={**metadata, "engine": ENGINE, "cli_version": "none"},
        )
        self.span_names.add(name)

    def node(self, name: str, *, metadata: dict[str, Any], input: Any) -> Any:
        if self.client is None or self.root is None:
            return None
        self.span_names.add(name)
        return self.client.start_observation(
            name=name,
            as_type="chain",
            trace_context={"trace_id": self.trace_id, "parent_span_id": self.root.id},
            input=input,
            metadata={**metadata, "engine": ENGINE, "node": name, "cli_version": "none"},
        )

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

    def read_back(self, *, environment: str, deadline_s: float = 30.0) -> dict[str, Any]:
        if self.client is None:
            return {"status": self.note}
        self.client.flush()
        deadline = time.monotonic() + deadline_s
        observations: list[Any] = []
        while True:
            observations = self.client.api.observations.get_many(
                trace_id=self.trace_id,
                environment=environment,
                fields="core,basic,io,model",
                limit=100,
            ).data
            seen = {getattr(item, "name", "") for item in observations}
            if self.span_names.issubset(seen) or time.monotonic() >= deadline:
                break
            time.sleep(2)
        return {
            "status": "read-back",
            "trace_url": self.client.get_trace_url(trace_id=self.trace_id),
            "observations": len(observations),
            "expected_observations": len(self.span_names),
            "observation_names": sorted(getattr(item, "name", "") for item in observations),
        }

    def shutdown(self) -> None:
        if self.client is not None:
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
        self.engine = FakeEngine(self.repo)
        self.metadata = {
            "task_id": task.task_id,
            "repo": str(self.repo),
            "baseline_sha": task.baseline_sha,
            "engine": ENGINE,
            "cli_version": "none",
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

    def test_integrity(self) -> dict[str, Any]:
        started = time.perf_counter()
        problems: list[str] = []
        baseline_files: list[str] = []
        for root in self.task.test_roots:
            listing = git(
                self.repo, "ls-tree", "-r", "--name-only", self.task.baseline_sha, "--", root
            )
            baseline_files.extend(
                line.replace("\\", "/") for line in listing.splitlines() if line.endswith(".py")
            )
        for relative in sorted(baseline_files):
            expected = sha256_text(git(self.repo, "show", f"{self.task.baseline_sha}:{relative}"))
            target = self.repo / relative
            if not target.is_file():
                problems.append(f"deleted baseline test file: {relative}")
                continue
            if file_digest(target) != expected:
                problems.append(f"modified baseline test file: {relative}")
        baseline_set = set(baseline_files)
        for root in self.task.test_roots:
            base = safe_relative(self.repo, root)
            if not base.is_dir():
                continue
            for path in sorted(base.rglob("*.py")):
                relative = path.relative_to(self.repo).as_posix()
                if relative in baseline_set or "__pycache__" in relative:
                    continue
                markers = SKIP_MARKERS.search(path.read_text(encoding="utf-8", errors="replace"))
                if markers:
                    problems.append(f"skip/xfail marker in new test file: {relative}")
        detail = "baseline test files unchanged; new test files carry no skip/xfail markers"
        if problems:
            detail = "; ".join(problems)
        result = GateResult(
            name="test-integrity",
            command=["sha256", "git-ls-tree", self.task.baseline_sha, *self.task.test_roots],
            exit_code=1 if problems else 0,
            duration_ms=int((time.perf_counter() - started) * 1000),
            passed=not problems,
            detail=detail[:2000],
        )
        return self.record_gate(result)

    def diff_scope(self) -> dict[str, Any]:
        started = time.perf_counter()
        files = changed_files(self.repo, self.task.baseline_sha)
        violations = [
            f"{path} matches forbidden pattern {reason}"
            for path in files
            if (reason := is_forbidden(path, self.task.forbidden_paths))
        ]
        detail = (
            "; ".join(violations)
            if violations
            else f"{len(files)} changed files, none in forbidden paths"
        )
        result = GateResult(
            name="diff-scope",
            command=["git", "diff", "--name-only", self.task.baseline_sha],
            exit_code=1 if violations else 0,
            duration_ms=int((time.perf_counter() - started) * 1000),
            passed=not violations,
            detail=detail[:2000],
        )
        return self.record_gate(result)

    def apply_patch(self, patch: dict[str, str], summary: str) -> dict[str, Any]:
        report = self.engine.apply(patch, summary=summary)
        files = sorted(set(changed_files(self.repo, self.task.baseline_sha)) | set(report.files_changed))
        return {
            "patch_report": report.model_dump(mode="json"),
            "diff": build_patch_text(self.repo, self.task.baseline_sha, files),
            "changed_files": files,
        }


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
        head = git(repo, "rev-parse", "HEAD").strip()
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
            "gate_results": state.get("gate_results", {}),
            "evidence_paths": state.get("evidence_paths", []),
            "plan": None,
            "diff": "",
            "patch_report": None,
            "changed_files": [],
            "verdict": "",
            "summary": "",
            "fix_history": [],
        }

    def plan(state: SkeletonState) -> dict[str, Any]:
        report = Plan(
            task_id=task.task_id,
            steps=["ingest", "plan", "edit(fake)", "fast_gate", "test", "gates", "evidence"],
            expected_files=sorted(path.replace("\\", "/") for path in task.patch),
            fix_attempts_allowed=min(MAX_FIX_ATTEMPTS, len(task.fix_patches)),
        )
        return {"plan": report.model_dump(mode="json")}

    def edit(state: SkeletonState) -> dict[str, Any]:
        return runtime.apply_patch(task.patch, f"{ENGINE} engine applied {len(task.patch)} file(s)")

    def fast_gate(state: SkeletonState) -> dict[str, Any]:
        results = dict(state.get("gate_results", {}))
        for result in (runtime.test_integrity(), runtime.diff_scope()):
            results[result["name"]] = result
        return {"gate_results": results}

    def run_tests(state: SkeletonState) -> dict[str, Any]:
        if runtime.sleep_in_test:
            (evidence / ".test-node-started").write_text(str(os.getpid()), encoding="utf-8", newline="\n")
            time.sleep(runtime.sleep_in_test)
        result = runtime.run_gate("tests", list(task.test_command), log_name="tests.log")
        results = dict(state.get("gate_results", {}))
        results["tests"] = result
        return {"gate_results": results}

    def fix(state: SkeletonState) -> dict[str, Any]:
        attempts = int(state.get("attempts", 0))
        history = list(state.get("fix_history") or [])
        if attempts >= len(task.fix_patches):
            return {"attempts": attempts + 1, "fix_history": [*history, "no fix patch available"]}
        update = runtime.apply_patch(
            task.fix_patches[attempts], f"{ENGINE} engine applied fix patch {attempts + 1}"
        )
        return {**update, "attempts": attempts + 1, "fix_history": [*history, f"fix patch {attempts + 1}"]}
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
        (evidence / "patch.sha256").write_text(sha256_text(diff_text) + "\n", encoding="utf-8", newline="\n")
        files = sorted(set(state.get("changed_files") or []))
        (evidence / "diff-name-only.txt").write_text("\n".join(files) + "\n", encoding="utf-8", newline="\n")
        gate_results = dict(state.get("gate_results", {}))
        for name in ("test-integrity", "diff-scope", "lint", "tests"):
            if name not in gate_results:
                gate_results[name] = runtime.not_run(name, "gate not executed before escalation")
        bundle = sorted(
            [path.name for path in evidence.glob("gate-*.json")]
            + ["patch.diff", "patch.sha256", "diff-name-only.txt", "tests.log", "report.json"]
        )
        bundle = [name for name in bundle if (evidence / name).exists()]
        report = {
            "task_id": task.task_id,
            "repo": str(runtime.repo),
            "baseline_sha": task.baseline_sha,
            "engine": ENGINE,
            "cli_version": None,
            "verdict": state.get("verdict") or "fail",
            "attempts": int(state.get("attempts", 0)),
            "trace_id": state.get("trace_id") or recorder.trace_id,
            "tracing": recorder.note,
            "plan": state.get("plan"),
            "patch_report": state.get("patch_report"),
            "patch_sha256": sha256_text(diff_text),
            "changed_files": files,
            "gates": {name: item for name, item in sorted(gate_results.items())},
            "summary": state.get("summary", ""),
            "fix_history": state.get("fix_history") or [],
            "evidence_paths": [str(evidence / name) for name in bundle],
            "evidence_writes": writes,
        }
        json_dump(previous, report)
        return {"evidence_paths": report["evidence_paths"], "verdict": report["verdict"]}

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
    }
    builder = StateGraph(SkeletonState)
    for name, node in nodes.items():
        builder.add_node(name, node)
    builder.add_edge(START, "ingest")
    builder.add_edge("ingest", "plan")
    builder.add_edge("plan", "edit")
    builder.add_edge("edit", "fast_gate")
    builder.add_conditional_edges(
        "fast_gate", after_fast_gate, {"test": "test", "escalate": "escalate"}
    )
    builder.add_conditional_edges(
        "test", after_test, {"gates": "gates", "fix": "fix", "escalate": "escalate"}
    )
    builder.add_edge("fix", "test")
    builder.add_edge("gates", "evidence")
    builder.add_edge("escalate", "evidence")
    builder.add_edge("evidence", END)
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
) -> dict[str, Any]:
    settings.state_db.parent.mkdir(parents=True, exist_ok=True)
    recorder = TraceRecorder(settings)
    metadata = {
        "task_id": task.task_id,
        "repo": str(task.repo.resolve()),
        "baseline_sha": task.baseline_sha,
        "engine": ENGINE,
        "cli_version": "none",
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
        recorder.start(
            name=f"tracer-bullet:{task.task_id}",
            metadata=metadata,
            input={"thread_id": thread_id or task.task_id, "patch": sorted(task.patch)},
            trace_id=previous_trace,
        )
        with tracing_context(enabled=False):
            if resume:
                app.invoke(None, config)
            else:
                app.invoke(
                    {
                        "trace_id": recorder.trace_id,
                        "attempts": 0,
                        "gate_results": {},
                        "evidence_paths": [],
                        "verdict": "",
                        "fix_history": [],
                    },
                    config,
                )
        values = app.get_state(config).values
        report_path = settings.evidence_root / task.task_id / "report.json"
        recorder.finish(
            {
                "verdict": values.get("verdict"),
                "attempts": values.get("attempts"),
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
                "trace": trace_info,
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
                "trace_id": report.get("trace_id"),
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
