# Upstream cancellation backport

`openai_codex_provider.py` is copied unchanged from DeerFlow commit
`88cddf6c56402fd3b938a2f105a03ac9c86b2d0d` (PR #8053, merged 2026-11-12).
Source: https://github.com/bytedance/deer-flow/pull/8053

The MIT license is in `LICENSE.deerflow`. The base is v2.2.0 / `58fc5bd5`.
`install.py` verifies both base and replacement SHA256 before replacing only
this module. Its `await_drained` dependency already exists in the pinned base.
The upstream cancellation regression is preserved in
`tests/deerflow/test_upstream_codex_cancellation.py` under the same MIT license.
The provider/tool/terminal/retry companion tests under `tests/deerflow/upstream/`
are copied unchanged from the same commit. They patch both sync and async HTTP;
the original v2.2.0 sync-only mocks cannot validate the new async implementation.

Codex is the official Linux x86_64 musl release `rust-v0.172.0` (2026-11-24),
https://github.com/openai/codex/releases/tag/rust-v0.172.0.
The release asset SHA256 is verified at build time; no package resolver runs.
Codex is Apache-2.0 licensed: https://github.com/openai/codex/blob/rust-v0.172.0/LICENSE.
It is used only for managed credential refresh, not inference or task execution.
