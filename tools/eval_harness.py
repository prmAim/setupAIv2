"""Golden-5 eval harness: history isolation, hidden tests, control runs.

The harness answers a single question: can a *measurement* claim "resolved" be
trusted?  It therefore never gives an engine the gold answer, never gives it the
hidden tests, and never accepts the agent's own word for success:

1. A synthetic repository is built from the baseline commit alone
   (``git archive`` into an empty directory, one commit, no remote, no other
   refs), so the future commits that contain the fix are not reachable;
2. the engine sees only ``issue.md`` and that tree;
3. ``tests.patch`` is applied *after* the engine finished;
4. the verdict is ``resolved`` only when the hidden tests exit 0, the AST
   test-integrity gate passes, and no existing test file was touched;
5. control engines (``gold``/``noop``/``cheat``) prove the meter separates
   "solved", "not solved" and "cheated".

Golden artifacts are read straight out of a git ref (``research/golden-5``), so
the same bytes feed every run; patch digests are recorded and re-checked.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TypedDict

ROOT = Path(__file__).resolve().parents[1]
TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import test_integrity as ast_integrity  # noqa: E402  (tools dir is on sys.path above)
from agent_skeleton import (  # noqa: E402
    SkeletonSettings,
    TraceRecorder,
    build_patch_text,
    changed_files,
)
from codex_engine import DEFAULT_CODEX_BIN, DEFAULT_TIMEOUT_S, CodexEngine  # noqa: E402
from container_test_runner import ContainerTestRunner  # noqa: E402
from patch_report import INFRASTRUCTURE_ERRORS, PatchReport  # noqa: E402

GOLDEN_DIR = "eval/golden"
DEFAULT_REF = "research/golden-5"
DEFAULT_SOURCE = Path(r"C:\Git\setupAIv2")
DEFAULT_ORBITA = Path(r"C:\Git\orbita")
DEFAULT_WORK_ROOT = Path(r"C:\Git\_wf\t24-runs")
DEFAULT_EVIDENCE_ROOT = Path(r"C:\Git\_wf\t24-evidence")
DEFAULT_JAVA_HOME = Path(r"C:\Program Files\Java\jdk-25")

MODES = ("gold", "noop", "cheat", "codex")
EXPECTED = {
    "gold": {"verdict": "resolved", "test_intrusion": False},
    "noop": {"verdict": "fail", "test_intrusion": False},
    "cheat": {"verdict": "fail", "test_intrusion": True},
}
COMMIT_IDENTITY = (
    "-c",
    "user.name=eval-harness",
    "-c",
    "user.email=eval@harness.local",
    "-c",
    "commit.gpgsign=false",
)
ASSERT_LINE = re.compile(
    r"^(?P<indent>\s*)"
    r"(?P<call>(?:self\.|test\.|org\.junit\.jupiter\.api\.Assertions\.)?assert\w+)\s*\("
)
PATCH_HEADER = re.compile(r"^diff --git a/(?P<a>\S+) b/(?P<b>\S+)$", re.MULTILINE)


class HarnessError(RuntimeError):
    """A deterministic, non-secret failure of the harness itself."""


@dataclasses.dataclass(frozen=True)
class CommandOutcome:
    command: list[str]
    cwd: str
    exit_code: int
    duration_ms: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    @property
    def combined(self) -> str:
        return f"{self.stdout}{self.stderr}"


def run_command(
    command: Sequence[str],
    *,
    cwd: Path | str | None = None,
    timeout_s: int = 300,
    env: dict[str, str] | None = None,
) -> CommandOutcome:
    argv = [str(part) for part in command]
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout_s,
            env=env,
        )
    except subprocess.TimeoutExpired as error:
        return CommandOutcome(
            command=argv,
            cwd=str(cwd or ""),
            exit_code=124,
            duration_ms=int((time.perf_counter() - started) * 1000),
            stdout=_decode(error.stdout),
            stderr=_decode(error.stderr) or f"timeout after {timeout_s}s",
            timed_out=True,
        )
    except OSError as error:
        return CommandOutcome(
            command=argv,
            cwd=str(cwd or ""),
            exit_code=127,
            duration_ms=int((time.perf_counter() - started) * 1000),
            stderr=f"{type(error).__name__}: {error}",
        )
    return CommandOutcome(
        command=argv,
        cwd=str(cwd or ""),
        exit_code=completed.returncode,
        duration_ms=int((time.perf_counter() - started) * 1000),
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )


def run_bytes(command: Sequence[str], *, timeout_s: int = 300) -> tuple[int, bytes, str]:
    """Byte-exact capture for git blobs (patches must not be re-encoded)."""
    argv = [str(part) for part in command]
    try:
        completed = subprocess.run(
            argv, capture_output=True, check=False, timeout=timeout_s
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return 127, b"", f"{type(error).__name__}: {error}"
    return (
        completed.returncode,
        completed.stdout or b"",
        (completed.stderr or b"").decode("utf-8", errors="replace"),
    )


def _decode(payload: Any) -> str:
    if payload is None:
        return ""
    if isinstance(payload, bytes):
        return payload.decode("utf-8", errors="replace")
    return str(payload)

def shell_command(exe: Path, args: Sequence[str]) -> list[str]:
    """Batch wrappers (``mvnw.cmd``) need cmd.exe; plain binaries do not."""
    if exe.suffix.lower() in {".cmd", ".bat"}:
        return ["cmd", "/c", str(exe), *args]
    return [str(exe), *args]


def git(repo: Path, *args: str, timeout_s: int = 300) -> str:
    outcome = run_command(
        ["git", "-c", "core.quotepath=false", "-C", str(repo), *args], timeout_s=timeout_s
    )
    if outcome.exit_code != 0:
        raise HarnessError(
            f"git {' '.join(args)} failed with exit {outcome.exit_code}: "
            f"{outcome.stderr.strip()[:400]}"
        )
    return outcome.stdout


def json_dump(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def remove_tree(path: Path) -> None:
    """rmtree that also clears the read-only bit git puts on its objects."""

    def onexc(function: Callable[..., Any], target: str, error: BaseException) -> None:
        try:
            os.chmod(target, stat.S_IWRITE)
            function(target)
        except OSError:
            raise error

    if path.exists():
        shutil.rmtree(path, onexc=onexc)


def parse_patch_paths(patch: bytes) -> list[str]:
    text = patch.decode("utf-8", errors="replace")
    return [match.group("b") for match in PATCH_HEADER.finditer(text)]


def test_roots_from_patch(paths: Sequence[str]) -> list[str]:
    roots: set[str] = set()
    for raw in paths:
        path = raw.replace("\\", "/").lstrip("/")
        marker = "/src/test/"
        if marker in path:
            roots.add(path.split(marker, 1)[0] + "/src/test/java")
        elif path.startswith("tests/"):
            roots.add("tests")
    return sorted(roots)


@dataclasses.dataclass(frozen=True)
class GoldenTask:
    number: int
    task_id: str
    path: str
    language: str
    gate: str
    baseline_sha: str
    merge_sha: str
    issue: bytes
    gold_patch: bytes
    tests_patch: bytes
    spec: dict[str, Any]
    manifest_entry: dict[str, Any]
    digests: dict[str, str]

    @property
    def test_paths(self) -> list[str]:
        return parse_patch_paths(self.tests_patch)

    @property
    def test_roots(self) -> list[str]:
        return test_roots_from_patch(self.test_paths)

    @property
    def expected_red(self) -> list[str]:
        raw = self.spec.get("expected_red") or self.manifest_entry.get("expected_red") or []
        return [str(item) for item in raw]


class GoldenSet:
    """Read ``eval/golden/**`` from a git ref without touching the worktree."""

    def __init__(self, repo: Path, ref: str = DEFAULT_REF) -> None:
        self.repo = Path(repo).resolve()
        self.ref = ref
        manifest = self.file_bytes(f"{GOLDEN_DIR}/manifest.json")
        self.manifest = json.loads(manifest.decode("utf-8"))
        entries = self.manifest.get("tasks") or []
        if not entries:
            raise HarnessError(f"{ref}:{GOLDEN_DIR}/manifest.json lists no tasks")
        self.tasks = [self._task(number, entry) for number, entry in enumerate(entries, 1)]

    def file_bytes(self, path: str) -> bytes:
        exit_code, payload, error = run_bytes(
            ["git", "-C", str(self.repo), "cat-file", "blob", f"{self.ref}:{path}"],
            timeout_s=120,
        )
        if exit_code != 0:
            raise HarnessError(f"{self.ref}:{path} is not readable: {error.strip()[:300]}")
        return payload

    def _task(self, number: int, entry: dict[str, Any]) -> GoldenTask:
        base = f"{GOLDEN_DIR}/{number}"
        spec = json.loads(self.file_bytes(f"{base}/task.json").decode("utf-8"))
        issue = self.file_bytes(f"{base}/issue.md")
        gold = self.file_bytes(f"{base}/gold.patch")
        tests = self.file_bytes(f"{base}/tests.patch")
        if not issue.strip() or not gold.strip() or not tests.strip():
            raise HarnessError(f"task {number}: issue/gold/tests artifacts are incomplete")
        for field in ("baseline_sha", "id"):
            if spec.get(field) != entry.get(field):
                raise HarnessError(
                    f"task {number}: task.json {field}={spec.get(field)!r} "
                    f"does not match manifest {entry.get(field)!r}"
                )
        digests = {
            "issue.md": sha256_bytes(issue),
            "gold.patch": sha256_bytes(gold),
            "tests.patch": sha256_bytes(tests),
            "task.json": sha256_bytes(self.file_bytes(f"{base}/task.json")),
        }
        declared = {**(entry.get("sha256") or {}), **(spec.get("sha256") or {})}
        for name, expected in declared.items():
            if name in digests and digests[name] != expected:
                raise HarnessError(
                    f"task {number}: declared sha256 for {name} does not match the artifact"
                )
        return GoldenTask(
            number=number,
            task_id=str(spec.get("id") or entry.get("id")),
            path=base,
            language=str(spec.get("language") or entry.get("language") or ""),
            gate=str(spec.get("gate") or entry.get("gate") or ""),
            baseline_sha=str(spec["baseline_sha"]),
            merge_sha=str(spec.get("merge_sha") or entry.get("merge_sha") or ""),
            issue=issue,
            gold_patch=gold,
            tests_patch=tests,
            spec=spec,
            manifest_entry=entry,
            digests=digests,
        )

    def record_digests(self, path: Path) -> dict[str, Any]:
        """Write the golden digests once, then fail closed on later drift."""
        payload = {
            "ref": self.ref,
            "tasks": {str(task.number): task.digests for task in self.tasks},
        }
        if path.is_file():
            stored = json.loads(path.read_text(encoding="utf-8"))
            if stored.get("tasks") != payload["tasks"]:
                raise HarnessError(f"golden digests changed since {path.name}: {self.ref}")
        else:
            json_dump(path, payload)
        return payload

@dataclasses.dataclass(frozen=True)
class Sandbox:
    path: Path
    baseline_commit: str
    baseline_tree: str
    source_tree: str
    proof: dict[str, Any]
    issue_path: Path


def isolation_proof(repo: Path) -> dict[str, Any]:
    log = [line for line in git(repo, "log", "--all", "--oneline").splitlines() if line.strip()]
    remotes = git(repo, "remote", "-v").strip()
    rev_list = int(git(repo, "rev-list", "--all", "--count").strip() or 0)
    refs = [
        line
        for line in git(repo, "for-each-ref", "--format=%(refname)").splitlines()
        if line.strip()
    ]
    problems: list[str] = []
    if len(log) != 1:
        problems.append(f"git log --all --oneline has {len(log)} commits, expected 1")
    if remotes:
        problems.append("git remote -v is not empty")
    if rev_list != 1:
        problems.append(f"git rev-list --all --count is {rev_list}, expected 1")
    if refs != ["refs/heads/main"]:
        problems.append(f"refs are {refs}, expected only refs/heads/main")
    return {
        "commits": len(log),
        "log": log,
        "remotes": remotes,
        "rev_list_all": rev_list,
        "refs": refs,
        "problems": problems,
        "passed": not problems,
    }


def executable_paths(source_repo: Path, baseline_sha: str) -> list[str]:
    """Executable bits are lost when tar extracts on Windows; restore them."""
    listing = git(source_repo, "ls-tree", "-r", baseline_sha)
    paths: list[str] = []
    for line in listing.splitlines():
        meta, _, relative = line.partition("\t")
        if meta.startswith("100755") and relative:
            paths.append(relative)
    return paths


def prepare_sandbox(
    *,
    source_repo: Path,
    baseline_sha: str,
    dest: Path,
    issue: bytes,
) -> Sandbox:
    """Materialize only the baseline tree, as one commit, with no origin."""
    source_repo = Path(source_repo).resolve()
    dest = Path(dest).resolve()
    if dest.exists():
        remove_tree(dest)
    dest.mkdir(parents=True)
    archive = dest.parent / f".{dest.name}-baseline.tar"
    try:
        outcome = run_command(
            [
                "git",
                "-C",
                str(source_repo),
                "archive",
                "--format=tar",
                f"--output={archive}",
                baseline_sha,
            ],
            timeout_s=600,
        )
        if outcome.exit_code != 0:
            raise HarnessError(
                f"git archive {baseline_sha} failed: {outcome.stderr.strip()[:300]}"
            )
        outcome = run_command(
            ["tar", "-x", "-f", str(archive), "-C", str(dest)], timeout_s=600
        )
        if outcome.exit_code != 0:
            raise HarnessError(f"tar extraction failed: {outcome.stderr.strip()[:300]}")
    finally:
        archive.unlink(missing_ok=True)
    git(dest, "init", "-q", "-b", "main")
    git(dest, *COMMIT_IDENTITY, "add", "-A", "--force")
    for relative in executable_paths(source_repo, baseline_sha):
        git(dest, "update-index", "--chmod=+x", "--", relative)
    git(dest, *COMMIT_IDENTITY, "commit", "-q", "-m", f"baseline {baseline_sha}")
    issue_path = dest / "issue.md"
    issue_path.write_bytes(issue)
    exclude = dest / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write("/issue.md\n")
    proof = isolation_proof(dest)
    if not proof["passed"]:
        raise HarnessError("sandbox is not history-isolated: " + "; ".join(proof["problems"]))
    commit = git(dest, "rev-parse", "HEAD").strip()
    tree = git(dest, "rev-parse", "HEAD^{tree}").strip()
    source_tree = git(source_repo, "rev-parse", f"{baseline_sha}^{{tree}}").strip()
    if tree != source_tree:
        raise HarnessError(
            f"sandbox tree {tree[:12]} is not the baseline tree {source_tree[:12]}"
        )
    return Sandbox(
        path=dest,
        baseline_commit=commit,
        baseline_tree=tree,
        source_tree=source_tree,
        proof=proof,
        issue_path=issue_path,
    )


def git_apply(repo: Path, patch: bytes, *, label: str, timeout_s: int = 300) -> dict[str, Any]:
    """Apply a patch from a temp file outside the sandbox; report, never raise."""
    if not patch.strip():
        raise HarnessError(f"{label} patch is empty")
    handle, raw_path = tempfile.mkstemp(prefix=f"eval-{label}-", suffix=".patch")
    os.close(handle)
    path = Path(raw_path)
    path.write_bytes(patch)
    try:
        outcome = run_command(
            ["git", "-C", str(repo), "apply", "--whitespace=nowarn", "-p1", str(path)],
            timeout_s=timeout_s,
        )
    finally:
        path.unlink(missing_ok=True)
    return {
        "label": label,
        "command": outcome.command,
        "exit_code": outcome.exit_code,
        "duration_ms": outcome.duration_ms,
        "stderr": outcome.stderr[-2000:],
        "passed": outcome.exit_code == 0,
    }

@dataclasses.dataclass(frozen=True)
class CodexOptions:
    """Where the real engine comes from; ``None`` means "use the pinned default"."""

    binary: str | None = None
    model: str | None = None
    timeout_s: int = DEFAULT_TIMEOUT_S
    env: dict[str, str] | None = None

class EvalEngine:
    """Engine surface used by the eval graph (one apply, one PatchReport)."""

    name = "engine"

    def apply(self, repo: Path) -> PatchReport:  # pragma: no cover - interface
        raise NotImplementedError


class GoldEngine(EvalEngine):
    name = "gold"

    def __init__(self, patch: bytes, files: Sequence[str]) -> None:
        self.patch = patch
        self.files = sorted(files)

    def apply(self, repo: Path) -> PatchReport:
        outcome = git_apply(repo, self.patch, label="gold")
        if not outcome["passed"]:
            return PatchReport(
                engine="fake",
                summary="gold patch did not apply",
                engine_error="gold_patch_failed",
                error_detail=outcome["stderr"],
                exit_code=outcome["exit_code"],
                duration_ms=outcome["duration_ms"],
            )
        return PatchReport(
            files_changed=list(self.files),
            summary=f"gold patch applied to {len(self.files)} file(s)",
            engine="fake",
            exit_code=0,
            duration_ms=outcome["duration_ms"],
        )


class NoopEngine(EvalEngine):
    name = "noop"

    def apply(self, repo: Path) -> PatchReport:
        return PatchReport(
            files_changed=[],
            summary="noop engine: nothing changed",
            engine="fake",
            exit_code=0,
            duration_ms=0,
        )


class CheatEngine(EvalEngine):
    """Weaken an assertion in a baseline test file (a real cheat, not a no-op)."""

    name = "cheat"

    def __init__(self, target: str) -> None:
        self.target = target

    def apply(self, repo: Path) -> PatchReport:
        path = repo / self.target
        started = time.perf_counter()
        original = path.read_text(encoding="utf-8", errors="replace")
        lines = original.splitlines(keepends=True)
        replaced = False
        for index, line in enumerate(lines):
            match = ASSERT_LINE.match(line)
            if not match:
                continue
            indent = match.group("indent")
            if path.suffix == ".py":
                lines[index] = (
                    f"{indent}self.assertEqual(1, 1)  # eval-cheat: weakened assertion\n"
                )
            else:
                lines[index] = (
                    f"{indent}org.junit.jupiter.api.Assertions.assertTrue(true);"
                    " // eval-cheat: weakened assertion\n"
                )
            replaced = True
            break
        if not replaced:
            if not original.endswith("\n"):
                original += "\n"
            marker = (
                "# eval-cheat: assertion marker\n"
                if path.suffix == ".py"
                else "// eval-cheat: assertion marker\n"
            )
            lines = [original, marker]
        path.write_text("".join(lines), encoding="utf-8", newline="\n")
        return PatchReport(
            files_changed=[self.target],
            summary=(
                f"cheat engine weakened an assertion in {self.target}"
                if replaced
                else f"cheat engine marked {self.target}"
            ),
            engine="fake",
            exit_code=0,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )


def codex_prompt(issue: str) -> str:
    """Instructions for the edit engine: the issue, then the JSON contract."""
    return (
        "You are the edit engine of an isolated repository snapshot.\n"
        "Read `issue.md` in the current directory and make the smallest source change that "
        "satisfies it. Never modify or add test files, and do not use the network.\n"
        "When finished, reply with exactly one JSON object matching the provided schema: "
        "files_changed (repository-relative paths you touched), summary, tests_added, notes.\n\n"
        "--- issue.md ---\n"
        f"{issue}\n"
    )


class CodexEvalEngine(EvalEngine):
    """Real Codex CLI engine; failures stay values so the meter can tell them apart."""

    name = "codex"

    def __init__(
        self,
        *,
        binary: str | None = None,
        model: str | None = None,
        timeout_s: int = DEFAULT_TIMEOUT_S,
        env: dict[str, str] | None = None,
    ) -> None:
        self.binary = binary
        self.model = model
        self.timeout_s = timeout_s
        self.env = env

    def apply(self, repo: Path) -> PatchReport:
        engine = CodexEngine(
            repo,
            binary=self.binary,
            model=self.model,
            timeout_s=self.timeout_s,
            env=self.env,
        )
        issue = (repo / "issue.md").read_text(encoding="utf-8", errors="replace")
        return engine.run(codex_prompt(issue), task_id=repo.name)

def status_entries(repo: Path, roots: Sequence[str]) -> list[tuple[str, str]]:
    if not roots:
        return []
    outcome = run_command(
        ["git", "-C", str(repo), "status", "--porcelain=v1", "-z", "--", *roots],
        timeout_s=120,
    )
    if outcome.exit_code != 0:
        raise HarnessError(f"git status failed: {outcome.stderr.strip()[:300]}")
    entries: list[tuple[str, str]] = []
    for chunk in outcome.stdout.split("\0"):
        if not chunk.strip() or len(chunk) < 4:
            continue
        entries.append((chunk[:2], chunk[3:].replace("\\", "/")))
    return entries


def baseline_test_files(repo: Path, commit: str, roots: Sequence[str]) -> list[str]:
    files: set[str] = set()
    for root in roots:
        listing = git(repo, "ls-tree", "-r", "--name-only", commit, "--", root)
        files.update(
            line.replace("\\", "/")
            for line in listing.splitlines()
            if line.strip() and line.endswith((".py", ".java"))
        )
    return sorted(files)


def test_integrity(*, repo: Path, baseline_commit: str, roots: Sequence[str]) -> dict[str, Any]:
    """Strict check: any edit or deletion of a baseline test file is intrusion."""
    started = time.perf_counter()
    baseline = baseline_test_files(repo, baseline_commit, roots)
    entries = status_entries(repo, roots)
    findings: list[dict[str, str]] = []
    added: list[str] = []
    for code, rel in entries:
        if code == "??":
            added.append(rel)
        elif set(code) & {"M", "D", "R", "T"}:
            rule = "test-file-deleted" if "D" in code else "test-file-modified"
            findings.append({"file": rel, "rule": rule, "evidence": f"git status {code!r}"})
    candidates = sorted(set(baseline) | set(added))
    comparison = ast_integrity.compare(repo, baseline_commit, candidates) if candidates else None
    ast_findings = [
        {"file": item.file, "rule": item.rule, "evidence": item.evidence}
        for item in (comparison.findings if comparison else [])
    ]
    verdict = "pass" if not findings and not ast_findings else "fail"
    return {
        "verdict": verdict,
        "passed": verdict == "pass",
        "test_intrusion": bool(findings),
        "test_roots": list(roots),
        "baseline_test_files": len(baseline),
        "added_test_files": added,
        "findings": findings,
        "ast": {
            "verdict": "pass" if (comparison is None or comparison.exit_code == 0) else "fail",
            "exit_code": comparison.exit_code if comparison else 0,
            "summary": comparison.summary if comparison else "no test files to compare",
            "findings": ast_findings,
        },
        "duration_ms": int((time.perf_counter() - started) * 1000),
    }

def java_classes(expected_red: Sequence[str]) -> list[str]:
    classes: list[str] = []
    for raw in expected_red:
        token = raw.strip().split()[0].strip(",;") if raw.strip() else ""
        if token and re.search(r"(Test|IT)$", token.split(".")[-1]):
            classes.append(token)
    return classes


def plan_hidden(task: GoldenTask) -> dict[str, Any]:
    paths = task.test_paths
    patterns = sorted(
        {Path(path).name for path in paths if path.startswith("tests/") and path.endswith(".py")}
    )
    java_files = [path for path in paths if "/src/test/" in path and path.endswith(".java")]
    classes = java_classes(task.expected_red)
    notes: list[str] = []
    if java_files and not classes:
        if task.language == "java" or task.gate == "full":
            classes = sorted(
                {
                    Path(path).stem
                    for path in java_files
                    if path.endswith(("Test.java", "IT.java"))
                }
            )
        else:
            notes.append(
                f"{len(java_files)} java test file(s) applied; the java part of this task is "
                "covered by the full gate, so the eval gate uses the targeted python check"
            )
    if not patterns and not classes:
        raise HarnessError(f"task {task.number}: no hidden test target could be derived")
    return {
        "python_patterns": patterns,
        "java_classes": classes,
        "java_files": java_files,
        "notes": notes,
    }


def run_hidden_tests(
    *,
    repo: Path,
    plan: dict[str, Any],
    python_exe: str,
    java_home: Path | None,
    timeout_s: int,
) -> dict[str, Any]:
    commands: list[dict[str, Any]] = []
    duration_ms = 0
    exit_code = 0
    if plan["python_patterns"]:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        for pattern in plan["python_patterns"]:
            outcome = run_command(
                [python_exe, "-m", "unittest", "discover", "-s", "tests", "-p", pattern, "-v"],
                cwd=repo,
                timeout_s=timeout_s,
                env=env,
            )
            commands.append(
                {
                    "command": outcome.command,
                    "exit_code": outcome.exit_code,
                    "duration_ms": outcome.duration_ms,
                    "timed_out": outcome.timed_out,
                    "output": outcome.combined,
                }
            )
            duration_ms += outcome.duration_ms
            exit_code = exit_code or outcome.exit_code
    if plan["java_classes"]:
        backend = repo / "backend"
        wrapper = backend / ("mvnw.cmd" if os.name == "nt" else "mvnw")
        if not wrapper.is_file():
            commands.append(
                {
                    "command": [str(wrapper)],
                    "exit_code": 127,
                    "duration_ms": 0,
                    "timed_out": False,
                    "output": f"maven wrapper is missing: {wrapper}",
                }
            )
            exit_code = exit_code or 127
        else:
            env = dict(os.environ)
            if java_home and (Path(java_home) / "bin").is_dir():
                env["JAVA_HOME"] = str(java_home)
            selector = ",".join(plan["java_classes"])
            outcome = run_command(
                shell_command(wrapper, ["-B", "-ntp", "-o", f"-Dtest={selector}", "test"]),
                cwd=backend,
                timeout_s=timeout_s,
                env=env,
            )
            commands.append(
                {
                    "command": outcome.command,
                    "exit_code": outcome.exit_code,
                    "duration_ms": outcome.duration_ms,
                    "timed_out": outcome.timed_out,
                    "output": outcome.combined,
                }
            )
            duration_ms += outcome.duration_ms
            exit_code = exit_code or outcome.exit_code
    return {
        "exit_code": exit_code,
        "passed": exit_code == 0,
        "duration_ms": duration_ms,
        "commands": commands,
        "targets": {
            "python_patterns": plan["python_patterns"],
            "java_classes": plan["java_classes"],
        },
        "notes": plan["notes"],
    }


def run_full_verify(
    *,
    repo: Path,
    run_id: str,
    log_path: Path,
    timeout_s: int = 1800,
    runner_factory: Callable[..., ContainerTestRunner] | None = None,
) -> dict[str, Any]:
    factory = runner_factory or ContainerTestRunner
    runner = factory(run_id=run_id, repo_path=repo, profile="full")
    started = time.perf_counter()
    try:
        result = runner.run_profile("full", timeout_s=timeout_s, log_path=log_path)
    finally:
        runner.stop(cleanup=True)
    duration_ms = int((time.perf_counter() - started) * 1000)
    return {
        "command": ["full-profile"],
        "verdict": result.verdict,
        "exit_code": result.exit_code,
        "duration_ms": result.duration_ms or duration_ms,
        "log": str(log_path),
        "container_id": result.container_id,
        "image_id": result.image_id,
        "platform": result.platform,
        "reason": result.reason,
        "tail": summarize_log(result.output_tail or ""),
    }


def summarize_log(text: str, limit: int = 1600) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    return "\n".join(lines)[-limit:]


class EvalState(TypedDict, total=False):
    task_number: int
    mode: str
    verdict: str
    reason: str
    patch_report: dict[str, Any]
    integrity: dict[str, Any]
    hidden: dict[str, Any]
    isolation: dict[str, Any]
    order: list[str]


@dataclasses.dataclass
class CaseContext:
    task: GoldenTask
    mode: str
    sandbox: Sandbox
    engine: EvalEngine
    evidence: Path
    python_exe: str
    java_home: Path | None
    hidden_timeout_s: int
    recorder: TraceRecorder
    full_verify: bool = False
    runner_factory: Callable[..., ContainerTestRunner] | None = None
    full_timeout_s: int = 1800

def build_eval_graph(context: CaseContext):
    from langgraph.graph import END, START, StateGraph

    recorder = context.recorder
    evidence = context.evidence
    metadata = {
        "task_id": context.task.task_id,
        "mode": context.mode,
        "baseline_sha": context.sandbox.baseline_commit,
        "engine": context.engine.name,
    }

    def traced(name: str, summarize: Callable[[dict[str, Any]], Any], payload: Any):
        def wrap(node: Callable[[EvalState], dict[str, Any]]):
            def invoke(state: EvalState) -> dict[str, Any]:
                span = recorder.node(name, metadata=metadata, input=payload)
                try:
                    update = node(state)
                except Exception as error:  # noqa: BLE001 - evidence before re-raise
                    recorder.close(span, None, error=f"{type(error).__name__}: {error}")
                    raise
                recorder.close(span, summarize(update))
                return update

            return invoke

        return wrap

    def isolate(state: EvalState) -> dict[str, Any]:
        proof = isolation_proof(context.sandbox.path)
        isolation = {
            **proof,
            "baseline_sha": context.task.baseline_sha,
            "sandbox_commit": context.sandbox.baseline_commit,
            "tree_matches_baseline": context.sandbox.baseline_tree
            == context.sandbox.source_tree,
            "issue_path": str(context.sandbox.issue_path),
            "eval_dir_in_sandbox": (context.sandbox.path / "eval").exists(),
        }
        json_dump(evidence / "isolation.json", isolation)
        if not proof["passed"]:
            return {
                "verdict": "fail",
                "reason": "isolation",
                "isolation": isolation,
                "order": [*state.get("order", []), "isolate"],
            }
        return {"isolation": isolation, "order": [*state.get("order", []), "isolate"]}

    def agent(state: EvalState) -> dict[str, Any]:
        started = time.perf_counter()
        report = context.engine.apply(context.sandbox.path)
        duration_ms = int((time.perf_counter() - started) * 1000)
        recorder.generation(
            name=f"engine:{context.mode}:{context.task.number}",
            metadata={
                "task_id": context.task.task_id,
                "mode": context.mode,
                "engine": report.engine,
                "duration_ms": duration_ms,
            },
            input={"issue": "issue.md", "mode": context.mode},
            output_bytes=len(report.model_dump_json().encode("utf-8")),
        )
        payload = report.model_dump(mode="json")
        json_dump(evidence / "agent-patch-report.json", payload)
        files = changed_files(context.sandbox.path, context.sandbox.baseline_commit)
        diff = build_patch_text(
            context.sandbox.path, context.sandbox.baseline_commit, files
        )
        (evidence / "agent.diff").write_text(diff, encoding="utf-8", newline="\n")
        (evidence / "agent-diff-name-only.txt").write_text(
            "\n".join(files) + "\n", encoding="utf-8", newline="\n"
        )
        update: dict[str, Any] = {
            "patch_report": payload,
            "order": [*state.get("order", []), "agent"],
        }
        if payload.get("engine_error"):
            error = str(payload["engine_error"])
            update["verdict"] = "not_run" if error in INFRASTRUCTURE_ERRORS else "fail"
            update["reason"] = f"engine:{error}"
        return update

    def integrity(state: EvalState) -> dict[str, Any]:
        result = test_integrity(
            repo=context.sandbox.path,
            baseline_commit=context.sandbox.baseline_commit,
            roots=context.task.test_roots,
        )
        json_dump(evidence / "integrity.json", result)
        recorder.tool(
            name="test-integrity",
            cmd=["git", "status", "--porcelain", *context.task.test_roots],
            exit_code=0 if result["passed"] else 1,
            duration_ms=result["duration_ms"],
            metadata={"task_id": context.task.task_id, "mode": context.mode},
        )
        update: dict[str, Any] = {
            "integrity": result,
            "order": [*state.get("order", []), "integrity"],
        }
        if not result["passed"] and not state.get("verdict"):
            update["verdict"] = "fail"
            update["reason"] = (
                "test-intrusion" if result["test_intrusion"] else "test-integrity"
            )
        return update

    def hidden(state: EvalState) -> dict[str, Any]:
        if str(state.get("reason") or "").startswith("engine:"):
            return {"order": [*state.get("order", []), "hidden-skipped"]}
        plan = plan_hidden(context.task)
        apply_outcome = git_apply(
            context.sandbox.path, context.task.tests_patch, label="tests"
        )
        result = run_hidden_tests(
            repo=context.sandbox.path,
            plan=plan,
            python_exe=context.python_exe,
            java_home=context.java_home,
            timeout_s=context.hidden_timeout_s,
        )
        output = "\n\n".join(
            f"$ {' '.join(item['command'])}\n{item['output']}" for item in result["commands"]
        )
        (evidence / "hidden-tests.log").write_text(output, encoding="utf-8", newline="\n")
        result["tests_patch"] = apply_outcome
        result["targets"]["roots"] = context.task.test_roots
        result["summary"] = "\n".join(
            f"$ {' '.join(item['command'])} (exit {item['exit_code']})"
            for item in result["commands"]
        ) or "no hidden test command ran"
        result["tail"] = summarize_log(output)
        json_dump(evidence / "hidden-tests.json", result)
        recorder.tool(
            name="hidden-tests",
            cmd=result["commands"][0]["command"] if result["commands"] else ["hidden-tests"],
            exit_code=result["exit_code"],
            duration_ms=result["duration_ms"],
            metadata={
                "task_id": context.task.task_id,
                "mode": context.mode,
                "tests_patch": apply_outcome["exit_code"],
            },
        )
        update: dict[str, Any] = {
            "hidden": result,
            "order": [*state.get("order", []), "hidden"],
        }
        if not apply_outcome["passed"] and not state.get("verdict"):
            update["verdict"] = "fail"
            update["reason"] = "hidden:tests_patch_apply"
        elif not result["passed"] and not state.get("verdict"):
            update["verdict"] = "fail"
            update["reason"] = "hidden-tests"
        return update

    def report_node(state: EvalState) -> dict[str, Any]:
        return {
            "verdict": state.get("verdict") or "resolved",
            "reason": state.get("reason") or "hidden tests green; integrity clean",
            "order": [*state.get("order", []), "report"],
        }

    nodes = {
        "isolate": traced(
            "isolate",
            lambda update: update.get("isolation", {}).get("passed"),
            {"phase": "isolation"},
        )(isolate),
        "agent": traced(
            "agent", lambda update: update["patch_report"].get("summary"), {"phase": "agent"}
        )(agent),
        "integrity": traced(
            "integrity", lambda update: update["integrity"]["verdict"], {"phase": "integrity"}
        )(integrity),
        "hidden": traced(
            "hidden", lambda update: update.get("hidden", {}).get("exit_code"), {"phase": "hidden"}
        )(hidden),
        "report": traced("report", lambda update: update["verdict"], {"phase": "report"})(
            report_node
        ),
    }
    builder = StateGraph(EvalState)
    for name, node in nodes.items():
        builder.add_node(name, node)
    builder.add_edge(START, "isolate")
    builder.add_edge("isolate", "agent")
    builder.add_edge("agent", "integrity")
    builder.add_edge("integrity", "hidden")
    builder.add_edge("hidden", "report")
    builder.add_edge("report", END)
    return builder.compile()

def _recorder(evidence: Path) -> TraceRecorder:
    settings = SkeletonSettings(
        tracing=False,
        evidence_root=evidence,
        state_db=evidence / "state.db",
    )
    return TraceRecorder(settings)


def run_case(context: CaseContext) -> dict[str, Any]:
    started = time.perf_counter()
    context.evidence.mkdir(parents=True, exist_ok=True)
    metadata = {
        "task_id": context.task.task_id,
        "baseline_sha": context.sandbox.baseline_commit,
        "engine": context.mode,
    }
    graph = build_eval_graph(context)
    context.recorder.start(
        name=f"eval:{context.task.task_id}:{context.mode}",
        metadata=metadata,
        input={"issue": "issue.md", "mode": context.mode},
        session_id=f"eval-{context.task.number}-{context.mode}",
    )
    harness_error = ""
    state: EvalState = {"task_number": context.task.number, "mode": context.mode}
    try:
        state = graph.invoke({"task_number": context.task.number, "mode": context.mode})
    except Exception as error:  # noqa: BLE001 - a measurement must still report
        harness_error = f"{type(error).__name__}: {error}"
        state = {"verdict": "fail", "reason": "harness-error"}
    finally:
        context.recorder.finish({"verdict": state.get("verdict")})
    wall_ms = int((time.perf_counter() - started) * 1000)
    patch_report = state.get("patch_report") or {}
    integrity = state.get("integrity") or {}
    hidden = state.get("hidden") or {}
    isolation = state.get("isolation") or {}
    verdict = state.get("verdict") or "resolved"
    report: dict[str, Any] = {
        "task": context.task.number,
        "task_id": context.task.task_id,
        "language": context.task.language,
        "gate": context.task.gate,
        "mode": context.mode,
        "verdict": verdict,
        "reason": state.get("reason") or "",
        "resolved": verdict == "resolved",
        "not_run": verdict == "not_run",
        "incomplete": verdict == "not_run" or bool(harness_error),
        "iterations": 1 if patch_report else 0,
        "engine_calls": 1 if patch_report else 0,
        "wall_ms": wall_ms,
        "trace_id": context.recorder.trace_id,
        "session_id": context.recorder.session_id,
        "order": state.get("order") or [],
        "harness_error": harness_error or None,
        "engine": {
            "name": context.engine.name,
            "summary": patch_report.get("summary"),
            "files_changed": patch_report.get("files_changed"),
            "tests_added": patch_report.get("tests_added") or [],
            "notes": patch_report.get("notes") or [],
            "cli_version": patch_report.get("cli_version"),
            "error": patch_report.get("engine_error"),
            "detail": patch_report.get("error_detail"),
            "infrastructure": patch_report.get("engine_error") in INFRASTRUCTURE_ERRORS,
            "reset_at": patch_report.get("reset_at"),
            "reset_at_utc": patch_report.get("reset_at_utc"),
        },
        "isolation": {
            "passed": isolation.get("passed", False),
            "commits": isolation.get("commits"),
            "rev_list_all": isolation.get("rev_list_all"),
            "remotes": isolation.get("remotes"),
            "refs": isolation.get("refs"),
            "log": isolation.get("log"),
            "tree_matches_baseline": isolation.get("tree_matches_baseline"),
            "sandbox_commit": isolation.get("sandbox_commit"),
            "eval_dir_in_sandbox": isolation.get("eval_dir_in_sandbox"),
        },
        "test_integrity": {
            "verdict": integrity.get("verdict", "not_run"),
            "test_intrusion": bool(integrity.get("test_intrusion")),
            "findings": integrity.get("findings") or [],
            "added_test_files": integrity.get("added_test_files") or [],
            "ast": integrity.get("ast"),
        },
        "hidden_tests": {
            "exit_code": hidden.get("exit_code"),
            "passed": hidden.get("passed"),
            "duration_ms": hidden.get("duration_ms"),
            "targets": hidden.get("targets"),
            "tests_patch_exit": (hidden.get("tests_patch") or {}).get("exit_code"),
            "notes": hidden.get("notes") or [],
            "summary": hidden.get("summary") or "",
            "tail": hidden.get("tail") or "",
            "log": str(context.evidence / "hidden-tests.log"),
        },
        "sandbox": str(context.sandbox.path),
        "evidence": str(context.evidence),
        "full_verify": None,
    }
    if context.full_verify and verdict == "resolved":
        report["full_verify"] = run_full_verify(
            repo=context.sandbox.path,
            run_id=f"t24-task{context.task.number}-{context.mode}",
            log_path=context.evidence / "full-verify.log",
            timeout_s=context.full_timeout_s,
            runner_factory=context.runner_factory,
        )
    json_dump(context.evidence / "report.json", report)
    return report

def pick_cheat_target(repo: Path, commit: str, roots: Sequence[str]) -> str:
    candidates = baseline_test_files(repo, commit, roots)
    if not candidates:
        raise HarnessError("cheat mode needs a baseline test file to weaken")
    python = [item for item in candidates if item.endswith(".py")]
    return python[0] if python else candidates[0]


def make_engine(
    mode: str,
    *,
    task: GoldenTask,
    sandbox: Sandbox,
    roots: Sequence[str],
    codex: CodexOptions | None = None,
) -> EvalEngine:
    if mode == "gold":
        return GoldEngine(task.gold_patch, parse_patch_paths(task.gold_patch))
    if mode == "noop":
        return NoopEngine()
    if mode == "cheat":
        return CheatEngine(pick_cheat_target(sandbox.path, sandbox.baseline_commit, roots))
    if mode == "codex":
        options = codex or CodexOptions()
        return CodexEvalEngine(
            binary=options.binary,
            model=options.model,
            timeout_s=options.timeout_s,
            env=options.env,
        )
    raise HarnessError(f"unknown mode: {mode!r}")


@dataclasses.dataclass(frozen=True)
class HarnessPaths:
    source: Path = DEFAULT_SOURCE
    orbita: Path = DEFAULT_ORBITA
    ref: str = DEFAULT_REF
    work_root: Path = DEFAULT_WORK_ROOT
    evidence_root: Path = DEFAULT_EVIDENCE_ROOT
    report_root: Path = ROOT / "eval"
    python: str = dataclasses.field(default_factory=lambda: sys.executable)
    java_home: Path | None = DEFAULT_JAVA_HOME
    hidden_timeout_s: int = 1800
    full_timeout_s: int = 1800
    codex: CodexOptions = dataclasses.field(default_factory=CodexOptions)



def run_control(
    *,
    task: GoldenTask,
    mode: str,
    paths: HarnessPaths,
    report_ts: str,
    keep_sandboxes: bool = False,
    full_verify: bool = False,
    runner_factory: Callable[..., ContainerTestRunner] | None = None,
) -> dict[str, Any]:
    evidence = paths.evidence_root / f"{mode}-task{task.number}"
    if evidence.exists():
        remove_tree(evidence)
    evidence.mkdir(parents=True)
    sandbox_dir = paths.work_root / f"{mode}-task{task.number}"
    sandbox = prepare_sandbox(
        source_repo=paths.orbita,
        baseline_sha=task.baseline_sha,
        dest=sandbox_dir,
        issue=task.issue,
    )
    (evidence / "issue.md").write_bytes(task.issue)
    engine = make_engine(
        mode, task=task, sandbox=sandbox, roots=task.test_roots, codex=paths.codex
    )
    context = CaseContext(
        task=task,
        mode=mode,
        sandbox=sandbox,
        engine=engine,
        evidence=evidence,
        python_exe=paths.python,
        java_home=paths.java_home,
        hidden_timeout_s=paths.hidden_timeout_s,
        recorder=_recorder(evidence),
        full_verify=full_verify,
        runner_factory=runner_factory,
        full_timeout_s=paths.full_timeout_s,
    )
    try:
        report = run_case(context)
    finally:
        if not keep_sandboxes:
            remove_tree(sandbox_dir)
    report["report_ts"] = report_ts
    return report


def check_expectations(cases: Sequence[dict[str, Any]]) -> list[str]:
    mismatches: list[str] = []
    for case in cases:
        mode = case["mode"]
        expected = EXPECTED.get(mode)
        if not expected:
            continue
        label = f"task {case['task']} {mode}"
        if case["harness_error"]:
            mismatches.append(f"{label}: harness error {case['harness_error']}")
        if case["verdict"] != expected["verdict"]:
            mismatches.append(
                f"{label}: verdict {case['verdict']!r}, expected {expected['verdict']!r}"
            )
        intrusion = bool(case["test_integrity"]["test_intrusion"])
        if intrusion != expected["test_intrusion"]:
            mismatches.append(
                f"{label}: test_intrusion={intrusion}, expected {expected['test_intrusion']}"
            )
        if not case["isolation"]["passed"]:
            mismatches.append(f"{label}: history isolation proof failed")
    return mismatches

def render_markdown(
    *, report_ts: str, cases: Sequence[dict[str, Any]], digests: dict[str, Any]
) -> str:
    lines = [
        f"# Eval harness control runs: {report_ts}",
        "",
        "Golden set: `research/golden-5` (`eval/golden/manifest.json`). "
        "Verdict = hidden tests exit 0 AND AST test-integrity pass AND no test-intrusion.",
        "",
        "| task | language | mode | verdict | reason | iterations | engine calls | "
        "wall ms | trace | integrity | intrusion | hidden exit |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for case in cases:
        lines.append(
            "| {task} | {language} | {mode} | {verdict} | {reason} | {iterations} | "
            "{engine_calls} | {wall_ms} | `{trace_id}` | {integrity} | {intrusion} | "
            "{hidden} |".format(
                task=case["task"],
                language=case["language"],
                mode=case["mode"],
                verdict=case["verdict"],
                reason=case["reason"],
                iterations=case["iterations"],
                engine_calls=case["engine_calls"],
                wall_ms=case["wall_ms"],
                trace_id=case["trace_id"],
                integrity=case["test_integrity"]["verdict"],
                intrusion="yes" if case["test_integrity"]["test_intrusion"] else "no",
                hidden=case["hidden_tests"]["exit_code"],
            )
        )
    counts = summarize_cases(cases)
    lines.extend(
        [
            "",
            "## Counters",
            "",
            f"- resolved: {counts['resolved']}",
            f"- failed: {counts['failed']}",
            f"- not_run (infrastructure): {counts['not_run']}",
            f"- test intrusion: {counts['intrusion']}",
            f"- harness errors: {counts['harness_errors']}",
            f"- incomplete: {counts['incomplete']}",
            "",
        ]
    )
    for case in cases:
        engine = case.get("engine") or {}
        if engine.get("error") and engine.get("infrastructure"):
            lines.append(
                f"- task {case['task']} {case['mode']}: not_run because "
                f"{engine['error']}"
                + (f" (reset {engine['reset_at_utc']})" if engine.get("reset_at_utc") else "")
            )
    lines.extend(["", "## History isolation", ""])
    for case in cases:
        isolation = case["isolation"]
        lines.append(
            f"- task {case['task']} {case['mode']}: commits={isolation['commits']}, "
            f"rev-list --all --count={isolation['rev_list_all']}, "
            f"remotes={isolation['remotes']!r}, refs={isolation['refs']}, "
            f"tree==baseline={isolation['tree_matches_baseline']}"
        )
    lines.extend(
        [
            "",
            "## Hidden tests and order",
            "",
        ]
    )
    for case in cases:
        lines.append(
            f"- task {case['task']} {case['mode']}: order={' -> '.join(case['order'])}, "
            f"targets={case['hidden_tests']['targets']}, "
            f"tests.patch exit={case['hidden_tests']['tests_patch_exit']}"
        )
    full = [case for case in cases if case.get("full_verify")]
    if full:
        lines.extend(["", "## Full gate (runner container)", ""])
        for case in full:
            verify = case["full_verify"]
            lines.append(
                f"- task {case['task']} {case['mode']}: verdict={verify['verdict']}, "
                f"exit={verify['exit_code']}, {verify['duration_ms']} ms, "
                f"container=`{verify['container_id']}`, image=`{verify['image_id']}`, "
                f"platform={verify['platform']}"
            )
    lines.extend(
        [
            "",
            "## Evidence",
            "",
            *[
                f"- task {case['task']} {case['mode']}: `{case['evidence']}`"
                for case in cases
            ],
            "",
            "## Golden digests (sha256)",
            "",
            "| task | file | sha256 |",
            "| --- | --- | --- |",
        ]
    )
    for number, files in sorted(digests.get("tasks", {}).items()):
        for name, digest in sorted(files.items()):
            lines.append(f"| {number} | {name} | `{digest}` |")
    return "\n".join(lines) + "\n"


EXIT_OK = 0
EXIT_FAILURES = 1
EXIT_INCOMPLETE = 3
"""Exit codes: 1 means the agent failed somewhere, 3 means conditions were not met.

An ``incomplete`` run must never be reported as either a win or a loss: the
shadow-mode metric only counts runs where the engine actually ran.
"""


def summarize_cases(cases: Sequence[dict[str, Any]]) -> dict[str, int | bool]:
    counts: dict[str, int | bool] = {
        "total": len(cases),
        "resolved": 0,
        "failed": 0,
        "not_run": 0,
        "intrusion": 0,
        "harness_errors": 0,
    }
    for case in cases:
        verdict = case.get("verdict")
        intrusion = bool((case.get("test_integrity") or {}).get("test_intrusion"))
        if verdict == "resolved":
            counts["resolved"] += 1
        elif intrusion:
            counts["intrusion"] += 1
        elif verdict == "not_run":
            counts["not_run"] += 1
        else:
            counts["failed"] += 1
        if case.get("harness_error"):
            counts["harness_errors"] += 1
    counts["incomplete"] = bool(counts["not_run"] or counts["harness_errors"])
    return counts


def exit_code_for(cases: Sequence[dict[str, Any]], mismatches: Sequence[str]) -> int:
    if mismatches:
        return EXIT_FAILURES
    counts = summarize_cases(cases)
    if counts["failed"] or counts["intrusion"]:
        return EXIT_FAILURES
    if counts["incomplete"]:
        return EXIT_INCOMPLETE
    return EXIT_OK

def write_report(
    *,
    paths: HarnessPaths,
    report_ts: str,
    cases: Sequence[dict[str, Any]],
    digests: dict[str, Any],
) -> dict[str, str]:
    mismatches = check_expectations(cases)
    payload = {
        "report_ts": report_ts,
        "golden_ref": paths.ref,
        "source_repo": str(paths.source),
        "orbita_repo": str(paths.orbita),
        "meter": {
            "resolved": "hidden tests exit 0 AND test-integrity pass AND no test-intrusion",
            "not_run": "engine failed for infrastructure reasons (quota/auth/network/timeout)",
            "expected": EXPECTED,
        },
        "digests": digests,
        "runs": list(cases),
        "counts": summarize_cases(cases),
        "incomplete": summarize_cases(cases)["incomplete"],
        "exit_code": exit_code_for(cases, mismatches),
        "mismatches": mismatches,
    }
    root = paths.report_root
    json_path = root / f"report-{report_ts}.json"
    md_path = root / f"report-{report_ts}.md"
    json_dump(json_path, payload)
    md_path.write_text(
        render_markdown(report_ts=report_ts, cases=cases, digests=digests),
        encoding="utf-8",
        newline="\n",
    )
    return {"json": str(json_path), "markdown": str(md_path)}


def select_tasks(tasks: Sequence[GoldenTask], numbers: Sequence[int]) -> list[GoldenTask]:
    if not numbers:
        return list(tasks)
    by_number = {task.number: task for task in tasks}
    missing = [number for number in numbers if number not in by_number]
    if missing:
        raise HarnessError(f"golden set has no task(s): {missing}")
    return [by_number[number] for number in numbers]


def parse_numbers(raw: str) -> list[int]:
    return [int(part) for part in raw.split(",") if part.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="show the golden tasks in the ref")
    listing.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    listing.add_argument("--ref", default=DEFAULT_REF)

    run = sub.add_parser("run", help="execute control runs and write a report")
    run.add_argument("--tasks", default="1,2", help="comma-separated task numbers")
    run.add_argument("--modes", default="gold,noop,cheat", help="comma-separated modes")
    run.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    run.add_argument("--orbita", type=Path, default=DEFAULT_ORBITA)
    run.add_argument("--ref", default=DEFAULT_REF)
    run.add_argument("--work-root", type=Path, default=DEFAULT_WORK_ROOT)
    run.add_argument("--evidence-root", type=Path, default=DEFAULT_EVIDENCE_ROOT)
    run.add_argument("--report-root", type=Path, default=ROOT / "eval")
    run.add_argument("--python", default=sys.executable)
    run.add_argument("--java-home", type=Path, default=DEFAULT_JAVA_HOME)
    run.add_argument("--hidden-timeout", type=int, default=1800)
    run.add_argument("--full-verify", default="", help="task number that also runs the full gate")
    run.add_argument("--full-timeout", type=int, default=1800)
    run.add_argument("--codex-bin", default=None, help=f"default: $CODEX_BIN or {DEFAULT_CODEX_BIN}")
    run.add_argument("--codex-model", default=None, help="default: $CODEX_MODEL or config.toml")
    run.add_argument("--codex-timeout", type=int, default=DEFAULT_TIMEOUT_S)
    run.add_argument("--keep-sandboxes", action="store_true")
    run.add_argument("--report-ts", default=None)
    args = parser.parse_args(argv)

    if args.command == "list":
        golden = GoldenSet(args.source, args.ref)
        for task in golden.tasks:
            print(
                json.dumps(
                    {
                        "task": task.number,
                        "id": task.task_id,
                        "language": task.language,
                        "gate": task.gate,
                        "baseline_sha": task.baseline_sha,
                        "test_roots": task.test_roots,
                        "expected_red": task.expected_red,
                    },
                    ensure_ascii=False,
                )
            )
        return 0

    golden = GoldenSet(args.source, args.ref)
    digests = golden.record_digests(args.report_root / "golden-digests.json")
    tasks = select_tasks(golden.tasks, parse_numbers(args.tasks))
    modes = [mode.strip() for mode in args.modes.split(",") if mode.strip()]
    unknown = [mode for mode in modes if mode not in MODES]
    if unknown:
        raise HarnessError(f"unknown mode(s): {unknown}")
    report_ts = args.report_ts or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    paths = HarnessPaths(
        source=args.source,
        orbita=args.orbita,
        ref=args.ref,
        work_root=args.work_root,
        evidence_root=args.evidence_root,
        report_root=args.report_root,
        python=args.python,
        java_home=args.java_home,
        hidden_timeout_s=args.hidden_timeout,
        full_timeout_s=args.full_timeout,
        codex=CodexOptions(
            binary=args.codex_bin,
            model=args.codex_model,
            timeout_s=args.codex_timeout,
        ),
    )
    full_verify_task = int(args.full_verify) if args.full_verify.strip() else None
    cases: list[dict[str, Any]] = []
    for task in tasks:
        for mode in modes:
            print(f"== task {task.number} ({task.task_id}) mode {mode}", flush=True)
            case = run_control(
                task=task,
                mode=mode,
                paths=paths,
                report_ts=report_ts,
                keep_sandboxes=args.keep_sandboxes,
                full_verify=full_verify_task == task.number and mode == "gold",
            )
            print(
                json.dumps(
                    {
                        "task": case["task"],
                        "mode": case["mode"],
                        "verdict": case["verdict"],
                        "reason": case["reason"],
                        "wall_ms": case["wall_ms"],
                        "hidden_exit": case["hidden_tests"]["exit_code"],
                        "intrusion": case["test_integrity"]["test_intrusion"],
                        "full_verify": (case.get("full_verify") or {}).get("verdict"),
                        "harness_error": case["harness_error"],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            cases.append(case)
    written = write_report(paths=paths, report_ts=report_ts, cases=cases, digests=digests)
    mismatches = check_expectations(cases)
    counts = summarize_cases(cases)
    exit_code = exit_code_for(cases, mismatches)
    print(
        json.dumps(
            {
                "report": written,
                "runs": len(cases),
                "counts": counts,
                "incomplete": counts["incomplete"],
                "mismatches": mismatches,
                "exit_code": exit_code,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())