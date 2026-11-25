import base64
import io
import json
import multiprocessing
import os
import subprocess
import sys
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

# Operator fixture pins; never taken from the runner response.
PROJECT = ('d' * 40, (('-m', 'unittest', 'tests.check'),), (('tests/check.py', 'e' * 64),))
STAGED = 'f' * 40


def passed(p, edit):
    from hashlib import sha256
    return dict(status='passed', exit=0, duration_ms=1, image=p.image, inspected_image=p.image,
                container='2' * 64, baseline=p.project_tree, protected=dict(p.protected), tree=STAGED,
                commands=list(map(list, p.commands)), edit_sha256=sha256(edit).hexdigest())


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
            return [[self.issue | dict(comments=len(self.log()))]]
        if path.endswith('/19'):
            return self.issue | dict(comments=len(self.log()))
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
        return comment


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

    def test_verified_execution_keeps_guard_until_effects_finish(self):
        import fcntl
        from tools.claim_issue import run_profile

        calls = []
        def execute(claim, task, recheck):
            calls.append(claim['claim_id'])
            fd = os.open(self.state, os.O_DIRECTORY)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)
            return dict(status='not_run', **claim)

        def resume():
            return run_profile('example', RUN, resume=True, profile=PROFILE,
                               github=self.remote.read, post=self.remote.post, approved_repo='owner/project',
                               owner=123, state=self.state, execute=execute, authenticate=lambda: USER)

        self.assertEqual(self.invoke()['status'], 'claimed')
        self.assertEqual(resume()['status'], 'not_run')
        self.remote.issue['state'] = 'closed'
        self.assertEqual(resume()['status'], 'diagnostic')
        self.assertEqual(calls, [42])

    def test_selection_pin_alone_never_authorizes_a_live_claim(self):
        from tools.claim_issue import command
        with patch.object(Path, 'read_text', side_effect=FileNotFoundError), patch('subprocess.run') as gh:
            self.assertEqual(command(['example', RUN])['status'], 'diagnostic')
            gh.assert_not_called()

    def test_container_with_unknown_or_different_image_is_removed_without_start(self):
        from tools.docker_gate import isolated, IMAGE
        cid = 'a' * 64
        spec = dict(Id=cid, Platform='linux', Mounts=[], Config=dict(Image=IMAGE, User='65534:65534'),
                    HostConfig=dict(Privileged=False, NetworkMode='none', ReadonlyRootfs=True,
                                    CapAdd=[], Devices=[], PidMode='', CapDrop=['ALL']))
        for inspected in (spec, spec | dict(Image='sha256:' + 'b' * 64)):
            with patch('subprocess.run') as control, patch('subprocess.Popen') as start:
                replies = [json.dumps(['name=seccomp,profile=builtin']).encode(), cid.encode(),
                           json.dumps([inspected]).encode(), b'']
                control.side_effect = [subprocess.CompletedProcess([], 0, raw) for raw in replies]
                self.assertIsNone(isolated(Path('/bin/docker'), '', b'', b''))
                start.assert_not_called()
                self.assertEqual(control.call_args.args[0][-3:-1], ['rm', '-f'])

    def test_profile_executes_only_allowlisted_edit_and_reconciles_draft(self):
        from tools.claim_issue import run_profile
        from tools.draft import Draft, Profile
        from hashlib import sha256

        source = b'print(1)'
        (self.root / 'solution.py').write_bytes(source)
        (self.root / 'private').write_text('PRIVATE-MARKER')
        image = 'sha256:' + '1' * 64
        base, head = 'b' * 40, 'c' * 40
        policy = Profile('owner/project', 'main', base, 'solution.py', sha256(source).hexdigest(),
                         image, b'', b'2\n', *PROJECT)
        effects = []
        class Interrupted(BaseException):
            pass
        class Publisher:
            branch = pr = None
            parent = '9' * 40
            commit_tree = STAGED
            commit_head = head
            def commit(_, repo, sha):
                self.assertEqual(sha, head)
                return dict(sha=_.commit_head, parents=[dict(sha=_.parent)], tree=dict(sha=_.commit_tree))
            def prepare(_, claim, profile, data, *, tested_tree):
                self.assertEqual(tested_tree, STAGED)
                self.assertEqual(data, b'print(2)')
                return head
            def ref(_, repo, branch):
                return base if branch == 'main' else _.branch
            def create_ref(_, repo, branch, sha):
                effects.append('ref'); _.branch = sha
                raise Interrupted
            def drafts(_, repo, branch):
                return [_.pr] if _.pr else []
            def create_draft(_, repo, branch, target, number):
                effects.append('draft')
                _.pr = dict(repo=repo, base_repo=repo, branch=branch, base=target, head=head,
                            state='open', draft=True, number=7)
                raise Interrupted
        def propose(snapshot, task):
            self.assertEqual(snapshot, {'solution.py': source})
            return {'solution.py': b'print(2)'}
        def gate(policy, data):
            return passed(policy, data)
        pub = Publisher()
        original_ref = pub.ref
        def invoke(model, runner):
            return run_profile('example', RUN, resume=True, profile=PROFILE, github=self.remote.read,
                               post=self.remote.post, approved_repo=policy.repo, owner=123, state=self.state,
                               execute=Draft(policy, self.root, model, runner, pub, self.root / 'draft.sqlite'), authenticate=lambda: USER)
        self.assertEqual(self.invoke()['status'], 'claimed')
        self.assertEqual(invoke(lambda *_: {'private': b'edit'}, gate)['status'], 'invalid_edit')
        self.assertEqual(invoke(propose, lambda *_: None)['status'], 'not_run')
        self.assertEqual(effects, [])
        self.assertEqual(invoke(propose, gate)['status'], 'invalid_evidence')
        self.assertEqual(effects, [])
        pub.parent = base
        for attr in ('commit_head', 'commit_tree'):
            good = getattr(pub, attr)
            setattr(pub, attr, '8' * 40)
            self.assertEqual(invoke(propose, gate)['status'], 'invalid_evidence')
            self.assertEqual(effects, [])
            setattr(pub, attr, good)
        def revoked(repo, branch):
            if branch != 'main':
                self.remote.issue['state'] = 'closed'
            return original_ref(repo, branch)
        pub.ref = revoked
        self.assertEqual(invoke(propose, gate)['status'], 'diagnostic')
        self.assertEqual(effects, [])
        self.remote.issue['state'] = 'open'
        pub.ref = original_ref
        with self.assertRaises(Interrupted):
            invoke(propose, gate)
        def already_proposed(*_):
            self.fail('resume discarded the checked candidate')
        pub.ref = revoked
        self.assertEqual(invoke(already_proposed, gate)['status'], 'diagnostic')
        self.assertEqual(effects, ['ref'])
        pub.commit_tree = '8' * 40
        self.remote.issue['state'] = 'open'
        pub.ref = original_ref
        self.assertEqual(invoke(already_proposed, gate)['status'], 'invalid_evidence')
        self.assertEqual(effects, ['ref'])
        pub.commit_tree = STAGED
        with self.assertRaises(Interrupted):
            invoke(already_proposed, gate)
        for _ in range(2):
            report = invoke(already_proposed, lambda *_: self.fail('runner required after publication'))
            self.assertEqual(report['status'], 'draft')
            self.assertEqual(report['head'], head)
            self.assertEqual(report['pr'], 'https://github.com/owner/project/pull/7')
            self.assertNotIn('PRIVATE-MARKER', repr(report))
        self.assertEqual(effects, ['ref', 'draft'])
        pub.parent = '9' * 40
        self.assertEqual(invoke(already_proposed, gate)['status'], 'invalid_evidence')
        pub.parent = base
        import sqlite3
        with sqlite3.connect(self.root / 'draft.sqlite') as journal:
            saved, = journal.execute('SELECT evidence FROM proof').fetchone()
            for key, wrong in [('inspected_image', 'sha256:' + '8' * 64), ('protected', {}),
                               ('baseline', '8' * 40), ('tree', '8' * 40), ('legacy', True)]:
                broken = json.loads(saved)
                broken[key] = wrong
                journal.execute('UPDATE proof SET evidence=?', (json.dumps(broken),))
                journal.commit()
                self.assertNotEqual(invoke(already_proposed, gate)['status'], 'draft')
            del broken['legacy'], broken['inspected_image']
            journal.execute('UPDATE proof SET evidence=?', (json.dumps(broken),))
            journal.commit()
            self.assertNotEqual(invoke(already_proposed, gate)['status'], 'draft')
            journal.execute('UPDATE proof SET evidence=?', (saved,))
        pub.pr['number'] = 8
        self.assertEqual(invoke(propose, gate)['status'], 'remote_conflict')
        pub.pr['number'] = 7
        pub.pr['draft'] = False
        self.assertEqual(invoke(propose, gate)['status'], 'remote_conflict')
        pub.pr = None  # An absent read after an attempted write is not permission to replay.
        self.assertEqual(invoke(propose, gate)['status'], 'write_unconfirmed')
        self.assertEqual(effects, ['ref', 'draft'])
        with sqlite3.connect(self.root / 'draft.sqlite') as journal:
            journal.execute('UPDATE candidate SET data=?', (b'other candidate',))
        self.assertEqual(invoke(already_proposed, gate)['status'], 'invalid_evidence')
        self.assertEqual(effects, ['ref', 'draft'])

    def test_installed_adapters_keep_role_credentials_out_of_profile_data(self):
        from hashlib import sha256
        from tools.claim_issue import run_profile
        from tools.draft import Draft, Profile
        from tools.claim_issue import command
        from tools.draft import AdapterFailure
        from tools.draft_github import GitHubDrafts
        source = b'print(1)'
        (self.root / 'a.py').write_bytes(source)
        p = Profile('owner/project', 'main', 'a' * 40, 'a.py', sha256(source).hexdigest(),
                    'sha256:' + '1' * 64, b'', b'2\n', *PROJECT)
        evidence = passed(p, b'print(2)')
        workflow = Draft(p, self.root, lambda *_: {'a.py': b'print(2)'}, lambda *_: evidence,
                         GitHubDrafts(p, self.root, 'publisher-only'), self.root / 'candidate.db')
        self.assertEqual(self.invoke()['status'], 'claimed')
        def invoke():
            return run_profile('example', RUN, resume=True, profile=PROFILE, github=self.remote.read,
                               post=self.remote.post, approved_repo=p.repo, owner=123, state=self.state, execute=workflow,
                               authenticate=lambda: USER)
        with (patch.dict(os.environ, {'TokenGitHub': 'private-marker'}),
              patch('http.client.HTTPSConnection') as conn, patch('subprocess.run') as gh):
            self.assertEqual(command(['example', RUN, '--draft'])['status'], 'not_run')
            conn.assert_not_called()
            model = workflow.propose
            for failure in ('auth_expired', 'quota_exhausted', 'network_blocked'):
                def unavailable(*_):
                    raise AdapterFailure(failure)
                workflow.propose = unavailable
                self.assertEqual(invoke()['status'], failure)
                gh.assert_not_called()
            workflow.propose = model
            gh.side_effect = subprocess.TimeoutExpired('gh', 30)
            self.assertEqual(invoke()['status'], 'network_blocked')
            gh.reset_mock(side_effect=True)
            gh.return_value = subprocess.CompletedProcess([], 0, b'[]')
            self.assertEqual(invoke()['status'], 'remote_conflict')  # base ref absent: no writes
            conn.assert_not_called()
            gh.assert_called_once()
            args, opts = gh.call_args
            self.assertEqual(args[0], ['/usr/bin/gh', 'api', '--hostname', 'github.com', '--method', 'GET',
                                       'repos/owner/project/git/matching-refs/heads/main'])
            self.assertEqual(opts['env']['GH_TOKEN'], 'publisher-only')
            self.assertNotIn('TokenGitHub', opts['env'])
            self.assertNotIn('model-only', repr(opts))

    def test_incomplete_or_changing_comment_pages_block_write_and_recovery(self):
        original = self.remote.read
        comment = dict(id=1, body='discussion')
        for resume in (False, True):
            if resume:
                self.remote.read = original
                self.assertEqual(self.invoke()['status'], 'claimed')
            marker = json.loads(self.remote.log()[0]) if resume else comment
            for first, second in (([[marker]], [[marker]]),
                                  ([[marker], [marker]], [[marker], [marker]]),
                                  ([[marker], [dict(id=2, body='old')]], [[marker], [dict(id=2, body='edit')]])):
                locked = False
                reads = iter([first, second])
                def read(args):
                    nonlocal locked
                    if args[-1].endswith('/19'):
                        locked = True
                        return original(args) | dict(comments=2)
                    if locked and '/comments?' in args[-1]:
                        return next(reads)
                    return original(args)
                self.remote.read = read
                self.assertEqual(self.invoke(resume=resume)['status'], 'diagnostic')
                self.assertEqual(len(self.remote.log()), int(resume))

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
                value = self.remote.post('owner/project', 19, json.loads(data)['body'])
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
            self.assertEqual(command(['example', RUN, '--resume'])['status'], 'verified_only')
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
                return Remote.post(self.remote, *args)
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
        self.assertEqual(self.invoke(resume=True), dict(status='verified_only', repo='owner/project',
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
        self.assertEqual(self.invoke(resume=True)['status'], 'verified_only')
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

    def test_successful_post_id_survives_replacement_before_first_readback(self):
        def post(*args):
            observed = Remote.post(self.remote, *args)
            return observed | dict(id=41)  # GET now sees replacement 42.
        self.remote.post = post
        self.assertEqual(self.invoke()['phase'], 'write_unconfirmed')
        self.assertEqual(self.invoke(resume=True)['status'], 'diagnostic')
        self.assertEqual(len(self.remote.log()), 1)

    def test_concurrent_resume_is_verification_not_execution_permission(self):
        self.assertEqual(self.invoke()['status'], 'claimed')
        ctx = multiprocessing.get_context('fork')
        barrier, output = ctx.Barrier(2, timeout=5), ctx.SimpleQueue()
        def worker():
            barrier.wait()
            output.put(self.invoke(resume=True)['status'])
        children = [ctx.Process(target=worker) for _ in range(2)]
        for child in children:
            child.start()
        for child in children:
            child.join(10)
            self.assertEqual(child.exitcode, 0)
        self.assertEqual([output.get(), output.get()], ['verified_only'] * 2)
        self.assertEqual(len(self.remote.log()), 1)

    def test_comment_posted_between_queue_scans(self):
        original = self.remote.read
        for race in (False, True):
            scans = 0
            def read(args):
                nonlocal scans
                if '/issues?' in args[-1]:
                    scans += 1
                    if scans == 2:
                        if race:
                            self.remote.read = original
                            self.assertEqual(self.invoke('b' * 32)['status'], 'claimed')
                        else:
                            self.remote.post('owner/project', 19, 'discussion')
                return original(args)
            self.remote.read = read
            self.assertEqual(self.invoke()['status'], 'lost_race' if race else 'diagnostic')
            self.assertEqual(len(self.remote.log()), 1)
            self.assertEqual(len(list(self.state.iterdir())), int(race))
            (self.root / 'posts').unlink()

    def test_lock_deadline_is_prewrite(self):
        import fcntl
        fd = os.open(self.state, os.O_RDONLY)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.assertEqual(self.invoke(), dict(status='diagnostic', phase='prewrite', reason='repository lock wait expired'))
        self.assertEqual(self.remote.log(), [])

    def test_isolated_launcher_from_untrusted_cwd(self):
        launcher = Path(__file__).resolve().parents[1] / 'claim.py'
        (self.root / 'tools.py').write_text('raise AssertionError()')
        p = subprocess.run([sys.executable, '-I', '-B', str(launcher), 'bad/name', RUN], cwd=self.root,
                           env={'PYTHONPATH': str(self.root)}, capture_output=True, text=True, timeout=10)
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertEqual(json.loads(p.stdout), dict(status='diagnostic', phase='prewrite',
                         reason='expected PROFILE RUN_ID [--resume] on Linux'))
