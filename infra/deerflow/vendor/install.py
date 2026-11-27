"""Offline image build: reject unexpected base/provider/binary content."""

import hashlib
import importlib.util
from pathlib import Path
import shutil
import tarfile


def verify(path, expected):
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise SystemExit("Pinned build input checksum mismatch")


root = Path(__file__).parent
archive = Path("/tmp/codex.tar.gz")
verify(archive, "a2342a8f7f68f088be30620ccfe9cb84162c93a6b56177855961cda3ed6be392")
provider = Path(importlib.util.find_spec("deerflow.models.openai_codex_provider").origin)
verify(provider, "402b498f74e38eb3f869924f798e3069a2ba537ca29f99eb3fd7415e940754f3")
replacement = root / "openai_codex_provider.py"
verify(replacement, "1e843f957d5224ad8c030c608565fedeb9a5216dc4ca92b7fce8c17e0196b627")
shutil.copyfile(replacement, provider)
with tarfile.open(archive) as source:
    members = [m for m in source.getmembers() if m.isfile() and m.name == "codex-x86_64-unknown-linux-musl"]
    if len(members) != 1:
        raise SystemExit("Unexpected Codex archive layout")
    with source.extractfile(members[0]) as src, Path("/usr/local/bin/codex").open("wb") as dst:
        shutil.copyfileobj(src, dst)
Path("/usr/local/bin/codex").chmod(0o755)
Path("/run/codex").mkdir(mode=0o700, exist_ok=True)
Path("/run/codex").chmod(0o700)
archive.unlink()
