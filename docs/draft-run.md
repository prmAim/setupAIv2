# Verified draft run (single-file profile)

On a trusted Linux installation, `python -I -B /opt/setupAIv2/claim.py NAME RUN_ID --draft`
continues an existing #29 intent under its repository lock. Plain `--resume` is still
verification only. No credentials, profile or live target are provisioned by this change.

Operator-owned `/etc/setupAIv2/draft-profiles/NAME/policy.json` contains `repo`, `base`,
`claim_profile_revision` (the approved selection revision), `revision` (base commit),
`source` (one top-level Python file), `digest` (SHA-256 of the approved source bytes),
`image` (the immutable image in `tools/docker_gate.py`), and the protected strings
`stdin` and `expected`. Install the reviewed, secret-free base blob under `source/`
beside the policy. No repository mirror, hidden tests, arbitrary commands or shell
configuration are inputs. Only provision a profile for a repository whose existing
push/PR automation is safe for untrusted changes. This tool does not modify workflows.

Use private operator-owned directories, including `/var/lib/setupAIv2/drafts/OWNER/REPO/`
and `/etc/setupAIv2/draft-credentials/OWNER/REPO/` (lowercase repository). The latter
holds separate `openai-api-key` and repo-scoped `publish-token` files. Model access is
opt-in Responses API access, **not** subscription login. The model gets only bounded
issue data and the approved source; there are no tools or filesystem capabilities.
The publisher uses only fixed GitHub API operations. Do not give the runner either
credential. The broker needs the pinned Linux/amd64 image preinstalled and a trusted
local Docker daemon with seccomp. No image pull or host test fallback occurs at run time.

The fixed gate executes the candidate with the supplied input and compares exact stdout
outside the container. It is not a general repository test suite. Report `draft` means
only that this gate passed and the exact open draft was observed; it is not CI approval.
The SQLite journal retains sensitive candidate bytes, prepared head, write intents and
confirmed PR number. Keep it private and intact. An unconfirmed intent with no remote
effect requires operator reconciliation; do not delete it and rerun blindly.

Offline unit tests use a capability-limited remote, not GitHub. For an independent native
runner check, run the trusted `tools/prove_draft_runner.py` with `python -I -B`.
It needs no model or GitHub credential. It reports actual Docker IDs and exits nonzero
if the isolation probe or deliberate output mismatch does not behave as expected.
