import base64
import json
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


class Canary(unittest.IsolatedAsyncioTestCase):
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
        for changes in [dict(scope='openid'), dict(token_type='other'), dict(id_token='not-a-jwt')]:
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

    async def test_two_browser_transactions(self):
        attempts, calls = [], []

        def browser(url):
            auth = urlsplit(url)
            self.assertEqual(auth.scheme + '://' + auth.netloc + auth.path, app.ISS + '/api/accounts/authorize')
            params = dict(parse_qsl(auth.query))
            attempts.append(params)
            uri = urlsplit(params['redirect_uri'])
            self.assertEqual((uri.scheme, uri.hostname, uri.path), ('http', '127.0.0.1', '/auth/callback'))
            q = dict(state=params['state'], code='test-code')
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
                self.assertEqual(form['redirect_uri'], p['redirect_uri'])
                self.assertEqual(form['client_id'], 'oaiapp_test')
                self.assertEqual(form['resource'], app.API)
                self.assertEqual(base64.urlsafe_b64encode(app.digest(form['code_verifier'])).decode().rstrip('='),
                                 p['code_challenge'])
                body = self.tokens(nonce=p['nonce'])
            elif req.url.path.endswith('/jwks.json'):
                body = self.keys
            else:
                self.assertEqual(req.headers['authorization'], 'Bearer fake-bearer')
                body = dict(models=[dict(slug='test', visibility='list')])
                if req.url.path.endswith('/responses'):
                    payload = json.loads(req.content)
                    self.assertEqual(set(payload), {'model', 'input', 'store', 'stream'})
                    self.assertEqual((payload['model'], payload['store'], payload['stream']), ('test', False, True))
                    data = wire(stream())
                    return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                                          stream=httpx.ByteStream(data))
            return httpx.Response(200, headers={'content-type': 'application/json'},
                                  stream=httpx.ByteStream(json.dumps(body).encode()))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'registration'
            async with httpx.AsyncClient(transport=httpx.MockTransport(dispatch)) as http:
                for _ in range(2):
                    self.assertEqual(await app.canary(root, http, browser), ANSWER)
            self.assertEqual(set(p.name for p in root.iterdir()), {'host-id', 'registration.json'})
            self.assertEqual(json.loads((root / 'registration.json').read_text()),
                             ['oaiapp_test', app.digest('account').hex()])
        self.assertEqual(calls, [app.ISS + '/api/accounts/oauth/token', app.ISS + '/.well-known/jwks.json',
                                 app.API + '/models', app.API + '/responses'] * 2)
        first, second = attempts
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
