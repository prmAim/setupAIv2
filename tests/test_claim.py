import base64
import io
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from test_selection import Selection as _Selection, application, timeline

PROFILE = _Selection.profile
del _Selection
RUN = 'a' * 32
USER = {'type': 'User', 'id': 123}


class Remote:
    def __init__(self, root):
        self.root = root
        self.issue = dict(number=19, state='open', user=USER, assignees=[], body='',
                          repository_url='https://api.github.com/repos/owner/project',
                          labels=[dict(name='ready-for-agent', node_id='label-A')],
                          issue_dependencies_summary=dict(total_blocked_by=0, blocked_by=0))

    def read(self, args):
        if args[0] == 'graphql':
            return timeline(19, [[application()]])
        assert 'GET' in args
        path = args[-1]
        if '/issues?' in path:
            return [[self.issue.copy()]]
        if path.endswith('/19'):
            return self.issue.copy()
        if '/comments?' in path:
            return [[json.loads(line) for line in self.log()]]
        if '/blocked_by?' in path:
            return [[]]
        raise AssertionError(path)

    def log(self):
        path = self.root / 'posts'
        return path.read_text().splitlines() if path.exists() else []

    def post(self, repo, number, body):
        assert (repo, number) == ('owner/project', 19)
        comment = dict(id=42, user=USER, body=body, created_at='now', updated_at='now')
        with (self.root / 'posts').open('a') as out:
            print(json.dumps(comment), file=out)


