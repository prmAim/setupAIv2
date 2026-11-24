# #10 solver preflight: blocked, not a scored attempt

Local branch `fix/t10-solver-gate`, starting at `7df4e47`. No real solver turn or golden task run. Only the trusted harness prepares the task snapshot. For `run --modes codex`, the graph has exactly one node: `START -> solver-preflight -> END`. The eval adapter has no process-launch implementation. Selecting a binary, model or sandbox cannot enable it. This is a refusal to run, not a sandbox for other host programs.

## Pinned capability inspection (Windows, 2026-11-24)

Using `C:/Git/_wf/codex-pinned/node_modules/.bin/codex.cmd` as `$cli`:

```powershell
& $cli --version
& $cli exec --help
& $cli app-server generate-json-schema --experimental --out $schemaDir
```

Version `codex-cli 0.171.0`; native executable SHA256 `CEFEA4FFEE1FFF04983A99C0DDA99944D8B380E891E740A28B32F4B815A6E0B7`. Generated `v2/TurnStartParams.json` SHA256 `B64078D4310CB1FA591C7F63344BED6B8C08D216F9F81D86C00045136187A2B6`. This schema, including experimental fields, does not advertise `allowedTools` or `toolPolicy`. Thread start's `dynamicTools` is not evidence that built-ins are removed. `exec --help` advertises output schema and sandbox modes, not an all-tools-off switch. The turn sandbox policy's readOnly arm has networkAccess; workspaceWrite has writableRoots, not a read allowlist. Neither `permissions` profile selection nor disabling individual features proves all host reads, hooks, MCP, search and test-capable shell routes are denied. No native configuration qualified for a live canary. No inference from model compliance, cwd or Git history.

## Reproduction and observed result

From the branch worktree with the project venv: `python -m unittest discover -s tests -p test_eval_harness.py -v` (22 passed); `python -m unittest discover -s tests` (105 passed). The benign marker executable in `ControlMatrixTests` was never invoked. Gold/noop/cheat fixture controls retained their expected outcomes.

Called `eval_harness.main()` with `run --tasks 1 --modes codex` against the synthetic `test_eval_harness.build_fixture` source and private temporary roots, not orbita. Captured aggregate: `docs/validation/t10-synthetic-preflight.json`. Exit 3, not_run=1, failed=resolved=0, incomplete=true, mismatches empty. Per-case reason `solver_isolation_unverified`, engine calls/iterations 0/0, hidden exit null. The artifact's fixture checkout was temporary; it is not a real task report.

## Minimum facility before replacing the denial

Keep the subscription and authentication on the host. Require a pinned, reviewed host transport that **enforces** an empty tool dispatcher (including built-ins, hosted tools, plugins, subagents and MCP) before any model turn, and has no implicit workspace/context loading, hooks or resume state. Bind the preflight to the exact binary and effective session policy; unknown versions, routes, config drift or missing evidence must return not_run, with no fallback to exec. A caller flag or a transcript showing no calls is not evidence.

The trusted harness should send only the issue and a byte-bounded, path-sorted bundle from the pinned baseline's explicitly allowlisted regular source blobs. No checkout traversal, Git objects beyond selected blobs, tests, grader data, auth or local config. The only response is a bounded schema of proposed replacements (path, original content digest, new text). Validate the entire batch before writing to a fresh runner staging tree: exact allowlist membership, unique canonical paths, regular files only, no links/reparse points or aliases, digest match and size limits. Reject the entire batch on error; never execute response text. Only the runner may execute tests.

Before enabling: independently review the transport's request construction and dispatch boundary, observe effective empty capabilities using benign synthetic inputs, and verify rejection of unallowlisted input/output paths. If choosing a native filesystem backend instead, prove an inside read succeeds and a benign outside read is denied by the actual boundary, and verify no alternate shell/test, MCP or search route. Do not use real secrets, answers or golden tasks as probes. Independent review and a real enforcing facility are both still required.
