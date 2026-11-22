"""Diff-scope rules as data: orbita profile, invented paths, Windows casing."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from diff_scope import (  # noqa: E402
    ORBITA_ALLOW,
    ORBITA_FORBIDDEN,
    DiffScopeRules,
    evaluate,
    load_profiles,
    match_pattern,
    resolve_rules,
    verdict,
)

ORBITA_ROOT = Path(r"C:\Git\orbita")


def hit_rules(paths: list[str], rules: DiffScopeRules | None = None) -> list[str]:
    return [hit.rule for hit in evaluate(rules or resolve_rules(), paths)]


class OrbitaProfileTests(unittest.TestCase):
    def test_profile_matches_the_ticket(self) -> None:
        rules = resolve_rules()
        self.assertEqual(rules.forbidden, ORBITA_FORBIDDEN)
        self.assertEqual(rules.allow, ORBITA_ALLOW)
        self.assertTrue(rules.enforce_allow)
        self.assertEqual(load_profiles()["orbita"].forbidden, ORBITA_FORBIDDEN)

    def test_declared_forbidden_paths_are_denied(self) -> None:
        paths = [
            ".github/workflows/ci.yml",
            ".github/workflows/release.yaml",
            "backend/src/main/resources/db/migration/V99__next.sql",
            "uv.lock",
            "frontend/package-lock.json",
            "backend/pom.xml",
            ".env",
            ".env.local",
            "deploy/.env",
        ]
        for path in paths:
            with self.subTest(path=path):
                hits = evaluate(resolve_rules(), [path])
                self.assertEqual(verdict(hits), "deny")
                self.assertEqual(hits[0].rule, "forbidden")

    def test_declared_allow_paths_pass(self) -> None:
        paths = [
            "backend/src/main/java/ru/orbita/account/AccountReadService.java",
            "backend/src/test/java/ru/orbita/broker/AccountReadTest.java",
            "frontend/src/account-ui.ts",
            "tools/check_ci_scope.py",
            "docs/acceptance/code-review.md",
            "tests/test_configuration.py",
        ]
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(evaluate(resolve_rules(), [path]), [])

    def test_paths_outside_allow_are_denied(self) -> None:
        for path in ("README.md", "deploy/compose.yaml", "infra/langfuse/compose.yaml"):
            with self.subTest(path=path):
                hits = evaluate(resolve_rules(), [path])
                self.assertEqual(verdict(hits), "deny")
                self.assertEqual(hits[0].rule, "out-of-scope")

    def test_windows_separators_and_case_are_normalised(self) -> None:
        denied = [
            ".GITHUB\\workflows\\CI.yml",
            "Backend\\Pom.xml",
            "FRONTEND\\package-LOCK.json",
            "backend\\src\\main\\resources\\db\\migration\\V1__x.sql",
            "Deploy\\.Env",
        ]
        allowed = [
            "Backend\\src\\main\\java\\ru\\orbita\\OrbitaApplication.java",
            "FRONTEND\\SRC\\app.ts",
            ".\\tests\\test_configuration.py",
        ]
        allowed_unix = [item.replace("\\", "/") for item in allowed]
        for path in denied:
            with self.subTest(path=path):
                self.assertEqual(verdict(evaluate(resolve_rules(), [path])), "deny")
        for path in allowed_unix:
            with self.subTest(path=path):
                self.assertEqual(evaluate(resolve_rules(), [path]), [])

    def test_exact_patterns_do_not_leak_to_similar_names(self) -> None:
        self.assertTrue(match_pattern("backend/pom.xml", "backend/pom.xml"))
        self.assertFalse(match_pattern("backend/pom.xml.bak", "backend/pom.xml"))
        self.assertFalse(match_pattern("uv.lock.example", "uv.lock"))

    def test_legacy_forbidden_override_keeps_working(self) -> None:
        rules = resolve_rules(profile="orbita", explicit=[".github/"])
        self.assertEqual(rules.allow, [])
        self.assertFalse(rules.enforce_allow)
        self.assertEqual(hit_rules([".github/workflows/ci.yml"], rules), ["forbidden"])
        self.assertEqual(hit_rules(["README.md"], rules), [])


@unittest.skipUnless(ORBITA_ROOT.is_dir(), "orbita checkout is not available")
class OrbitaRealPathTests(unittest.TestCase):
    def test_existing_orbita_allow_files_pass(self) -> None:
        candidates = [
            "backend/src/main/java/ru/orbita/OrbitaApplication.java",
            "backend/src/test/java/ru/orbita/broker/AccountReadTest.java",
            "frontend/src/app.ts",
            "tools/check_ci_scope.py",
            "docs/acceptance/code-review.md",
            "tests/test_configuration.py",
        ]
        missing = [item for item in candidates if not (ORBITA_ROOT / item).is_file()]
        self.assertEqual(missing, [], "expected real orbita files to exist")
        for path in candidates:
            with self.subTest(path=path):
                self.assertEqual(evaluate(resolve_rules(), [path]), [])

    def test_existing_orbita_forbidden_files_deny(self) -> None:
        candidates = [
            ".github/workflows/ci.yml",
            "backend/pom.xml",
            "uv.lock",
            "frontend/package-lock.json",
            "deploy/.env",
            "backend/src/main/resources/db/migration/V10__position_inventory_basis.sql",
        ]
        missing = [item for item in candidates if not (ORBITA_ROOT / item).is_file()]
        self.assertEqual(missing, [], "expected real orbita files to exist")
        for path in candidates:
            with self.subTest(path=path):
                hits = evaluate(resolve_rules(), [path])
                self.assertEqual(verdict(hits), "deny")
                self.assertEqual(hits[0].rule, "forbidden")


if __name__ == "__main__":
    unittest.main()