class Claim(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / 'owner-project'
        self.state.mkdir()
        self.remote = Remote(self.root)

    def invoke(self, run=RUN, resume=False):
        from tools.claim_issue import run_profile
        return run_profile('example', run, resume=resume, profile=PROFILE, github=self.remote.read,
                           post=self.remote.post, approved_repo='owner/project', owner=123, state=self.state)

    def test_claim_is_one_observable_owned_marker(self):
        result = self.invoke()
        self.assertEqual(result, dict(status='claimed', repo='owner/project', number=19, run_id=RUN, owner=123))
        self.assertEqual(len(self.remote.log()), 1)
        self.assertIn(RUN, json.loads(self.remote.log()[0])['body'])

    def test_selection_pin_alone_never_authorizes_a_live_claim(self):
        from tools.claim_issue import command
        with patch.object(Path, 'read_text', side_effect=FileNotFoundError), patch('subprocess.run') as gh:
            self.assertEqual(command(['example', RUN])['status'], 'diagnostic')
            gh.assert_not_called()

    def test_command_checks_pin_and_authenticated_writer_before_post(self):
        from tools.claim_issue import command
        pin = dict(repo=PROFILE['repo'], revision='c' * 40)
        owner = 123
        revision = pin['revision']
        original_read = Path.read_text
        def read(path, **kwargs):
            if not str(path).startswith('/etc/setupAIv2/'):
                return original_read(path, **kwargs)
            if path.name == 'selection-profiles.json':
                return json.dumps(dict(example=pin))
            if path.name == 'claim-profiles.json':
                return json.dumps(dict(example=pin | dict(owner=123, revision=revision)))
            self.assertEqual(str(path), '/etc/setupAIv2/claim-credentials/owner/project')
            return 'github_pat_fixture'
        def github(args):
            if '/contents/' in args[-1]:
                self.assertTrue(args[-1].endswith(pin['revision']))
                return dict(type='file', encoding='base64', content=base64.b64encode(json.dumps(PROFILE).encode()).decode())
            return self.remote.read(args)
        def response(method, path, data, headers):
            self.assertEqual(headers['Authorization'], 'Bearer github_pat_fixture')
            if method == 'GET':
                self.assertEqual(path, '/user')
                value = USER | dict(id=owner)
            else:
                self.assertEqual((method, path), ('POST', '/repos/owner/project/issues/19/comments'))
                self.remote.post('owner/project', 19, json.loads(data)['body'])
                value = {}
            reply = io.BytesIO(json.dumps(value).encode())
            reply.status = 200 if method == 'GET' else 201
            conn.return_value.getresponse.return_value = reply
        (self.root / 'owner/project').mkdir(parents=True)
        def location(path):
            return self.root if path.startswith('/var/lib/') else Path(path)
        with (patch.object(Path, 'read_text', autospec=True, side_effect=read),
              patch('tools.claim_issue.Path', side_effect=location), patch('tools.claim_issue.gh', github),
              patch('http.client.HTTPSConnection') as conn):
            conn.return_value.request.side_effect = response
            revision = 'd' * 40
            self.assertEqual(command(['example', RUN])['status'], 'diagnostic')
            conn.assert_not_called()
            revision = pin['revision']
            owner = 999
            self.assertEqual(command(['example', RUN])['status'], 'diagnostic')
            self.assertFalse((self.root / 'posts').exists())
            owner = 123
            self.assertEqual(command(['example', RUN])['status'], 'claimed')
            self.assertEqual(command(['example', RUN, '--resume'])['status'], 'resumed')
        self.assertEqual(len(self.remote.log()), 1)

    def test_two_processes_select_same_issue_only_winner_writes(self):
        ctx = multiprocessing.get_context('fork')
        barrier = ctx.Barrier(2, timeout=5)
        output = ctx.SimpleQueue()

        def consumer(run):
            calls = 0
            def read(args):
                nonlocal calls
                value = Remote.read(self.remote, args)
                if '/issues?' in args[-1]:
                    calls += 1
                    if calls == 2:
                        barrier.wait()
                return value
            def post(*args):
                time.sleep(0.2)  # Widen the read/write gap across processes.
                Remote.post(self.remote, *args)
            self.remote.read, self.remote.post = read, post
            output.put(self.invoke(run))

        children = [ctx.Process(target=consumer, args=(c * 32,)) for c in 'ab']
        for child in children:
            child.start()
        for child in children:
            child.join(10)
            self.assertEqual(child.exitcode, 0)
        self.assertEqual(sorted(output.get()['status'] for _ in children), ['claimed', 'lost_race'])
        self.assertEqual(len(self.remote.log()), 1)
        self.assertEqual(len(list(self.state.iterdir())), 1)

    def test_locked_reread_rejects_changed_issue_before_any_write(self):
        for change in (dict(assignees=[USER]), dict(labels=[]), dict(body='Blocked by: #42')):
            with self.subTest(change=change):
                def read(args):
                    if args[-1].endswith('/19'):
                        return self.remote.issue | change
                    if args[-1].endswith('/42'):
                        return dict(state='open')
                    return Remote.read(self.remote, args)
                self.remote.read = read
                self.assertEqual(self.invoke()['status'], 'lost_race')
                self.assertEqual(self.remote.log(), [])
                self.assertEqual(list(self.state.iterdir()), [])

    def test_process_dies_after_remote_commit_before_checkpoint(self):
        def crash():
            def post(*args):
                Remote.post(self.remote, *args)
                os._exit(79)
            self.remote.post = post
            self.invoke()
        child = multiprocessing.get_context('fork').Process(target=crash)
        child.start()
        child.join(10)
        self.assertEqual(child.exitcode, 79)
        self.assertEqual(self.invoke(resume=True), dict(status='resumed', repo='owner/project',
                                                       number=19, run_id=RUN, owner=123))
        self.assertEqual(len(self.remote.log()), 1)

    def test_exit_after_confirmation_without_caller_checkpoint(self):
        def crash():
            if self.invoke()['status'] == 'claimed':
                os._exit(73)
        child = multiprocessing.get_context('fork').Process(target=crash)
        child.start()
        child.join(10)
        self.assertEqual(child.exitcode, 73)
        self.assertEqual(self.invoke(resume=True)['run_id'], RUN)
        self.assertEqual(self.invoke('c' * 32, resume=True)['status'], 'diagnostic')
        self.remote.issue['assignees'] = [USER]
        self.assertEqual(self.invoke(resume=True)['status'], 'diagnostic')
        self.assertEqual(len(self.remote.log()), 1)

    def test_unknown_outcome_without_commit_never_reposts(self):
        attempts = []
        def timeout(*args):
            attempts.append(args)
            raise TimeoutError()
        self.remote.post = timeout
        for run, resume in ((RUN, False), (RUN, True), (RUN, False), ('b' * 32, False)):
            self.assertEqual(self.invoke(run, resume)['status'], 'diagnostic')
        self.assertEqual(len(attempts), 1)

    def test_timeout_after_commit_then_stale_or_ambiguous_evidence(self):
        def uncertain(*args):
            Remote.post(self.remote, *args)
            raise TimeoutError()
        self.remote.post = uncertain
        self.assertEqual(self.invoke()['status'], 'diagnostic')
        self.assertEqual(self.invoke(resume=True)['status'], 'resumed')
        comment = json.loads(self.remote.log()[0])
        for rows in ([], [comment, comment], [comment | dict(user=dict(type='User', id=999))],
                     [comment | dict(id=43)],
                     [comment | dict(body=comment['body'].replace(RUN, 'c' * 32))],
                     [comment | dict(updated_at='later')]):
            with self.subTest(rows=rows):
                self.remote.read = lambda args: [rows] if '/comments?' in args[-1] else Remote.read(self.remote, args)
                self.assertEqual(self.invoke(resume=True)['status'], 'diagnostic')
                self.assertNotEqual(self.invoke('b' * 32)['status'], 'claimed')
                self.assertEqual(len(self.remote.log()), 1)
