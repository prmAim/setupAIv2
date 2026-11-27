import base64
import asyncio
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import gc
import io
import json
import os
import socket
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric import rsa
import httpx
import jwt

from tools import siwc_canary as app
from tools.siwc_stream import ANSWER, answer


def stream():
    loc = dict(item_id='m', output_index=0, content_index=0)
    p = dict(type='output_text', text=ANSWER.decode(), annotations=[])
    m = dict(id='m', type='message', role='assistant', status='completed', content=[p])
    r = dict(id='r', status='completed', output=[m])
    records = [
        ('created', dict(response=r | dict(status='in_progress', output=[]))),
        ('output_item.added', dict(output_index=0, item=m | dict(status='in_progress', content=[]))),
        ('content_part.added', loc | dict(part=p | dict(text=''))),
        ('output_text.delta', loc | dict(delta=p['text'])),
        ('output_text.done', loc | dict(text=p['text'])),
        ('content_part.done', loc | dict(part=p)),
        ('output_item.done', dict(output_index=0, item=m)),
        ('completed', dict(response=r)),
    ]
    return [dict(type='response.' + k, sequence_number=i, **v) for i, (k, v) in enumerate(records)]


def wire(events):
    return ''.join('data: ' + json.dumps(e) + '\n\n' for e in events).encode()


class Listener(unittest.TestCase):
    def test_cli_loop_and_overlapping_callbacks(self):
        errors, loops = [], []

        def browser(url):
            params = dict(parse_qsl(urlsplit(url).query))
            uri = urlsplit(params['redirect_uri'])
            address = ('127.0.0.1', uri.port)
            with ExitStack() as sockets:
                valid = sockets.enter_context(socket.create_connection(address, 2))
                for _ in range(4):
                    sockets.enter_context(socket.create_connection(address, 2))
                query = urlencode(dict(state=params['state'], code='dummy'))
                valid.sendall(f'GET {uri.path}?{query} HTTP/1.1\r\n\r\n'.encode())
                for _ in range(8):  # Race additional accepts against listener close.
                    try:
                        sockets.enter_context(socket.create_connection(address, 0.05))
                    except OSError:
                        break
            return True

        async def local_only(stage, catalog_only):
            loop = asyncio.get_running_loop()
            loop.set_debug(True)  # Report transport creation errors as well as callback failures.
            loops.append(loop)
            loop.set_exception_handler(lambda _, context: errors.append(context['message']))
            async with asyncio.timeout(15):
                for _ in range(20):
                    _, form = await app.authorize('host', 'oaiapp_test', browser)
                    self.assertEqual(form['code'], 'dummy')
            return 0

        with (patch.object(app, 'live', local_only), patch.object(app.sys.stdin, 'isatty', return_value=True),
              patch('builtins.input', return_value='Continue with ChatGPT'),
              patch('sys.unraisablehook', side_effect=lambda event: errors.append(event.exc_type.__name__))):
            self.assertEqual(app.main(['--catalog-only']), 0)
            gc.collect()
        if os.name == 'nt':
            self.assertIsInstance(loops[0], asyncio.SelectorEventLoop)
        self.assertTrue(loops[0].is_closed())
        self.assertEqual(errors, [])


