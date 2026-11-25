'''Operator-only, one-account synthetic canary. No credential imports or fallback.'''

import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import ssl
import sys
import threading
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlencode, urlsplit
import uuid
import webbrowser

import certifi
import httpx
import jwt

from tools.docker_gate import IMAGE, docker_gate
from tools.siwc_stream import answer, require

ISS = 'https://auth.openai.com'
API = 'https://api.openai.com/v1'
SCOPES = 'openid resource.invoke chatgpt.tokens.use.direct'
NEW = 'dynamic_agent_client'
CLIENT = r'oaiapp_[A-Za-z0-9_-]{1,200}'
CALLBACK_SECONDS = 150


class Denied(ValueError):
    pass


def digest(value):
    return hashlib.sha256(value.encode()).digest()


def callback(target, state, client):
    url = urlsplit(target)
    require(not url.scheme and not url.netloc and not url.fragment and url.path == '/auth/callback')
    pairs = parse_qsl(url.query, keep_blank_values=True, strict_parsing=True, max_num_fields=12)
    q = dict(pairs)
    require(len(q) == len(pairs) and secrets.compare_digest(q['state'], state))
    if q.get('error') == 'access_denied' and 'code' not in q:
        raise Denied()
    require('error' not in q)
    issued = q.get('client_id', client)
    require(re.fullmatch(CLIENT, issued) and client in (NEW, issued) and q['code'])
    return issued, q['code']


