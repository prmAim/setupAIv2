'''Single-source draft workflow. All adapter objects are installed by the operator.'''

from dataclasses import dataclass
from contextlib import closing
from hashlib import sha256
import json
import os
import re
import stat
import sqlite3


LIMIT = 65536


@dataclass(frozen=True)
class Profile:
    repo: str
    base: str
    revision: str
    source: str
    digest: str
    image: str
    stdin: bytes
    expected: bytes
    project_tree: str = ''
    commands: tuple = ()
    protected: tuple = ()  # Reviewed, complete non-editable path/SHA-256 pairs.

    def __post_init__(self):
        for value, pattern in ((self.repo, r'[\w-]+/[\w-]+'), (self.base, r'[a-zA-Z0-9_-]+'),
                               (self.revision, '[a-f0-9]{40}'), (self.source, r'[a-z][a-z0-9_]*\.py'),
                               (self.digest, '[a-f0-9]{64}'), (self.image, 'sha256:[a-f0-9]{64}')):
            if not re.fullmatch(pattern, value, re.ASCII):
                raise ValueError('invalid operator profile')
        if any(type(v) is not bytes or len(v) > LIMIT for v in (self.stdin, self.expected)):
            raise ValueError('invalid gate vector')
        if self.commands or self.project_tree or self.protected:
            if (not re.fullmatch('[a-f0-9]{40}', self.project_tree) or not 1 <= len(self.commands) <= 4
                    or any(len(cmd) != 3 or cmd[:2] != ('-m', 'unittest') or not re.fullmatch(
                        r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+', cmd[2], re.ASCII) for cmd in self.commands)):
                raise ValueError('unsupported project test profile')
            pins = dict(self.protected)
            if (type(self.protected) is not tuple or not pins or len(pins) != len(self.protected)
                    or self.source in pins or any(not re.fullmatch('[a-f0-9]{64}', h) for h in pins.values())
                    or any(cmd[2].replace('.', '/') + '.py' not in pins for cmd in self.commands)):
                raise ValueError('missing protected project inputs')

    def snapshot(self, directory):
        # Root is operator-owned, not a user-supplied working directory.
        fd = os.open(directory / self.source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError('not a regular source')
            data = stream.read(LIMIT + 1)
        if len(data) > LIMIT or sha256(data).hexdigest() != self.digest:
            raise ValueError('unverified source')
        return data


class AdapterFailure(Exception):
    '''Adapter-sanitized infrastructure classification; never copy arbitrary messages.'''


@dataclass
class Draft:
    profile: Profile
    directory: object
    propose: object
    gate: object
    publisher: object
    journal: object

    def binding(self, claim):
        return sha256(repr((self.profile, claim['repo'], claim['number'], claim['owner'], claim['claim_id'])).encode()).hexdigest()

    def own_ref(self, claim):
        if not self.journal.is_file():
            return None
        with closing(sqlite3.connect(self.journal)) as db:
            if not db.execute('SELECT 1 FROM sqlite_master WHERE name=?', ('proof',)).fetchone():
                return None
            row = db.execute('SELECT head FROM proof WHERE run=? AND binding=?',
                             (claim['run_id'], self.binding(claim))).fetchone()
        if row and re.fullmatch('[a-f0-9]{40}', row[0]):
            return claim['repo'], 'setupai/{number}-{run_id}'.format(**claim), row[0]

    def __call__(self, claim, task, recheck):
        p, remote = self.profile, self.publisher
        report = dict(**claim, base=p.revision, image=p.image, head=None, status='not_run')
        unconfirmed = False
        if p.repo != claim['repo'] or not p.commands or not p.protected:
            return report
        try:
            source = p.snapshot(self.directory)
            report['profile_digest'] = sha256(repr(p).encode()).hexdigest()
            binding = self.binding(claim)
            with closing(sqlite3.connect(self.journal)) as db, db:
                db.execute('CREATE TABLE IF NOT EXISTS candidate (run TEXT PRIMARY KEY, binding TEXT, data BLOB)')
                row = db.execute('SELECT binding,data FROM candidate WHERE run=?', (claim['run_id'],)).fetchone()
                if row:
                    if row[0] != binding:
                        return report | dict(status='profile_changed')
                    data = row[1]
                else:
                    edit = self.propose({p.source: source}, task)
                    if type(edit) is not dict or set(edit) != {p.source}:
                        return report | dict(status='invalid_edit')
                    data = edit[p.source]
                if type(data) is not bytes or not 0 < len(data) <= LIMIT or data == source:
                    return report | dict(status='invalid_edit')
                if not row:
                    db.execute('INSERT INTO candidate VALUES (?,?,?)', (claim['run_id'], binding, data))
            digest = sha256(data).hexdigest()
            with closing(sqlite3.connect(self.journal)) as db, db:
                db.execute('CREATE TABLE IF NOT EXISTS proof (run TEXT PRIMARY KEY, binding TEXT, digest TEXT, head TEXT, evidence TEXT)')
                proof = db.execute('SELECT binding,digest,head,evidence FROM proof WHERE run=?', (claim['run_id'],)).fetchone()
            if proof:
                # Only passing, well-formed evidence can have been journaled.
                report['status'] = 'invalid_evidence'
                if (proof[:2] != (binding, digest) or not re.fullmatch('[a-f0-9]{40}', proof[2])
                        or proof[2] == p.revision):
                    return report
            evidence = json.loads(proof[3]) if proof else self.gate(p, data)
            fields = {'commands', 'edit_sha256', 'tree', 'baseline', 'protected', 'inspected_image',
                      'image', 'status', 'container', 'exit', 'duration_ms'}
            if not isinstance(evidence, dict) or set(evidence) != fields:
                return report
            if (evidence['image'] != p.image or evidence['inspected_image'] != p.image
                    or evidence['baseline'] != p.project_tree or evidence['protected'] != dict(p.protected)
                    or evidence['commands'] != list(map(list, p.commands)) or evidence['edit_sha256'] != digest
                    or evidence['tree'] == p.project_tree or not re.fullmatch('[a-f0-9]{40}', evidence['tree'])):
                return report
            if (not re.fullmatch('[a-f0-9]{64}', evidence.get('container', ''))
                    or type(evidence.get('exit')) is not int or type(evidence.get('duration_ms')) is not int
                    or evidence['duration_ms'] < 0):
                return report
            report.update({k: evidence[k] for k in ('container', 'exit', 'duration_ms')},
                          changed=[p.source], edit_sha256=digest, gate=p.commands or 'exact_stdout')
            if not proof and evidence.get('status') == 'failed':
                return report | dict(status='test_failed')
            if evidence.get('status') != 'passed' or evidence['exit'] != 0:
                return report
            report['status'] = 'not_run'
            branch = 'setupai/{number}-{run_id}'.format(**claim)
            if remote.ref(p.repo, p.base) != p.revision:
                return report | dict(status='remote_conflict')
            if proof:
                head = proof[2]
            else:
                recheck()
                head = remote.prepare(claim, p, data, tested_tree=evidence['tree'])
            if not re.fullmatch('[a-f0-9]{40}', head) or head == p.revision:
                return report
            def verify_commit():
                commit = remote.commit(p.repo, head)
                if (commit['sha'] != head or commit['tree']['sha'] != evidence['tree']
                        or [parent['sha'] for parent in commit['parents']] != [p.revision]):
                    raise AdapterFailure('invalid_evidence')
            verify_commit()
            if not proof:
                with closing(sqlite3.connect(self.journal)) as db, db:
                    db.execute('INSERT INTO proof VALUES (?,?,?,?,?)',
                               (claim['run_id'], binding, digest, head, json.dumps(evidence)))
            report.update(head=head, branch=branch)
            def observe():
                return remote.ref(p.repo, branch), remote.drafts(p.repo, branch)
            def attempt(kind, write, *args):
                nonlocal unconfirmed
                verify_commit()
                recheck((p.repo, branch, head) if kind == 'pr' else None)
                with closing(sqlite3.connect(self.journal)) as db, db:
                    db.execute('CREATE TABLE IF NOT EXISTS intent (run TEXT, kind TEXT, PRIMARY KEY(run,kind))')
                    if not db.execute('INSERT OR IGNORE INTO intent VALUES (?,?)', (claim['run_id'], kind)).rowcount:
                        return False
                try:
                    unconfirmed = True
                    write(*args)
                except (TimeoutError, ConnectionError):
                    pass  # Never infer success or retry here. Read the remote.
                return True
            ref, prs = observe()
            if ref is None and prs == []:
                if not attempt('ref', remote.create_ref, p.repo, branch, head):
                    return report | dict(status='write_unconfirmed')
                ref, prs = observe()
                if ref is None:
                    return report | dict(status='write_unconfirmed')
            if ref != head:
                return report | dict(status='remote_conflict')
            unconfirmed = False
            if prs == []:
                if not attempt('pr', remote.create_draft, p.repo, branch, p.base, claim['number']):
                    return report | dict(status='write_unconfirmed')
            ref, prs = observe()
            if prs == []:
                return report | dict(status='write_unconfirmed')
            if ref == head and remote.ref(p.repo, p.base) == p.revision and len(prs) == 1:
                pr = prs[0]
                expected = dict(repo=p.repo, base_repo=p.repo, branch=branch, base=p.base, head=head, state='open')
                if (all(pr.get(k) == v for k, v in expected.items()) and pr.get('draft') is True
                        and type(pr.get('number')) is int and pr['number'] > 0):
                    with closing(sqlite3.connect(self.journal)) as db, db:
                        db.execute('CREATE TABLE IF NOT EXISTS published (run TEXT PRIMARY KEY, number INTEGER)')
                        db.execute('INSERT OR IGNORE INTO published VALUES (?,?)', (claim['run_id'], pr['number']))
                        confirmed, = db.execute('SELECT number FROM published WHERE run=?', (claim['run_id'],)).fetchone()
                        if confirmed != pr['number']:
                            return report | dict(status='remote_conflict')
                    return report | dict(status='draft', pr='https://github.com/{}/pull/{}'.format(p.repo, pr['number']))
            return report | dict(status='remote_conflict')
        except AdapterFailure as exc:
            if exc.args in [('quota_exhausted',), ('auth_expired',), ('network_blocked',), ('invalid_evidence',)]:
                report['status'] = exc.args[0]
        except (ConnectionError, TimeoutError):
            report['status'] = 'write_unconfirmed' if unconfirmed else 'network_blocked'
        except (ValueError, OSError, KeyError, TypeError, sqlite3.Error):
            pass
        return report
