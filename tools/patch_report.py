"""Engine-neutral patch report returned by fake and Codex adapters."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class PatchReport(BaseModel):
    """What an edit engine produced, including structured failures.

    Failures are reported as values (``engine_error``) instead of exceptions so
    that the graph can escalate deterministically and write evidence.
    """

    files_changed: list[str] = Field(default_factory=list)
    summary: str = ""
    engine: Literal["fake", "codex"] = "fake"
    cli_version: str | None = None
    engine_error: str | None = None
    error_detail: str | None = None
    exit_code: int | None = None
    duration_ms: int | None = None