async def authorize(host, client, browser):
    state, nonce, verifier = (secrets.token_urlsafe(48) for _ in range(3))
    loop = asyncio.get_running_loop()
    done = loop.create_future()

    async def receive(reader, writer):
        try:
            async with asyncio.timeout(3):
                headers = (await reader.readuntil(b'\r\n\r\n')).decode('ascii').split('\r\n')
                method, target, version = headers[0].split(' ')
                require(method == 'GET' and version in ('HTTP/1.0', 'HTTP/1.1'))
                try:
                    result = callback(target, state, client)
                except Denied:
                    result = None
                writer.write(b'HTTP/1.1 200 OK\r\nCache-Control: no-store\r\nReferrer-Policy: no-referrer\r\nConnection: close\r\n\r\nReturn to the terminal.')
                if not done.done():
                    done.set_result(result)
                await writer.drain()
        except (KeyError, TypeError, ValueError, OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass  # Stray sockets and unrelated requests cannot consume the transaction.
        finally:
            writer.close()

    async with await asyncio.start_server(receive, '127.0.0.1', 0, limit=8192) as server:
        uri = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/auth/callback'
        params = dict(client_id=client, ext_agent_host_id=host, response_type='code', redirect_uri=uri,
                      scope=SCOPES, resource=API, state=state, nonce=nonce, code_challenge_method='S256',
                      code_challenge=base64.urlsafe_b64encode(digest(verifier)).decode().rstrip('='))
        if client == NEW:
            params['agent_name_hint'] = 'setupAIv2 Synthetic Canary'
        def launch():
            try:
                if browser(ISS + '/api/accounts/authorize?' + urlencode(params)):
                    return
            except Exception:
                pass
            try:
                loop.call_soon_threadsafe(lambda: not done.done() and done.set_result(False))
            except RuntimeError:
                pass  # Loop already closed after deadline.

        async with asyncio.timeout(CALLBACK_SECONDS):
            threading.Thread(target=launch, daemon=True).start()
            result = await done
            if result is None:
                raise Denied()
            require(result)
            issued, code = result
    return nonce, dict(grant_type='authorization_code', client_id=issued, code=code,
                       code_verifier=verifier, redirect_uri=uri, resource=API)


async def request(http, method, url, mime='application/json', **kwargs):
    async with asyncio.timeout(45), http.stream(method, url, **kwargs) as response:
        require(response.status_code == 200 and response.headers.get('content-type', '').split(';')[0] == mime
                and response.headers.get('content-encoding', 'identity') == 'identity')
        data = bytearray()
        async for chunk in response.aiter_raw():
            require(len(data) + len(chunk) <= 256 * 1024)
            data.extend(chunk)
        return json.loads(data) if mime == 'application/json' else answer(data)


def identity(tokens, keys, issued, nonce):
    id_token = tokens['id_token']
    kid = jwt.get_unverified_header(id_token)['kid']
    key = jwt.PyJWKSet.from_dict(keys)[kid]
    claims = jwt.decode(id_token, key.key, algorithms=['RS256'], audience=issued, issuer=ISS,
                        options={'require': ['sub', 'iat', 'exp', 'nonce']})
    require(claims['aud'] in (issued, [issued]) and claims.get('azp', issued) == issued
            and claims['sub'] and secrets.compare_digest(claims['nonce'], nonce))
    require(tokens['token_type'].lower() == 'bearer' and sorted(SCOPES.split()) == sorted(tokens['scope'].split())
            and re.fullmatch(r'[A-Za-z0-9._~+/=-]+', tokens['access_token']))
    return digest(claims['sub']).hex()


def create(path, text):
    with open(path, 'x', encoding='ascii', opener=lambda p, f: os.open(p, f, 0o600)) as file:
        file.write(text)


async def canary(root, http, browser, stage=lambda name: None):
    root.mkdir(mode=0o700, exist_ok=True)
    hostfile = root / 'host-id'
    if not hostfile.exists():
        create(hostfile, uuid.uuid4().urn)
    host = hostfile.read_text('ascii')
    u = uuid.UUID(host)
    require(u.version == 4 and u.urn == host)
    reg = root / 'registration.json'
    pending = root / 'pending-client-id'
    if reg.exists():
        client, subject = json.loads(reg.read_text('ascii'))
        require(re.fullmatch(CLIENT, client) and re.fullmatch('[0-9a-f]{64}', subject))
        require(not pending.exists() or pending.read_text('ascii') == client)
    elif pending.exists():
        client, subject = pending.read_text('ascii'), None
        require(re.fullmatch(CLIENT, client))
    else:
        client, subject = NEW, None
    stage('browser_authorization')
    nonce, form = await authorize(host, client, browser)
    if client == NEW:
        create(pending, form['client_id'])  # Not an identity; never use it as proof of authorization.
    stage('code_exchange')
    tokens = await request(http, 'POST', ISS + '/api/accounts/oauth/token', data=form)
    stage('identity_and_permissions')
    keys = await request(http, 'GET', ISS + '/.well-known/jwks.json')
    verified = identity(tokens, keys, form['client_id'], nonce)
    if subject is None:
        create(reg, json.dumps([form['client_id'], verified]))
    else:
        require(subject == verified)
    pending.unlink(missing_ok=True)
    headers = {'Authorization': 'Bearer ' + tokens['access_token']}
    stage('catalog')
    catalog = await request(http, 'GET', API + '/models', headers=headers)
    model = next(m['slug'] for m in catalog['models'] if m['visibility'] == 'list')
    require(isinstance(model, str) and model)
    body = dict(model=model, store=False, stream=True, input=[dict(role='user', content=
                'Return only the exact contents of synthetic answer.py: print(42) followed by one LF. No fences.')])
    stage('responses_post_attempted')
    return await request(http, 'POST', API + '/responses', 'text/event-stream', json=body,
                         headers=headers | {'Accept': 'text/event-stream'})


async def live(stage):
    root = Path.home() / '.setupai-siwc-canary'
    require(not root.resolve().is_relative_to(Path(__file__).resolve().parents[1]))
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    tls.load_verify_locations(certifi.where())
    async with httpx.AsyncClient(verify=tls, trust_env=False, follow_redirects=False, timeout=10,
                                 headers={'Accept-Encoding': 'identity'}) as http:
        data = await canary(root, http, webbrowser.open_new, stage)
    stage('runner')
    proof = broker(data, b'42\n')
    require(proof and proof['status'] == 'passed')
    print('synthetic passed', proof['inspected_image'], proof['container'])
    return 0


def broker(data, expected):
    cli = (Path.home() / 'AppData/Local/Programs/DockerDesktop/resources/bin/docker.exe'
           if os.name == 'nt' else Path('/usr/bin/docker'))
    return docker_gate(SimpleNamespace(image=IMAGE, stdin=b'', expected=expected), data, cli=cli)


def main(args):
    if not args:
        print('not_run')
        return 0
    if args not in (['--preflight'], ['--run-synthetic']):
        return 2
    last, possible = 'preflight', False

    def stage(name):
        nonlocal last, possible
        last = name
        possible |= name == 'responses_post_attempted'

    try:
        proof = broker(b'print(17)\n', b'17\n')
        require(proof and proof['status'] == 'passed')
        print('preflight passed; no sign-in or plan use')
        if args == ['--preflight']:
            return 0
        stage('operator_consent')
        if not sys.stdin.isatty():
            print('interactive terminal required; no sign-in or plan use')
            return 2
        if input('Use your ChatGPT plan for one synthetic request? Type Continue with ChatGPT: ') != 'Continue with ChatGPT':
            print('cancelled; no sign-in or plan use')
            return 2
        return asyncio.run(live(stage))
    except (Exception, KeyboardInterrupt):
        print('synthetic not passed; stage=' + last + '; plan_use=' + ('possible' if possible else 'not_attempted'))
        return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
