"""Span shape and session linkage, checked against a recording double."""

from __future__ import annotations

import os
import sys
import uuid
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "tools"
TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import agent_skeleton as skeleton  # noqa: E402
from test_agent_skeleton import GOOD_FUNCTION, GOOD_TEST, GraphFixture  # noqa: E402


class FakeSpan:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.id = f"span{len(payload)}{abs(hash(payload.get('name', ''))) % 10000}"
        self.updates: list[dict] = []
        self.ended = False

    def update(self, **kwargs):
        self.updates.append(kwargs)

    def end(self) -> None:
        self.ended = True


class FakeClient:
    def __init__(self) -> None:
        self.spans: list[FakeSpan] = []
        self.seed = 0
        self.flushed = False

    def create_trace_id(self) -> str:
        return uuid.uuid4().hex

    def start_observation(self, **kwargs) -> FakeSpan:
        span = FakeSpan(kwargs)
        self.spans.append(span)
        return span

    def flush(self) -> None:
        self.flushed = True

    def get_trace_url(self, trace_id: str) -> str:
        return f"http://localhost:3000/traces/{trace_id}"

    def shutdown(self) -> None:
        pass


class SpanTests(GraphFixture):
    def run_with(self, task, client, **kwargs):
        recorder = skeleton.TraceRecorder(
            skeleton.SkeletonSettings(_env_file=None, tracing=True), client_factory=lambda _: client
        )
        settings = skeleton.SkeletonSettings(
            _env_file=None,
            tracing=True,
            evidence_root=self.evidence,
            state_db=self.state_db,
        )
        return skeleton.run_graph(task=task, settings=settings, recorder=recorder, **kwargs)

    def test_generation_and_tool_spans_carry_required_metadata(self) -> None:
        client = FakeClient()
        task = self.build_task(
            patch={
                "tools/smoke_ping.py": GOOD_FUNCTION,
                "tests/test_smoke_ping.py": GOOD_TEST,
            },
            task_id="t16-spans",
        )
        report = self.run_with(task, client)
        self.assertEqual(report["verdict"], "pass")
        self.assertEqual(report["session_id"], "t16-spans")
        self.assertEqual(report["trace"]["status"], "local-recorder")
        self.assertEqual(report["trace"]["trace_ids"], [report["trace_id"]])
        self.assertEqual(
            report["trace"]["traces"][report["trace_id"]]["session_ids"], ["t16-spans"]
        )
        types = set(report["trace"]["traces"][report["trace_id"]]["types"])
        self.assertTrue({"AGENT", "CHAIN", "GENERATION", "TOOL"}.issubset(types), types)

        generations = [span for span in client.spans if span.payload["as_type"] == "generation"]
        self.assertEqual(len(generations), 1)
        metadata = generations[0].payload["metadata"]
        self.assertEqual(metadata["engine"], "fake")
        self.assertIn("cli_version", metadata)
        self.assertEqual(len(metadata["prompt_sha256"]), 64)
        self.assertIsInstance(metadata["output_bytes"], int)
        self.assertEqual(metadata["task_id"], "t16-spans")

        tools = [span for span in client.spans if span.payload["as_type"] == "tool"]
        self.assertTrue(tools)
        for span in tools:
            meta = span.payload["metadata"]
            self.assertIsInstance(meta["cmd"], list)
            self.assertIsInstance(meta["exit"], int)
            self.assertIsInstance(meta["duration_ms"], int)
        self.assertTrue(any(meta.payload["metadata"]["cmd"][0] == "git" for meta in tools))
        self.assertTrue(all(span.ended for span in client.spans))
        self.assertEqual(os.environ["NO_PROXY"], "localhost,127.0.0.1")

    def test_resume_opens_a_new_trace_linked_by_session_id(self) -> None:
        first_client = FakeClient()
        task = self.build_task(
            patch={
                "tools/smoke_ping.py": GOOD_FUNCTION,
                "tests/test_smoke_ping.py": GOOD_TEST,
            },
            task_id="t16-spans-resume",
        )
        first = self.run_with(task, first_client)
        second_client = FakeClient()
        second = self.run_with(task, second_client, resume=True, thread_id="t16-spans-resume")
        self.assertNotEqual(first["trace_id"], second["trace_id"])
        self.assertEqual(second["previous_trace_id"], first["trace_id"])
        self.assertEqual(second["session_id"], "t16-spans-resume")
        first_root = next(span for span in first_client.spans if span.payload["as_type"] == "agent")
        second_root = next(span for span in second_client.spans if span.payload["as_type"] == "agent")
        self.assertEqual(first_root.payload["metadata"]["session_id"], "t16-spans-resume")
        self.assertEqual(second_root.payload["metadata"]["session_id"], "t16-spans-resume")
        self.assertEqual(
            second_root.payload["metadata"]["previous_trace_id"], first["trace_id"]
        )


if __name__ == "__main__":
    unittest.main()