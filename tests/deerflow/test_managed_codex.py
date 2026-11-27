"""Offline credential lifecycle and native-provider integration acceptance."""

import asyncio
import base64
import json
import os
from pathlib import Path
import sys
import stat
import time

import httpx
import pytest
from langchain_core.messages import HumanMessage, ToolMessage
from deerflow.models import openai_codex_provider as provider
from setupaiv2_deerflow import auth
from setupaiv2_deerflow.codex import ManagedCodexChatModel, CodexStopped


def write_auth(home, *, expires=None, account="fixture-account"):
    home.mkdir(mode=0o700, exist_ok=True)
    home.chmod(0o700)
    payload = base64.urlsafe_b64encode(json.dumps({"exp": expires or time.time() + 3600}).encode()).decode().rstrip("=")
    data = {"auth_mode": "chatgpt", "tokens": {"access_token": "fixture." + payload + ".sig",
            "refresh_token": "fixture-refresh", "account_id": account}}
    path = home / "auth.json"
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    return data


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / "codex"
    write_auth(path)
    monkeypatch.setenv("CODEX_HOME", str(path))
    return path


@pytest.fixture
def rpc_peer(monkeypatch):
    spawn = asyncio.create_subprocess_exec
    processes = []
    mode = ["renew"]

    async def fake_spawn(*command, **kwargs):
        assert command == ("/usr/local/bin/codex", "-c", 'cli_auth_credentials_store="file"',
                           "-c", 'forced_login_method="chatgpt"', "app-server")
        assert "OPENAI_API_KEY" not in kwargs["env"]
        assert "CODEX_API_KEY" not in kwargs["env"]
        kwargs["env"]["FIXTURE_MODE"] = mode[0]
        proc = await spawn(sys.executable, str(Path(__file__).with_name("fake_codex.py")), **kwargs)
        processes.append(proc)
        return proc

    monkeypatch.setattr(auth.asyncio, "create_subprocess_exec", fake_spawn)
    return mode, processes


@pytest.mark.asyncio
async def test_fresh_token_does_not_spawn(home, rpc_peer):
    result = await auth.fresh_credential()
    assert result.account_id == "fixture-account"
    assert rpc_peer[1] == []
    assert result.access_token not in repr(result)


@pytest.mark.asyncio
async def test_expiry_refresh_rpc_rotation_and_concurrent_calls(home, rpc_peer):
    write_auth(home, expires=time.time() - 1)
    results = await asyncio.gather(*(auth.fresh_credential() for _ in range(8)))
    assert len(rpc_peer[1]) == 1
    assert rpc_peer[1][0].returncode is not None
    assert all(r.access_token.endswith("renewed") for r in results)
    assert (home / "rpc-methods").read_text().splitlines() == ["initialize", "initialized", "account/read"]
    assert not (home / "refresh.pending").exists()


@pytest.mark.asyncio
async def test_refresh_intent_directory_is_synced_before_rpc_and_after_removal(home, monkeypatch):
    write_auth(home, expires=time.time() - 1)
    events = []
    fsync = os.fsync

    def sync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            events.append("create" if (home / "refresh.pending").exists() else "remove")
        fsync(fd)

    async def rotate(path):
        assert events == ["create"]
        events.append("rpc")
        write_auth(path)

    monkeypatch.setattr(auth.os, "fsync", sync)
    monkeypatch.setattr(auth, "refresh_with_codex", rotate)
    await auth.fresh_credential()
    assert events == ["create", "rpc", "remove"]


@pytest.mark.asyncio
async def test_failed_intent_directory_sync_prevents_rotation(home, rpc_peer, monkeypatch):
    write_auth(home, expires=time.time() - 1)
    fsync = os.fsync

    def sync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("fixture disk failure")
        fsync(fd)

    monkeypatch.setattr(auth.os, "fsync", sync)
    with pytest.raises(auth.AuthStopped, match="owner_recovery"):
        await auth.fresh_credential()
    assert rpc_peer[1] == []
    assert (home / "refresh.pending").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["error", "exit", "unchanged", "account-change", "apikey"])
async def test_refresh_failure_stops_and_does_not_retry(home, rpc_peer, mode):
    write_auth(home, expires=time.time() - 1)
    rpc_peer[0][0] = mode
    with pytest.raises(auth.AuthStopped) as error:
        await auth.fresh_credential()
    assert "SECRET-FIXTURE" not in str(error.value)
    assert (home / "refresh.pending").exists()
    with pytest.raises(auth.AuthStopped, match="owner_recovery"):
        await auth.fresh_credential()
    assert len(rpc_peer[1]) == 1
    assert rpc_peer[1][0].returncode is not None


