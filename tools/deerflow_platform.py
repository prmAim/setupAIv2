"""Prepare and operate project-separated DeerFlow; never print credentials.

Runtime data stays in .scratch/deerflow. Source repositories are never mounted.
The initial profile exposes file tools, not shell or dynamically installed tools.
"""

from __future__ import annotations

import argparse
import base64
import io
import http.cookiejar
import json
from pathlib import Path
import secrets
import subprocess
import tarfile
import urllib.error
import urllib.parse
import urllib.request

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
HOME = ROOT / ".scratch/deerflow"
UPSTREAM = HOME / "upstream"
PROJECTS = {"fortune": (ROOT.parent / "Fortune", 2026), "orbita": (ROOT.parent / "orbita", 2027)}
COMPOSE = HOME / "compose.json"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def snapshot(repo: Path, target: Path) -> str:
    """Copy HEAD only, never ignored files, symlinks or local credentials."""
    revision = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    if target.exists():
        return (target.parent / "source-revision.txt").read_text().strip()
    archive = subprocess.check_output(["git", "-C", str(repo), "archive", "--format=tar", revision])
    target.mkdir(parents=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(archive)) as source:
            for member in source.getmembers():
                path = Path(member.name)
                if not member.isfile() or any(p in {".git", ".scratch", ".offline"} for p in path.parts):
                    continue
                if path.name.startswith(".env") and path.name != ".env.example":
                    continue
                if path.suffix.lower() in {".pem", ".key", ".pfx", ".p12"}:
                    continue
                destination = (target / path).resolve()
                if not destination.is_relative_to(target.resolve()):
                    raise ValueError("Unsafe archive member")
                destination.parent.mkdir(parents=True, exist_ok=True)
                data = source.extractfile(member)
                if data is not None:
                    destination.write_bytes(data.read())
        (target.parent / "source-revision.txt").write_text(revision + "\n")
    except Exception:
        raise RuntimeError("Snapshot incomplete; inspect the project runtime directory") from None
    return revision


