'''Single-host claim boundary. Adapters and state directory are operator-owned.'''

from contextlib import contextmanager
import json
import http.client
import os
from pathlib import Path
import re
import subprocess
import sys

from .select_issue import Diagnostic, eligible, gh, pinned_profile, select, unique_object, validate_profile


@contextmanager
def locked(state):
    import fcntl  # The writer is supported only on a local Linux host.
    fd = os.open(state, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        os.close(fd)


def verify(issue_read, body, owner, known_id):
    comment_id = None
    def proof(markers):
        nonlocal comment_id
        if len(markers) != 1:
            return False
        marker = markers[0]
        comment_id = marker['id']
        return (type(comment_id) is int and comment_id > 0 and known_id in (None, comment_id)
                and marker['body'] == body and marker['user']['type'] == 'User'
                and type(marker['user']['id']) is int and marker['user']['id'] == owner
                and isinstance(marker['created_at'], str) and bool(marker['created_at'])
                and marker['updated_at'] == marker['created_at'])
    if not issue_read(verify_claim=proof):
        raise Diagnostic('remote claim ownership or eligibility not confirmed')
    return comment_id


def run_profile(name, run_id, *, resume, profile, github, post, approved_repo, owner, state):
    try:
        if (profile['repo'] != approved_repo or type(owner) is not int or owner <= 0
                or not re.fullmatch('[0-9a-f]{32}', run_id)):
            raise Diagnostic('invalid claim approval or run identity')
        validate_profile(profile)
        selected = None if resume else select([name], {name: profile}, github)
        with locked(state) as directory:
            # The filename is the entire immutable write-ahead intent. No torn JSON.
            intents = {}
            numbers = set()
            for path in state.iterdir():
                match = re.fullmatch(r'([1-9][0-9]*)\.([a-f0-9]{32})\.([1-9][0-9]*)(?:\.([1-9][0-9]*))?', path.name)
                if not match or match[2] in intents or int(match[1]) in numbers:
                    raise Diagnostic('invalid claim journal')
                numbers.add(int(match[1]))
                intents[match[2]] = (int(match[1]), int(match[3]), int(match[4]) if match[4] else None)
            known_id = None
            if resume:
                if run_id not in intents or intents[run_id][1] != owner:
                    raise Diagnostic('no matching claim intent')
                number, _, known_id = intents[run_id]
            else:
                if run_id in intents:
                    raise Diagnostic('run already has intent; use resume')
                if selected['status'] != 'selected':
                    return selected
                number = selected['number']

            def issue_read(**kwargs):
                issue = github(['--method', 'GET', f'repos/{approved_repo}/issues/{number}'])
                if type(issue['number']) is not int or issue['number'] != number:
                    raise Diagnostic('issue identity changed')
                return eligible(github, approved_repo, issue, profile['trusted_user_ids'], **kwargs)

            body = f'<!-- setupai:claim v1 {approved_repo}#{number} {run_id} {owner} -->'
            record = state / f'{number}.{run_id}.{owner}'
            if not resume:
                if not issue_read():
                    return dict(status='lost_race')
                if number in numbers:
                    raise Diagnostic('unresolved earlier claim intent')
                with record.open('xb') as intent:
                    os.fsync(intent.fileno())
                os.fsync(directory)
                post(approved_repo, number, body)
            comment_id = verify(issue_read, body, owner, known_id)
            if known_id is None:
                record.rename(record.with_name(f'{record.name}.{comment_id}'))
                os.fsync(directory)
        return dict(status='resumed' if resume else 'claimed', repo=approved_repo, number=number,
                    run_id=run_id, owner=owner)
    except Diagnostic as error:
        return dict(status='diagnostic', reason=str(error))
    except (ImportError, OSError, ValueError, AttributeError, TypeError, KeyError, subprocess.SubprocessError):
        return dict(status='diagnostic', reason='unconfirmed state; do not retry the write')


def command(argv):
    try:
        if (sys.platform != 'linux' or len(argv) not in (2, 3)
                or not re.fullmatch('[a-zA-Z0-9_-]+', argv[0])
                or not re.fullmatch('[a-f0-9]{32}', argv[1]) or argv[2:] not in ([], ['--resume'])):
            raise Diagnostic('expected PROFILE RUN_ID [--resume] on Linux')
        name, run = argv[:2]
        root = Path('/etc/setupAIv2')
        grants = json.loads((root / 'claim-profiles.json').read_text(), object_pairs_hook=unique_object)
        grant = grants[name]
        if set(grant) != {'repo', 'revision', 'owner'} or type(grant['owner']) is not int or grant['owner'] <= 0:
            raise Diagnostic('invalid claim approval')
        repo, owner = grant['repo'], grant['owner']
        profile = pinned_profile(name, gh, required_pin=dict(repo=repo, revision=grant['revision']))
        validate_profile(profile)
        token = (root / 'claim-credentials' / repo.lower()).read_text().strip()
        if not re.fullmatch('github_pat_[A-Za-z0-9_]+', token):
            raise Diagnostic('invalid claim credential')

        def request(method, path, data=None):
            # Fixed origin, no redirects, retries, shared gh config or stderr with secrets.
            conn = http.client.HTTPSConnection('api.github.com', timeout=60)
            try:
                conn.request(method, path, data, {'Authorization': 'Bearer ' + token,
                             'User-Agent': 'setupAIv2-claim', 'Content-Type': 'application/json'})
                reply = conn.getresponse()
                if reply.status != (200 if method == 'GET' else 201):
                    raise Diagnostic('unconfirmed claim adapter response')
                return json.load(reply, object_pairs_hook=unique_object)
            finally:
                conn.close()

        actor = request('GET', '/user')
        if actor['type'] != 'User' or type(actor['id']) is not int or actor['id'] != owner:
            raise Diagnostic('claim credential owner mismatch')

        def post(repository, number, body):
            request('POST', f'/repos/{repository}/issues/{number}/comments', json.dumps(dict(body=body)))

        return run_profile(name, run, resume=bool(argv[2:]), profile=profile, github=gh, post=post,
                           approved_repo=repo, owner=owner, state=Path('/var/lib/setupAIv2/claims') / repo.lower())
    except Diagnostic as error:
        return dict(status='diagnostic', reason=str(error))
    except (OSError, ValueError, TypeError, AttributeError, KeyError, http.client.HTTPException):
        return dict(status='diagnostic', reason='unconfirmed claim configuration or remote state')


if __name__ == '__main__':
    result = command(sys.argv[1:])
    print(json.dumps(result))
    sys.exit(2 if result['status'] == 'diagnostic' else 0)
