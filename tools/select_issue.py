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
    output = subprocess.run([binary, 'api', '--hostname', 'github.com', *args], env=env,
                            stdin=subprocess.DEVNULL, capture_output=True, check=True, timeout=60)
    return json.loads(output.stdout, object_pairs_hook=unique_object)


def pages(github, path):
    result = github(['--method', 'GET', '--paginate', '--slurp', path])
    if not isinstance(result, list) or not result or not all(isinstance(p, list) for p in result):
        raise Diagnostic('unreadable GitHub pages')
    return [row for page in result for row in page]


WORK_LINKS = '''query($owner:String!, $repo:String!, $number:Int!) {
  repository(owner:$owner, name:$repo) { issue(number:$number) {
    linkedBranches(first:1) { totalCount }
    closedByPullRequestsReferences(first:1, includeClosedPrs:false) { totalCount }
  } }
}'''


def issue_state(row):
    if row['state'] not in ('open', 'closed'):
        raise Diagnostic('unknown issue state')
    return row['state']


def eligible(github, repo, issue, trusted):
    ready = 'ready-for-agent'
    if 'pull_request' in issue or issue_state(issue) != 'open':
        return False
    if issue['repository_url'].lower() != f'https://api.github.com/repos/{repo}'.lower():
        raise Diagnostic('unexpected repository')
    if issue['user']['login'].lower() not in trusted:
        return False
    if not isinstance(issue['assignees'], list) or not isinstance(issue['labels'], list):
        raise Diagnostic('unreadable issue metadata')
    labels = {label['name'].lower() for label in issue['labels']}
    if issue['assignees'] or ready not in labels or labels & {'claimed', 'in-progress'}:
        return False

    path = f"repos/{repo}/issues/{issue['number']}"
    changes = [e for e in pages(github, path + '/events?per_page=100')
               if e['event'] in ('labeled', 'unlabeled') and e['label']['name'].lower() == ready]
    if not changes or changes[-1]['event'] != 'labeled':
        raise Diagnostic('unverified ready label history')
    if changes[-1]['actor']['login'].lower() not in trusted:
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
    for comment in pages(github, path + '/comments?per_page=100'):
        if '<!-- setupai:claim' in comment['body'].lower():
            return False

    owner, name = repo.split('/')
    links = github(['graphql', '-f', 'query=' + WORK_LINKS, '-f', 'owner=' + owner,
                    '-f', 'repo=' + name, '-F', f"number={issue['number']}"])
    if 'errors' in links:
        raise Diagnostic('unreadable work links')
    linked_issue = links['data']['repository']['issue']
    counts = [linked_issue[k]['totalCount'] for k in ('linkedBranches', 'closedByPullRequestsReferences')]
    if any(type(count) is not int or count < 0 for count in counts):
        raise Diagnostic('unreadable work links')
    return not any(counts)


def pinned_profile(name, github):
    location = Path('C:/Program Files/setupAIv2') if os.name == 'nt' else Path('/etc/setupAIv2')
    index = json.loads((location / 'selection-profiles.json').read_text(encoding='utf-8'),
                       object_pairs_hook=unique_object)
    if name not in index:
        raise Diagnostic('unknown profile')
    pin = index[name]
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


def select(argv, profiles, github):
    if len(argv) != 1 or not re.fullmatch(r'[a-zA-Z0-9_-]+', argv[0]):
        raise Diagnostic('unknown profile')
    profile = pinned_profile(argv[0], github) if profiles is None else profiles[argv[0]]
    fields = {'repo', 'base', 'version', 'order', 'trusted_users', 'required_skills', 'required_mcp'}
    if (set(profile) != fields or type(profile['version']) is not int or profile['version'] != 1
            or profile['order'] != 'number_ascending'
            or not re.fullmatch(r'[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*', profile['base'])):
        raise Diagnostic('invalid selection profile')
    users = profile['trusted_users']
    if not isinstance(users, list) or not users or not all(re.fullmatch(r'[A-Za-z0-9-]+', u) for u in users):
        raise Diagnostic('invalid trusted users')
    for key in ('required_skills', 'required_mcp'):
        if profile[key] != []:
            raise Diagnostic('unverified capability: ' + key)
    repo = profile['repo']
    if not REPOSITORY.fullmatch(repo):
        raise Diagnostic('invalid repository')
    queue = pages(github, f'repos/{repo}/issues?state=open&labels=ready-for-agent&per_page=100')
    numbers = [row['number'] for row in queue]
    if any(type(n) is not int or n < 1 for n in numbers) or len(set(numbers)) != len(numbers):
        raise Diagnostic('invalid or unstable issue list')
    trusted = {user.lower() for user in profile['trusted_users']}
    for issue in sorted(queue, key=lambda row: row['number']):
        if eligible(github, repo, issue, trusted):
            return {'status': 'selected', 'repo': repo, 'number': issue['number'],
                    'reason': 'first eligible issue by number_ascending'}
    return {'status': 'no_work'}


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
