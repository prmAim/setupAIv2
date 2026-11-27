'''Complete, bounded regular-file Git tree; commands execute only inside the pinned image.'''

import base64
from hashlib import sha1, sha256
import json
from pathlib import Path
import re

from .docker_gate import IMAGE, isolated


BOOT = '''import base64,json,os,pathlib,subprocess,sys
files,commands = json.load(sys.stdin)
os.mkdir('/tmp/project')
os.chdir('/tmp/project')
for name, content in files.items():
    p = pathlib.Path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(base64.b64decode(content, validate=True))
for cmd in commands:
    result = subprocess.run([sys.executable, '-B', *cmd], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode: sys.exit(1)
print('passed')
'''


def tree_id(files):
    def oid(kind, data):
        return sha1(kind + b' %d\0' % len(data) + data).digest()
    root = {}
    for name, content in files.items():
        parts = name.split('/')
        if len(parts) > 12 or any(not re.fullmatch('[a-zA-Z0-9_.-]{1,100}', s) or s.lower() in (
                '.', '..', '.git') for s in parts):
            raise ValueError('unsupported project path')
        node = root
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise ValueError('file/directory conflict')
        if parts[-1] in node:
            raise ValueError('duplicate path')
        node[parts[-1]] = oid(b'blob', content)
    def tree(node):
        entries = []
        for name, value in node.items():
            directory = isinstance(value, dict)
            entries.append((name + ('/' if directory else ''), (b'40000 ' if directory else b'100644 ')
                            + name.encode() + b'\0' + (tree(value) if directory else value)))
        return oid(b'tree', b''.join(raw for _, raw in sorted(entries)))
    return tree(root).hex()


def project_gate(profile, edit, *, files, cli=Path('/usr/bin/docker')):
    # files is an operator-provisioned, secret-free bundle, not a recursive workspace read.
    if (profile.image != IMAGE or not profile.commands or not cli.is_absolute() or not 0 < len(edit) <= 65536
            or not 0 < len(files) <= 512 or sum(map(len, files.values())) > 2**21):
        return None
    files = files.copy()
    if (profile.source not in files or any(type(v) is not bytes for v in files.values()) or type(edit) is not bytes
            or tree_id(files) != profile.project_tree or sha256(files[profile.source]).hexdigest() != profile.digest):
        return None
    staged = files | {profile.source: edit}
    protected = {k: sha256(v).hexdigest() for k, v in files.items() if k != profile.source}
    if protected != dict(profile.protected):
        return None
    encoded = {name: base64.b64encode(content).decode('ascii') for name, content in staged.items()}
    evidence = isolated(cli, BOOT, json.dumps([encoded, profile.commands]).encode(), b'passed\n')
    if evidence:
        evidence.update(baseline=profile.project_tree, protected=protected, tree=tree_id(staged),
                        edit_sha256=sha256(edit).hexdigest(), commands=list(map(list, profile.commands)))
    return evidence