class Canary(unittest.IsolatedAsyncioTestCase):
    loop_factory = asyncio.SelectorEventLoop if os.name == 'nt' else None

    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.keys = dict(keys=[json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(cls.key.public_key())) | dict(kid='k')])

    def tokens(self, **changes):
        claims = dict(sub='account', aud=['oaiapp_test'], iss=app.ISS, nonce='n', iat=int(time.time()),
                      exp=int(time.time()) + 90) | changes
        token = jwt.encode(claims, self.key, algorithm='RS256', headers={'kid': 'k'})
        return dict(id_token=token, scope=app.SCOPES, access_token='fake-bearer', token_type='Bearer')

    def test_identity(self):
        for fields in [dict(iss='foreign'), dict(aud='other'), dict(exp=0), dict(nonce='other'), dict(sub='')]:
            with self.subTest(fields=fields), self.assertRaises((jwt.InvalidTokenError, ValueError)):
                app.identity(self.tokens(**fields), self.keys, 'oaiapp_test', 'n')
        for changes in [dict(scope='openid'), dict(scope=app.SCOPES + ' email'),
                        dict(token_type='other'), dict(id_token='not-a-jwt')]:
            with self.assertRaises((jwt.InvalidTokenError, ValueError)):
                app.identity(self.tokens() | changes, self.keys, 'oaiapp_test', 'n')
        wrong = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        with self.assertRaises(jwt.InvalidSignatureError):
            app.identity(self.tokens(), dict(keys=[json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(wrong.public_key()))
                                                   | dict(kid='k')]), 'oaiapp_test', 'n')

    def test_callback_rejections(self):
        for q in ['state=x&code=c&client_id=oaiapp_test', 'state=s&state=s&code=c',
                  'state=s&code=c', 'state=s&code=c&client_id=dynamic_agent_client',
                  'state=s&error=access_denied&code=c&client_id=oaiapp_test']:
            with self.assertRaises(ValueError):
                app.callback('/auth/callback?' + q, 's', app.NEW)
        with self.assertRaises(ValueError):
            app.callback('/auth/callback?state=s&code=c&client_id=oaiapp_other', 's', 'oaiapp_test')

    def test_stream_rejections(self):
        self.assertEqual(answer(wire(stream())), ANSWER)
        for kind in ['response.failed', 'response.incomplete', 'response.function_call_arguments.delta', 'error']:
            events = stream()
            events[3]['type'] = kind
            with self.assertRaises(ValueError):
                answer(wire(events))
        for mutate in [lambda e: e.pop(), lambda e: e.append(e[-1]),
                       lambda e: e[7]['response']['output'].append(dict(type='function_call')),
                       lambda e: e[2].update(output_index=1), lambda e: e[3].update(delta='print(0)'),
                       lambda e: e[4].update(text='other'), lambda e: e[5].update(item_id='other')]:
            events = stream()
            mutate(events)
            with self.assertRaises((ValueError, StopIteration)):
                answer(wire(events))

    def test_default(self):
        with patch.object(app, 'live', side_effect=AssertionError):
            self.assertEqual(app.main([]), 0)
            self.assertEqual(app.main(['--run-synthetic', '--model=other']), 2)

    def test_sse_framing(self):
        raw = wire(stream())
        for newline in (b'\n', b'\r\n', b'\r'):
            framed = b'\xef\xbb\xbf\n' + raw.replace(b'data: ', b': heartbeat\nid: private\nretry: 15\ndata: ')
            self.assertEqual(answer(framed.replace(b'\n', newline)), ANSWER)
        for tail in (b'data: {', b'data: {}\n', b'event: private\n'):
            with self.assertRaises(app.Rejected):
                answer(raw + tail)
        for pos in (1, 6, 7):
            events = stream()
            # Serialize first to avoid sharing nested fixture objects.
            events = [json.loads(json.dumps(e)) for e in events]
            msg = events[pos]['item'] if pos != 7 else events[pos]['response']['output'][0]
            msg['phase'] = 'commentary'
            with self.assertRaises(app.Rejected) as caught:
                answer(wire(events))
            self.assertEqual(caught.exception.reason, 'forbidden_item')
        self.assertEqual(answer(raw + b'data: [DONE]\n\n'), ANSWER)
        for tail in [b'null', b'[]', b'42', b'false', b'{}', b'[DONE]\n\ndata: [DONE]']:
            with self.assertRaises(ValueError):
                answer(raw + b'data: ' + tail + b'\n\n')
        key = json.dumps('type').encode() + b': '
        nested = key + json.dumps('message').encode()
        with self.assertRaises(ValueError):
            answer(raw.replace(nested, key + json.dumps('function_call').encode() + b', ' + nested))

    async def test_deadline_and_denial(self):
        with patch.object(app, 'CALLBACK_SECONDS', 0.1), self.assertRaises(TimeoutError):
            await app.authorize('host', app.NEW, lambda url: True)

        def deny(url):
            p = dict(parse_qsl(urlsplit(url).query))
            uri = urlsplit(p['redirect_uri'])
            with socket.create_connection((uri.hostname, uri.port), 3) as sock:
                state = p['state']
                sock.sendall(f'GET {uri.path}?state={state}&error=access_denied HTTP/1.1\r\n\r\n'.encode())
            return True

        with self.assertRaises(app.Denied):
            await app.authorize('host', app.NEW, deny)

    def test_preflight_blocks_prompt(self):
        with patch.object(app, 'broker', return_value=None), patch('builtins.input', side_effect=AssertionError), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(app.main(['--run-synthetic']), 1)
        self.assertIn('stage=preflight; plan_use=not_attempted', out.getvalue())

    async def test_two_browser_transactions(self):
        attempts, calls, verifiers = [], [], set()
        exchanges = {
            'grant': (400, b'{"error":"invalid_grant","error_description":"private"}', 'http_400; oauth_error=invalid_grant'),
            'client': (401, b'{"error":"invalid_client"}', 'http_401; oauth_error=invalid_client'),
            'unknown_error': (400, b'{"error":"private"}', 'http_400'),
            'nested_error': (403, b'{"error":{"code":"server_error"}}', 'http_403'),
            'large_error': (400, b'{"error":"invalid_grant"}'.ljust(8193), 'http_400'),
            'bad_error': (500, b'private', 'http_500'),
            'json': (200, b'private', 'invalid_json'), 'shape': (200, b'{}', 'malformed_shape'),
            'exchange_timeout': (0, httpx.ReadTimeout('private'), 'timeout'),
            'exchange_transport': (0, httpx.ConnectError('private'), 'transport'),
        }

        def browser(url):
            auth = urlsplit(url)
            self.assertEqual(auth.scheme + '://' + auth.netloc + auth.path, app.ISS + '/api/accounts/authorize')
            params = dict(parse_qsl(auth.query))
            attempts.append(params)
            uri = urlsplit(params['redirect_uri'])
            self.assertEqual((uri.scheme, uri.hostname, uri.path), ('http', '127.0.0.1', '/auth/callback'))
            q = dict(state=params['state'], code='test-code')
            for stray in ['', '/favicon.ico', uri.path + '?state=wrong&error=access_denied',
                          uri.path + '?state=wrong&code=bad&client_id=oaiapp_fake']:
                with socket.create_connection((uri.hostname, uri.port), 3) as sock:
                    if stray:
                        sock.sendall(f'GET {stray} HTTP/1.1\r\n\r\n'.encode())
                        self.assertEqual(sock.recv(1), b'')
            if len(attempts) == 1:
                q['client_id'] = 'oaiapp_test'
            with socket.create_connection((uri.hostname, uri.port), 3) as sock:
                sock.sendall(f'GET {uri.path}?{urlencode(q)} HTTP/1.1\r\nHost: {uri.netloc}\r\n\r\n'.encode())
                self.assertIn(b'200 OK', sock.recv(512))
            return True

        def dispatch(req):
            calls.append(str(req.url))
            p = attempts[-1]
            if req.url.path.endswith('/token'):
                form = dict(parse_qsl(req.content.decode()))
                self.assertEqual(req.method, 'POST')
                self.assertEqual(req.headers['content-type'], 'application/x-www-form-urlencoded')
                self.assertEqual(set(form), {'grant_type', 'client_id', 'code', 'code_verifier', 'redirect_uri', 'resource'})
                self.assertEqual((form['grant_type'], form['code']), ('authorization_code', 'test-code'))
                self.assertEqual((p['resource'], p['code_challenge_method'], p['response_type']), (app.API, 'S256', 'code'))
                self.assertTrue(form['code_verifier'] not in verifiers)
                verifiers.add(form['code_verifier'])
                self.assertEqual(form['redirect_uri'], p['redirect_uri'])
                self.assertEqual(form['client_id'], 'oaiapp_test')
                self.assertEqual(form['resource'], app.API)
                self.assertEqual(base64.urlsafe_b64encode(app.digest(form['code_verifier'])).decode().rstrip('='),
                                 p['code_challenge'])
                if mode == 'exchange_failure':
                    return httpx.Response(400)
                if mode in exchanges:
                    status, payload, _ = exchanges[mode]
                    if isinstance(payload, Exception):
                        raise payload
                    return httpx.Response(status, headers={'content-type': 'application/json'}, stream=httpx.ByteStream(payload))
                body = self.tokens(nonce=p['nonce'], sub='another' if mode == 'mismatch' else 'account')
            elif req.url.path.endswith('/jwks.json'):
                body = self.keys
            else:
                self.assertEqual(req.headers['authorization'], 'Bearer fake-bearer')
                body = dict(models=[dict(slug='test', visibility='list')])
                if mode == 'catalog_only':
                    body['models'] = [dict(slug=f'private-{i}', visibility='list', description='x' * 4096)
                                      for i in range(65)]
                if mode == 'forbidden':
                    return httpx.Response(403)
                if req.url.path.endswith('/responses'):
                    payload = json.loads(req.content)
                    self.assertEqual(set(payload), {'model', 'input', 'store', 'stream'})
                    self.assertEqual((payload['model'], payload['store'], payload['stream']), ('test', False, True))
                    data = wire(stream()[:-1] if mode == 'interrupted' else stream())
                    if mode in ('admission', 'unknown_code', 'oversize_error'):
                        code = 'insufficient_quota' if mode == 'admission' else 'private'
                        error = json.dumps(dict(error=dict(code=code, message='private'))).encode()
                        return httpx.Response(429, headers={'content-type': 'application/json'}, stream=httpx.ByteStream(
                            error if mode != 'oversize_error' else error.ljust(8193)))
                    if mode in ('failed', 'failed_private', 'incomplete', 'refusal', 'reasoning', 'unknown'):
                        e = stream()[1]
                        if mode.startswith('failed'):
                            e.update(type='response.failed', response=dict(error=dict(message='private',
                                     code='server_error' if mode == 'failed' else 'private')))
                        elif mode == 'incomplete':
                            e.update(type='response.incomplete')
                        elif mode == 'refusal':
                            e.update(type='response.refusal.delta', delta='private')
                        else:
                            e['item']['type'] = mode
                        data = wire([stream()[0], e])
                    if mode == 'timeout':
                        raise httpx.ReadTimeout('private')
                    if mode == 'transport':
                        raise httpx.ReadError('private')
                    if mode in ('html', 'json_media', 'missing', 'other'):
                        media = {'html': 'text/html; charset=UTF-8', 'json_media': 'application/json',
                                 'other': 'text/private'}.get(mode)
                        class Unread(httpx.AsyncByteStream):
                            def __aiter__(self):
                                raise AssertionError('rejected body read')
                        return httpx.Response(200, headers={'content-type': media} if media else {}, stream=Unread())
                    if mode == 'size':
                        data = data.ljust(262145)
                    if mode == 'multiline':
                        data = data.replace(b', "sequence_number"', b',\ndata: "sequence_number"')
                    if mode == 'wrong':
                        data = data.replace(b'print(42)', b'private')
                    return httpx.Response(200, headers={'content-type': 'text/event-stream; charset=utf-8'},
                                          stream=httpx.ByteStream(data))
            return httpx.Response(200, headers={'content-type': 'application/json'},
                                  stream=httpx.ByteStream(json.dumps(body).encode()))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'registration'
            original_client, original_canary, original_broker = httpx.AsyncClient, app.canary, app.broker
            def client(**kwargs):
                return original_client(transport=httpx.MockTransport(dispatch), **kwargs)

            def broker(data, expected):
                executions.append(expected)
                self.assertNotIn(mode, ('catalog_only', 'forbidden'))
                if (data, expected) == (ANSWER, b'42\n'):
                    if mode == 'runner_failure':
                        return dict(status='failed', exit=1)
                    self.assertIn(mode, ('success', 'multiline'))
                else:
                    self.assertEqual((data, expected), (b'print(17)\n', b'17\n'))
                if os.environ.get('SETUPAI_TEST_LOCAL_BROKER') == '1':
                    return original_broker(data, expected)
                return dict(status='passed', exit=0, inspected_image=app.IMAGE, container='a' * 64)

            full = [app.ISS + '/api/accounts/oauth/token', app.ISS + '/.well-known/jwks.json',
                    app.API + '/models', app.API + '/responses']
            with (patch.object(app.webbrowser, 'open_new', browser), patch.object(app.httpx, 'AsyncClient', client),
                  patch.object(app, 'canary', lambda _, *args: original_canary(root, *args)),
                  patch.object(app.sys.stdin, 'isatty', return_value=True), patch.object(app, 'broker', broker),
                  patch('builtins.input', return_value='Continue with ChatGPT')):
                reasons = dict(interrupted='stream_interrupted', wrong='wrong_answer', size='size',
                               admission='http_429; provider_code=insufficient_quota', unknown_code='http_429',
                               oversize_error='http_429', failed='response_failed; provider_code=server_error',
                               failed_private='response_failed', incomplete='response_incomplete', refusal='refusal',
                               reasoning='forbidden_item', unknown='forbidden_item', timeout='timeout', transport='transport')
                reasons.update({mode: 'media_type; media_class=' + category for mode, category in
                                [('html', 'html'), ('json_media', 'json'), ('missing', 'missing'), ('other', 'other')]})
                for mode, count, result in [('exchange_failure', 1, 'stage=code_exchange; plan_use=not_attempted'),
                                            *((m, 1, 'stage=code_exchange; plan_use=not_attempted; reason=' + r)
                                              for m, (_, _, r) in exchanges.items()),
                                            ('success', 4, 'synthetic passed'),
                                            ('mismatch', 2, 'stage=identity_and_permissions; plan_use=not_attempted'),
                                            ('multiline', 4, 'synthetic passed'),
                                            *((m, 4, 'stage=responses_post_attempted; plan_use=possible; reason=' + r)
                                              for m, r in reasons.items()),
                                            ('runner_failure', 4, 'stage=runner; plan_use=possible'),
                                            ('forbidden', 3, 'stage=catalog; plan_use=not_attempted; reason=http_403'),
                                            ('catalog_only', 3, 'catalog passed; plan_use=not_attempted')]:
                    calls.clear()
                    executions = []
                    with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
                        diagnostic = mode in exchanges or mode in ('catalog_only', 'forbidden')
                        rc = await asyncio.to_thread(app.main, ['--catalog-only' if diagnostic else '--run-synthetic'])
                    self.assertEqual(rc, 0 if mode in ('success', 'multiline', 'catalog_only') else 1)
                    if mode in exchanges:
                        self.assertEqual(out.getvalue().splitlines(), ['catalog not passed; ' + result])
                        self.assertFalse(err.getvalue() or executions)
                    self.assertIn(result, out.getvalue())
                    if mode in reasons:
                        self.assertEqual(out.getvalue().splitlines(), ['preflight passed; no sign-in or plan use',
                                                                       'synthetic not passed; ' + result])
                        self.assertEqual(err.getvalue(), '')
                        self.assertEqual(executions, [b'17\n'])  # Preflight only; never run rejected output.
                    self.assertNotIn('fake-bearer', out.getvalue())
                    self.assertEqual(calls, full[:count])
                    if mode == 'exchange_failure':
                        self.assertEqual(executions, [b'17\n'])  # Preflight only.
                        self.assertFalse((root / 'registration.json').exists())
                        self.assertEqual((root / 'pending-client-id').read_text(), 'oaiapp_test')
            self.assertEqual(set(p.name for p in root.iterdir()), {'host-id', 'registration.json'})
            self.assertEqual(json.loads((root / 'registration.json').read_text()),
                             ['oaiapp_test', app.digest('account').hex()])
        first, second = attempts[:2]
        for key in ('state', 'nonce', 'code_challenge'):
            self.assertEqual(len({attempt[key] for attempt in attempts}), len(attempts))
        for attempt in attempts[1:]:
            self.assertTrue(attempt['client_id'] == 'oaiapp_test')
            self.assertTrue(attempt['ext_agent_host_id'] == first['ext_agent_host_id'])
            self.assertNotIn('agent_name_hint', attempt)
        self.assertEqual(first['client_id'], app.NEW)
        self.assertEqual(second['client_id'], 'oaiapp_test')
        self.assertNotIn('agent_name_hint', second)
        self.assertEqual(first['ext_agent_host_id'], second['ext_agent_host_id'])
        for name in ['nonce', 'state', 'code_challenge']:
            self.assertNotEqual(first[name], second[name])

    async def test_http_rejections(self):
        for status, headers, data in [(307, {'location': 'https://example.invalid'}, b''),
                                      (200, {'content-type': 'text/html'}, b''),
                                      (200, {'content-type': 'application/json'}, b' ' * 262145)]:
            calls = []

            def reject(req):
                calls.append(req.url)
                return httpx.Response(status, headers=headers, stream=httpx.ByteStream(data))

            async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as http:
                with self.assertRaises(ValueError):
                    await app.request(http, 'GET', app.API + '/models')
            self.assertEqual(len(calls), 1)

    async def test_catalog_diagnostics(self):
        valid = b'{"models":[{"visibility":"list","slug":"private"}]}'
        boundary = valid.ljust(4 * 1024 * 1024)
        for fixture, headers, expected in [
            (boundary, {}, None), (boundary + b' ', {}, 'size'),
            (valid, {'content-type': 'text/private'}, 'media_type'),
            (valid, {'content-encoding': 'private'}, 'encoding'),
            (b'private', {}, 'invalid_json'), (b'{"data":[]}', {}, 'malformed_shape'),
            (b'{"models":{}}', {}, 'malformed_shape'), (b'{"models":[null]}', {}, 'malformed_shape'),
            (b'{"models":[{"visibility":"list"}]}', {}, 'malformed_shape'),
            (b'{"models":[{"visibility":"hidden"}]}', {}, 'no_eligible'),
            (httpx.ReadTimeout('private'), {}, 'timeout'), (TimeoutError('private'), {}, 'timeout'),
            (httpx.ConnectError('private'), {}, 'transport'),
        ]:
            def dispatch(req):
                if isinstance(fixture, Exception):
                    raise fixture
                return httpx.Response(200, headers={'content-type': 'application/json'} | headers,
                                      stream=httpx.ByteStream(fixture))

            async with httpx.AsyncClient(transport=httpx.MockTransport(dispatch)) as http:
                if expected is None:
                    self.assertEqual(await app.discover(http, {}), 'private')
                else:
                    with self.assertRaises(app.Rejected) as caught:
                        await app.discover(http, {})
                    self.assertEqual(caught.exception.reason, expected)

    def test_catalog_only_requires_consent(self):
        for tty, consent in [(False, 'Continue with ChatGPT'), (True, 'no')]:
            with (patch.object(app.sys.stdin, 'isatty', return_value=tty),
                  patch('builtins.input', return_value=consent) as prompt,
                  patch.object(app, 'broker') as broker, patch.object(app, 'live') as live):
                self.assertEqual(app.main(['--catalog-only']), 2)
                self.assertEqual(prompt.called, tty)
                broker.assert_not_called()
                live.assert_not_called()
