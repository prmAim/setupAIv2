# One synthetic Sign in with ChatGPT request

This standalone canary does not select a project, invoke Codex, or publish a change.
Only run it from the trusted, reviewed worktree, on your own desktop with a system
browser and a private home directory. Synthetic execution also needs trusted local Docker. Do not
run it under a debugger, HTTP recorder or instrumented Python installation.

From the worktree root, install the locked environment with `uv sync --locked`.
`uv run --offline --locked python -m tools.siwc_canary` just prints `not_run`.
`uv run --offline --locked python -m tools.siwc_canary --preflight` runs a fixed
offline smoke test through the Docker CLI, daemon and pinned image. It needs no
typing, makes no OpenAI requests and creates no registration. The synthetic command
also runs that preflight before prompting. After review, the **owner only** can
start the live experiment from an interactive terminal with:

```powershell
uv run --offline --locked python -m tools.siwc_canary --run-synthetic
```

`--offline` applies to uv, not the canary. Confirm the terminal prompt only if
you authorize one request against your ChatGPT plan, using the first visible
model in your account catalog. Use the system browser to choose the account and
authorize the app; never paste a code or token into the terminal. There are no
automatic retries, account switching, refreshes, API keys or paid-key fallback.

For **discovery only**, the owner can instead run:

```powershell
uv run --offline --locked python -m tools.siwc_canary --catalog-only
```

Type `Continue with ChatGPT` only to authorize a fresh browser sign-in. This uses
the same registration and identity checks, including the token-exchange POST,
but stops after `GET /v1/models`. It does not use Docker or POST to `/responses`.
`catalog passed; plan_use=not_attempted` means discovery only, not inference.
On catalog failure share only the terminal summary, for example
`catalog not passed; stage=catalog; plan_use=not_attempted; reason=http_403`.
The reason is a status code or a fixed category, not a provider explanation.

On confirmation the app creates `~/.setupai-siwc-canary/host-id` (stable UUIDv4
URN). A state-validated new registration saves only the issued ID in
`pending-client-id` before code exchange. A failed exchange stops; a later manual
run uses that ID for a fresh browser authorization, never the old code. Pending
state is not a verified identity. After validation it saves `registration.json`
(issued client ID and SHA-256 of verified subject, **not** a token). These files
are local to one installation/account; do not sync them or copy to another
host. Preserve the host ID across sign-ins. Removing only `registration.json`
forgets the local registration, not the server grant. Revoke access in ChatGPT
settings if needed. Tokens stay in process memory. No Codex files are consulted.

## What is being tested

- [Registration and sign-in](https://developers.openai.com/siwc/token-sharing-open-source/sign-in.md): initial dynamic registration, then the issued client and same host on return. Each attempt uses fresh PKCE S256, state, nonce and `http://127.0.0.1:<port>/auth/callback`. Returning sign-in may show an account selector **without** a new provider consent screen. The terminal opt-in is required each time.
- [Signature validation](https://developers.openai.com/siwc/website.md): PyJWT verifies RS256 using rotating keys from the fixed HTTPS JWKS URL `https://auth.openai.com/.well-known/jwks.json` (not a pinned signing key), issuer, issued-client audience, expiry and attempt nonce. A returning verified subject must match. Granted scopes must match `openid resource.invoke chatgpt.tokens.use.direct` exactly: no profile/email or refresh access. Acceptance of this scope set is not live-verified.
- [Inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference.md) and [preview limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations.md): one fixed `POST https://api.openai.com/v1/responses`, input array, no tools, `stream:true`, `store:false`, no redirects, continuation or unsupported `max_output_tokens`. The ceilings are **client-side**, not provider-side billing caps: 4 MiB for model discovery, 256 KiB for other HTTP responses, 10-second HTTPX timeouts and a 45-second total per operation. Unknown events (including separate reasoning items), tool calls, refusals, interruptions and mixed outputs cannot pass.
- Only the nine bytes `print(42)` plus LF (ten bytes total) qualify as the complete contents of `answer.py`. No normalization or host execution. The Docker gate passes the validated bytes over stdin to the local, inspected, non-root Linux/amd64 image `sha256:c5ba3deef00c7758561a0f03c26f44f10589263d40e732b8748e0d1f38f6dd89`, with a bounded `/tmp` tmpfs, no host bind mounts or volumes, no external network and no image pull. No image/runner is not a pass.

**Success** is exit 0 **and** `synthetic passed <inspected-image> <container-id>`.
It requires a completed SSE message matching every text boundary and runner
stdout exactly `42` plus LF. It proves only this synthetic trip through that
account and model. A valid sign-in, HTTP 200, partial text, or fixture test alone
is not success and does not authorize a project or claim-to-draft run.
Failure reports a fixed last stage, not provider text. `plan_use=not_attempted`
means the Responses POST was not attempted; `plan_use=possible` means it was,
even if the stream or runner later failed. Do not retry automatically.

[Eligibility](https://developers.openai.com/siwc/quickstart.md): the local OSS
preview is for eligible Plus/Pro users; workspace policy, granted plan access,
catalog and quota can still prevent inference. Do not assume a commercial or
remote deployment qualifies. The public contract and offline fixtures were
checked, not live registration, model behavior, or account eligibility.
