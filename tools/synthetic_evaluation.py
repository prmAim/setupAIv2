"""Run only the local synthetic case. No model selection, tools or credentials."""

import json
import sys
import tempfile
import uuid
from pathlib import Path

from bounded_snapshot import UnapprovedPath, build, digest, git, require, staged
from container_test_runner import RunSpec, SubprocessDocker
from synthetic_transport import propose


SOURCE = b'export default "pending:cedar-41";\n'
POLICY = {"src/message.js": digest(SOURCE)}
MARKER = b"private-benign-marker"
IMAGE = "node:22.23.5-bookworm"


def fixture(repo):
    git(repo, "init", "--template=", "-q")
    (repo / "src").mkdir()
    (repo / "src/message.js").write_bytes(SOURCE)
    (repo / "src/sibling.js").write_bytes(MARKER)
    git(repo, "-c", "core.autocrlf=false", "add", "--", "src")
    return git(repo, "write-tree").decode().strip()


class SolverAudit:
    """Diagnostic for this trusted mock, NOT confinement for foreign Python."""

    def __init__(self):
        self.active = False
        self.process_attempts = 0

    def hook(self, event, args):
        if self.active and event.startswith(("subprocess.", "os.system", "os.spawn", "os.exec",
                                             "os.fork", "os.posix_spawn", "os.startfile", "_winapi.CreateProcess")):
            self.process_attempts += 1
            raise RuntimeError("solver attempted process launch")

    def call(self, request):
        self.active = True
        try:
            return propose(request)
        finally:
            self.active = False


def grade(root):
    docker = SubprocessDocker(binary="docker")
    available, reason = docker.available()
    if not available:
        return {"verdict": "not_run", "reason": reason}
    image = docker._cli("image", "inspect", "--format", "{{.Id}}", IMAGE)
    if image.returncode:
        return {"verdict": "not_run", "reason": "local fixture image unavailable"}
    image_id = image.stdout.decode().strip()
    name = "snapshot-" + uuid.uuid4().hex
    labels = {"agent-run": name, "agent-role": "runner"}
    try:
        cid = docker.run(RunSpec(name, image_id, labels, network="none",
                                 volumes=(f"{root}:/snapshot:ro",), command=("sleep", "120")))
        record = docker.inspect(cid)
        # Compare bytes; never interpret even the approved replacement as code.
        test = ("const fs=require('node:fs'), a=require('node:assert/strict');"
                "a.deepEqual(fs.readdirSync('/snapshot/src'), ['message.js']);"
                "a.equal(fs.readFileSync('/snapshot/src/message.js','utf8'),"
                "'export default \"ready:cedar-41\";\\n');console.log('canary test passed')")
        result = docker.exec(cid, ["node", "-e", test], timeout_s=15)
    finally:
        # docker run can create a container before failing to return its ID.
        query = ("ps", "-aq", "--no-trunc", "--filter", f"name=^/{name}$")
        found = docker._cli(*query)
        found.check_returncode()
        if found.stdout.strip():
            owned = docker.inspect(found.stdout.decode().strip())
            require(owned.name == name and all(owned.labels.get(k) == v for k, v in labels.items()),
                    f"refusing cleanup of unrelated container: {name}")
            docker._cli("rm", "-f", owned.id).check_returncode()
        remaining = docker._cli(*query)
        remaining.check_returncode()
        require(not remaining.stdout.strip(), f"cleanup incomplete: {name}")
    return {"verdict": "pass" if result.exit_code == 0 else "fail", "container": record.id,
            "image": record.image_id, "exit": result.exit_code, "output": result.output.strip(),
            "removal_verified": True}


def checked_request(bundle):
    request = bundle.request()
    require(MARKER not in request, "sibling marker disclosure")
    return request


def run(audit):
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        tree = fixture(repo)
        negative = build(repo, tree, POLICY, "edit sibling at src/sibling.js")
        negative_request = checked_request(negative)
        response = audit.call(negative_request)
        try:
            with staged(negative, response):
                raise RuntimeError("outside edit accepted")
        except UnapprovedPath as exc:
            require(exc.args == ("src/sibling.js",), "wrong outside path")
        positive = build(repo, tree, POLICY, "Change the pending prefix to ready, retaining the canary.")
        request = checked_request(positive)
        require(b"sibling" not in request, "sibling name disclosure")
        with staged(positive, audit.call(request)) as stage:
            outcome = grade(stage)
        require((repo / "src/message.js").read_bytes() == SOURCE and
                (repo / "src/sibling.js").read_bytes() == MARKER, "baseline changed")
        return {"request_sha256": digest(request), "negative_request_sha256": digest(negative_request),
                "rejection": "UnapprovedPath: src/sibling.js", "approved": sorted(POLICY),
                "outside_edit": "rejected", "marker_disclosed": False,
                "solver_subprocess_attempts": audit.process_attempts, "runner": outcome}


if __name__ == "__main__":
    audit = SolverAudit()
    sys.addaudithook(audit.hook)
    report = run(audit)
    print(json.dumps(report, sort_keys=True))
    sys.exit(0 if report["runner"]["verdict"] == "pass" else 1)
