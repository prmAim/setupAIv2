"""Checkpoint payload for the DeerFlow embedded integration.

This is progress, not authorization. The trusted controller must revalidate
claim ownership, policy and remote effects before dispatching any action.
Credentials and provider response bodies must never enter this state.
"""

from typing import TypedDict

from langchain.agents import AgentState


class CycleProgress(TypedDict):
    run_id: str
    repository: str
    issue_number: int
    profile_revision: str
    goal: str
    decisions: list[str]
    plan: list[str]
    completed_steps: list[str]
    checks: list[dict]
    external_effects: list[dict]
    next_action: str
    stop_reason: str | None


class CycleState(AgentState):
    """Pass as state_schema to create_deerflow_agent (full checkpoint mode)."""

    cycle: CycleProgress
