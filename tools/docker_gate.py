'''Local Docker broker. The installed CLI and the pinned, credential-free image are trusted.'''

import os
import json
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
import uuid


# Fixed official python:3.12-alpine image. No volumes, healthcheck or credentials.
IMAGE = 'sha256:c5ba3deef00c7758561a0f03c26f44f10589263d40e732b8748e0d1f38f6dd89'
BOOT = ('import sys; b=sys.stdin.buffer; n=int.from_bytes(b.read(4)); '
        '''exec(compile(b.read(n), 'solution.py', 'exec'), {'__name__':'__main__'})''')


def docker_gate(profile, data, *, cli=Path('/usr/bin/docker')):
    if not cli.is_absolute() or profile.image != IMAGE or not 0 < len(data) <= 65536:
        return None
    return isolated(cli, BOOT, len(data).to_bytes(4) + data + profile.stdin, profile.expected)


def isolated(cli, bootstrap, payload, expected):
    name = 'setupai-' + uuid.uuid4().hex
    env = {'SystemRoot': os.environ['SystemRoot']} if os.name == 'nt' else {}
    host = 'npipe:////./pipe/docker_engine' if os.name == 'nt' else 'unix:///var/run/docker.sock'
    start = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp:
        args = [str(cli), '--host', host, '--config', tmp]
        opts = dict(env=env, cwd=tmp, stderr=subprocess.DEVNULL)
        def control(*cmd):
            return subprocess.run([*args, *cmd], **opts, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, timeout=10, check=True).stdout.strip()
        try:
            security = json.loads(control('info', '--format', '{{json .SecurityOptions}}'))
            if not any(s.startswith('name=seccomp,') for s in security):
                return None
            cid = control('create', '-i', '--name', name, '--pull=never', '--platform=linux/amd64',
                          '--network=none', '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
                          '--user=65534:65534', '--memory=96m', '--memory-swap=96m', '--pids-limit=12',
                          '--cpus=0.5', '--ipc=none', '--log-driver=none', '--no-healthcheck',
                           '--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=8m', '--entrypoint=/usr/bin/env', IMAGE,
                          '-i', '/bin/busybox', 'timeout', '-s', 'KILL', '10', '/usr/local/bin/python',
                           '-I', '-B', '-c', bootstrap).decode('ascii')
            if not re.fullmatch('[a-f0-9]{64}', cid):
                return None
            spec = json.loads(control('inspect', cid))[0]
            host_cfg = spec['HostConfig']
            if (spec['Id'] != cid or spec['Platform'] != 'linux' or spec['Mounts'] or host_cfg['Privileged']
                    or host_cfg['NetworkMode'] != 'none' or not host_cfg['ReadonlyRootfs']
                    or host_cfg['CapAdd'] or host_cfg['Devices'] or host_cfg['PidMode'] == 'host'
                    or spec['Config']['User'] != '65534:65534' or 'ALL' not in host_cfg['CapDrop']):
                return None
            with subprocess.Popen([*args, 'start', '-ai', cid], **opts, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE) as proc:
                # Never buffer unlimited candidate output. Blocking pipe I/O has a finite lifetime.
                result = []
                def exchange():
                    try:
                        proc.stdin.write(payload)
                        proc.stdin.close()
                        result.append(proc.stdout.read(len(expected) + 1))
                    except OSError:
                        pass
                worker = threading.Thread(target=exchange, daemon=True)
                worker.start()
                worker.join(12)
                try:
                    if worker.is_alive() or not result:
                        return None
                    proc.wait(timeout=2)
                finally:
                    proc.kill()
                    proc.wait()
                    worker.join(2)
            state = control('inspect', '--format', '{{.State.Status}} {{.State.ExitCode}} {{.State.OOMKilled}}', cid)
            if state != f'exited {proc.returncode} false'.encode():
                return None
            passed = proc.returncode == 0 and result[0] == expected
            return dict(status='passed' if passed else 'failed', exit=proc.returncode, image=IMAGE,
                        container=cid, duration_ms=int((time.monotonic() - start) * 1000))
        except (OSError, subprocess.SubprocessError, ValueError, IndexError, KeyError, TypeError):
            return None
        finally:
            try:
                control('rm', '-f', name)
            except (OSError, subprocess.SubprocessError):
                return None
