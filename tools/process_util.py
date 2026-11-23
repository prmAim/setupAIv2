"""Process helpers shared by the publish and engine adapters.

Everything here is local-only: no network calls, no shell interpolation of
untrusted values, and stderr is always redacted and bounded before it reaches
an evidence bundle.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

STDERR_LIMIT = 2000
SECRET_ENV_MARKERS = ("SECRET", "TOKEN", "PASSWORD", "API_KEY", "PRIVATE_KEY")
SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{8,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{8,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]{8,}){0,2}"),
    re.compile(r"(?i)(authorization:\s*(bearer|token)\s+)\S+"),
)


def redact(text: str | None, *, limit: int = STDERR_LIMIT, env: dict[str, str] | None = None) -> str:
    """Trim to ``limit`` characters and strip obvious credentials."""
    value = text or ""
    source = env if env is not None else dict(os.environ)
    for key, secret in source.items():
        if not secret or len(secret) < 6:
            continue
        if any(marker in key.upper() for marker in SECRET_ENV_MARKERS):
            value = value.replace(secret, "[redacted]")
    for pattern in SECRET_PATTERNS:
        value = pattern.sub("[redacted]", value)
    return value[:limit]


def command_for(binary: str, *args: str) -> list[str]:
    """Return an argv that Windows can execute, including ``.cmd`` stubs."""
    suffix = Path(binary).suffix.lower()
    if os.name == "nt" and suffix in {".cmd", ".bat"}:
        return ["cmd", "/c", binary, *args]
    return [binary, *args]


def resolve_binary(env_name: str, default: str, env: dict[str, str] | None = None) -> str:
    source = env if env is not None else os.environ
    value = (source.get(env_name) or "").strip()
    return value or default


def process_env(extra: dict[str, Any] | None = None) -> dict[str, str]:
    merged = dict(os.environ)
    for key, value in (extra or {}).items():
        if value is not None:
            merged[str(key)] = str(value)
    return merged