@pytest.mark.asyncio
async def test_refresh_timeout_kills_subprocess(home, rpc_peer, monkeypatch):
    write_auth(home, expires=time.time() - 1)
    rpc_peer[0][0] = "hang"
    timeout = asyncio.timeout
    monkeypatch.setattr(auth.asyncio, "timeout", lambda seconds: timeout(0.5 if seconds == 30 else seconds))
    with pytest.raises(auth.AuthStopped):
        await auth.fresh_credential()
    assert rpc_peer[1][0].returncode is not None
    assert (home / "refresh.pending").exists()


@pytest.mark.asyncio
async def test_cancel_drains_rotation_before_unlock(home, monkeypatch):
    write_auth(home, expires=time.time() - 1)
    started, finish = asyncio.Event(), asyncio.Event()

    async def rotate(path):
        started.set()
        await finish.wait()
        write_auth(path)

    monkeypatch.setattr(auth, "refresh_with_codex", rotate)
    task = asyncio.create_task(auth.fresh_credential())
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not (home / "refresh.pending").exists()
    assert (await auth.fresh_credential()).expires_at > time.time() + 300


@pytest.mark.parametrize("damage", ["missing", "malformed", "key", "symlink", "permissions", "expiry"])
def test_invalid_auth_rejected_without_secret(home, damage):
    path = home / "auth.json"
    if damage == "missing":
        path.unlink()
    elif damage == "malformed":
        path.write_text("SECRET-FIXTURE")
    elif damage == "key":
        path.write_text(json.dumps({"OPENAI_API_KEY": "SECRET-FIXTURE"}))
    elif damage == "symlink":
        path.rename(home / "secret")
        path.symlink_to(home / "secret")
    elif damage == "permissions":
        path.chmod(0o644)
    else:
        data = write_auth(home)
        data["tokens"]["access_token"] = "fixture.invalid.sig"
        path.write_text(json.dumps(data))
    with pytest.raises(auth.AuthStopped) as error:
        auth.read_credential(home)
    assert "SECRET-FIXTURE" not in str(error.value)


@pytest.mark.asyncio
async def test_two_projects_have_disjoint_credentials(home, tmp_path):
    other = tmp_path / "other"
    write_auth(other, account="other-account")
    assert auth.read_credential(home).account_id != auth.read_credential(other).account_id


@pytest.mark.asyncio
async def test_local_owner_recovery_never_refreshes(home, rpc_peer):
    marker = home / "refresh.pending"
    marker.write_text(json.dumps({"account_id": "fixture-account"}))
    with pytest.raises(auth.AuthStopped, match="owner_recovery"):
        await auth.inspect_auth()
    write_auth(home, expires=time.time() - 1)
    with pytest.raises(auth.AuthStopped, match="needs_renewal"):
        await auth.inspect_auth(recover=True)
    assert marker.exists()
    write_auth(home, account="other")
    with pytest.raises(auth.AuthStopped, match="account_changed"):
        await auth.inspect_auth(recover=True)
    assert marker.exists()
    write_auth(home)
    assert await auth.inspect_auth(recover=True) == "codex_auth_ready_local_only"
    assert not marker.exists()
    assert rpc_peer[1] == []


@pytest.fixture
def http_peer(monkeypatch):
    client = httpx.AsyncClient
    requests = []
    replies = []

    def respond(request):
        requests.append(request)
        status, response = replies.pop(0)
        return httpx.Response(status, headers={"content-type": "text/event-stream"},
                              text="data: " + json.dumps(response) + "\n\n")

    monkeypatch.setattr(provider.httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs))
    return requests, replies


def event(output):
    return {"type": "response.completed", "response": {"output": output}}


def test_factory_accepts_native_subclass_profile(home):
    from deerflow.config.app_config import AppConfig
    from deerflow.models.factory import create_chat_model

    config = AppConfig(sandbox={"use": "deerflow.sandbox.local:LocalSandboxProvider"}, models=[{
        "name": "codex", "use": "setupaiv2_deerflow.codex:ManagedCodexChatModel",
        "model": "fixture-model", "retry_max_attempts": 1,
        "supports_thinking": True, "supports_reasoning_effort": True,
    }], llm_call={"retry_max_attempts": 1})
    model = create_chat_model("codex", app_config=config, attach_tracing=False)
    assert isinstance(model, ManagedCodexChatModel)
    assert model.model == "fixture-model"
    assert model.retry_max_attempts == 1


