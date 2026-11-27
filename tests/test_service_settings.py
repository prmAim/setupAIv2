"""Exercise provider isolation and configuration failures without network calls."""

import os
from pathlib import Path
import sys
import subprocess
import textwrap
import unittest
from unittest.mock import patch

from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from service_settings import Settings


class SettingsTests(unittest.TestCase):
    def test_settings_fixture_preserves_inherited_empty_git_config(self):
        # Use a fresh process so the regression is independent of the host's
        # Git configuration and cannot corrupt the rest of the test suite.
        env = dict(os.environ)
        env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.hooksPath", GIT_CONFIG_VALUE_0="")
        script = textwrap.dedent("""
            import subprocess
            import tempfile
            from test_service_settings import SettingsTests

            with tempfile.TemporaryDirectory() as folder:
                def init():
                    result = subprocess.run(
                        ["git", "init", "-q"], cwd=folder,
                        capture_output=True, text=True, timeout=10,
                    )
                    assert result.returncode == 0, result.stderr

                init()
                SettingsTests().settings()
                # No explicit env: exercise the native inherited environment.
                init()
        """)
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=Path(__file__).resolve().parent,
            env=env, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def settings(self, **changes):
        # Replace the Python view only. Clearing the native environment and
        # restoring empty values can unset them on Windows (e.g. Git config).
        with patch.object(os, "environ", {}):
            return Settings(
                _env_file=None, langfuse_public_key="public-test",
                langfuse_secret_key="secret-test", **changes,
            )

    def test_settings_fixture_ignores_ambient_credentials(self):
        ambient = {"OPENAI_API_KEY": "ambient-test", "LLM_PROVIDER": "deepseek"}
        with patch.object(os, "environ", ambient):
            config = self.settings()
            self.assertEqual(config.llm_provider, "openai")
            self.assertEqual(config.openai_api_key.get_secret_value(), "")
            self.assertIs(os.environ, ambient)

    def test_missing_key_fails_before_call(self):
        with self.assertRaisesRegex(ValueError, "OPENAI_API_KEY"):
            self.settings().model_credentials()

    def test_deepseek_does_not_reuse_openai_key(self):
        with self.assertRaisesRegex(ValueError, "DEEPSEEK_API_KEY"):
            self.settings(llm_provider="deepseek", llm_model="deepseek-flash",
                          openai_api_key="openai-secret").model_credentials()

    def test_provider_endpoint_and_key_stay_together(self):
        config = self.settings(llm_provider="deepseek", llm_model="deepseek-flash",
                               openai_api_key="openai-secret", deepseek_api_key="deepseek-secret")
        endpoint, key = config.model_credentials()
        self.assertEqual(endpoint, "https://api.deepseek.com")
        self.assertEqual(key.get_secret_value(), "deepseek-secret")
        self.assertNotIn("deepseek-secret", repr(config))

    def test_wrong_provider_model_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "LLM_MODEL"):
            self.settings(llm_provider="deepseek", deepseek_api_key="secret").model_credentials()

    def test_cloud_trace_endpoint_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.settings(langfuse_base_url="https://cloud.langfuse.com")


if __name__ == "__main__":
    unittest.main()
