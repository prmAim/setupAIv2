"""Offline process-restart probe, run inside the pinned DeerFlow image.

Uses real DeerFlow agent assembly and SQLite checkpoints, a synthetic model,
no tools and no provider credentials. Not a live Gateway/claim/PR acceptance.
Run with python -B -m tools.verify_deerflow_resume. All state is temporary.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def initial_progress(project):
    return {
        "run_id": "a" * 32, "repository": f"fixture/{project}", "issue_number": 1,
        "profile_revision": "b" * 40, "goal": f"Resume {project} fixture",
        "decisions": ["DeerFlow harness", "no external effects"],
        "plan": ["preserve context", "synthetic model", "stop"],
        "completed_steps": ["preserve context"],
        "checks": [{"name": "fixture-only", "status": "passed"}],
        "external_effects": [], "next_action": "synthetic model", "stop_reason": None,
    }


def worker(root, project, phase):
    from deerflow.agents.factory import create_deerflow_agent
    from langchain.agents.middleware import AgentMiddleware
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.types import Command, interrupt

    from tools.deerflow_cycle_state import CycleState

    local = root / project
    local.mkdir(exist_ok=True)
    calls = local / "model-calls"

    class SyntheticModel(BaseChatModel):
        @property
        def _llm_type(self):
            return "offline-resume-fixture"

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            # Exclusive creation makes an unintended second call fail the probe.
            with calls.open("x", encoding="utf-8") as stream:
                stream.write("one synthetic call\n")
                stream.flush()
                os.fsync(stream.fileno())
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="fixture completed"))])

    class Pause(AgentMiddleware):
        state_schema = CycleState

        def before_model(self, state, runtime):
            answer = interrupt({"kind": "offline_fixture_pause"})
            require(answer == "continue fixture", "unexpected resume value")

        def after_model(self, state, runtime):
            progress = dict(state["cycle"])
            progress["completed_steps"] = [*progress["completed_steps"], "synthetic model"]
            progress["next_action"] = "stop"
            progress["stop_reason"] = "fixture_complete"
            return {"cycle": progress}

    config = {"configurable": {"thread_id": "same-thread-in-both-projects"}}
    with SqliteSaver.from_conn_string(str(local / "checkpoints.sqlite")) as saver:
        agent = create_deerflow_agent(
            SyntheticModel(), tools=[], middleware=[Pause()],
            state_schema=CycleState, checkpointer=saver,
        )
        expected = initial_progress(project)
        if phase == "pause":
            require(not agent.get_state(config).values, "unexpected existing thread")
            agent.invoke({"messages": [{"role": "user", "content": "offline fixture"}],
                          "cycle": expected}, config)
            state = agent.get_state(config)
            require(state.values["cycle"] == expected, "checkpoint lost progress")
            require(bool(state.next) and any(task.interrupts for task in state.tasks), "pause not saved")
            require(not calls.exists(), "model called before pause")
            # Leave without closing the SQLite connection or Python cleanup.
            os._exit(73)
        state = agent.get_state(config)
        if phase == "resume":
            require(state.values["cycle"] == expected, "restart changed progress")
            agent.invoke(Command(resume="continue fixture"), config)
            # Second process dies after the terminal checkpoint, before reporting.
            os._exit(74)
        require(phase == "verify", "unknown phase")
        expected["completed_steps"].append("synthetic model")
        expected["next_action"] = "stop"
        expected["stop_reason"] = "fixture_complete"
        require(state.values["cycle"] == expected, "terminal checkpoint mismatch")
        require(not state.next, "completed run still has pending nodes")
        require(calls.read_text() == "one synthetic call\n", "unexpected model calls")
        require(state.values["messages"][-1].content == "fixture completed", "response lost")
        # Deliberately inspect only: a completed checkpoint is not a new turn.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", choices=["pause", "resume", "verify"])
    parser.add_argument("--root", type=Path)
    parser.add_argument("--project", choices=["orbita", "fortune"])
    args = parser.parse_args()
    if args.worker:
        require(args.root is not None and args.project is not None, "worker context missing")
        worker(args.root, args.project, args.worker)
        return
    with tempfile.TemporaryDirectory(prefix="deerflow-resume-") as folder:
        for phase, expected in [("pause", 73), ("resume", 74), ("verify", 0)]:
            for project in ("orbita", "fortune"):
                result = subprocess.run(
                    [sys.executable, "-B", "-m", "tools.verify_deerflow_resume",
                     "--worker", phase, "--root", folder, "--project", project],
                    capture_output=True, text=True, timeout=60,
                )
                require(result.returncode == expected,
                        f"{project}/{phase}: exit {result.returncode}; {result.stderr[-4000:]}")
        print(json.dumps({"status": "passed", "runtime": "DeerFlow embedded factory",
                          "projects": ["orbita", "fortune"], "processes": 6,
                          "synthetic_model_calls_per_project": 1,
                          "live_model_calls": 0, "external_writes": 0,
                          "gateway_restart": "not_run", "live_effect_recovery": "not_run"}))


if __name__ == "__main__":
    main()