@pytest.mark.asyncio
async def test_native_tool_roundtrip_and_token_reload_without_mutation(home, http_peer):
    requests, replies = http_peer
    model = ManagedCodexChatModel(retry_max_attempts=1)
    original = model._access_token
    replies.append((200, event([{"type": "function_call", "name": "echo", "call_id": "one", "arguments": '{"value":"ok"}'}])))
    answer = await model.bind_tools([{"name": "echo", "parameters": {"type": "object"}}], tool_choice="required").ainvoke("fixture")
    write_auth(home, expires=time.time() + 7200)
    replies.append((200, event([{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}])))
    result = await model.ainvoke([HumanMessage("fixture"), answer, ToolMessage("ok", tool_call_id="one")])
    assert result.content == "ok"
    assert requests[0].headers["Authorization"] != requests[1].headers["Authorization"]
    assert model._access_token == original
    payload = json.loads(requests[1].content)
    assert payload["input"][-1]["call_id"] == "one"
    assert payload["stream"] is True and payload["store"] is False
    assert original not in json.dumps(model.to_json())


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_http_failure_no_replay_no_token_or_body(home, http_peer, status):
    requests, replies = http_peer
    replies.append((status, {"secret": "SECRET-FIXTURE"}))
    with pytest.raises(CodexStopped) as error:
        await ManagedCodexChatModel(retry_max_attempts=3).ainvoke("fixture")
    assert len(requests) == 1
    assert "SECRET-FIXTURE" not in str(error.value)


@pytest.mark.asyncio
async def test_invalid_auth_after_construction_stops_before_http(home, http_peer):
    model = ManagedCodexChatModel()
    (home / "auth.json").unlink()
    with pytest.raises(CodexStopped):
        await model.ainvoke("fixture")
    assert not http_peer[0]


@pytest.mark.asyncio
async def test_account_switch_requires_owner_review(home, http_peer):
    model = ManagedCodexChatModel()
    write_auth(home, account="different")
    with pytest.raises(CodexStopped, match="account_changed"):
        await model.ainvoke("fixture")
    assert not http_peer[0]


@pytest.mark.asyncio
async def test_actual_codex_binary_handshake_without_credentials(tmp_path):
    # The test container has --network none. No auth file and no login/turn RPC.
    proc = await asyncio.create_subprocess_exec(
        "/usr/local/bin/codex", "-c", 'cli_auth_credentials_store="file"',
        "-c", 'forced_login_method="chatgpt"', "app-server",
        env={"HOME": str(tmp_path), "CODEX_HOME": str(tmp_path), "RUST_LOG": "off"},
        cwd=tmp_path, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, limit=65536,
    )
    try:
        async with asyncio.timeout(15):
            result = await auth._rpc(proc, {"id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "setupaiv2_offline", "version": "1"}}})
            assert isinstance(result, dict)
            proc.stdin.write(b'{"method":"initialized","params":{}}\n')
            await proc.stdin.drain()
            result = await auth._rpc(proc, {"id": 2, "method": "account/read", "params": {"refreshToken": False}})
            assert result["account"] is None
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()


@pytest.mark.asyncio
async def test_embedded_harness_keeps_auth_failure_as_failure(home, monkeypatch):
    from deerflow.agents.factory import create_deerflow_agent
    from deerflow.agents.middlewares.llm_error_handling_middleware import LLMErrorHandlingMiddleware
    from deerflow.config.app_config import AppConfig
    from langgraph.checkpoint.memory import InMemorySaver

    requests = []
    async def unexpected_http(*args, **kwargs):
        requests.append(True)
        raise AssertionError("No HTTP allowed")
    monkeypatch.setattr(provider.CodexChatModel, "_astream_response", unexpected_http)
    middleware = LLMErrorHandlingMiddleware(app_config=AppConfig(
        sandbox={"use": "deerflow.sandbox.local:LocalSandboxProvider"},
        llm_call={"retry_max_attempts": 1}))
    agent = create_deerflow_agent(ManagedCodexChatModel(), tools=[], middleware=[middleware], checkpointer=InMemorySaver())
    (home / "auth.json").unlink()
    config = {"configurable": {"thread_id": "offline-failure"}}
    with pytest.raises(CodexStopped):
        await agent.ainvoke({"messages": [HumanMessage("fixture")]}, config)
    state = await agent.aget_state(config)
    assert not any(getattr(m, "type", None) == "ai" for m in state.values["messages"])
    assert not requests


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["cancel", "deadline"])
async def test_managed_model_cancellation_closes_real_loopback(home, monkeypatch, ending):
    arrived, disconnected = asyncio.Event(), asyncio.Event()

    async def handle(reader, writer):
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            size = next(int(line.split(b":", 1)[1]) for line in headers.split(b"\r\n") if line.lower().startswith(b"content-length:"))
            await reader.readexactly(size)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n")
            await writer.drain()
            arrived.set()
            assert await reader.read() == b""
            disconnected.set()
        finally:
            writer.close()
            await writer.wait_closed()

    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    if ending == "deadline":
        timeout = asyncio.timeout
        monkeypatch.setattr(asyncio, "timeout", lambda seconds: timeout(0.5 if seconds == 90 else seconds))
    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    monkeypatch.setattr(provider, "CODEX_BASE_URL", f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}")
    task = asyncio.create_task(ManagedCodexChatModel().ainvoke("fixture"))
    try:
        await asyncio.wait_for(arrived.wait(), 5)
        if ending == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(CodexStopped, match="deadline"):
                await task
        await asyncio.wait_for(disconnected.wait(), 2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        server.close()
        await server.wait_closed()
