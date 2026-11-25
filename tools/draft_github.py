'''Create-only publisher. No checkout, Git hooks, credential helper, push or merge operation.'''

from hashlib import sha1
import json
import os
import re
import subprocess
import tempfile
from urllib.parse import urlencode

from .draft import AdapterFailure


class GitHubDrafts:
    def __init__(self, profile, directory, token):
        self.profile = profile
        self.directory = directory
        self._token = token

    def _request(self, path, body=None):
        # Private transport: callers below supply fixed endpoint families, not issue text.
        with tempfile.TemporaryDirectory() as config:
            args = ['/usr/bin/gh', 'api', '--hostname', 'github.com', '--method',
                    'GET' if body is None else 'POST', f'repos/{self.profile.repo}/{path}']
            env = dict(GH_TOKEN=self._token, GH_CONFIG_DIR=config, HOME=config,
                       GH_PROMPT_DISABLED='1', GH_NO_UPDATE_NOTIFIER='1')
            try:
                output = subprocess.run(args + ([] if body is None else ['--input', '-']),
                                        env=env, cwd=config, input=b'' if body is None else json.dumps(body).encode(),
                                        capture_output=True, timeout=30)
            except subprocess.TimeoutExpired:
                raise TimeoutError from None
            if output.returncode:
                if b'HTTP 401' in output.stderr:
                    raise AdapterFailure('auth_expired')
                # Unknown response, including 422 after a racing create: reconcile before retry.
                raise ConnectionError
            return json.loads(output.stdout)

    def _scope(self, repo, branch):
        if repo != self.profile.repo or not re.fullmatch(r'setupai/[1-9][0-9]*-[0-9a-f]{32}', branch):
            raise ValueError('outside publication scope')

    def ref(self, repo, branch):
        if repo != self.profile.repo or branch != self.profile.base:
            self._scope(repo, branch)
        rows = self._request('git/matching-refs/heads/' + branch)
        if not isinstance(rows, list):
            raise ValueError('unknown refs')
        for row in rows:
            if row['ref'] == 'refs/heads/' + branch:
                if row['object']['type'] != 'commit' or not re.fullmatch('[a-f0-9]{40}', row['object']['sha']):
                    raise ValueError('not a commit')
                return row['object']['sha']
        return None

    def prepare(self, claim, p, data):
        if p != self.profile or claim['repo'] != p.repo:
            raise ValueError('unapproved profile')
        original = p.snapshot(self.directory)
        parent = self._request('git/commits/' + p.revision)
        if parent['sha'] != p.revision:
            raise ValueError('base changed')
        tree = parent['tree']['sha']
        if not re.fullmatch('[0-9a-f]{40}', tree):
            raise ValueError('invalid tree')
        listing = self._request('git/trees/' + tree)
        rows = [e for e in listing['tree'] if e['path'] == p.source]
        if (listing['truncated'] is not False or listing['sha'] != tree or len(rows) != 1
                or rows[0]['type'] != 'blob' or rows[0]['mode'] != '100644'
                or rows[0]['sha'] != sha1(b'blob %d\0' % len(original) + original).hexdigest()):
            raise ValueError('source is not the approved base blob')
        new = self._request('git/trees', dict(base_tree=tree, tree=[dict(path=p.source, mode='100644',
                                 type='blob', content=data.decode('utf-8'))]))['sha']
        if not re.fullmatch('[a-f0-9]{40}', new):
            raise ValueError('unknown tree')
        actor = dict(name='SetupAI', email='setupai@users.noreply.github.com', date=parent['committer']['date'])
        commit = self._request('git/commits', dict(message='Draft run ' + claim['run_id'], parents=[p.revision],
                                                    tree=new, author=actor, committer=actor))
        if commit['tree']['sha'] != new or [r['sha'] for r in commit['parents']] != [p.revision]:
            raise ValueError('unexpected commit')
        return commit['sha']

    def create_ref(self, repo, branch, sha):
        self._scope(repo, branch)
        self._request('git/refs', dict(ref='refs/heads/' + branch, sha=sha))

    def drafts(self, repo, branch):
        self._scope(repo, branch)
        query = urlencode(dict(state='all', head=repo.split('/')[0] + ':' + branch, per_page=2))
        rows = self._request('pulls?' + query)
        if not isinstance(rows, list):
            raise ValueError('unknown pull requests')
        return [dict(repo=r['head']['repo']['full_name'], base_repo=r['base']['repo']['full_name'],
                     branch=r['head']['ref'], base=r['base']['ref'], head=r['head']['sha'],
                     state=r['state'], draft=r['draft'], number=r['number']) for r in rows]

    def create_draft(self, repo, branch, base, number):
        self._scope(repo, branch)
        if base != self.profile.base:
            raise ValueError('invalid base')
        self._request('pulls', dict(head=branch, base=base, draft=True, maintainer_can_modify=False,
                                    title=f'Draft for issue {number}', body='Awaiting human review.'))
