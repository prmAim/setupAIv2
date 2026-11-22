"""Fix loop: declared fix patches only, three attempts, then escalate."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "tools"
TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from test_agent_skeleton import GraphFixture  # noqa: E402

FAILING_TEMPLATE = """import unittest


class AlwaysFailingTests(unittest.TestCase):
    def test_never_green(self):
        self.assertEqual(1, 2)
"""

DIFFERENT_FAILURE = """import unittest


class AlwaysFailingTests(unittest.TestCase):
    def test_never_green(self):
        self.assertEqual(2, 3)
"""


class FixLoopTests(GraphFixture):
    def failing_task(self, fix_patches):
        return self.build_task(
            patch={"tests/test_always_failing.py": FAILING_TEMPLATE},
            fix_patches=fix_patches,
        )

    def test_three_failed_gates_escalate_without_a_fourth_attempt(self) -> None:
        fixes = [
            {"tests/test_always_failing.py": FAILING_TEMPLATE},
            {"tests/test_always_failing.py": DIFFERENT_FAILURE},
            {"tests/test_always_failing.py": FAILING_TEMPLATE},
        ]
        report = self.run_graph(self.failing_task(fixes))
        self.assertEqual(report["verdict"], "escalated")
        self.assertEqual(report["attempts"], 3)
        self.assertEqual(report["engine_calls"], 4)
        self.assertEqual(
            report["fix_history"],
            ["fix patch 1", "fix patch 2", "fix patch 3"],
        )
        self.assertIn("tests", report["gates"])
        self.assertFalse(report["gates"]["tests"]["passed"])

    def test_declared_patches_are_capped_at_three(self) -> None:
        fixes = [{"tests/test_always_failing.py": FAILING_TEMPLATE} for _ in range(5)]
        report = self.run_graph(self.failing_task(fixes))
        self.assertEqual(report["verdict"], "escalated")
        self.assertEqual(report["attempts"], 3)
        self.assertEqual(report["engine_calls"], 4)
        self.assertEqual(report["plan"]["fix_attempts_allowed"], 3)
        self.assertEqual(len(report["fix_history"]), 3)

    def test_no_declared_fix_patch_means_no_extra_engine_call(self) -> None:
        report = self.run_graph(self.failing_task([]))
        self.assertEqual(report["verdict"], "escalated")
        self.assertEqual(report["attempts"], 0)
        self.assertEqual(report["engine_calls"], 1)
        self.assertEqual(report["fix_history"], [])


if __name__ == "__main__":
    unittest.main()