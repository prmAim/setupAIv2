"""Trusted Git projection and data-only replacement protocol for synthetic evaluation."""

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


FILE_LIMIT = 16384
TOTAL_LIMIT = 65536
OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def wire(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("ascii")


def text_bytes(text, limit):
    require(type(text) is str and len(text) <= limit and "\0" not in text, "invalid text")
    data = text.encode("utf-8")
    require(len(data) <= limit, "text exceeds byte budget")
    return data


def git(repo, *args, data=None):
    # In particular, an inherited GIT_INDEX_FILE or GIT_DIR must not redirect fixture writes.
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_NO_LAZY_FETCH="1",
               GIT_ALLOW_PROTOCOL="", GIT_TERMINAL_PROMPT="0", GIT_ATTR_NOSYSTEM="1")
    return subprocess.run(["git", "--no-replace-objects", "-C", str(repo), *args], env=env,
                          input=data, capture_output=True, check=True, timeout=10).stdout


def object_bytes(repo, oid, kind, limit):
    require(OID.fullmatch(oid), "expected full object id")
    require(git(repo, "cat-file", "-t", oid).strip() == kind.encode(), "wrong object type")
    size = int(git(repo, "cat-file", "-s", oid))
    require(0 <= size <= limit, "object exceeds budget")
    data = git(repo, "cat-file", kind, oid)
    require(len(data) == size, "object size mismatch")
    return data


def directory(repo, oid):
    # Inspect immediate sibling names (not contents) before any path resolution.
    raw = object_bytes(repo, oid, "tree", TOTAL_LIMIT)
    entries = {}
    pos = 0
    while pos < len(raw):
        end = raw.index(b"\0", pos)
        mode, name = raw[pos:end].decode("ascii").split(" ", 1)
        require(portable(name) and name.lower() not in entries, "ambiguous tree siblings")
        pos = end + 1 + len(oid) // 2
        require(pos <= len(raw), "short tree entry")
        entries[name.lower()] = (name, mode, raw[end + 1:pos].hex())
    return entries


def portable(name):
    return (re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name) and not name.endswith(".")
            and name.split(".")[0].upper() not in {"CON", "PRN", "AUX", "NUL",
                *(f"{p}{i}" for p in ("COM", "LPT") for i in range(10))})


def source_path(path):
    require(type(path) is str and re.fullmatch(
        r"src/(?:[a-z][a-z0-9_]{0,31}/){0,3}[a-z][a-z0-9_]{0,31}\.(py|js|java)", path
    ) and all(portable(p) for p in path.split("/")), "expected canonical source path")


@dataclass(frozen=True)
class Bundle:
    issue: str
    files: tuple[tuple[str, bytes], ...]

    def request(self):
        return wire({"issue": self.issue, "files": [
            {"path": p, "sha256": digest(b), "text": b.decode()} for p, b in self.files]})


def build(repo, tree, approved, issue):
    """approved is an exact source inventory reviewed by the operator, never issue data."""
    text_bytes(issue, 4096)
    require(0 < len(approved) <= 8 and len(set(approved)) == len(approved), "bad inventory")
    files = []
    cache = {}
    total = 0
    for path in sorted(approved):
        source_path(path)
        oid = tree
        parts = path.split("/")
        for i, part in enumerate(parts):
            if oid not in cache:
                cache[oid] = directory(repo, oid)
            entry = cache[oid].get(part)
            require(entry and entry[0] == part, "missing exact tracked path")
            _, mode, oid = entry
            require(mode == ("100644" if i == len(parts)-1 else "40000"), "unexpected tree mode")
        data = object_bytes(repo, oid, "blob", FILE_LIMIT)
        require(digest(data) == approved[path], "blob not reviewed for disclosure")
        text_bytes(data.decode(), FILE_LIMIT)
        total += len(data)
        require(total <= TOTAL_LIMIT, "snapshot exceeds budget")
        files.append((path, data))
    return Bundle(issue, tuple(files))


def unique(pairs):
    obj = dict(pairs)
    require(len(obj) == len(pairs), "duplicate JSON field")
    return obj


def replacements(bundle, response):
    require(type(response) is bytes and len(response) <= 2 * TOTAL_LIMIT, "invalid response size")
    obj = json.loads(response.decode(), object_pairs_hook=unique)
    require(type(obj) is dict and obj.keys() == {"request", "edits"}
            and obj["request"] == digest(bundle.request()), "wrong envelope or request")
    edits = obj["edits"]
    require(type(edits) is list and 0 < len(edits) <= len(bundle.files), "bad edit count")
    result = dict(bundle.files)
    seen = set()
    for edit in edits:
        require(type(edit) is dict and edit.keys() == {"path", "sha256", "text"}, "bad edit fields")
        path = edit["path"]
        require(type(path) is str and path in result and path not in seen, "unapproved or repeated path")
        require(edit["sha256"] == digest(result[path]), "stale baseline")
        result[path] = text_bytes(edit["text"], FILE_LIMIT)
        seen.add(path)
    require(sum(map(len, result.values())) <= TOTAL_LIMIT, "result exceeds budget")
    return result


@contextmanager
def staged(bundle, response):
    """No caller-owned destination. Publish only a complete private snapshot."""
    files = replacements(bundle, response)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        created = {root}
        for path, data in files.items():
            source_path(path)
            parent = root
            for component in path.split("/")[:-1]:
                parent = parent / component
                if parent not in created:
                    parent.mkdir()  # Refuse even a pre-existing ordinary directory.
                    created.add(parent)
                info = parent.lstat()
                require(stat.S_ISDIR(info.st_mode) and not getattr(info, "st_file_attributes", 0)
                        & stat.FILE_ATTRIBUTE_REPARSE_POINT, "unexpected staging parent")
            target = root / path
            with target.open("xb") as f:
                f.write(data)
        yield root
