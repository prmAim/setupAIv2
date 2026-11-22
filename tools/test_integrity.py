"""AST-based test integrity gate for Python and Java test files.

The gate compares a baseline git revision with the working tree and reports
changes that weaken the test suite. Formatting, comments and whitespace are
ignored because both sides are reduced to a canonical syntax tree.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import subprocess
import tokenize
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Iterable, Iterator


TEST_METHOD_ANNOTATIONS = frozenset(
    {"Test", "ParameterizedTest", "RepeatedTest", "TestFactory"}
)
SKIP_ANNOTATIONS = frozenset({"Disabled", "Ignore"})
JAVA_COMMENTS = frozenset({"line_comment", "block_comment"})
JAVA_TYPE_NODES = frozenset(
    {
        "class_declaration",
        "interface_declaration",
        "enum_declaration",
        "record_declaration",
        "annotation_type_declaration",
    }
)
PY_SKIP_NAMES = frozenset(
    {"skip", "skipif", "skipunless", "xfail", "expectedfailure", "skiptest"}
)
SKIPPED_DIRS = frozenset(
    {".git", ".venv", "__pycache__", "target", "build", "node_modules", ".gradle"}
)


@dataclass(frozen=True)
class Finding:
    """A single denied change; every finding makes the gate fail."""

    file: str
    rule: str
    evidence: str


@dataclass(frozen=True)
class GateResult:
    """Outcome of a comparison: 0 allows, 1 denies, 2 cannot evaluate."""

    exit_code: int
    findings: list[Finding]
    summary: str


@dataclass(frozen=True)
class _Symbol:
    digest: str
    skips: frozenset[str] = frozenset()


class _ParseError(Exception):
    pass


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _unique(base: str, counters: dict[str, int]) -> str:
    index = counters.get(base, 0)
    counters[base] = index + 1
    return base if index == 0 else f"{base}~{index}"


def _normalize_space(text: str) -> str:
    return " ".join(text.split())


@lru_cache(maxsize=1)
def _java_parser():
    from tree_sitter import Language, Parser
    import tree_sitter_java

    return Parser(Language(tree_sitter_java.language()))


def _canonical_java(node, source: bytes) -> str:
    parts: list[str] = []

    def walk(current) -> None:
        if current.type in JAVA_COMMENTS:
            return
        parts.append(current.type)
        if current.child_count == 0:
            parts.append("=")
            parts.append(current.text.decode("utf-8", "replace"))
            return
        parts.append("(")
        for child in current.children:
            walk(child)
        parts.append(")")

    walk(node)
    return "".join(parts)


def _java_text(node) -> str:
    return node.text.decode("utf-8", "replace")


def _java_annotations(node, source: bytes) -> set[str]:
    names: set[str] = set()
    modifiers = next((child for child in node.children if child.type == "modifiers"), None)
    if modifiers is None:
        return names
    for child in modifiers.children:
        if child.type not in {"annotation", "marker_annotation"}:
            continue
        name = child.child_by_field_name("name")
        if name is not None:
            names.add(_java_text(name).split(".")[-1])
    return names


def _first_java_error(node) -> int:
    stack = [node]
    while stack:
        current = stack.pop()
        if current.is_error or current.is_missing:
            return current.start_byte
        stack.extend(current.children)
    return node.start_byte


def _java_members(body) -> Iterator:
    for child in body.named_children:
        if child.type == "enum_body_declarations":
            yield from child.named_children
        else:
            yield child


@dataclass
class _JavaModel:
    imports: dict[str, str]
    tests: dict[str, _Symbol]
    helpers: dict[str, str]
    classes: dict[str, _Symbol]


def _empty_java_model() -> _JavaModel:
    return _JavaModel({}, {}, {}, {})


def _java_model(source: bytes) -> _JavaModel:
    root = _java_parser().parse(source).root_node
    if root.has_error:
        raise _ParseError(f"java syntax error near byte {_first_java_error(root)}")
    model = _empty_java_model()

    def visit_type(decl, prefix: str) -> None:
        name_node = decl.child_by_field_name("name")
        if name_node is None:
            return
        simple = _java_text(name_node)
        qname = f"{prefix}.{simple}" if prefix else simple
        body = decl.child_by_field_name("body")
        body_span = (body.start_byte, body.end_byte) if body is not None else None
        signature = "".join(
            _canonical_java(child, source)
            for child in decl.children
            if body_span is None or (child.start_byte, child.end_byte) != body_span
        )
        model.classes[qname] = _Symbol(
            _digest(signature),
            frozenset(_java_annotations(decl, source) & SKIP_ANNOTATIONS),
        )
        if body is None:
            return
        counters: dict[str, int] = {}

        def visit_member(member, owner: str) -> None:
            kind = member.type
            if kind == "method_declaration":
                name = member.child_by_field_name("name")
                method = _java_text(name) if name is not None else "?"
                annotations = _java_annotations(member, source)
                key = _unique(f"{owner}#{method}", counters)
                digest = _digest(_canonical_java(member, source))
                if annotations & TEST_METHOD_ANNOTATIONS:
                    model.tests[key] = _Symbol(
                        digest, frozenset(annotations & SKIP_ANNOTATIONS)
                    )
                else:
                    model.helpers[key] = digest
            elif kind in JAVA_TYPE_NODES:
                visit_type(member, owner)
            elif kind in {"field_declaration", "constant_declaration"}:
                digest = _digest(_canonical_java(member, source))
                names = [
                    _java_text(declarator.child_by_field_name("name"))
                    for declarator in member.named_children
                    if declarator.type == "variable_declarator"
                    and declarator.child_by_field_name("name") is not None
                ]
                for field_name in names or ["?"]:
                    model.helpers[_unique(f"{owner}$field${field_name}", counters)] = digest
            else:
                digest = _digest(_canonical_java(member, source))
                model.helpers[_unique(f"{owner}${kind}", counters)] = digest

        for member in _java_members(body):
            visit_member(member, qname)

    for child in root.named_children:
        if child.type == "import_declaration":
            raw = _normalize_space(_java_text(child))
            model.imports[_digest(_canonical_java(child, source))] = raw
        elif child.type in JAVA_TYPE_NODES:
            visit_type(child, "")
    return model


def _py_source(source: bytes) -> str:
    encoding, _ = tokenize.detect_encoding(io.BytesIO(source).readline)
    return source.decode(encoding)


def _py_digest(node: ast.AST) -> str:
    return _digest(ast.dump(node, annotate_fields=False, include_attributes=False))


def _dotted_name(node: ast.AST | None) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted_name(node.value)}.{node.attr}"
    if isinstance(node, ast.Call):
        return _dotted_name(node.func)
    return ""


def _is_skip_name(name: str) -> bool:
    if not name:
        return False
    return name.split(".")[-1].lower() in PY_SKIP_NAMES


def _decorator_skips(decorators: Iterable[ast.expr]) -> frozenset[str]:
    return frozenset(
        name for name in (_dotted_name(item) for item in decorators) if _is_skip_name(name)
    )


def _call_skips(body: Iterable[ast.stmt]) -> frozenset[str]:
    names: set[str] = set()
    for statement in body:
        for node in ast.walk(statement):
            if isinstance(node, ast.Call):
                name = _dotted_name(node.func)
                if _is_skip_name(name):
                    names.add(name)
    return frozenset(names)


def _test_skips(node: ast.FunctionDef | ast.AsyncFunctionDef) -> frozenset[str]:
    return _decorator_skips(node.decorator_list) | _call_skips(node.body)


def _py_class_signature(node: ast.ClassDef) -> str:
    parts = [node.name]
    parts.extend(
        "decor:" + ast.dump(item, annotate_fields=False) for item in node.decorator_list
    )
    parts.extend("base:" + ast.dump(item, annotate_fields=False) for item in node.bases)
    parts.extend(
        "kw:" + (item.arg or "**") + "=" + ast.dump(item.value, annotate_fields=False)
        for item in node.keywords
    )
    parts.extend(
        "tp:" + ast.dump(item, annotate_fields=False)
        for item in getattr(node, "type_params", [])
    )
    return "|".join(parts)


@dataclass
class _PyModel:
    imports: dict[str, str]
    tests: dict[str, _Symbol]
    helpers: dict[str, str]
    classes: dict[str, _Symbol]
    data: dict[str, str]
    data_skips: dict[str, frozenset[str]]


def _empty_py_model() -> _PyModel:
    return _PyModel({}, {}, {}, {}, {}, {})


def _py_model(source: bytes) -> _PyModel:
    try:
        tree = ast.parse(_py_source(source))
    except (SyntaxError, UnicodeDecodeError) as exc:
        raise _ParseError(f"python syntax error: {exc}") from exc
    model = _empty_py_model()
    module_counters: dict[str, int] = {}

    def add_function(node, owner: str | None, counters: dict[str, int]) -> None:
        base = f"{owner}.{node.name}" if owner else node.name
        key = _unique(base, counters)
        digest = _py_digest(node)
        if node.name.startswith("test"):
            model.tests[key] = _Symbol(digest, _test_skips(node))
        else:
            model.helpers[key] = digest

    def add_class(node: ast.ClassDef, prefix: str) -> None:
        qname = f"{prefix}.{node.name}" if prefix else node.name
        qname = _unique(qname, module_counters)
        model.classes[qname] = _Symbol(
            _digest(_py_class_signature(node)),
            _decorator_skips(node.decorator_list),
        )
        class_counters: dict[str, int] = {}
        for statement in node.body:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                add_function(statement, qname, class_counters)
            elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
                names = _py_targets(statement)
                digest = _py_digest(statement)
                for name in names or ["?"]:
                    model.helpers[_unique(f"{qname}$data${name}", class_counters)] = digest
            elif isinstance(statement, ast.ClassDef):
                add_class(statement, qname)
            elif isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant):
                continue
            else:
                model.helpers[
                    _unique(f"{qname}${type(statement).__name__}", class_counters)
                ] = _py_digest(statement)

    for statement in tree.body:
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            readable = ast.unparse(statement)
            model.imports[readable] = readable
        elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            add_function(statement, None, module_counters)
        elif isinstance(statement, ast.ClassDef):
            add_class(statement, "")
        elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
            names = _py_targets(statement)
            digest = _py_digest(statement)
            for name in names or ["?"]:
                model.data[name] = digest
            value = statement.value
            if "pytestmark" in names and value is not None:
                model.data_skips["pytestmark"] = _decorator_skips([value])
        elif isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant):
            continue
    return model


def _py_targets(statement: ast.Assign | ast.AnnAssign) -> list[str]:
    targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
    return [item.id for item in targets if isinstance(item, ast.Name)]


def _compare_symbols(
    rel: str,
    label: str,
    before: dict[str, _Symbol],
    after: dict[str, _Symbol],
    findings: list[Finding],
    removed_rule: str,
    modified_rule: str,
) -> None:
    for key in sorted(before):
        if key not in after and removed_rule:
            findings.append(Finding(rel, removed_rule, key))
    for key in sorted(before.keys() & after.keys()):
        added = after[key].skips - before[key].skips
        if added:
            markers = " ".join(f"@{item}" for item in sorted(added))
            findings.append(Finding(rel, "skip-marker-added", f"{label} {key}: {markers}"))
        elif after[key].digest != before[key].digest:
            findings.append(Finding(rel, modified_rule, f"{label} {key}"))


def _compare_java(rel: str, before: bytes | None, after: bytes | None) -> list[Finding]:
    findings: list[Finding] = []
    try:
        old = _java_model(before) if before is not None else _empty_java_model()
        new = _java_model(after) if after is not None else _empty_java_model()
    except _ParseError as exc:
        return [Finding(rel, "parse-error", str(exc))]
    for key in sorted(old.imports.keys() - new.imports.keys()):
        findings.append(Finding(rel, "import-removed", old.imports[key]))
    _compare_symbols(
        rel, "test", old.tests, new.tests, findings, "test-removed", "test-body-modified"
    )
    _compare_symbols(
        rel, "class", old.classes, new.classes, findings, "", "helper-modified"
    )
    for key in sorted(old.helpers):
        if key not in new.helpers:
            findings.append(Finding(rel, "helper-removed", key))
    for key in sorted(old.helpers.keys() & new.helpers.keys()):
        if old.helpers[key] != new.helpers[key]:
            findings.append(Finding(rel, "helper-modified", key))
    return findings


def _compare_python(rel: str, before: bytes | None, after: bytes | None) -> list[Finding]:
    findings: list[Finding] = []
    try:
        old = _py_model(before) if before is not None else _empty_py_model()
        new = _py_model(after) if after is not None else _empty_py_model()
    except _ParseError as exc:
        return [Finding(rel, "parse-error", str(exc))]
    for key in sorted(old.imports.keys() - new.imports.keys()):
        findings.append(Finding(rel, "import-removed", key))
    _compare_symbols(
        rel, "test", old.tests, new.tests, findings, "test-removed", "test-body-modified"
    )
    _compare_symbols(
        rel, "class", old.classes, new.classes, findings, "", "helper-modified"
    )
    for key in sorted(old.helpers):
        if key not in new.helpers:
            findings.append(Finding(rel, "helper-removed", key))
    for key in sorted(old.helpers.keys() & new.helpers.keys()):
        if old.helpers[key] != new.helpers[key]:
            findings.append(Finding(rel, "helper-modified", key))
    added_skips = new.data_skips.get("pytestmark", frozenset()) - old.data_skips.get(
        "pytestmark", frozenset()
    )
    if added_skips:
        markers = " ".join(f"@{item}" for item in sorted(added_skips))
        findings.append(Finding(rel, "skip-marker-added", f"pytestmark: {markers}"))
    for key in sorted(old.data):
        if key == "pytestmark":
            if key in new.data and old.data[key] != new.data[key]:
                findings.append(Finding(rel, "module-data-modified", key))
        elif key not in new.data:
            findings.append(Finding(rel, "module-data-modified", f"removed {key}"))
        elif old.data[key] != new.data[key]:
            findings.append(Finding(rel, "module-data-modified", key))
    return findings


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, check=False
    )


def _baseline_exists(repo: Path, sha: str) -> bool:
    result = _run_git(repo, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}")
    return result.returncode == 0


def _git_show(repo: Path, sha: str, rel: str) -> bytes | None:
    result = _run_git(repo, "show", f"{sha}:{rel}")
    return result.stdout if result.returncode == 0 else None


def _read_working(repo: Path, rel: str) -> bytes | None:
    path = repo / PurePosixPath(rel)
    try:
        return path.read_bytes()
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        return None


def _is_test_path(rel: str) -> bool:
    path = PurePosixPath(rel)
    if path.suffix == ".py":
        return path.name.startswith("test_")
    if path.suffix == ".java":
        return path.name.endswith(("Test.java", "IT.java")) or "/src/test/java/" in f"/{rel}"
    return False


def _discover(repo: Path, sha: str) -> list[str]:
    paths: set[str] = set()
    result = _run_git(repo, "ls-tree", "-r", "-z", "--name-only", sha)
    if result.returncode == 0:
        for raw in result.stdout.split(b"\0"):
            if raw:
                paths.add(raw.decode("utf-8", "replace"))
    for path in repo.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(repo)
        if any(part in SKIPPED_DIRS for part in rel.parts):
            continue
        paths.add(rel.as_posix())
    return sorted(rel for rel in paths if _is_test_path(rel))


def _normalize_path(raw: str) -> str:
    path = PurePosixPath(raw.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or Path(raw).is_absolute():
        raise ValueError(f"path escapes repository: {raw!r}")
    return path.as_posix()


def compare(
    repo: Path, baseline_sha: str, paths: list[str] | None = None
) -> GateResult:
    """Compare baseline ``baseline_sha`` with the working tree of ``repo``.

    Returns exit code 0 when every change is additive or cosmetic, 1 when at
    least one change weakens the suite, and 2 when the comparison cannot run.
    """
    repo = Path(repo)
    if not repo.is_dir():
        return GateResult(2, [Finding("", "invalid-repo", str(repo))], "repository not found")
    if not _baseline_exists(repo, baseline_sha):
        return GateResult(
            2,
            [Finding("", "invalid-baseline", baseline_sha)],
            "baseline revision is not a commit",
        )
    if paths is None:
        selected = _discover(repo, baseline_sha)
    else:
        selected = []
        for raw in paths:
            try:
                selected.append(_normalize_path(raw))
            except ValueError as exc:
                return GateResult(2, [Finding(raw, "invalid-path", str(exc))], str(exc))
    if not selected:
        return GateResult(0, [], "no test files to compare")
    findings: list[Finding] = []
    for rel in selected:
        before = _git_show(repo, baseline_sha, rel)
        after = _read_working(repo, rel)
        if rel.endswith(".py"):
            findings.extend(_compare_python(rel, before, after))
        elif rel.endswith(".java"):
            findings.extend(_compare_java(rel, before, after))
        else:
            findings.append(Finding(rel, "unsupported-file", rel))
    findings.sort(key=lambda item: (item.file, item.rule, item.evidence))
    counts = Counter(item.rule for item in findings)
    if findings:
        detail = ", ".join(f"{rule}={count}" for rule, count in sorted(counts.items()))
        summary = f"checked {len(selected)} file(s); denied {len(findings)} ({detail})"
        return GateResult(1, findings, summary)
    return GateResult(
        0, [], f"checked {len(selected)} file(s); no weakening changes"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("paths", nargs="*")
    arguments = parser.parse_args(argv)
    result = compare(arguments.repo, arguments.baseline, arguments.paths or None)
    print(
        json.dumps(
            {
                "exit_code": result.exit_code,
                "summary": result.summary,
                "findings": [item.__dict__ for item in result.findings],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
