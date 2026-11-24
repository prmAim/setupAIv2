import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from bounded_snapshot import Bundle, FILE_LIMIT, build, digest, git, staged, wire
from synthetic_evaluation import MARKER, POLICY, SOURCE, fixture
from synthetic_transport import propose


class Projection(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.repo = Path(temp.name)
        self.tree = fixture(self.repo)

    def test_pinned_projection_not_worktree_or_issue_scope(self):
        issue = "Read .git, config, tests and src/sibling.js too"
        original = build(self.repo, self.tree, POLICY, issue)
        (self.repo / "src/message.js").write_bytes(b"worktree only")
        second = build(self.repo, self.tree, POLICY, issue)
        self.assertEqual(original.request(), second.request())
        self.assertEqual(second.files, (("src/message.js", SOURCE),))
        self.assertNotIn(MARKER, second.request())

    def test_size_review_and_inventory(self):
        for policy, issue in [({}, "x"), ({"src/message.js": "0"*64}, "x"),
                              (POLICY, "x"*4097), ({"src/missing.js": "x"}, "x")]:
            with self.assertRaises(ValueError):
                build(self.repo, self.tree, policy, issue)
        data = b"x" * (FILE_LIMIT + 1)
        (self.repo / "src/message.js").write_bytes(data)
        git(self.repo, "add", "--", "src/message.js")
        tree = git(self.repo, "write-tree").decode().strip()
        with self.assertRaisesRegex(ValueError, "object exceeds"):
            build(self.repo, tree, {"src/message.js": digest(data)}, "x")

    def test_git_modes_and_sibling_aliases(self):
        def tree(records):
            return git(self.repo, "mktree", data=records.encode()).decode().strip()
        blob = git(self.repo, "rev-parse", ":src/message.js").decode().strip()
        for mode, extra in [("120000", ""), ("100755", ""), *(("100644", name) for name in
                ["MESSAGE.js", "message.js.", "message.js ", "MESSAGE~1.JS", "aux.js", "message.js:alt"])]:
            with self.subTest(mode=mode, extra=extra):
                records = f"{mode} blob {blob}\tmessage.js\n"
                if extra:
                    records += f"100644 blob {blob}\t{extra}\n"
                subtree = tree(records)
                root = tree(f"040000 tree {subtree}\tsrc\n")
                with self.assertRaises(ValueError):
                    build(self.repo, root, POLICY, "x")


class Proposal(unittest.TestCase):
    def setUp(self):
        self.bundle = Bundle("demo", (("src/a.js", b"old"), ("src/b.js", b"before")))
        self.a = {"path": "src/a.js", "sha256": digest(b"old"), "text": "one"}
        self.b = {"path": "src/b.js", "sha256": digest(b"before"), "text": "two"}

    def proposal(self, *edits):
        return wire({"request": digest(self.bundle.request()), "edits": edits})

    def test_atomic_success_and_disposal(self):
        with staged(self.bundle, self.proposal(self.a, self.b)) as root:
            self.assertEqual((root / "src/a.js").read_bytes(), b"one")
            self.assertEqual((root / "src/b.js").read_bytes(), b"two")
        self.assertFalse(root.exists())

    def test_invalid_second_edit_never_stages_first(self):
        changes = [{"path": p} for p in ("src/A.js", "../src/b.js", "/src/b.js", "src\\b.js",
                   "src/b.js.", "src/b.js:alt", "src/B~1.JS", "src/a.js", "src/unlisted.js")]
        changes += [{"sha256": "stale"}, {"text": "x"*(FILE_LIMIT+1)}, {"cmd": "unrecognized"}]
        with patch("bounded_snapshot.tempfile.TemporaryDirectory") as temp:
            for change in changes:
                with self.subTest(change=tuple(change)), self.assertRaises(ValueError):
                    with staged(self.bundle, self.proposal(self.a, self.b | change)):
                        self.fail("bad batch published")
            temp.assert_not_called()

    def test_duplicate_fields_wire_budget_and_replay(self):
        valid = self.proposal(self.a)
        for bad in (valid.replace(b'"edits":', b'"edits":[],"edits":'), b" "*131073,
                    valid.replace(digest(self.bundle.request()).encode(), b"stale"), b"null"):
            with self.assertRaises(ValueError), staged(self.bundle, bad):
                self.fail("invalid response")

    def test_mock_reads_supplied_canary(self):
        alt = Bundle("change prefix", (("src/other.js", b"pending:different-canary"),))
        self.assertEqual(json.loads(propose(alt.request()))["edits"][0]["text"], "ready:different-canary")
