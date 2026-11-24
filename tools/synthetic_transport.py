"""Trusted mock: only the request bytes cross this boundary. No host capabilities."""

import hashlib
import json


def propose(request: bytes) -> bytes:
    message = json.loads(request)
    source, = message["files"]
    edit = dict(source, text=source["text"].replace("pending:", "ready:"))
    if "edit sibling" in message["issue"]:
        edit["path"] = "src/sibling.js"
    return json.dumps({"request": hashlib.sha256(request).hexdigest(), "edits": [edit]}).encode()
