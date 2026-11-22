"""Idempotent branch push / PR creation against a local bare remote and a gh stub."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from publish import (  # noqa: E402
    GitHubClient,
    PublishConfig,
    Publisher,
    branch_name,
    pr_marker,
)

GH_STUB = """@echo off
echo %*>>"%GH_STUB_LOG%"
if "%1 %2"=="pr list" goto list
if "%1 %2"=="pr create" goto create
echo unexpected-gh-call>&2
exit /b 2

:list
set /p STATE=<"%GH_STUB_STATE%"
if "%STATE%"=="existing" goto found
echo []
exit /b 0

:found
echo [{"number":7,"url":"https://example.invalid/pull/7","body":"<!-- agent-run:run1 -->"}]
exit /b 0

:create
type "%~6">>"%GH_STUB_LOG%" 2>nul
>"%GH_STUB_STATE%" echo existing
echo https://example.invalid/pull/7
exit /b 0
"""

PATCH_FILE = "tools/agent_demo.py"
PATCH_TEXT = '"""File created by the publish idempotency fixture."""\n'


class PublishFixture(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "work"
        self.repo.mkdir()
        self.bare = self.root / "t16-remote.git"
        self.stub = self.root / "gh-stub.cmd"
        self.stub.write_text(GH_STUB, encoding="utf-8", newline="\r\n")
        self.log = self.root / "gh-journal.log"
        self.state = self.root / "gh-state.txt"
        self.state.write_text("new\n", encoding="utf-8")
        self.git(self.repo, "init", "-q")
        self.git(self.repo, "config", "user.email", "t16@example.invalid")
        self.git(self.repo, "config", "user.name", "t16 fixture")
        self.git(self.repo, "config", "commit.gpgsign", "false")
        self.git(self.repo, "config", "core.autocrlf", "false")
        (self.repo / "README.md").write_text("fixture\n", encoding="utf-8")
        self.git(self.repo, "add", "-A")
        self.git(self.repo, "commit", "-q", "-m", "baseline")
        self.git(self.root, "init", "--bare", "-q", str(self.bare))
        self.git(self.repo, "remote", "add", "origin", str(self.bare))
        (self.repo / PATCH_FILE).parent.mkdir(parents=True, exist_ok=True)
        (self.repo / PATCH_FILE).write_text(PATCH_TEXT, encoding="utf-8", newline="\n")

    def git(self, cwd: Path, *args: str, check: bool = True) -> str:
        completed = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
        )
        if check and completed.returncode != 0:
            raise AssertionError(completed.stderr)
        return completed.stdout

    def env(self) -> dict[str, str]:
        return {
            **os.environ,
            "GH_STUB_LOG": str(self.log),
            "GH_STUB_STATE": str(self.state),
        }

    def config(self) -> PublishConfig:
        return PublishConfig(
            repo_slug="prmAim/setupAIv2",
            issue="16",
            run_id="run1",
            base="main",
            title="agent: t16 idempotency",
            body="Publish idempotency fixture.",
        )

    def publisher(self, repo: Path | None = None) -> Publisher:
        env = self.env()
        client = GitHubClient(binary=str(self.stub), env=env)
        return Publisher(repo or self.repo, self.config(), client, env=env)

    def journal(self) -> list[str]:
        if not self.log.is_file():
            return []
        return [line for line in self.log.read_text(encoding="utf-8").splitlines() if line]

    def branch_heads(self) -> list[str]:
        output = self.git(self.root, "ls-remote", "--heads", str(self.bare))
        return [line.split("\t")[-1] for line in output.splitlines() if line.strip()]

    def commit_count(self, branch: str) -> int:
        return int(self.git(self.repo, "rev-list", "--count", branch).strip())


class PublishIdempotencyTests(PublishFixture):
    def test_two_node_runs_create_one_branch_and_one_pr(self) -> None:
        branch = branch_name("16", "run1")
        first = self.publisher()
        push_first = first.push()
        pr_first = first.open_pr()

        second = self.publisher()
        push_second = second.push()
        pr_second = second.open_pr()

        self.assertEqual(push_first["status"], "pushed")
        self.assertEqual(push_second["status"], "pushed")
        self.assertEqual(pr_first["status"], "created")
        self.assertEqual(pr_second["status"], "reused")
        self.assertEqual(pr_first["number"], pr_second["number"])
        self.assertEqual(pr_first["url"], pr_second["url"])
        self.assertEqual(self.branch_heads(), [f"refs/heads/{branch}"])
        self.assertEqual(self.commit_count(branch), 2)
        journal = self.journal()
        self.assertEqual(sum("pr create" in line for line in journal), 1, journal)
        self.assertEqual(sum("pr list" in line for line in journal), 2, journal)
        self.assertIn(pr_marker("run1"), "\n".join(journal))
        self.assertIn("--body-file", "\n".join(journal))

    def test_existing_marked_pr_is_adopted_without_create(self) -> None:
        self.state.write_text("existing\n", encoding="utf-8")
        publisher = self.publisher()
        publisher.push()
        result = publisher.open_pr()
        journal = self.journal()
        self.assertEqual(result["status"], "reused")
        self.assertFalse(result["created"])
        self.assertEqual(result["number"], 7)
        self.assertEqual(result["url"], "https://example.invalid/pull/7")
        self.assertEqual(sum("pr create" in line for line in journal), 0, journal)
        self.assertEqual(sum("pr list" in line for line in journal), 1, journal)

    def test_resume_from_a_fresh_clone_reuses_the_remote_branch(self) -> None:
        branch = branch_name("16", "run1")
        first = self.publisher()
        first.push()
        first.open_pr()
        before = self.commit_count(branch)

        clone = self.root / "resume"
        clone.mkdir()
        self.git(clone, "init", "-q")
        self.git(clone, "config", "user.email", "t16@example.invalid")
        self.git(clone, "config", "user.name", "t16 fixture")
        self.git(clone, "remote", "add", "origin", str(self.bare))
        resumed = self.publisher(clone)
        push = resumed.push()
        self.assertEqual(push["status"], "pushed")
        self.assertFalse(push["created"])
        self.assertFalse(push["committed"])
        self.assertEqual(self.git(clone, "rev-parse", "--abbrev-ref", "HEAD").strip(), branch)
        self.assertEqual(self.branch_heads(), [f"refs/heads/{branch}"])
        self.assertEqual(self.commit_count(branch), before)


if __name__ == "__main__":
    unittest.main()