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
{"version":1,"repo":"example/project","base":"main","order":"number_ascending","trusted_users":["maintainer"],"required_skills":[],"required_mcp":[]}
```

Version 1 supports ascending issue number only. No skill or MCP verifier is
registered: any nonempty requirement stops the run. Never remove requirements
to get a selection. Issue text and comments cannot configure capabilities or
supply commands. No profile or pin is installed by this change.

## What the selector can see

It reads GitHub's open ready queue, follows REST pagination with `gh api
--paginate --slurp`, and checks the current label against its most recent
application. Native blocker counts must agree with the dependency list. The
documented `Blocked by: #1, #2` fallback is restricted to the same repository.
An assignee, `claimed`/`in-progress` label, `<!-- setupai:claim` comment,
linked branch, or open closing PR excludes an issue. A marker only excludes;
it cannot grant rights. Remove stale markers through the normal human process.

Unpublished worktrees, unlinked branches/PRs, private work invisible to the
credential, and informal conversations are not proof the tracker can supply.
Multiple reads are not atomic. A later claimant must recheck; #28 never claims.

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
