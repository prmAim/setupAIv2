"""Standalone Linux test: mount index.json and gh at their protected paths."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def snapshot(root):
    return {p.relative_to(root): (p.stat().st_mtime_ns, p.read_bytes() if p.is_file() else None)
            for p in [root, *root.rglob('*')]}


with tempfile.TemporaryDirectory() as scratch:
    target = Path(scratch)
    (target / 'selection-profiles.json').write_text('invalid unapproved local decoy')
    before = snapshot(target)
    proc = subprocess.run([sys.executable, '-I', '-B', '/work/tools/select_issue.py', 'example'],
                          cwd=target, env={'HOME': scratch, 'PATH': os.environ['PATH']},
                          capture_output=True, timeout=15, check=True)
    assert json.loads(proc.stdout) == {'status': 'no_work'} and not proc.stderr
    assert snapshot(target) == before
    requests = [json.loads(line) for line in Path('/tmp/selection-requests').read_text().splitlines()]
    assert len(requests) == 3 and all(r[:5] == ['api', '--hostname', 'github.com', '--method', 'GET'] for r in requests)
    assert '/contents/' in requests[0][-1] and requests[1] == requests[2] and '/issues?' in requests[1][-1]
    print('no_work: 1 pinned profile GET, 2 paginated queue GETs; target/HOME unchanged')
