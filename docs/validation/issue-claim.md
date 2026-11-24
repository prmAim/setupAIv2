# Single-host issue claim (#29)

Install the launcher and its sibling `tools/` from the same reviewed tree under
an administrator-controlled directory, not the target checkout. For example:
`python -I -B /opt/setupAIv2/claim.py PROFILE RUN_ID`. The launcher imports
from its own resolved installation, not the working directory or `PYTHONPATH`.
The caller retains a fresh random UUID (32 lowercase hex digits) before invoking
the operation; no issue number is supplied. After interruption use the same
arguments plus `--resume`. A new claim returns `claimed`; resume returns
`verified_only`, an observation about the run, **not permission to execute**.
`lost_race` and `no_work` are no-ops; `diagnostic` (exit 2) requires attention.
The claim is one comment recognized by the #28 selector, not an assignment.
There is no model call, branch, PR, or solver checkpoint in this command.

**Mandatory execution handoff:** before starting or restarting downstream work,
the trusted supervisor must acquire exclusive execution ownership for this repo
and issue, verify the run under that guard, and retain it through execution and
checkpoint. The original worker must also participate. Never dispatch work based
on `--resume` stdout alone: two verifiers can correctly observe the same claim.
If that handoff is unavailable, stop after verification; this CLI does not supply it.

## Approval and deployment

No grant, token or profile is installed. The read-only selection approval is
not a write grant. On a trusted Linux adapter host, outside the checkout:

- Keep the existing protected selection index, pinned policy and read-only gh
  identity as described in [issue selection](issue-selection.md).
- Separately approve a claim comment in protected `/etc/setupAIv2/claim-profiles.json`:
  for each approved profile name supply exactly `repo`, `revision` (both must
  match the selection pin), and `owner` (numeric GitHub user ID).
- Provision `/etc/setupAIv2/claim-credentials/<owner>/<repo>` (lowercase path)
  with a fine-grained PAT restricted to that repository and Issues write. Only
  the adapter may read it. The operator verifies scope; the adapter verifies
  identity via `/user`. Its sole write is POST to the selected issue's comments
  endpoint, without redirects or retries. Restrict egress to GitHub and keep
  the adapter, credentials and journal inaccessible to models and tests.
- Pre-create `/var/lib/setupAIv2/claims/<owner>/<repo>` (lowercase) on durable
  local storage. Only the service may write it. Protect its parents against
  replacement; every cooperating consumer must see the same directory inode.

## Guarantees and limits

After selection, Linux `flock` on the repository directory serializes all
issues in that repo, deliberately sacrificing parallelism. No lockfile is
created by a loser. Lock acquisition times out after two seconds. Under lock,
reread the issue and apply the selector's
author, label provenance, assignment, dependency, linked work and marker
checks. Before writing or confirming, read the full paginated comment list
twice; require stable rows with distinct positive IDs, a count matching issue
metadata, and unchanged issue metadata on reread. Only then fsync the intent
and POST. A consumer that selected the
same issue and lost the recheck writes nothing; a later scan may see no work.
Process exit releases the kernel lock. A newly posted claim between queue scans
is a lost race only if the isolated comment-count change matches a confirmed
journal entry and its verified remote comment. Unexplained changes are diagnostic.

Intent binds issue, run and owner. Confirmation requires one exact GitHub marker
and its numeric author, unchanged timestamps and current eligible metadata.
On a successful POST, pin its returned ID by rename and directory fsync **before**
the first confirmation read. If the response or ID was lost before persistence,
reconciliation is weaker: it can verify the unique intact marker and trusted
writer, but cannot distinguish an identical repost by that writer before the
first pin. Pin the recovered ID once; do not repeat the POST. Diagnostics report
`prewrite` (no POST by this invocation), `write_unconfirmed` (reconcile, never
blindly retry), or `verification` (resume could not verify). Resume never posts.
Missing, duplicate, edited, replaced after pinning, or
conflicting evidence cannot authorize a second claim. An intent with no marker
needs human reconciliation even if the POST never reached GitHub. No timeout
steals ownership; do not remove, split, replace or roll back the journal.

This is not distributed locking or an atomic GitHub transaction. External
writers and independent hosts can change state between any reads and writes,
including after confirmation. Do not use separate journals or network filesystems.

## Reproduce offline

In the review worktree (PowerShell), run once for each of `test_claim.py` and
`test_selection.py` using the local selection runner image:

```powershell
$suite = 'test_claim.py'
docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp -v ${PWD}:/work:ro -w /work --entrypoint python3 setupaiv2-selection-test:28 -B -m unittest discover -s tests -p $suite -v
```

Two barrier-synchronized processes share a fake remote and journal. Separate
children die at remote commit and after confirmed return but before checkpoint.
Timeout fixtures cover both committed and uncommitted writes. No credentials,
Docker socket, live issue or external network are needed.
