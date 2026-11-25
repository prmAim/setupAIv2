'''Trusted operator probe; only the Docker broker runs here, never the candidate.'''

import json
import os
from pathlib import Path
import runpy
import sys
from hashlib import sha256
from types import SimpleNamespace


if __name__ == '__main__':
    gate = runpy.run_path(str(Path(__file__).with_name('docker_gate.py')))
    cli = (Path.home() / 'AppData/Local/Programs/DockerDesktop/resources/bin/docker.exe'
           if os.name == 'nt' else Path('/usr/bin/docker'))
    fixture = b'''import os
from pathlib import Path
import errno, socket
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
    try:
        probe.connect(('192.0.2.1', 9))  # Route lookup only; never send data.
    except OSError as error:
        assert error.errno == errno.ENETUNREACH
    else:
        raise AssertionError('unexpected external route')
assert 'SETUPAI_PRIVATE_MARKER' not in os.environ
assert not Path('/var/run/docker.sock').exists()
p = Path('/tmp/allowed')
p.write_text(input())
try:
    Path('/outside-workspace-marker').write_text('marker')
except OSError:
    print(p.read_text())
'''
    os.environ['SETUPAI_PRIVATE_MARKER'] = 'benign-private-marker'
    profile = SimpleNamespace(image=gate['IMAGE'], stdin=b'public marker\n', expected=b'public marker\n')
    green = gate['docker_gate'](profile, fixture, cli=cli)
    profile.expected = b'different\n'
    red = gate['docker_gate'](profile, fixture, cli=cli)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tools.project_gate import project_gate, tree_id
    from tools.draft import Profile
    files = {'answer.py': b'answer = 0', 'expected.txt': b'42', 'tests/__init__.py': b'',
             'tests/test_answer.py': b'''import unittest
from pathlib import Path
from answer import answer
class Check(unittest.TestCase):
    def test_answer(self):
        self.assertEqual(answer, int(Path('expected.txt').read_text()))
'''}
    policy = Profile('fixture/project', 'main', 'a' * 40, 'answer.py', sha256(files['answer.py']).hexdigest(),
                     gate['IMAGE'], b'', b'', tree_id(files), (('-m', 'unittest', 'tests.test_answer'),))
    project_ok = project_gate(policy, b'answer = 42', files=files, cli=cli)
    project_red = project_gate(policy, b'answer = 41', files=files, cli=cli)
    del files['expected.txt']
    incomplete = project_gate(policy, b'answer = 42', files=files, cli=cli)
    print(json.dumps(dict(matching=green, mismatch=red, project=project_ok, project_mismatch=project_red,
                          incomplete=incomplete)))
    raise SystemExit(0 if green and green['status'] == 'passed' and red and red['status'] == 'failed'
                     and project_ok and project_ok['status'] == 'passed' and project_red
                     and project_red['status'] == 'failed' and incomplete is None else 1)
