# Draft workflow: partial implementation

The installed `claim.py NAME RUN_ID --draft` returns `not_run` before credential lookup,
network calls or writes. There is no approved subscription transport and no API-key
fallback. `--resume` remains verification only. Do not provision or copy model tokens.

The library operation keeps the single-host claim lock through side effects. Its caller
must supply trusted authentication, proposal, gate and publication adapters. The model
seam receives one bounded SHA-256-pinned source blob plus issue text, never a checkout.
The operator-owned SQLite journal stores private candidate bytes, passing gate evidence,
the corresponding prepared head, publication intents and the confirmed PR number.
Do not delete ambiguous intents to force a retry. A recovered result requires an exact
remote branch and open draft PR; it is not permission to merge.

`project_gate` supports complete regular-file Git trees (100644 only, at most 512 files
and 2 MiB) supplied as an operator-reviewed secret-free bundle. It checks the base tree
OID, replaces only the approved file, stages all files in a bounded tmpfs and runs up to
four operator-selected `python -B -m unittest MODULE` commands in the pinned Linux
image. No mounts, image pull, network, host commands or package installation. Before
start, the broker verifies the created container's inspected image ID. The operator
must independently pin the path and SHA-256 of every file except the editable source,
including the selected test modules and fixture data. The journal records that manifest,
baseline and staged trees, requested and inspected images, commands, edit digest,
container, exit and duration. Reuse checks the evidence against the current profile;
legacy stdout-only evidence cannot authorize publication. The publisher reads the
prepared commit back by SHA and checks its identity, sole parent and tested tree on
reuse and before creating a ref or draft. These checks protect input identity, not
test execution from other Python code in the same interpreter. The project tests
run as the same unprivileged user as the candidate; this is not a hidden or independently
protected test service. General build systems and project-specific dependencies are not
supported. The single-source stdout probe is not a substitute for project gate evidence.

`python -I -B tools/prove_draft_runner.py` runs the trusted Docker broker with benign
native fixtures (matching and failing project tests, incomplete tree, access markers).
The profile-operation tests use a hermetic GitHub fixture. Neither is evidence of a
live solver/publisher run. Before enabling a real target: approve and prove a restricted
subscription adapter, pin its dependency image and complete tree, and verify the
claim-to-draft path including remote crash recovery in an authorized test repository.
