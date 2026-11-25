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


def digest(value):
    return hashlib.sha256(value.encode()).digest()


def callback(target, state, client):
    url = urlsplit(target)
    require(not url.scheme and not url.netloc and not url.fragment and url.path == '/auth/callback')
    pairs = parse_qsl(url.query, keep_blank_values=True, strict_parsing=True, max_num_fields=12)
    q = dict(pairs)
    require(len(q) == len(pairs) and secrets.compare_digest(q['state'], state) and 'error' not in q)
    issued = q.get('client_id', client)
    require(re.fullmatch(CLIENT, issued) and client in (NEW, issued) and q['code'])
    return issued, q['code']


async def authorize(host, client, browser):
    state, nonce, verifier = (secrets.token_urlsafe(48) for _ in range(3))
    done = asyncio.get_running_loop().create_future()

    async def receive(reader, writer):
        try:
            async with asyncio.timeout(3):
                headers = (await reader.readuntil(b'\r\n\r\n')).decode('ascii').split('\r\n')
                method, target, version = headers[0].split(' ')
                require(method == 'GET' and version in ('HTTP/1.0', 'HTTP/1.1'))
                result = callback(target, state, client)
                if not done.done():
                    done.set_result(result)
                writer.write(b'HTTP/1.1 200 OK\r\nCache-Control: no-store\r\nReferrer-Policy: no-referrer\r\nConnection: close\r\n\r\nReturn to the terminal.')
                await writer.drain()
        except Exception:
            if not done.done():
                done.set_exception(ValueError('callback rejected'))
        finally:
            writer.close()

    async with await asyncio.start_server(receive, '127.0.0.1', 0, limit=8192) as server:
        uri = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/auth/callback'
        params = dict(client_id=client, ext_agent_host_id=host, response_type='code', redirect_uri=uri,
                      scope=SCOPES, resource=API, state=state, nonce=nonce, code_challenge_method='S256',
                      code_challenge=base64.urlsafe_b64encode(digest(verifier)).decode().rstrip('='))
        if client == NEW:
            params['agent_name_hint'] = 'setupAIv2 Synthetic Canary'
        async with asyncio.timeout(150):
            require(await asyncio.to_thread(browser, ISS + '/api/accounts/authorize?' + urlencode(params)))
            issued, code = await done
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
    require(tokens['token_type'].lower() == 'bearer' and set(SCOPES.split()) <= set(tokens['scope'].split())
            and re.fullmatch(r'[A-Za-z0-9._~+/=-]+', tokens['access_token']))
    return digest(claims['sub']).hex()


def create(path, text):
    with open(path, 'x', encoding='ascii', opener=lambda p, f: os.open(p, f, 0o600)) as file:
        file.write(text)


async def canary(root, http, browser):
    root.mkdir(mode=0o700, exist_ok=True)
    hostfile = root / 'host-id'
    if not hostfile.exists():
        create(hostfile, uuid.uuid4().urn)
    host = hostfile.read_text('ascii')
    u = uuid.UUID(host)
    require(u.version == 4 and u.urn == host)
    reg = root / 'registration.json'
    client, subject = json.loads(reg.read_text('ascii')) if reg.exists() else (NEW, None)
    require((not reg.exists() and client == NEW) or re.fullmatch(CLIENT, client))
    nonce, form = await authorize(host, client, browser)
    tokens = await request(http, 'POST', ISS + '/api/accounts/oauth/token', data=form)
    keys = await request(http, 'GET', ISS + '/.well-known/jwks.json')
    verified = identity(tokens, keys, form['client_id'], nonce)
    if client == NEW:
        create(reg, json.dumps([form['client_id'], verified]))
    else:
        require(subject == verified)
    headers = {'Authorization': 'Bearer ' + tokens['access_token']}
    catalog = await request(http, 'GET', API + '/models', headers=headers)
    model = next(m['slug'] for m in catalog['models'] if m['visibility'] == 'list')
    require(isinstance(model, str) and model)
    body = dict(model=model, store=False, stream=True, input=[dict(role='user', content=
                'Return only the exact contents of synthetic answer.py: print(42) followed by one LF. No fences.')])
    return await request(http, 'POST', API + '/responses', 'text/event-stream', json=body,
                         headers=headers | {'Accept': 'text/event-stream'})


async def live():
    root = Path.home() / '.setupai-siwc-canary'
    require(not root.resolve().is_relative_to(Path(__file__).resolve().parents[1]))
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    tls.load_verify_locations(certifi.where())
    async with httpx.AsyncClient(verify=tls, trust_env=False, follow_redirects=False, timeout=10,
                                 headers={'Accept-Encoding': 'identity'}) as http:
        data = await canary(root, http, webbrowser.open_new)
    cli = (Path.home() / 'AppData/Local/Programs/DockerDesktop/resources/bin/docker.exe'
           if os.name == 'nt' else Path('/usr/bin/docker'))
    proof = docker_gate(SimpleNamespace(image=IMAGE, stdin=b'', expected=b'42\n'), data, cli=cli)
    if not proof or proof['status'] != 'passed':
        print('response validated; runner unavailable or failed (not a pass)')
        return 1
    print('synthetic passed', proof['inspected_image'], proof['container'])
    return 0


def main(args):
    if not args:
        print('not_run')
        return 0
    if args != ['--run-synthetic'] or not sys.stdin.isatty():
        return 2
    try:
        if input('Use your ChatGPT plan for one synthetic request? Type Continue with ChatGPT: ') != 'Continue with ChatGPT':
            return 2
        return asyncio.run(live())
    except (Exception, KeyboardInterrupt):
        print('synthetic not passed; sign-in, permission, transport or response rejected')
        return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
