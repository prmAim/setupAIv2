"""Read-only issue selection. Run with python -B -m tools.select_issue NAME."""

import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys


class Diagnostic(Exception):
    pass


def unique_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise Diagnostic('ambiguous JSON object')
    return result


REPOSITORY = re.compile(r'[A-Za-z0-9_-]+/[A-Za-z0-9_-][A-Za-z0-9_.-]*')


def gh(args):
    binary = 'C:/Program Files/GitHub CLI/gh.exe' if os.name == 'nt' else '/usr/bin/gh'
    env = os.environ | {'GH_NO_UPDATE_NOTIFIER': '1', 'GH_PROMPT_DISABLED': '1'}
    operation = 'GitHub timeline query' if args[0] == 'graphql' else 'GitHub GET'
    try:
        output = subprocess.run([binary, 'api', '--hostname', 'github.com', *args], env=env,
                                stdin=subprocess.DEVNULL, capture_output=True, check=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise Diagnostic(operation + ' timed out') from None
    except subprocess.CalledProcessError as error:
        status = re.search(rb'\bHTTP ([45][0-9]{2})\b', error.stderr or b'')
        suffix = ' (HTTP ' + status[1].decode('ascii') + ')' if status else ''
        raise Diagnostic(operation + ' failed' + suffix) from None
    return json.loads(output.stdout, object_pairs_hook=unique_object)


def pages(github, path):
    result = github(['--method', 'GET', '--paginate', '--slurp', path])
    if not isinstance(result, list) or not result or not all(isinstance(p, list) for p in result):
        raise Diagnostic('unreadable GitHub pages')
    return [row for page in result for row in page]


def verified_history(github, repo, number, label_id, trusted):
    owner, name = repo.split('/')
    query = Path(__file__).with_name('issue_timeline.graphql').read_text(encoding='utf-8')
    replies = github(['graphql', '--paginate', '--slurp', '-f', 'query=' + query,
                      '-f', 'owner=' + owner, '-f', 'repo=' + name, '-F', f'number={number}'])
    if not isinstance(replies, list) or not replies:
        raise Diagnostic('unreadable issue timeline')
    latest = None
    active = False
    seen = set()
    cursors = set()
    for i, reply in enumerate(replies):
        if 'errors' in reply:
            raise Diagnostic('incomplete issue timeline')
        issue = reply['data']['repository']['issue']
        for field in ('linkedBranches', 'closedByPullRequestsReferences'):
            count = issue[field]['totalCount']
            if type(count) is not int or count < 0:
                raise Diagnostic('unreadable work links')
            active |= count > 0
        items = issue['timelineItems']
        info = items['pageInfo']
        more = i + 1 < len(replies)
        if info['hasNextPage'] is not more or (more and not info['endCursor']) or info['endCursor'] in cursors:
            raise Diagnostic('incomplete timeline pagination')
        cursors.add(info['endCursor'])
        if type(items['nodes']) is not list or type(items['totalCount']) is not int:
            raise Diagnostic('invalid timeline page')
        if i == 0:
            expected = items['totalCount']
        if expected != items['totalCount']:
            raise Diagnostic('timeline changed during selection')
        for event in items['nodes']:
            if not event['id'] or event['id'] in seen:
                raise Diagnostic('unstable timeline events')
            seen.add(event['id'])
            kind = event['__typename']
            if kind not in ('LabeledEvent', 'UnlabeledEvent', 'CrossReferencedEvent'):
                raise Diagnostic('unknown timeline event')
            if kind == 'CrossReferencedEvent':
                source = event.get('source')
                if not isinstance(source, dict) or source.get('__typename') not in ('Issue', 'PullRequest'):
                    raise Diagnostic('unreadable cross-reference source')
                if source['__typename'] == 'PullRequest':
                    if source.get('state') not in ('OPEN', 'CLOSED', 'MERGED'):
                        raise Diagnostic('unknown referenced PR state')
                    active |= source['state'] == 'OPEN'
            else:
                identity = event['label']['id']
                if not isinstance(identity, str) or not identity:
                    raise Diagnostic('unknown historical label')
                if identity == label_id:
                    latest = event
    if len(seen) != expected or not latest or latest['__typename'] != 'LabeledEvent':
        raise Diagnostic('unverified ready label history')
    actor = latest.get('actor')
    if not actor or actor.get('__typename') != 'User' or type(actor.get('databaseId')) is not int or actor['databaseId'] <= 0:
        raise Diagnostic('unknown ready label actor')
    return not active and actor['databaseId'] in trusted


def issue_state(row):
    if row['state'] not in ('open', 'closed'):
        raise Diagnostic('unknown issue state')
    return row['state']


def eligible(github, repo, issue, trusted, *, verify_claim=None, read_comments=pages):
    ready = 'ready-for-agent'
    if 'pull_request' in issue or issue_state(issue) != 'open':
        return False
    if issue['repository_url'].lower() != f'https://api.github.com/repos/{repo}'.lower():
        raise Diagnostic('unexpected repository')
    author = issue['user']
    if type(author['id']) is not int or author['id'] <= 0:
        raise Diagnostic('unknown issue author')
    if author['type'] != 'User' or author['id'] not in trusted:
        return False
    if not isinstance(issue['assignees'], list) or not isinstance(issue['labels'], list):
        raise Diagnostic('unreadable issue metadata')
    labels = {label['name'].lower() for label in issue['labels']}
    if issue['assignees'] or ready not in labels or labels & {'claimed', 'in-progress'}:
        return False

    path = f"repos/{repo}/issues/{issue['number']}"
    ids = [label['node_id'] for label in issue['labels'] if label['name'].lower() == ready]
    if len(ids) != 1 or not isinstance(ids[0], str) or not ids[0]:
        raise Diagnostic('unknown current ready label')
    if not verified_history(github, repo, issue['number'], ids[0], trusted):
        return False
    blockers = pages(github, path + '/dependencies/blocked_by?per_page=100')
    summary = issue['issue_dependencies_summary']
    total, opened = summary['total_blocked_by'], summary['blocked_by']
    if (type(total) is not int or type(opened) is not int or total != len(blockers)
            or opened != sum(issue_state(b) == 'open' for b in blockers)):
        raise Diagnostic('incomplete dependency data')
    if opened:
        return False
    body = issue['body'] if issue['body'] is not None else ''
    for declaration in re.findall(r'^\s*Blocked by:([^\r\n]*)', body, re.I | re.M):
        if not re.fullmatch(r'\s*#[1-9][0-9]*(?:\s*,\s*#[1-9][0-9]*)*\s*', declaration):
            raise Diagnostic('unverified fallback dependency')
        for number in re.findall(r'#([0-9]+)', declaration):
            blocker = github(['--method', 'GET', f'repos/{repo}/issues/{number}'])
            if 'pull_request' in blocker:
                raise Diagnostic('dependency is not an issue')
            if issue_state(blocker) == 'open':
                return False
    markers = [comment for comment in read_comments(github, path + '/comments?per_page=100')
               if '<!-- setupai:claim' in comment['body'].lower()]
    return not markers if verify_claim is None else verify_claim(markers)


def pinned_profile(name, github, *, required_pin=None):
    location = Path('C:/Program Files/setupAIv2') if os.name == 'nt' else Path('/etc/setupAIv2')
    try:
        index = json.loads((location / 'selection-profiles.json').read_text(encoding='utf-8'),
                           object_pairs_hook=unique_object)
    except (OSError, ValueError):
        raise Diagnostic('approved profile index unavailable or invalid') from None
    if name not in index:
        raise Diagnostic('unknown profile')
    pin = index[name]
    if required_pin is not None and pin != required_pin:
        raise Diagnostic('claim approval does not match selection pin')
    if (set(pin) != {'repo', 'revision'} or not REPOSITORY.fullmatch(pin['repo'])
            or not re.fullmatch(r'[0-9a-f]{40}', pin['revision'])):
        raise Diagnostic('invalid profile pin')
    path = f"repos/{pin['repo']}/contents/.agent/selection.json?ref={pin['revision']}"
    blob = github(['--method', 'GET', path])
    if blob['type'] != 'file' or blob['encoding'] != 'base64':
        raise Diagnostic('unreadable pinned profile')
    raw = base64.b64decode(blob['content'].replace('\n', ''), validate=True)
    profile = json.loads(raw, object_pairs_hook=unique_object)
    if profile['repo'] != pin['repo']:
        raise Diagnostic('profile repository mismatch')
    return profile


def validate_profile(profile):
    fields = {'repo', 'base', 'version', 'order', 'trusted_user_ids', 'required_skills', 'required_mcp'}
    if (set(profile) != fields or type(profile['version']) is not int or profile['version'] != 2
            or profile['order'] != 'number_ascending'
            or not re.fullmatch(r'[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*', profile['base'])):
        raise Diagnostic('invalid selection profile')
    users = profile['trusted_user_ids']
    if not isinstance(users, list) or not users or not all(type(u) is int and u > 0 for u in users):
        raise Diagnostic('invalid trusted users')
    for key in ('required_skills', 'required_mcp'):
        if profile[key] != []:
            raise Diagnostic('unverified capability: ' + key)
    repo = profile['repo']
    if not REPOSITORY.fullmatch(repo):
        raise Diagnostic('invalid repository')


def select(argv, profiles, github, *, read_comments=pages, on_change=None):
    if len(argv) != 1 or not re.fullmatch(r'[a-zA-Z0-9_-]+', argv[0]):
        raise Diagnostic('unknown profile')
    profile = pinned_profile(argv[0], github) if profiles is None else profiles[argv[0]]
    validate_profile(profile)
    repo = profile['repo']
    queue_path = f'repos/{repo}/issues?state=open&labels=ready-for-agent&per_page=100'
    queue = pages(github, queue_path)
    numbers = [row['number'] for row in queue]
    if any(type(n) is not int or n < 1 for n in numbers) or len(set(numbers)) != len(numbers):
        raise Diagnostic('invalid or unstable issue list')
    trusted = set(profile['trusted_user_ids'])
    result = {'status': 'no_work'}
    for issue in sorted(queue, key=lambda row: row['number']):
        if eligible(github, repo, issue, trusted, read_comments=read_comments):
            result = {'status': 'selected', 'repo': repo, 'number': issue['number'],
                      'reason': 'first eligible issue by number_ascending'}
            break
    after = pages(github, queue_path)
    if queue != after:
        if on_change is not None:
            return on_change(queue, after)
        raise Diagnostic('queue changed during selection')
    return result


def main(argv=None, *, profiles=None, github=gh):
    try:
        result = select(sys.argv[1:] if argv is None else argv, profiles, github)
    except Diagnostic as exc:
        result = {'status': 'diagnostic', 'reason': str(exc)}
    except (OSError, ValueError, AttributeError, TypeError, KeyError, subprocess.SubprocessError):
        result = {'status': 'diagnostic', 'reason': 'unreadable profile or GitHub data'}
    print(json.dumps(result))
    return 2 if result['status'] == 'diagnostic' else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
