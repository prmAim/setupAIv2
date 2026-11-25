'''Single-source draft workflow. All adapter objects are installed by the operator.'''

from dataclasses import dataclass
from contextlib import closing
from hashlib import sha256
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

    def __post_init__(self):
        for value, pattern in ((self.repo, r'[\w-]+/[\w-]+'), (self.base, r'[a-zA-Z0-9_-]+'),
                               (self.revision, '[a-f0-9]{40}'), (self.source, r'[a-z][a-z0-9_]*\.py'),
                               (self.digest, '[a-f0-9]{64}'), (self.image, 'sha256:[a-f0-9]{64}')):
            if not re.fullmatch(pattern, value, re.ASCII):
                raise ValueError('invalid operator profile')
        if any(type(v) is not bytes or len(v) > LIMIT for v in (self.stdin, self.expected)):
            raise ValueError('invalid gate vector')

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

    def __call__(self, claim, task):
        p, remote = self.profile, self.publisher
        report = dict(**claim, base=p.revision, image=p.image, head=None, status='not_run')
        if p.repo != claim['repo']:
            return report
        try:
            source = p.snapshot(self.directory)
            report['profile_digest'] = sha256(repr(p).encode()).hexdigest()
            binding = sha256(repr((p, claim['repo'], claim['number'], claim['owner'], claim['claim_id'])).encode()).hexdigest()
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
            evidence = self.gate(p, data)
            if not isinstance(evidence, dict) or evidence.get('image') != p.image:
                return report
            if (not re.fullmatch('[a-f0-9]{64}', evidence.get('container', ''))
                    or type(evidence.get('exit')) is not int or type(evidence.get('duration_ms')) is not int
                    or evidence['duration_ms'] < 0):
                return report
            report.update({k: evidence[k] for k in ('container', 'exit', 'duration_ms')},
                          changed=[p.source], edit_sha256=sha256(data).hexdigest(), gate='exact_stdout')
            if evidence.get('status') == 'failed':
                return report | dict(status='test_failed')
            if evidence.get('status') != 'passed' or evidence['exit'] != 0:
                return report
            branch = 'setupai/{number}-{run_id}'.format(**claim)
            if remote.ref(p.repo, p.base) != p.revision:
                return report | dict(status='remote_conflict')
            with closing(sqlite3.connect(self.journal)) as db, db:
                db.execute('CREATE TABLE IF NOT EXISTS prepared (run TEXT PRIMARY KEY, head TEXT)')
                row = db.execute('SELECT head FROM prepared WHERE run=?', (claim['run_id'],)).fetchone()
                head = row[0] if row else remote.prepare(claim, p, data)
                if not row and re.fullmatch('[a-f0-9]{40}', head):
                    db.execute('INSERT INTO prepared VALUES (?,?)', (claim['run_id'], head))
            if not re.fullmatch('[a-f0-9]{40}', head) or head == p.revision:
                return report
            report.update(head=head, branch=branch)
            def observe():
                return remote.ref(p.repo, branch), remote.drafts(p.repo, branch)
            def attempt(kind, write, *args):
                with closing(sqlite3.connect(self.journal)) as db, db:
                    db.execute('CREATE TABLE IF NOT EXISTS intent (run TEXT, kind TEXT, PRIMARY KEY(run,kind))')
                    if not db.execute('INSERT OR IGNORE INTO intent VALUES (?,?)', (claim['run_id'], kind)).rowcount:
                        return False
                try:
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
            if exc.args in [('quota_exhausted',), ('auth_expired',), ('network_blocked',)]:
                report['status'] = exc.args[0]
        except (ValueError, OSError, KeyError, TypeError, sqlite3.Error):
            pass
        return report
