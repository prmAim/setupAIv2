import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from bounded_snapshot import Bundle, FILE_LIMIT, WIRE_LIMIT, UnapprovedPath, build, digest, git, staged, wire
from synthetic_evaluation import MARKER, POLICY, SOURCE, fixture, grade, run
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

    def test_unapproved_is_distinct_from_repeated(self):
        with self.assertRaises(UnapprovedPath), staged(self.bundle, self.proposal(self.a | {"path": "src/out.js"})):
            self.fail("outside edit accepted")
        with self.assertRaisesRegex(ValueError, "^repeated path$") as err, staged(self.bundle, self.proposal(self.a, self.a)):
            self.fail("duplicate edit accepted")
        self.assertNotIsInstance(err.exception, UnapprovedPath)

    def test_wire_and_staged_limits_are_independent(self):
        # 64 KiB of UTF-8 fits staging but escapes can exceed the request wire budget.
        escaped = Bundle("demo", tuple((f"src/{c}.js", b"\x01"*FILE_LIMIT) for c in "abcd"))
        with self.assertRaisesRegex(ValueError, "serialized request"):
            escaped.request()
        payload = self.proposal(self.a).ljust(WIRE_LIMIT)
        with staged(self.bundle, payload) as root:
            self.assertEqual((root / "src/a.js").read_bytes(), b"one")
        with self.assertRaisesRegex(ValueError, "response size"), staged(self.bundle, payload + b" "):
            self.fail("over wire limit")
        self.bundle = Bundle("demo", (("src/a.js", b"old"), *((f"src/{c}.js", b"x"*FILE_LIMIT) for c in "bcd"), ("src/e.js", b"")))
        exact = self.a | {"text": "\u00e9"*(FILE_LIMIT//2)}
        with staged(self.bundle, self.proposal(exact)) as root:
            self.assertEqual((root / "src/a.js").stat().st_size, FILE_LIMIT)
        one_more = {"path": "src/e.js", "sha256": digest(b""), "text": "x"}
        with self.assertRaisesRegex(ValueError, "result exceeds"), staged(self.bundle, self.proposal(exact, one_more)):
            self.fail("over staged aggregate limit")


class SyntheticBoundary(unittest.TestCase):
    def test_marker_aborts_before_each_transport_call(self):
        for contaminated in (0, 1):
            bundles = [Bundle(issue + (MARKER.decode() if i == contaminated else ""), (("src/message.js", SOURCE),))
                       for i, issue in enumerate(("edit sibling", "change prefix"))]
            audit = Mock()
            audit.call.side_effect = propose
            with patch("synthetic_evaluation.fixture"), patch("synthetic_evaluation.build", side_effect=bundles), self.assertRaisesRegex(ValueError, "marker disclosure"):
                run(audit)
            self.assertEqual(audit.call.call_count, contaminated)
            for received in audit.call.call_args_list:
                self.assertNotIn(MARKER, received.args[0])

    def test_partial_start_cleanup_and_errors(self):
        def reply(data=b"", code=0):
            return subprocess.CompletedProcess("docker", code, data, b"")
        for failure in ("none", "remove", "verify", "still_present", "foreign"):
            docker = Mock()
            docker.available.return_value = (True, "")
            docker.run.side_effect = RuntimeError("partial start")
            docker._cli.side_effect = [reply(b"image"), reply(b"cid"), reply(code=int(failure == "remove")),
                                       reply(b"cid" if failure == "still_present" else b"", int(failure == "verify"))]
            def owned(cid):
                spec = docker.run.call_args.args[0]
                record = Mock(id=cid, labels={} if failure == "foreign" else spec.labels)
                record.name = spec.name
                return record
            docker.inspect.side_effect = owned
            error = {"none": RuntimeError, "remove": subprocess.CalledProcessError,
                     "verify": subprocess.CalledProcessError}.get(failure, ValueError)
            with self.subTest(failure=failure), patch("synthetic_evaluation.SubprocessDocker", return_value=docker), self.assertRaises(error):
                grade(Path("fixture"))
            commands = [c.args[0] for c in docker._cli.call_args_list]
            self.assertEqual(commands, ["image", "ps"] + ([] if failure == "foreign" else ["rm"]) +
                             ([] if failure in ("foreign", "remove") else ["ps"]))
