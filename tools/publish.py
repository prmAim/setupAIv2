"""Idempotent branch push and pull-request creation.

Both operations are safe to repeat after a resume: the branch is reused, the
commit step is a no-op when the tree is clean, ``git push`` is idempotent, and
a PR carrying the run marker is adopted instead of creating a second one.
``gh`` is reached through :class:`GitHubClient`, so tests can point ``GH_BIN``
at a logging stub and never touch GitHub.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from process_util import command_for, process_env, redact, resolve_binary

PR_MARKER = "<!-- agent-run:{run_id} -->"
DEFAULT_TIMEOUT_S = 120

ToolHook = Callable[[list[str], int, int], None]


class PublishError(RuntimeError):
    """A deterministic publish failure with redacted stderr."""


class PublishConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_slug: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    issue: str
    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    remote: str = "origin"
    base: str = "main"
    title: str = ""
    body: str = ""

    @field_validator("issue")
    @classmethod
    def _issue_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("issue must not be blank")
        return value.strip()


def pr_marker(run_id: str) -> str:
    return PR_MARKER.format(run_id=run_id)


def branch_name(issue: str, run_id: str) -> str:
    def clean(value: str) -> str:
        return re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-") or "run"

    return f"agent/{clean(issue)}-{clean(run_id)}"


class GitHubClient:
    """Thin ``gh`` wrapper: argv only, no shell, stdout parsed as JSON."""

    def __init__(
        self,
        binary: str | None = None,
        *,
        timeout_s: int = DEFAULT_TIMEOUT_S,
        env: dict[str, str] | None = None,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ) -> None:
        self.binary = binary or resolve_binary("GH_BIN", "gh", env)
        self.timeout_s = timeout_s
        self.env = process_env(env)
        self.runner = runner

    def _run(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
        command = command_for(self.binary, *args)
        return self.runner(
            command,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=self.timeout_s,
            env=self.env,
        )

    def find_pull_request(self, *, repo: str, marker: str) -> dict[str, object] | None:
        search = marker.replace("<!--", "").replace("-->", "").strip()
        completed = self._run(
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "all",
            "--search",
            search,
            "--json",
            "number,url,body",
            "--limit",
            "50",
        )
        if completed.returncode != 0:
            raise PublishError(
                f"gh pr list failed with exit {completed.returncode}: "
                f"{redact(completed.stderr, env=self.env)}"
            )
        try:
            items = json.loads(completed.stdout.strip() or "[]")
        except ValueError as error:
            raise PublishError(f"gh pr list returned invalid JSON: {error}") from error
        for item in items:
            if isinstance(item, dict) and marker in str(item.get("body") or ""):
                return {
                    "number": item.get("number"),
                    "url": item.get("url"),
                    "reused": True,
                }
        return None

    def create_pull_request(
        self, *, repo: str, head: str, base: str, title: str, body: str
    ) -> dict[str, object]:
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".md", delete=False, encoding="utf-8", newline="\n"
        )
        try:
            handle.write(body)
            handle.close()
            completed = self._run(
                "pr",
                "create",
                "--repo",
                repo,
                "--body-file",
                handle.name,
                "--head",
                head,
                "--base",
                base,
                "--title",
                title,
            )
        finally:
            try:
                os.unlink(handle.name)
            except OSError:
                pass
        if completed.returncode != 0:
            raise PublishError(
                f"gh pr create failed with exit {completed.returncode}: "
                f"{redact(completed.stderr, env=self.env)}"
            )
        url = ""
        for line in reversed(completed.stdout.strip().splitlines()):
            if "http" in line:
                url = line.strip()
                break
        number = None
        match = re.search(r"/pull/(\d+)", url)
        if match:
            number = int(match.group(1))
        return {"number": number, "url": url, "reused": False}


class Publisher:
    """Runs git locally; only :class:`GitHubClient` may leave the machine."""

    def __init__(
        self,
        repo: Path,
        config: PublishConfig,
        github: GitHubClient,
        *,
        git_binary: str = "git",
        on_tool: ToolHook | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.repo = Path(repo).resolve()
        self.config = config
        self.github = github
        self.git_binary = git_binary
        self.on_tool = on_tool
        self.env = process_env(env)
        self.branch = branch_name(config.issue, config.run_id)

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        command = [self.git_binary, "-c", "core.quotepath=false", "-C", str(self.repo), *args]
        started = time.perf_counter()
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=300,
            env=self.env,
        )
        if self.on_tool is not None:
            self.on_tool(command, completed.returncode, int((time.perf_counter() - started) * 1000))
        if check and completed.returncode != 0:
            raise PublishError(
                f"git {' '.join(args)} failed with exit {completed.returncode}: "
                f"{redact(completed.stderr, env=self.env)}"
            )
        return completed

    def _commit_if_needed(self) -> bool:
        self._git("add", "-A")
        staged = self._git("diff", "--cached", "--quiet", check=False)
        if staged.returncode == 0:
            return False
        title = self.config.title or f"agent run {self.config.run_id} for issue {self.config.issue}"
        self._git("commit", "-m", title)
        return True

    def ensure_branch(self) -> dict[str, object]:
        current = self._git(
            "rev-parse", "--abbrev-ref", "HEAD", check=False
        ).stdout.strip()
        local = self._git(
            "rev-parse", "--verify", "--quiet", f"refs/heads/{self.branch}", check=False
        ).returncode == 0
        remote = (
            self._git("ls-remote", "--heads", self.config.remote, self.branch, check=False)
            .stdout.strip()
        )
        created = False
        if local:
            if current != self.branch:
                self._git("switch", self.branch)
        elif remote:
            self._git("fetch", self.config.remote, self.branch)
            self._git("switch", "-c", self.branch, "FETCH_HEAD")
        else:
            self._git("switch", "-c", self.branch)
            created = True
        committed = self._commit_if_needed()
        return {"branch": self.branch, "created": created, "committed": committed}

    def push(self) -> dict[str, object]:
        branch_state = self.ensure_branch()
        self._git("push", "--set-upstream", self.config.remote, self.branch)
        return {
            "status": "pushed",
            "remote": self.config.remote,
            "branch": self.branch,
            "created": branch_state["created"],
            "committed": branch_state["committed"],
        }

    def _pr_body(self) -> str:
        text = (self.config.body or "Automated agent run.").rstrip()
        return f"{text}\n\n{pr_marker(self.config.run_id)}\n"

    def open_pr(self) -> dict[str, object]:
        marker = pr_marker(self.config.run_id)
        existing = self.github.find_pull_request(repo=self.config.repo_slug, marker=marker)
        if existing is not None:
            return {
                "status": "reused",
                "created": False,
                "number": existing.get("number"),
                "url": existing.get("url"),
                "branch": self.branch,
                "marker": marker,
            }
        title = self.config.title or f"agent: {self.config.issue} ({self.config.run_id})"
        try:
            created = self.github.create_pull_request(
                repo=self.config.repo_slug,
                head=self.branch,
                base=self.config.base,
                title=title,
                body=self._pr_body(),
            )
        except PublishError:
            adopted = self.github.find_pull_request(repo=self.config.repo_slug, marker=marker)
            if adopted is None:
                raise
            return {
                "status": "reused",
                "created": False,
                "number": adopted.get("number"),
                "url": adopted.get("url"),
                "branch": self.branch,
                "marker": marker,
            }
        return {
            "status": "created",
            "created": True,
            "number": created.get("number"),
            "url": created.get("url"),
            "branch": self.branch,
            "marker": marker,
        }