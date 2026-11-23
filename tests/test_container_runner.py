"""Lifecycle and reconciliation tests against an in-memory Docker facade."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import agent_skeleton as skeleton
from container_test_runner import (
    CACHE_VOLUMES,
    RUNNER_ENV,
    TMPFS_MOUNTS,
    ContainerRecord,
    ContainerTestRunner,
    ExecOutcome,
    RunSpec,
    SubprocessDocker,
)


class FakeDocker:
    """In-memory stand-in for the docker CLI used by ContainerTestRunner."""

    def __init__(self, *, available: bool = True, exec_queue: list[ExecOutcome] | None = None):
        self.is_available = available
        self.containers: dict[str, ContainerRecord] = {}
        self.networks: dict[str, bool] = {}
        self.network_calls: list[tuple[str, str]] = []
        self.run_specs: list[RunSpec] = []
        self.exec_calls: list[tuple[str, str, int]] = []
        self.killed: list[str] = []
        self.removed: list[str] = []
        self.exec_queue = list(exec_queue or [])
        self.default_outcome = ExecOutcome(exit_code=0, duration_ms=12, output="ok\n")
        self.counter = 0

    def _next_id(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}{self.counter:04d}"

    def add_container(self, role: str, run_id: str, *, status: str = "running") -> str:
        container_id = self._next_id(role)
        name = f"agent-{run_id}-{role}"
        self.containers[container_id] = ContainerRecord(
            id=container_id,
            name=name,
            labels={"agent-run": run_id, "agent-role": role},
            image_id=f"sha256:{role}-image",
            status=status,
        )
        return container_id

    def available(self) -> tuple[bool, str]:
        if self.is_available:
            return True, "29.7.2"
        return False, "Cannot connect to the Docker daemon"

    def ensure_network(self, name: str, *, internal: bool) -> None:
        self.network_calls.append(("ensure", name))
        self.networks.setdefault(name, internal)

    def remove_network(self, name: str) -> None:
        self.network_calls.append(("remove", name))
        self.networks.pop(name, None)

    def connect_network(self, network: str, container: str, *, alias: str | None = None) -> None:
        self.network_calls.append(("connect", f"{network}:{container}"))

    def list_containers(self, *, label: str | None = None) -> list[ContainerRecord]:
        records = list(self.containers.values())
        if label:
            key, _, value = label.partition("=")
            records = [item for item in records if item.labels.get(key) == value]
        return records

    def run(self, spec: RunSpec) -> str:
        self.run_specs.append(spec)
        container_id = self._next_id(spec.labels["agent-role"])
        self.containers[container_id] = ContainerRecord(
            id=container_id,
            name=spec.name,
            labels=dict(spec.labels),
            image_id=f"sha256:{spec.image.split(':')[0]}-image",
            status="running",
        )
        return container_id

    def start(self, container: str) -> None:
        record = self.containers[container]
        self.containers[container] = ContainerRecord(
            id=record.id,
            name=record.name,
            labels=record.labels,
            image_id=record.image_id,
            status="running",
        )

    def kill(self, container: str) -> None:
        self.killed.append(container)
        record = self.containers[container]
        self.containers[container] = ContainerRecord(
            id=record.id,
            name=record.name,
            labels=record.labels,
            image_id=record.image_id,
            status="exited",
        )

    def remove(self, container: str) -> None:
        self.removed.append(container)
        self.containers.pop(container, None)

    def inspect(self, container: str) -> ContainerRecord:
        return self.containers[container]

    def image_platform(self, image_id: str) -> str:
        return "linux/amd64"

    def exec(
        self,
        container: str,
        command,
        *,
        timeout_s: int,
        log_path: Path | None = None,
    ) -> ExecOutcome:
        text = command if isinstance(command, str) else " ".join(command)
        self.exec_calls.append((container, text, timeout_s))
        if "tcp://127.0.0.1:2375" in text:
            return ExecOutcome(exit_code=0, duration_ms=3, output="Server Version: 29.7.2\n")
        outcome = self.exec_queue.pop(0) if self.exec_queue else self.default_outcome
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(outcome.output, encoding="utf-8", newline="\n")
        return outcome

    def roles(self, run_id: str) -> list[str]:
        return sorted(
            item.labels["agent-role"]
            for item in self.containers.values()
            if item.labels.get("agent-run") == run_id
        )


class RunnerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.logs = self.root / "logs"
        self.logs.mkdir()

    def runner(self, docker: FakeDocker, run_id: str = "t23-unit") -> ContainerTestRunner:
        return ContainerTestRunner(run_id, self.repo, profile="fast", docker=docker)


class StartTests(RunnerTestCase):
    def test_start_creates_one_dind_and_one_runner(self) -> None:
        docker = FakeDocker()
        runner = self.runner(docker)
        self.assertEqual(runner.start(), "started")
        self.assertEqual(docker.roles("t23-unit"), ["dind", "runner"])
        self.assertTrue(docker.networks["runner-net-t23-unit"])
        self.assertFalse(docker.networks["runner-egress-t23-unit"])
        dind = next(item for item in docker.run_specs if item.labels["agent-role"] == "dind")
        self.assertTrue(dind.privileged)
        self.assertEqual(dind.network_alias, "dind")
        self.assertEqual(dind.volumes, ("orbita-runner-dind:/var/lib/docker",))
        work = next(item for item in docker.run_specs if item.labels["agent-role"] == "runner")
        self.assertFalse(work.privileged)
        self.assertEqual(work.network, "runner-net-t23-unit")
        self.assertEqual(work.env, RUNNER_ENV)
        self.assertEqual(work.workdir, "/work")
        self.assertIn(f"{self.repo.as_posix()}:/work", work.volumes)
        for volume in CACHE_VOLUMES.values():
            self.assertIn(volume, work.volumes)
        self.assertEqual(work.tmpfs, TMPFS_MOUNTS)
        self.assertFalse(any("docker.sock" in item for item in work.volumes))

    def test_second_start_reuses_live_containers(self) -> None:
        docker = FakeDocker()
        runner = self.runner(docker)
        self.assertEqual(runner.start(), "started")
        first = runner.runner_id
        self.assertEqual(runner.start(), "started")
        self.assertEqual(docker.roles("t23-unit"), ["dind", "runner"])
        self.assertEqual(runner.runner_id, first)
        self.assertEqual(docker.removed, [])

    def test_reconcile_adopts_dind_and_recreates_runner(self) -> None:
        docker = FakeDocker()
        dind = docker.add_container("dind", "t23-unit")
        stale = docker.add_container("runner", "t23-unit")
        runner = self.runner(docker)
        summary = runner.reconcile()
        self.assertEqual(summary["dind"], "adopted")
        self.assertEqual(summary["removed_runners"], 1)
        self.assertEqual(runner.dind_id, dind)
        self.assertIn(stale, docker.removed)
        self.assertNotIn(dind, docker.removed)
        runner.start()
        self.assertEqual(docker.roles("t23-unit"), ["dind", "runner"])
        self.assertEqual(
            [item.labels["agent-role"] for item in docker.run_specs], ["runner"]
        )

    def test_duplicate_dind_is_collapsed_to_one(self) -> None:
        docker = FakeDocker()
        first = docker.add_container("dind", "t23-unit")
        second = docker.add_container("dind", "t23-unit")
        runner = self.runner(docker)
        summary = runner.reconcile()
        self.assertEqual(summary["removed_dind_duplicates"], 1)
        self.assertEqual(runner.dind_id, first)
        self.assertIn(second, docker.removed)
        self.assertEqual(docker.roles("t23-unit"), ["dind"])

    def test_foreign_live_dind_blocks_start(self) -> None:
        docker = FakeDocker()
        docker.add_container("dind", "other-run")
        runner = self.runner(docker)
        self.assertEqual(runner.start(), "failed")
        self.assertIn("other-run", runner.reason)
        self.assertEqual(docker.run_specs, [])

    def test_reconcile_never_touches_foreign_runs(self) -> None:
        docker = FakeDocker()
        foreign_dind = docker.add_container("dind", "other-run")
        foreign_runner = docker.add_container("runner", "other-run")
        runner = self.runner(docker)
        runner.reconcile()
        self.assertEqual(docker.removed, [])
        self.assertEqual(docker.killed, [])
        self.assertIn(foreign_dind, docker.containers)
        self.assertIn(foreign_runner, docker.containers)

    def test_start_after_kill_replaces_dead_runner_only(self) -> None:
        docker = FakeDocker()
        runner = self.runner(docker)
        runner.start()
        dind = runner.dind_id
        docker.kill(runner.runner_id)
        runner.start()
        self.assertEqual(runner.dind_id, dind)
        self.assertEqual(docker.roles("t23-unit"), ["dind", "runner"])
        self.assertNotIn(dind, docker.removed)


class ExecTests(RunnerTestCase):
    def test_exec_passes_log_and_records_evidence(self) -> None:
        docker = FakeDocker()
        runner = self.runner(docker)
        log_path = self.logs / "tests.log"
        result = runner.exec(runner.command_for("fast"), 60, log_path=log_path)
        self.assertEqual(result.verdict, "pass")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.container_id, runner.runner_id)
        self.assertTrue(result.image_id)
        self.assertEqual(result.platform, "linux/amd64")
        self.assertGreaterEqual(result.duration_ms, 0)
        self.assertEqual(result.log_path, str(log_path))
        self.assertIn("ruff check", result.command[0])
        self.assertTrue(log_path.is_file())

    def test_timeout_kills_runner_and_reports_timeout(self) -> None:
        docker = FakeDocker(
            exec_queue=[ExecOutcome(exit_code=124, duration_ms=7000, timed_out=True, output="")]
        )
        runner = self.runner(docker)
        runner.start()
        container_id = runner.runner_id
        result = runner.exec("sleep 600", 1, log_path=self.logs / "timeout.log")
        self.assertEqual(result.verdict, "timeout")
        self.assertEqual(result.exit_code, 124)
        self.assertEqual(docker.killed, [container_id])

    def test_failed_command_is_fail_not_timeout(self) -> None:
        docker = FakeDocker(exec_queue=[ExecOutcome(exit_code=1, duration_ms=20, output="boom")])
        runner = self.runner(docker)
        result = runner.exec(runner.command_for("fast"), 60)
        self.assertEqual(result.verdict, "fail")
        self.assertEqual(result.exit_code, 1)
        self.assertIn("boom", result.output_tail)
        self.assertEqual(docker.killed, [])

    def test_docker_unavailable_is_not_run_without_host_fallback(self) -> None:
        docker = FakeDocker(available=False)
        runner = self.runner(docker)
        log_path = self.logs / "tests.log"
        self.assertEqual(runner.start(), "unavailable")
        result = runner.exec(runner.command_for("fast"), 60, log_path=log_path)
        self.assertEqual(result.verdict, "not_run")
        self.assertIn("Docker daemon", result.reason)
        self.assertEqual(docker.run_specs, [])
        self.assertEqual(docker.exec_calls, [])
        self.assertIn("not_run", log_path.read_text(encoding="utf-8"))

    def test_profile_names_are_rejected_when_unknown(self) -> None:
        runner = self.runner(FakeDocker())
        with self.assertRaises(ValueError):
            runner.command_for("overnight")


class RealAdapterTests(unittest.TestCase):
    def test_missing_docker_binary_is_not_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runner = ContainerTestRunner(
                "t23-no-docker",
                Path(temporary),
                docker=SubprocessDocker("definitely-not-a-docker-binary"),
            )
            self.assertEqual(runner.start(), "unavailable")
            result = runner.exec(runner.command_for("fast"), 60)
            self.assertEqual(result.verdict, "not_run")
            self.assertIn("not runnable", result.reason)
            self.assertEqual(result.container_id, "")


class StopTests(RunnerTestCase):
    def test_stop_cleanup_removes_only_own_containers(self) -> None:
        docker = FakeDocker()
        foreign = docker.add_container("runner", "other-run")
        runner = self.runner(docker)
        runner.start()
        runner.stop(cleanup=True)
        self.assertEqual(docker.roles("t23-unit"), [])
        self.assertIn(foreign, docker.containers)
        self.assertEqual(docker.networks, {})
        self.assertEqual(runner.state, "stopped")

    def test_stop_without_cleanup_keeps_containers_for_adoption(self) -> None:
        docker = FakeDocker()
        runner = self.runner(docker)
        runner.start()
        dind = runner.dind_id
        runner.stop(cleanup=False)
        self.assertEqual(docker.roles("t23-unit"), ["dind", "runner"])
        resumed = self.runner(docker)
        summary = resumed.reconcile()
        self.assertEqual(summary["dind"], "adopted")
        self.assertEqual(resumed.dind_id, dind)


GOOD_FUNCTION = '''"""Helper added by the container-gate graph test."""


def ping(name: str = "container") -> str:
    return f"pong:{name}"
'''

GOOD_TEST = '''import unittest

from tools.container_ping import ping


class ContainerPingTests(unittest.TestCase):
    def test_ping(self):
        self.assertEqual(ping("t23"), "pong:t23")
'''


class GraphRunTests(RunnerTestCase):
    def setUp(self) -> None:
        super().setUp()
        (self.repo / "tools").mkdir()
        (self.repo / "tests").mkdir()
        (self.repo / "README.md").write_text("fixture\n", encoding="utf-8")
        (self.repo / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
        self.git("init", "-q")
        self.git("config", "user.email", "t23@example.invalid")
        self.git("config", "user.name", "t23 fixture")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "fixture")
        self.sha = self.git("rev-parse", "HEAD").strip()
        self.evidence = self.root / "evidence"
        self.state_db = self.root / "state" / "graph.sqlite"

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.repo, capture_output=True, text=True, check=True
        ).stdout

    def test_graph_runs_fast_and_full_in_the_runner(self) -> None:
        docker = FakeDocker()
        task = skeleton.TaskSpec.model_validate(
            {
                "task_id": "t23-container-graph",
                "repo": str(self.repo),
                "baseline_sha": self.sha,
                "patch": {
                    "tools/container_ping.py": GOOD_FUNCTION,
                    "tests/test_container_ping.py": GOOD_TEST,
                },
                "runner": {"run_id": "t23-graph", "fast_timeout_s": 120, "full_timeout_s": 300},
            }
        )
        settings = skeleton.SkeletonSettings(
            _env_file=None,
            tracing=False,
            evidence_root=self.evidence,
            state_db=self.state_db,
        )

        def factory(task_spec, repo_path):
            return ContainerTestRunner(
                run_id=task_spec.runner.run_id,
                repo_path=repo_path,
                profile="fast",
                docker=docker,
            )

        report = skeleton.run_graph(task=task, settings=settings, runner_factory=factory)
        self.assertEqual(report["verdict"], "pass", json.dumps(report["gates"], indent=2))
        gates = report["gates"]
        self.assertEqual(sorted(gates), ["diff-scope", "full", "test-integrity", "tests"])
        for name in ("tests", "full"):
            gate = gates[name]
            self.assertTrue(gate["passed"], name)
            self.assertEqual(gate["verdict"], "pass", name)
            self.assertTrue(gate["container_id"], name)
            self.assertTrue(gate["image_id"], name)
            self.assertEqual(gate["platform"], "linux/amd64", name)
            self.assertTrue(gate["log_path"], name)
            self.assertGreaterEqual(gate["duration_ms"], 0, name)
            self.assertEqual(gate["exit_code"], 0, name)
        bundle = self.evidence / "t23-container-graph"
        self.assertTrue((bundle / "tests.log").is_file())
        self.assertTrue((bundle / "full.log").is_file())
        commands = [item[1] for item in docker.exec_calls]
        self.assertTrue(any("ruff check" in item for item in commands))
        self.assertTrue(any("npm ci --ignore-scripts" in item for item in commands))
        self.assertEqual(docker.roles("t23-graph"), [])
        self.assertEqual(docker.removed and len(docker.removed), 2)


if __name__ == "__main__":
    unittest.main()
