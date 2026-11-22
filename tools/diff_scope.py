"""Repo-specific diff-scope rules kept as data, with an orbita default profile.

Matching is case-insensitive and treats ``\\`` as ``/`` because agents edit
Windows checkouts while git and CI speak POSIX paths.
"""

from __future__ import annotations

import fnmatch
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

RULES_PATH = Path(__file__).resolve().parent / "diff_scope_rules.json"
DEFAULT_PROFILE = "orbita"

ORBITA_FORBIDDEN = [
    ".github/",
    "backend/src/main/resources/db/migration/",
    "uv.lock",
    "frontend/package-lock.json",
    "backend/pom.xml",
    ".env*",
    "deploy/.env",
]
ORBITA_ALLOW = [
    "backend/src/main/java/**",
    "backend/src/test/java/**",
    "frontend/src/**",
    "tools/**",
    "docs/**",
    "tests/**",
]


class DiffScopeRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    forbidden: list[str] = Field(default_factory=lambda: list(ORBITA_FORBIDDEN))
    allow: list[str] = Field(default_factory=lambda: list(ORBITA_ALLOW))
    enforce_allow: bool = True
    description: str = ""


class RuleHit(BaseModel):
    path: str
    rule: Literal["forbidden", "out-of-scope"]
    pattern: str
    reason: str


def normalise(path: str | Path) -> str:
    value = str(path).replace("\\", "/").strip()
    while value.startswith("./"):
        value = value[2:]
    while "//" in value:
        value = value.replace("//", "/")
    return value.lower().rstrip("/")


def _normalise_pattern(pattern: str) -> str:
    value = pattern.replace("\\", "/").strip()
    while value.startswith("./"):
        value = value[2:]
    return value.lower()


def match_pattern(path: str | Path, pattern: str) -> bool:
    candidate = normalise(path)
    raw = _normalise_pattern(pattern)
    if not raw:
        return False
    if raw.endswith("/"):
        prefix = raw.rstrip("/")
        return candidate == prefix or candidate.startswith(prefix + "/")
    if "/" not in raw:
        name = candidate.rsplit("/", 1)[-1]
        if any(char in raw for char in "*?["):
            return fnmatch.fnmatchcase(name, raw)
        return name == raw or candidate == raw
    if any(char in raw for char in "*?["):
        return fnmatch.fnmatchcase(candidate, raw)
    return candidate == raw


def evaluate(rules: DiffScopeRules, paths: list[str]) -> list[RuleHit]:
    hits: list[RuleHit] = []
    for path in sorted({str(item).replace("\\", "/") for item in paths}):
        forbidden = next(
            (pattern for pattern in rules.forbidden if match_pattern(path, pattern)), None
        )
        if forbidden is not None:
            hits.append(
                RuleHit(
                    path=path,
                    rule="forbidden",
                    pattern=forbidden,
                    reason=f"forbidden path matches {forbidden}",
                )
            )
            continue
        if rules.enforce_allow and rules.allow:
            if not any(match_pattern(path, pattern) for pattern in rules.allow):
                hits.append(
                    RuleHit(
                        path=path,
                        rule="out-of-scope",
                        pattern="",
                        reason="path is outside the allowed diff scope",
                    )
                )
    return hits


def verdict(hits: list[RuleHit]) -> Literal["pass", "deny"]:
    return "deny" if hits else "pass"


def load_profiles(path: Path | None = None) -> dict[str, DiffScopeRules]:
    source = path or RULES_PATH
    payload: dict[str, Any] = {}
    if source.is_file():
        payload = json.loads(source.read_text(encoding="utf-8"))
    raw_profiles = payload.get("profiles") or {}
    profiles = {
        name: DiffScopeRules.model_validate(item) for name, item in raw_profiles.items()
    }
    if DEFAULT_PROFILE not in profiles:
        profiles[DEFAULT_PROFILE] = DiffScopeRules()
    return profiles


def resolve_rules(
    *,
    profile: str = DEFAULT_PROFILE,
    explicit: list[str] | None = None,
    path: Path | None = None,
) -> DiffScopeRules:
    if explicit is not None:
        return DiffScopeRules(forbidden=list(explicit), allow=[], enforce_allow=False)
    profiles = load_profiles(path)
    if profile not in profiles:
        raise KeyError(f"unknown diff-scope profile: {profile}")
    return profiles[profile]