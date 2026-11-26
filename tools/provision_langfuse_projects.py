"""Provision local Langfuse projects with existing per-project keys.

Uses the supported LANGFUSE_INIT_* bootstrap, not direct database writes.
Never deletes volumes or regenerates credentials. Requires the local stack up.
"""

import base64
import json
from pathlib import Path
import subprocess
import urllib.request

from dotenv import dotenv_values, set_key

ROOT = Path(__file__).resolve().parents[1]
INFRA = ROOT / "infra/langfuse"
LOCAL = ROOT / ".scratch/langfuse-provisioning"


def project_access(env: dict, expected: str) -> dict:
    token = base64.b64encode(f"{env['LANGFUSE_PUBLIC_KEY']}:{env['LANGFUSE_SECRET_KEY']}".encode()).decode()
    request = urllib.request.Request("http://127.0.0.1:3000/api/public/projects", headers={"Authorization": "Basic " + token})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=15) as response:
        projects = json.load(response)["data"]
    if len(projects) != 1 or projects[0]["id"] != expected:
        raise RuntimeError(f"Project key scope does not match {expected}")
    return projects[0]


def main():
    LOCAL.mkdir(parents=True, exist_ok=True)
    compose = ["docker", "compose", "--env-file", str(INFRA / ".env"), "-f", str(INFRA / "compose.yaml")]
    infrastructure = dotenv_values(INFRA / ".env")
    project_access(infrastructure, "aim-setupaiv2")
    # Align this repository with its freshly initialized project, preserving OpenAI.
    for name in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        set_key(str(ROOT / ".env"), name, infrastructure[name])
    for project, repo in (("fortune", "Fortune"), ("orbita", "orbita")):
        env = dotenv_values(ROOT.parent / repo / ".env")
        if not all(env.get(name) for name in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")):
            raise RuntimeError(f"Existing {project} keys are missing")
        override = LOCAL / f"{project}.json"
        override.write_text(json.dumps({"services": {"web": {"environment": {
            "LANGFUSE_INIT_PROJECT_ID": project,
            "LANGFUSE_INIT_PROJECT_NAME": project,
            "LANGFUSE_INIT_PROJECT_PUBLIC_KEY": env["LANGFUSE_PUBLIC_KEY"],
            "LANGFUSE_INIT_PROJECT_SECRET_KEY": env["LANGFUSE_SECRET_KEY"],
        }}}}, indent=2), encoding="utf-8")
        try:
            subprocess.run([*compose, "-f", str(override), "up", "-d", "--no-deps", "--pull", "never", "--wait", "--wait-timeout", "180", "web"], check=True)
            project_access(env, project)
            runtime = ROOT / ".scratch/deerflow" / project
            if runtime.is_dir():
                (runtime / "tracing-verified").write_text(f"Verified local project: {project}\n", encoding="utf-8")
            print(f"PASS: Langfuse project {project}, existing keys, isolated API scope", flush=True)
        finally:
            # Return the long-lived web service to its normal configuration.
            subprocess.run([*compose, "up", "-d", "--no-deps", "--pull", "never", "--wait", "--wait-timeout", "180", "web"], check=True)


if __name__ == "__main__":
    main()
