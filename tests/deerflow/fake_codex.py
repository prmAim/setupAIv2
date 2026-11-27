"""Offline stdio peer for managed-auth protocol tests; never opens a socket."""

import base64
import json
import os
from pathlib import Path
import sys
import time

home = Path(os.environ["CODEX_HOME"])
mode = os.environ.get("FIXTURE_MODE", "renew")
for line in sys.stdin:
    message = json.loads(line)
    with (home / "rpc-methods").open("a") as log:
        log.write(message["method"] + "\n")
    if message["method"] == "initialize":
        print(json.dumps({"id": message["id"], "result": {"userAgent": "fixture"}}), flush=True)
    elif message["method"] == "account/read":
        assert message["params"] == {"refreshToken": True}
        if mode == "exit":
            sys.exit(1)
        if mode == "error":
            print(json.dumps({"id": message["id"], "error": {"message": "SECRET-FIXTURE"}}), flush=True)
            continue
        if mode == "hang":
            time.sleep(60)
        if mode != "unchanged":
            path = home / "auth.json"
            data = json.loads(path.read_text())
            payload = base64.urlsafe_b64encode(json.dumps({"exp": time.time() + 3600}).encode()).decode().rstrip("=")
            data["tokens"]["access_token"] = "fixture." + payload + ".renewed"
            data["tokens"]["refresh_token"] = "fixture-renewed"
            if mode == "account-change":
                data["tokens"]["account_id"] = "other-account"
            tmp = home / "new-auth"
            tmp.write_text(json.dumps(data))
            tmp.chmod(0o600)
            tmp.replace(path)
        result = {"account": {"type": "apiKey" if mode == "apikey" else "chatgpt"}}
        print(json.dumps({"id": message["id"], "result": result}), flush=True)