def prepare() -> None:
    images = json.loads((ROOT / "infra/deerflow/images.json").read_text())
    revision = subprocess.check_output(["git", "-C", str(UPSTREAM), "rev-parse", "HEAD"], text=True).strip()
    if revision != images["commit"]:
        raise RuntimeError("Upstream checkout does not match pinned release")
    base_env = dotenv_values(ROOT / ".env")
    if not base_env.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is missing from the existing local configuration")
    shared = HOME / "shared-skills"
    (shared / "public").mkdir(parents=True, exist_ok=True)
    (shared / "custom").mkdir(exist_ok=True)
    services, networks = {}, {}
    for project, (repo, port) in PROJECTS.items():
        local = HOME / project
        local.mkdir(exist_ok=True)
        (local / "data").mkdir(exist_ok=True)
        source_revision = snapshot(repo, local / "workspace")
        credentials = local / "credentials.json"
        if not credentials.exists():
            write_json(credentials, {"email": f"owner@{project}.example.com", "password": secrets.token_urlsafe(32),
                                     "jwt": secrets.token_urlsafe(48), "frontend": secrets.token_urlsafe(48),
                                     "encryption": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(), "internal": secrets.token_urlsafe(32)})
        private = json.loads(credentials.read_text())
        if len(private["encryption"]) == 43:
            private["encryption"] += "="
            write_json(credentials, private)
        project_env = dotenv_values(repo / ".env")
        env = {"OPENAI_API_KEY": base_env["OPENAI_API_KEY"], "AUTH_JWT_SECRET": private["jwt"],
               "DEER_FLOW_CREDENTIALS_KEY": private["encryption"],
               "DEER_FLOW_INTERNAL_AUTH_TOKEN": private["internal"], "LANGSMITH_TRACING": "false",
               "LANGFUSE_TRACING": "false", "LANGFUSE_BASE_URL": "http://host.docker.internal:3000"}
        for key in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
            if project_env.get(key):
                env[key] = project_env[key]
        # Tracing is enabled only after the project's keys have been verified.
        if (local / "tracing-verified").exists():
            env["LANGFUSE_TRACING"] = "true"
        (local / ".env").write_text("".join(f"{k}={json.dumps(v)}\n" for k, v in env.items()), encoding="utf-8")
        config = {
            "models": [{"name": "gpt-6-luna", "use": "langchain_openai:ChatOpenAI", "model": "gpt-6-luna",
                        "api_key": "$OPENAI_API_KEY", "use_responses_api": True, "max_retries": 1, "timeout": 90}],
            "database": {"backend": "sqlite", "sqlite_dir": "/data/database"},
            "run_events": {"backend": "db"}, "stream_bridge": {"type": "memory"},
            "sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider", "allow_host_bash": False,
                        "mounts": [{"host_path": "/workspace", "container_path": "/mnt/project", "read_only": False}]},
            "skills": {"path": "/shared-skills", "container_path": "/mnt/skills"},
            "memory": {"enabled": True, "injection_enabled": True},
            "auth": {"local": {"allow_registration": False}},
            "agents_api": {"enabled": True},
            "token_budget": {"enabled": True, "max_tokens": 50000},
            "recursion_limit": 100, "max_recursion_limit": 200,
            "tool_groups": [{"name": "file:read"}, {"name": "file:write"}],
            "tools": [{"name": n, "group": "file:write" if n in {"write_file", "str_replace"} else "file:read",
                       "use": f"deerflow.sandbox.tools:{n}_tool"} for n in ["ls", "read_file", "glob", "grep", "write_file", "str_replace"]],
            "lead_prompt_overlay": {"prepend": f"You work only on {project}. The project snapshot is /mnt/project, baseline {source_revision}. Read its AGENTS.md before work. Changes stay in this snapshot; never claim the original repository was modified. Shell execution and external publishing are unavailable. Answer the owner in Russian."},
        }
        write_json(local / "config.json", config)
        if not (local / "extensions.json").exists():
            write_json(local / "extensions.json", {"mcp_servers": {}, "skills": {}})
        nginx = (UPSTREAM / "docker/nginx/nginx.conf").read_text(encoding="utf-8")
        nginx = nginx.replace("gateway:8001", f"{project}-gateway:8001").replace("frontend:3000", f"{project}-frontend:3000")
        (local / "nginx.conf").write_text(nginx, encoding="utf-8")
        common = {"restart": "unless-stopped", "init": True, "networks": [project],
                  "security_opt": ["no-new-privileges:true"], "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}}}
        services[f"{project}-gateway"] = common | {
            "image": images["backend"], "env_file": [str(local / ".env")], "stop_grace_period": "90s",
            "environment": {"DEER_FLOW_HOME": "/data", "DEER_FLOW_PROJECT_ROOT": "/app",
                            "DEER_FLOW_CONFIG_PATH": "/config/config.json", "DEER_FLOW_EXTENSIONS_CONFIG_PATH": "/config/extensions.json",
                            "DEER_FLOW_SKILLS_PATH": "/shared-skills", "AUTH_TRUSTED_PROXIES": f"{project}-nginx"},
            "volumes": [f"{local.as_posix()}/config.json:/config/config.json:ro", f"{local.as_posix()}/extensions.json:/config/extensions.json",
                        f"{local.as_posix()}/data:/data", f"{local.as_posix()}/workspace:/workspace", f"{shared.as_posix()}:/shared-skills:ro"],
            "extra_hosts": ["host.docker.internal:host-gateway"],
            "healthcheck": {"test": ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/health/ready',timeout=5)"], "interval": "10s", "timeout": "6s", "retries": 18, "start_period": "30s"}}
        services[f"{project}-frontend"] = common | {"image": images["frontend"], "environment": {
            "BETTER_AUTH_SECRET": private["frontend"], "DEER_FLOW_INTERNAL_GATEWAY_BASE_URL": f"http://{project}-gateway:8001"}}
        services[f"{project}-nginx"] = common | {"image": images["nginx"], "ports": [f"127.0.0.1:{port}:2026"],
            "volumes": [f"{local.as_posix()}/nginx.conf:/etc/nginx/nginx.conf:ro"],
            "depends_on": {f"{project}-gateway": {"condition": "service_healthy"}, f"{project}-frontend": {"condition": "service_started"}}}
        networks[project] = {}
    write_json(COMPOSE, {"name": "deerflow-platform", "services": services, "networks": networks})
    print("Prepared pinned DeerFlow profiles: Fortune :2026, Orbita :2027. Credentials remain local.")


class LocalSession:
    def __init__(self, port: int):
        self.base = f"http://localhost:{port}"
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(self.cookies))

    def request(self, path: str, data: dict | None = None, *, form: bool = False, timeout: int = 30):
        headers = {}
        payload = None
        if data is not None:
            payload = (urllib.parse.urlencode(data) if form else json.dumps(data)).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
            for cookie in self.cookies:
                if cookie.name == "csrf_token":
                    headers["X-CSRF-Token"] = cookie.value
        request = urllib.request.Request(self.base + path, data=payload, headers=headers)
        with self.opener.open(request, timeout=timeout) as response:
            return json.load(response)


def initialize() -> None:
    """Initialize only our new runtimes; never reset existing accounts."""
    for project, (_, port) in PROJECTS.items():
        private = json.loads((HOME / project / "credentials.json").read_text())
        session = LocalSession(port)
        status = session.request("/api/v1/auth/setup-status")
        if status["needs_setup"]:
            session.request("/api/v1/auth/initialize", {"email": private["email"], "password": private["password"]})
        else:
            session.request("/api/v1/auth/login/local", {"username": private["email"], "password": private["password"]}, form=True)
        session.request("/api/v1/auth/me")
        listing = session.request("/api/agents")
        agents = listing["agents"]
        name = f"{project}-developer"
        if not any(agent["name"] == name for agent in agents):
            session.request("/api/agents", {"name": name, "display_name": f"{project.title()} Developer",
                            "description": f"Isolated {project} development workspace", "model": "gpt-6-luna",
                            "tool_groups": ["file:read", "file:write"], "mcp_plugins": [], "skills": [],
                            "soul": f"Work only on {project} in /mnt/project. Read AGENTS.md. Respond in Russian. Changes are proposals in a snapshot, never direct changes to the original repository."})
        print(f"PASS: {project} authentication and agent {name}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "start", "stop", "status", "initialize"])
    args = parser.parse_args()
    if args.action == "prepare":
        prepare()
        return
    if args.action == "initialize":
        initialize()
        return
    command = {"start": ["up", "-d", "--pull", "never", "--wait", "--wait-timeout", "240"],
               "stop": ["stop", "--timeout", "90"], "status": ["ps"]}[args.action]
    subprocess.run(["docker", "compose", "-f", str(COMPOSE), *command], check=True)


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as error:
        raise SystemExit(f"FAIL: local API HTTP {error.code}; credentials and response body suppressed") from None
