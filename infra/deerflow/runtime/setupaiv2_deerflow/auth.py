"""Refresh only through official Codex; never implement or expose OAuth tokens.

Linux runtime only. Each project owns a dedicated CODEX_HOME, never the user's
desktop CLI directory. JWT expiry is a scheduling hint, not identity validation.
"""

import asyncio
import base64
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import stat
import time

from deerflow.utils.file_io import await_drained


class AuthStopped(RuntimeError):
    """Safe diagnostic; neither raw RPC errors nor credential data escape."""


@dataclass(frozen=True)
class Credential:
    access_token: str = field(repr=False)
    account_id: str
    expires_at: float


def auth_home() -> Path:
    value = os.environ.get("CODEX_HOME")
    if not value or not Path(value).is_absolute():
        raise AuthStopped("codex_auth_home_required")
    return Path(value)


def read_credential(home: Path) -> Credential:
    """Require a private, regular file and managed ChatGPT login; no key fallback."""
    try:
        if home.is_symlink() or home.stat().st_mode & 0o077:
            raise ValueError()
        path = home / "auth.json"
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 65536:
            raise ValueError()
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("auth_mode") not in (None, "chatgpt") or data.get("OPENAI_API_KEY"):
            raise ValueError()
        tokens = data["tokens"]
        token, account = tokens["access_token"], tokens["account_id"]
        if not all(isinstance(v, str) and v.strip() for v in (token, account, tokens["refresh_token"])):
            raise ValueError()
        segment = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
        expiry = claims["exp"]
        if isinstance(expiry, bool) or not isinstance(expiry, (float, int)) or not math.isfinite(expiry):
            raise ValueError()
        return Credential(token, account, float(expiry))
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        raise AuthStopped("codex_auth_missing_or_invalid") from None


@asynccontextmanager
async def auth_lock(home: Path):
    import fcntl

    if home.is_symlink() or home.stat().st_mode & 0o077:
        raise AuthStopped("codex_auth_home_not_private")
    fd = os.open(home / "refresh.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(0.05)
        yield
    finally:
        os.close(fd)


async def _rpc(proc, request):
    proc.stdin.write((json.dumps(request) + "\n").encode())
    await proc.stdin.drain()
    # Bound each frame and total notifications, even from a broken executable.
    for _ in range(64):
        line = await proc.stdout.readline()
        if not line:
            raise AuthStopped("codex_refresh_process_exited")
        value = json.loads(line)
        if value.get("id") == request["id"]:
            if "error" in value or "result" not in value:
                raise AuthStopped("codex_refresh_rpc_failed")
            return value["result"]
    raise AuthStopped("codex_refresh_protocol_failed")


async def refresh_with_codex(home: Path):
    """Only initialize + account/read. Never start login, thread, turn or model/list."""
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(home), "CODEX_HOME": str(home),
           "RUST_LOG": "off", "NO_BROWSER": "1"}
    proc = await asyncio.create_subprocess_exec(
        "/usr/local/bin/codex", "-c", 'cli_auth_credentials_store="file"',
        "-c", 'forced_login_method="chatgpt"', "app-server",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, env=env, cwd=home, limit=65536,
    )
    try:
        async with asyncio.timeout(30):
            await _rpc(proc, {"id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "setupaiv2_auth", "version": "1"}}})
            proc.stdin.write(b'{"method":"initialized","params":{}}\n')
            await proc.stdin.drain()
            result = await _rpc(proc, {"id": 2, "method": "account/read", "params": {"refreshToken": True}})
            if not isinstance(result, dict) or (result.get("account") or {}).get("type") != "chatgpt":
                raise AuthStopped("codex_refresh_not_chatgpt")
    finally:
        if proc.returncode is None:
            proc.kill()
        await await_drained(proc.wait())


async def _transaction(home: Path, margin: int):
    async with auth_lock(home):
        pending = home / "refresh.pending"
        if pending.exists():
            raise AuthStopped("codex_refresh_requires_owner_recovery")
        before = await asyncio.to_thread(read_credential, home)
        if before.expires_at > time.time() + margin:
            return before
        # Durable intent prevents automatic retries after an uncertain rotation.
        with pending.open("x") as stream:
            json.dump({"account_id": before.account_id}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            sync_directory(home)
            await refresh_with_codex(home)
            after = await asyncio.to_thread(read_credential, home)
            if after.account_id != before.account_id:
                raise AuthStopped("codex_refresh_account_changed")
            if after.expires_at <= time.time() + margin:
                raise AuthStopped("codex_refresh_did_not_renew")
            pending.unlink()
            sync_directory(home)
            return after
        except Exception:
            # Preserve intent, including on timeout; never retry a rotating token
            # blindly, nor return RPC/provider messages which may contain secrets.
            raise AuthStopped("codex_refresh_failed_owner_recovery_required") from None


def sync_directory(home: Path):
    """Persist marker creation/removal before advancing the rotation protocol."""
    fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


async def fresh_credential():
    # Drain refresh before releasing the lock even when inference was cancelled.
    # This bounded delay protects an in-progress refresh-token rotation.
    async def bounded():
        async with asyncio.timeout(40):
            return await _transaction(auth_home(), margin=300)

    return await await_drained(bounded())


async def inspect_auth(*, recover: bool = False):
    """Owner-invoked local recovery, never login, refresh or inference."""
    home = auth_home()
    async with asyncio.timeout(2):
        async with auth_lock(home):
            credential = await asyncio.to_thread(read_credential, home)
            if credential.expires_at <= time.time() + 300:
                raise AuthStopped("codex_auth_needs_renewal")
            pending = home / "refresh.pending"
            if pending.exists():
                if not recover:
                    raise AuthStopped("codex_refresh_requires_owner_recovery")
                try:
                    expected = json.loads(pending.read_text())["account_id"]
                except (OSError, ValueError, KeyError, TypeError):
                    raise AuthStopped("codex_recovery_marker_invalid") from None
                if expected != credential.account_id:
                    raise AuthStopped("codex_recovery_account_changed")
                pending.unlink()
                sync_directory(home)
    return "codex_auth_ready_local_only"


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Local credential readiness; no network or tokens in output")
    parser.add_argument("action", choices=["status", "recover"])
    args = parser.parse_args()
    try:
        print(asyncio.run(inspect_auth(recover=args.action == "recover")))
    except (AuthStopped, TimeoutError, OSError) as error:
        code = str(error) if isinstance(error, AuthStopped) else "codex_auth_unavailable_or_busy"
        raise SystemExit(code) from None


if __name__ == "__main__":
    main()
