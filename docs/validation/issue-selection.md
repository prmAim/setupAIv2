# Read-only issue selection (#28)

From a trusted installation, pass a profile name, not a path or issue number:

```sh
python -I -B tools/select_issue.py orbita
```

Exit 0 prints one JSON object: `selected` with `repo`, `number` and `reason`,
or `no_work` after checking the queue. Exit 2 prints `diagnostic`. Selection
is an observation, not a reservation. There is no claim, checkpoint, model,
checkout or work branch in this command.

## Approval boundary

The administrator installs `selection-profiles.json` under `/etc/setupAIv2`
or `C:/Program Files/setupAIv2`. Protect that directory, the CLI and gh from
target-project writes. Each index entry maps a name to exactly `repo` and
`revision` (the approved full 40-character commit SHA). No CLI path, branch,
environment override, or fallback to current main. The read-only credential
must be able to see the queue, label events, dependencies and linked work.

At that revision `.agent/selection.json` is the separately approved selection
policy, not a general solver profile. Example shape only, not an approval:

```json
{"version":2,"repo":"example/project","base":"main","order":"number_ascending","trusted_user_ids":[123],"required_skills":[],"required_mcp":[]}
```

Version 2 trusts numeric GitHub user account IDs, never logins. Version 1
is rejected rather than auto-migrated. Only ascending issue number is supported. No skill or MCP verifier is
registered: any nonempty requirement stops the run. Never remove requirements
to get a selection. Issue text and comments cannot configure capabilities or
supply commands. No profile or pin is installed by this change.

## What the selector can see

It reads GitHub's open ready queue, follows REST pagination with `gh api
--paginate --slurp`, and checks the current label against its most recent
application by matching the current REST label node ID against the paginated
GraphQL timeline, not a historical label name. Native blocker counts must agree with the dependency list. The
documented `Blocked by: #1, #2` fallback is restricted to the same repository.
An assignee, `claimed`/`in-progress` label, `<!-- setupai:claim` comment,
linked branch, open closing PR, or visible timeline reference from any open PR
(including a non-closing `Refs #19`) excludes an issue. A marker only excludes;
it cannot grant rights. Remove stale markers through the normal human process.

Unpublished worktrees, unlinked branches/PRs, private work invisible to the
credential, and informal conversations are not proof the tracker can supply.
The full queue is read twice, once before evaluating candidates and once before
returning. Any observed difference is diagnostic, without retries. Two identical
observations are not an atomic snapshot: changes that reverse between reads
can be missed. A later claimant must recheck; #28 never claims.

## Acceptance record

Public seam: CLI arguments, JSON stdout, exit status, outbound GitHub reads.
Tests ran in a Linux container with no network, read-only root and source,
no credentials or Docker socket, and Python bytecode writes disabled.

| Slice | Observed red | Observed green |
| --- | --- | --- |
| Unknown profile | CLI module missing | diagnostic, 1 test passed |
| Empty queue | no profile transport | `no_work`, one paginated GET, 2 passed |
| Mixed queue | diagnostic instead of selected | #9 twice across two pages, 3 passed |
| Policy validation | seven invalid configurations reached transport | no requests, 4 passed |
| Dependency fallback | diagnostic instead of known blocked | no_work; read errors still diagnostic, 5 passed |
| Approved revision | pin treated as policy | exact SHA GET followed by queue GET, 6 passed |
| Hidden/inconsistent blockers | selected with missing dependency | diagnostic, full suite 11 passed |

The mixed fixture includes a PR sharing the issue namespace, untrusted author,
label removal/reapplication, assignee, blocker, claim, linked branch and open
PR. The empty fixture records every outbound request. The pinned-profile
fixture also checks the real adapter command line. No live orbita selection
was attempted without an approved pin.

### Review follow-up

| Container red | Green |
| --- | --- |
| Empty first scan returned `no_work` after membership changed | Second full scan detects the change; exact pin path and GET sequence verified |
| ID-only mixed queue returned diagnostic | Numeric author/actor IDs, current label node ID and paginated timeline select #9 twice |
| Open non-closing PR on page two selected; null/unknown PR sources selected | Open reference excludes; closed PR and issue references do not; unreadable source is diagnostic |
| HTTP 403 returned generic error | Diagnostic preserves operation and status but omits raw stderr |

The authenticated, read-only `tools/issue_timeline.graphql` query ran against
`prmAim/setupAIv2#28`. The label ID matched the REST issue label node ID; the
query returned the user database ID and a cross-reference with source type
`Issue`. The open non-closing PR case is fixture evidence, not a live orbita run.

Standalone subprocess test (Linux, repository root; no credentials):

```sh
f="$PWD/tests/fixtures/selection"
docker run --rm --network none --read-only --cap-drop ALL --tmpfs /tmp \
  -v "$PWD:/work:ro" -v "$f/gh:/usr/bin/gh:ro" \
  -v "$f/index.json:/etc/setupAIv2/selection-profiles.json:ro" \
  --entrypoint python3 setupaiv2-selection-test:28 -I -B /work/tests/fixtures/selection/check_no_work.py
```

Observed: `no_work`, exactly one pinned-profile GET and two paginated queue
GETs. The writable target/HOME (including a decoy index) was unchanged. Only
the fixture transport wrote its out-of-target audit log. No orbita profile was
created, installed or selected. Scope ends at the selection result.
