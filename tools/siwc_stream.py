'''Accept one Responses lifecycle and one reviewed answer.py, never a tool call.'''

import json

ANSWER = b'print(42)\n'


def require(condition):
    if not condition:
        raise ValueError('canary boundary rejected')


def events(raw):
    text = raw.decode().replace('\r\n', '\n')
    require('\r' not in text and text.endswith('\n\n'))
    for frame in text[:-2].split('\n\n'):
        lines = frame.split('\n')
        if all(line.startswith(':') for line in lines):
            continue
        name = None
        if lines[0].startswith('event:'):
            name = lines.pop(0)[6:].strip()
        require(len(lines) == 1 and lines[0].startswith('data:'))
        data = lines[0][5:].strip()
        if data == '[DONE]':
            require(name is None)
            yield None
        else:
            event = json.loads(data)
            require(name is None or name == event['type'])
            yield event


def answer(raw):
    stream = iter(events(raw))
    seq = 0

    def take(kind=None):
        nonlocal seq
        e = next(stream)
        require(e and e['sequence_number'] == seq and (not kind or e['type'] == 'response.' + kind))
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
        require(ANSWER.startswith(text.encode()))
        e = part(take())
    require(text.encode() == ANSWER and e['type'] == 'response.output_text.done' and e['text'] == text)
    p = part(take('content_part.done'))['part']
    require(p['type'] == 'output_text' and p['text'] == text and not p.get('annotations'))
    e = take('output_item.done')
    msg = e['item']
    require(e['output_index'] == 0 and msg['id'] == mid and msg['type'] == 'message'
            and msg['role'] == 'assistant' and msg['status'] == 'completed' and msg['content'] == [p])
    r = take('completed')['response']
    require(r['id'] == rid and r['status'] == 'completed' and r['output'] == [msg]
            and not r.get('error') and not r.get('incomplete_details'))
    require(list(stream) in ([], [None]))
    return text.encode()
