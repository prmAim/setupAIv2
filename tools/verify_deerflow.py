"""Check local DeerFlow authentication, separation and optional paid model calls."""

import argparse
import json
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone

from deerflow_platform import COMPOSE, HOME, PROJECTS, LocalSession, write_json


def login(project, port):
    session = LocalSession(port)
    credentials = json.loads((HOME / project / "credentials.json").read_text())
    session.request("/api/v1/auth/setup-status")
    session.request("/api/v1/auth/login/local", {"username": credentials["email"], "password": credentials["password"]}, form=True)
    session.request("/api/v1/auth/me")
    return session


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Run one synthetic agent task per project; auxiliary model calls may occur")
    args = parser.parse_args()
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "projects": {}}
    sessions = {p: login(p, port) for p, (_, port) in PROJECTS.items()}
    for project, session in sessions.items():
        evidence = {"authentication": "PASS"}
        agents = session.request("/api/agents")["agents"]
        assert any(a["name"] == f"{project}-developer" for a in agents)
        thread = session.request("/api/threads", {"assistant_id": f"{project}-developer", "metadata": {"purpose": "synthetic-isolation-check"}})
        thread_id = thread["thread_id"]
        session.request(f"/api/threads/{thread_id}")
        other = sessions["orbita" if project == "fortune" else "fortune"]
        try:
            other.request(f"/api/threads/{thread_id}")
        except urllib.error.HTTPError as error:
            assert error.code in (403, 404), error.code
            evidence["cross_project_thread"] = "PASS"
        else:
            raise AssertionError("Other project could read the thread")
        # A valid cookie from one runtime must not authenticate against the other.
        cookies = "; ".join(f"{c.name}={c.value}" for c in session.cookies)
        request = urllib.request.Request(other.base + "/api/v1/auth/me", headers={"Cookie": cookies})
        try:
            urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=10)
        except urllib.error.HTTPError as error:
            assert error.code == 401, error.code
            evidence["cross_project_session"] = "PASS"
        else:
            raise AssertionError("Other runtime accepted this project's cookie")
        probe = """
from pathlib import Path
from deerflow.config import get_app_config
from deerflow.sandbox.tools import validate_local_tool_path
config=get_app_config()
assert config.sandbox.allow_host_bash is False
assert not Path('/var/run/docker.sock').exists()
assert Path('/workspace/AGENTS.md').is_file()
validate_local_tool_path('/mnt/project/AGENTS.md', {}, read_only=True)
for path in ['/proc/self/environ', '/data/database/deerflow.db', '/mnt/project/../../data', '/mnt/other-project/AGENTS.md']:
    try: validate_local_tool_path(path, {}, read_only=True)
    except (PermissionError, ValueError): pass
    else: raise AssertionError('Unexpected access: '+path)
print('PASS: file-tool path gates')
"""
        result = subprocess.run(["docker", "compose", "-f", str(COMPOSE), "exec", "-T", f"{project}-gateway", "/app/backend/.venv/bin/python", "-"], input=probe, text=True, capture_output=True, timeout=60)
        if result.returncode:
            (HOME / f"{project}-probe-error.log").write_text(result.stderr, encoding="utf-8")
            raise RuntimeError(f"{project} path probe failed; inspect local error log")
        evidence["file_tool_paths"] = "PASS"
        evidence["thread_id"] = thread_id
        evidence["model"] = "NOT_RUN"
        if args.live:
            try:
                result = session.request("/api/runs/wait", {
                    "assistant_id": f"{project}-developer",
                    "config": {"configurable": {"thread_id": thread_id}, "recursion_limit": 100},
                    "input": {"messages": [{"role": "user", "content": "Synthetic connection check. Do not use any tools or read files. Reply exactly DEERFLOW_OK."}]},
                    "context": {"model_name": "gpt-6-luna", "thinking_enabled": False}}, timeout=150)
                messages = result.get("messages", [])
                replies = [m.get("content", "") for m in messages if m.get("type", m.get("role")) in ("ai", "assistant")]
                evidence["model"] = "PASS" if any("DEERFLOW_OK" in str(r) for r in replies) else "FAIL"
                write_json(HOME / f"{project}-synthetic-run.json", result)
            except Exception as error:
                evidence["model"] = f"FAIL: {type(error).__name__}"
        report["projects"][project] = evidence
        print(project, json.dumps(evidence))
        write_json(HOME / "acceptance.json", report)
    if args.live and any(p["model"] != "PASS" for p in report["projects"].values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
