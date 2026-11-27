"""Build a separate pinned Codex image offline; never start a service or read auth."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
ASSET_SHA256 = "a2342a8f7f68f088be30620ccfe9cb84162c93a6b56177855961cda3ed6be392"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-archive", type=Path, required=True)
    args = parser.parse_args()
    if hashlib.sha256(args.codex_archive.read_bytes()).hexdigest() != ASSET_SHA256:
        raise SystemExit("Codex archive checksum mismatch")
    candidate = ROOT / ".scratch/deerflow-codex"
    candidate.mkdir(parents=True, exist_ok=True)
    source = ROOT / "infra/deerflow"
    base = json.loads((source / "images.json").read_text())["backend"]
    dockerfile = source / "Dockerfile.codex"
    if dockerfile.read_text().splitlines()[0] != f"FROM {base}":
        raise SystemExit("Dockerfile base does not match images.json")
    # Never merge with an older context: deleted modules must stay deleted.
    with tempfile.TemporaryDirectory(prefix="build-", dir=candidate) as folder:
        context = Path(folder).resolve()
        if not context.is_relative_to(candidate.resolve()):
            raise RuntimeError("Build context escaped candidate directory")
        for name in ("runtime", "vendor"):
            shutil.copytree(source / name, context / name,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
        shutil.copyfile(dockerfile, context / "Dockerfile")
        shutil.copyfile(args.codex_archive, context / "codex.tar.gz")
        iid = context / "image.id"
        subprocess.run(["docker", "build", "--network=none", "--pull=false", "--iidfile", str(iid),
                        "-t", "setupaiv2-deerflow-codex:local", str(context)], check=True)
        image = iid.read_text().strip()
    (candidate / "image.json").write_text(json.dumps({"base": base, "image": image,
                                                      "codex_version": "0.172.0"}, indent=2) + "\n")
    print("Built candidate image; no services started and no credentials accessed")


if __name__ == "__main__":
    main()
