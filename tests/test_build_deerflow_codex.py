"""Offline build-input regressions; Docker is never invoked."""

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "build_deerflow_codex", Path(__file__).resolve().parents[1] / "tools/build_deerflow_codex.py")
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class BuildTests(unittest.TestCase):
    def test_rebuild_excludes_deleted_modules_and_bytecode(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "infra/deerflow"
            for name in ("runtime", "vendor"):
                (source / name).mkdir(parents=True)
            (source / "runtime/old.py").write_text("old")
            (source / "runtime/cache.pyc").write_bytes(b"stale")
            (source / "images.json").write_text(json.dumps({"backend": "fixture@sha256:base"}))
            (source / "Dockerfile.codex").write_text("FROM fixture@sha256:base\n")
            archive = root / "codex.tar.gz"
            archive.write_bytes(b"fixture")
            contexts, contents = [], []

            def build(command, **kwargs):
                context = Path(command[-1])
                contexts.append(context)
                contents.append({p.name for p in (context / "runtime").iterdir()})
                Path(command[command.index("--iidfile") + 1]).write_text("sha256:result")

            with patch.object(builder, "ROOT", root), patch.object(builder, "ASSET_SHA256", hashlib.sha256(b"fixture").hexdigest()), patch.object(builder.subprocess, "run", side_effect=build), patch("sys.argv", ["build", "--codex-archive", str(archive)]):
                builder.main()
                (source / "runtime/old.py").unlink()
                (source / "runtime/new.py").write_text("new")
                builder.main()
                self.assertEqual(contents, [{"old.py"}, {"new.py"}])
                self.assertNotEqual(contexts[0], contexts[1])
                self.assertTrue(all(not path.exists() for path in contexts))
                metadata = json.loads((root / ".scratch/deerflow-codex/image.json").read_text())
                self.assertEqual(metadata["base"], "fixture@sha256:base")
                (source / "images.json").write_text(json.dumps({"backend": "different"}))
                with self.assertRaisesRegex(SystemExit, "base does not match"):
                    builder.main()
                self.assertEqual(len(contexts), 2)


if __name__ == "__main__":
    unittest.main()
