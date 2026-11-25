'''Trusted operator probe; only the Docker broker runs here, never the candidate.'''

import json
import os
from pathlib import Path
import runpy
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
    print(json.dumps(dict(matching=green, mismatch=red)))
    raise SystemExit(0 if green and green['status'] == 'passed' and red and red['status'] == 'failed' else 1)
