"""Host-side isolation regression checks; no model or Docker required."""

import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("deerflow_platform", Path(__file__).resolve().parents[1] / "tools/deerflow_platform.py")
platform = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(platform)


class PlatformTests(unittest.TestCase):
    def test_codex_candidate_has_no_api_key_dependency_or_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "runtime"
            upstream = root / "upstream"
            home.mkdir()
            (upstream / "docker/nginx").mkdir(parents=True)
            (upstream / "docker/nginx/nginx.conf").write_text("gateway:8001 frontend:3000")
            (root / "infra/deerflow").mkdir(parents=True)
            images = {"commit": "test", "backend": "backend@sha256:test", "frontend": "frontend@sha256:test", "nginx": "nginx@sha256:test"}
            (root / "infra/deerflow/images.json").write_text(json.dumps(images))
            (home / "image.json").write_text(json.dumps({"base": images["backend"], "image": "sha256:candidate"}))
            compose = home / "compose.json"
            with patch.multiple(platform, ROOT=root, HOME=home, UPSTREAM=upstream, COMPOSE=compose), patch.object(platform.subprocess, "check_output", return_value="test"), patch.object(platform, "snapshot", return_value="baseline"), patch.object(platform, "dotenv_values", side_effect=AssertionError("Must not read API credentials")):
                platform.prepare(codex=True, model="fixture-model")
            cfg = json.loads(compose.read_text())
            self.assertEqual(cfg["name"], "deerflow-codex")
            self.assertEqual(set(cfg["volumes"]), {"fortune-codex-auth", "orbita-codex-auth"})
            for project in ("fortune", "orbita"):
                env = (home / project / ".env").read_text()
                self.assertNotIn("OPENAI_API_KEY", env)
                gateway = cfg["services"][f"{project}-gateway"]
                self.assertEqual(gateway["image"], "sha256:candidate")
                self.assertEqual(gateway["environment"]["CODEX_HOME"], "/run/codex")
                self.assertIn(f"{project}-codex-auth:/run/codex", gateway["volumes"])
                config = json.loads((home / project / "config.json").read_text())
                self.assertEqual(len(config["models"]), 1)
                self.assertEqual(config["models"][0]["model"], "fixture-model")
                self.assertEqual(config["llm_call"]["retry_max_attempts"], 1)
                self.assertFalse(config["sandbox"]["allow_host_bash"])

    def test_snapshot_excludes_credentials_and_preserves_agent_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            for name, content in {"code.py": "original", ".env": "secret", "private.key": "secret", ".env.example": "KEY="}.items():
                (repo / name).write_text(content)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "fixture"], check=True)
            (repo / "untracked.txt").write_text("private")
            target = root / "workspace"
            revision = platform.snapshot(repo, target)
            self.assertFalse((target / ".env").exists())
            self.assertFalse((target / "private.key").exists())
            self.assertFalse((target / "untracked.txt").exists())
            self.assertTrue((target / ".env.example").exists())
            (target / "code.py").write_text("agent edit")
            self.assertEqual(platform.snapshot(repo, target), revision)
            self.assertEqual((target / "code.py").read_text(), "agent edit")
            self.assertEqual((repo / "code.py").read_text(), "original")

    def test_profiles_have_disjoint_state_and_no_host_socket_or_repo_mount(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "runtime"
            upstream = root / "upstream"
            (upstream / "docker/nginx").mkdir(parents=True)
            (upstream / "docker/nginx/nginx.conf").write_text("gateway:8001 frontend:3000")
            (root / "infra/deerflow").mkdir(parents=True)
            (root / "infra/deerflow/images.json").write_text(json.dumps({"commit": "test", "backend": "backend@sha256:test", "frontend": "frontend@sha256:test", "nginx": "nginx@sha256:test"}))
            compose = home / "compose.json"
            with patch.multiple(platform, ROOT=root, HOME=home, UPSTREAM=upstream, COMPOSE=compose), patch.object(platform.subprocess, "check_output", return_value="test"), patch.object(platform, "snapshot", return_value="baseline"), patch.object(platform, "dotenv_values", return_value={"OPENAI_API_KEY": "synthetic"}):
                platform.prepare()
                credentials = (home / "fortune/credentials.json").read_text()
                platform.prepare()
                self.assertEqual((home / "fortune/credentials.json").read_text(), credentials)
            cfg = json.loads(compose.read_text())
            for project in ("fortune", "orbita"):
                other = "orbita" if project == "fortune" else "fortune"
                gateway = cfg["services"][f"{project}-gateway"]
                self.assertEqual(gateway["networks"], [project])
                self.assertNotIn("ports", gateway)
                self.assertTrue(all(other not in mount for mount in gateway["volumes"]))
                self.assertTrue(all("docker.sock" not in mount for mount in gateway["volumes"]))
                config = json.loads((home / project / "config.json").read_text())
                self.assertFalse(config["sandbox"]["allow_host_bash"])
                self.assertNotIn("bash", [tool["name"] for tool in config["tools"]])
                self.assertEqual(config["run_events"]["backend"], "db")
                self.assertFalse(config["auth"]["local"]["allow_registration"])


if __name__ == "__main__":
    unittest.main()
