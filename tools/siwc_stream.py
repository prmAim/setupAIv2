'''Accept one Responses lifecycle and one reviewed answer.py, never a tool call.'''

import json

ANSWER = b'print(42)\n'
DONE = object()
HTTP_REASONS = {i: f'http_{i}' for i in range(100, 600)}
REASONS = frozenset(HTTP_REASONS.values()) | {
    'http_other', 'timeout', 'transport', 'media_type', 'encoding', 'size',
    'invalid_json', 'malformed_shape', 'no_eligible', 'stream_invalid',
    'stream_interrupted', 'response_failed', 'response_incomplete', 'refusal',
    'forbidden_item', 'wrong_answer',
}
PROVIDER_CODES = ('insufficient_quota', 'rate_limit_exceeded', 'server_error', 'model_not_found')
OAUTH_ERRORS = ('invalid_request', 'invalid_client', 'invalid_grant', 'unauthorized_client',
                'unsupported_grant_type', 'invalid_scope', 'invalid_target', 'server_error',
                'temporarily_unavailable', 'access_denied')


class Rejected(ValueError):
    def __init__(self, reason, error=None, *, oauth_error=None):
        super().__init__()
        self.reason = reason if reason in REASONS else 'other'
        code = error.get('code') if isinstance(error, dict) else None
        self.code = code if code in PROVIDER_CODES else None
        self.oauth_error = oauth_error if oauth_error in OAUTH_ERRORS else None


def require(condition):
    if not condition:
        raise ValueError('canary boundary rejected')


def events(raw):
    def unique(pairs):
        obj = dict(pairs)
        require(len(obj) == len(pairs))
        return obj

    text = raw.decode('utf-8-sig').replace('\r\n', '\n').replace('\r', '\n')
    name, lines = None, []
    for line in text.split('\n')[:-1]:  # Only a terminated blank line dispatches data.
        if line:
            field, _, value = line.partition(':')
            value = value.removeprefix(' ')
            if field == 'event':
                require(name is None)
                name = value
            elif field == 'data':
                lines.append(value)
            # Comments, id/retry and unrecognized SSE fields carry no payload.
            continue
        if not lines:
            name = None
            continue
        data = '\n'.join(lines)
        if data == '[DONE]':
            require(name is None)
            yield DONE
        else:
            event = json.loads(data, object_pairs_hook=unique)
            require(isinstance(event, dict))
            require(name is None or name == event['type'])
            yield event
        name, lines = None, []
    if lines or name is not None or not text.endswith('\n'):
        raise Rejected('stream_interrupted')


def answer(raw):
    try:
        return _answer(raw)
    except Rejected:
        raise
    except StopIteration:
        raise Rejected('stream_interrupted') from None
    except (ValueError, KeyError, TypeError, RecursionError):
        raise Rejected('stream_invalid') from None


def _answer(raw):
    stream = iter(events(raw))
    seq = 0

    def take(kind=None):
        nonlocal seq
        e = next(stream)
        require(isinstance(e, dict) and type(e['sequence_number']) is int and e['sequence_number'] == seq)
        event = e['type']
        if event == 'response.failed':
            raise Rejected('response_failed', e['response'].get('error') if isinstance(e.get('response'), dict) else None)
        if event == 'response.incomplete':
            raise Rejected('response_incomplete')
        if event in ('response.refusal.delta', 'response.refusal.done'):
            raise Rejected('refusal')
        if event in ('response.output_item.added', 'response.output_item.done'):
            check_item(e['item'])
        if event in ('response.content_part.added', 'response.content_part.done'):
            check_part(e['part'])
        require(not kind or event == 'response.' + kind)
        seq += 1
        return e

    r = take('created')['response']
    rid = r['id']
    require(rid and r['status'] == 'in_progress' and r['output'] == [])
    e = take()
    if e['type'] == 'response.in_progress':
        r = e['response']
        require(r['id'] == rid and r['status'] == 'in_progress' and r['output'] == [])
        e = take()
    require(e['type'] == 'response.output_item.added' and e['output_index'] == 0)
    msg = e['item']
    mid = msg['id']
    require(mid and msg['type'] == 'message' and msg['role'] == 'assistant'
            and msg['status'] == 'in_progress' and msg['content'] == [])

    def part(e):
        require(e['item_id'] == mid and e['output_index'] == e['content_index'] == 0)
        return e

    p = part(take('content_part.added'))['part']
    require(p['type'] == 'output_text' and p['text'] == '' and not p.get('annotations'))
    text = ''
    e = part(take())
    while e['type'] == 'response.output_text.delta':
        text += e['delta']
        e = part(take())
    require(e['type'] == 'response.output_text.done' and e['text'] == text)
    p = part(take('content_part.done'))['part']
    require(p['type'] == 'output_text' and p['text'] == text and not p.get('annotations'))
    e = take('output_item.done')
    msg = e['item']
    require(e['output_index'] == 0 and msg['id'] == mid and msg['type'] == 'message'
            and msg['role'] == 'assistant' and msg['status'] == 'completed' and msg['content'] == [p])
    r = take('completed')['response']
    require(isinstance(r['output'], list))
    for item in r['output']:
        check_item(item)
    if len(r['output']) > 1:
        raise Rejected('forbidden_item')
    require(r['id'] == rid and r['status'] == 'completed' and r['output'] == [msg]
            and not r.get('error') and not r.get('incomplete_details'))
    require(list(stream) in ([], [DONE]))
    if text.encode() != ANSWER:
        raise Rejected('wrong_answer')
    return ANSWER


def check_part(part):
    if part['type'] == 'refusal':
        raise Rejected('refusal')
    if part['type'] != 'output_text':
        raise Rejected('forbidden_item')


def check_item(item):
    if item['type'] != 'message' or item.get('phase') not in (None, 'final_answer'):
        raise Rejected('forbidden_item')
    require(isinstance(item['content'], list))
    for part in item['content']:
        check_part(part)
