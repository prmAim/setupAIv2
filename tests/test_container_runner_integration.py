"""One real-Docker check: the fast gate runs inside the runner container.

Skipped when Docker, the runner image, or the orbita baseline checkout is absent.
The baseline is only read through the container mount; the host working tree of
setupAIv2 is never mounted.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from container_test_runner import ContainerTestRunner, SubprocessDocker

BASELINE = Path(os.environ.get("ORBITA_BASELINE", r"C:\Git\_wf\orbita-baseline"))
IMAGE = "orbita-runner:prototype"


def docker_ready() -> bool:
    return SubprocessDocker().available()[0]


def image_ready() -> bool:
    facade = SubprocessDocker()
    return facade._cli("image", "inspect", IMAGE, timeout=30).returncode == 0


@unittest.skipUnless(docker_ready(), "Docker is unavailable")
@unittest.skipUnless(BASELINE.is_dir(), f"orbita baseline is missing: {BASELINE}")
@unittest.skipUnless(image_ready(), f"runner image is missing: {IMAGE}")
class ContainerIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.logs = Path(temporary.name)

    def runner(self, run_id: str, profile: str = "fast") -> ContainerTestRunner:
        runner = ContainerTestRunner(run_id, BASELINE, profile=profile)
        self.addCleanup(runner.stop, True)
        return runner

    def test_fast_profile_passes_in_container(self) -> None:
        runner = self.runner("t23-it-fast")
        self.assertEqual(runner.start(), "started")
        log_path = self.logs / "fast.log"
        result = runner.run_profile("fast", timeout_s=300, log_path=log_path)
        self.assertEqual(result.verdict, "pass", result.output_tail[-1200:])
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(result.container_id)
        self.assertTrue(result.image_id.startswith("sha256:"))
        self.assertEqual(result.platform, "linux/amd64")
        self.assertGreaterEqual(result.duration_ms, 1000)
        self.assertLess(result.duration_ms, 120_000)
        self.assertEqual(result.log_path, str(log_path))
        text = log_path.read_text(encoding="utf-8", errors="replace")
        self.assertIn("Ran 58 tests", text)
        self.assertIn("OK (skipped=1)", text)

    def test_runner_has_no_socket_codex_or_tokens(self) -> None:
        runner = self.runner("t23-it-isolation")
        self.assertEqual(runner.start(), "started", runner.reason)
        command = (
            "set -e; "
            "echo home=$HOME; "
            "test ! -e ~/.codex && echo codex=absent; "
            "test ! -S /var/run/docker.sock && echo docker-sock=absent; "
            "! command -v docker >/dev/null && echo docker-cli=absent; "
            "echo DOCKER_HOST=$DOCKER_HOST; "
            "echo TESTCONTAINERS_HOST_OVERRIDE=$TESTCONTAINERS_HOST_OVERRIDE; "
            "env | grep -Ei 'openai|token|pat|langfuse' | grep -v '^PATH=' || echo secrets=absent"
        )
        log_path = self.logs / "isolation.log"
        info = runner.exec(command, 30, log_path=log_path)
        self.assertEqual(info.verdict, "pass", info.output_tail)
        text = log_path.read_text(encoding="utf-8", errors="replace")
        self.assertIn("codex=absent", text)
        self.assertIn("docker-sock=absent", text)
        self.assertIn("docker-cli=absent", text)
        self.assertIn("DOCKER_HOST=tcp://dind:2375", text)
        self.assertIn("TESTCONTAINERS_HOST_OVERRIDE=dind", text)
        self.assertIn("secrets=absent", text)

    def test_reconcile_reuses_dind_and_has_one_runner(self) -> None:
        docker = SubprocessDocker()
        first = self.runner("t23-it-reconcile")
        self.assertEqual(first.start(), "started")
        dind_id = first.dind_id
        runner_id = first.runner_id
        self.assertTrue(dind_id)
        self.assertTrue(runner_id)
        first.stop(cleanup=False)

        second = self.runner("t23-it-reconcile")
        self.assertEqual(second.start(), "started")
        self.assertEqual(second.dind_id, dind_id)
        self.assertNotEqual(second.runner_id, runner_id)
        records = docker.list_containers(label="agent-run=t23-it-reconcile")
        roles = sorted(item.labels["agent-role"] for item in records)
        self.assertEqual(roles, ["dind", "runner"])


if __name__ == "__main__":
    unittest.main()
