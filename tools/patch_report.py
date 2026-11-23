"""Engine-neutral patch report returned by fake and Codex adapters."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

INFRASTRUCTURE_ERRORS = frozenset(
    {
        "quota_exhausted",
        "auth_expired",
        "network_blocked",
        "timeout",
        "spawn_failed",
    }
)
"""Engine failures caused by the environment, not by the agent.

A benchmark run that hits one of these says nothing about patch quality: the
task is ``not_run``, the run is ``incomplete`` and the failure is counted
separately from ``fail`` so shadow-mode metrics stay honest.
"""


class PatchReport(BaseModel):
    """What an edit engine produced, including structured failures.

    Failures are reported as values (``engine_error``) instead of exceptions so
    that the graph can escalate deterministically and write evidence.
    """

    files_changed: list[str] = Field(default_factory=list)
    summary: str = ""
    tests_added: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    engine: Literal["fake", "codex"] = "fake"
    cli_version: str | None = None
    engine_error: str | None = None
    error_detail: str | None = None
    reset_at: str | None = None
    reset_at_utc: str | None = None
    exit_code: int | None = None
    duration_ms: int | None = None

    @property
    def infrastructure_failure(self) -> bool:
        return self.engine_error in INFRASTRUCTURE_ERRORS