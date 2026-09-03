---
status: resolved
trigger: "修复后 revise-and-implement 仍返回 expected route version 3, current version is 4"
created: 2026-09-03
updated: 2026-09-03
---

# Symptoms

- expected: `revise-and-implement` automatically snapshots the currently active route version and completes without requiring a manual rebase.
- actual: requirement remains failed with `route_version=3` and `last_error="expected route version 3, current version is 4"`.
- errors: `expected route version 3, current version is 4`.
- timeline: reproduced after commit `46efcf5` was created to separate requirement and operation identities.
- reproduction: POST the supplied revision to `/api/v1/manage/requirements/1fc30b79b777468d8ec669d9282acc6b/revise-and-implement`, then GET that requirement.

# Current Focus

- hypothesis: Confirmed — deployment revision skew caused the recurrence: deployment ran `e3974aa`, whose old worker uses persisted requirement version 3, while the fix exists in `46efcf5` and the active route is version 4.
- test: Complete — human confirmation and repository evidence agree that deployment version skew was the sole remaining issue.
- expecting: Session remains archived as a durable diagnosis and future deployment-skew recall pattern.
- next_action: None; archive and index this resolution in the project knowledge base.
- bug_class: bohrbug
- reasoning_checkpoint:
    hypothesis: "Deployment at `e3974aa` causes the exact conflict because its revise worker passes persisted `requirement.route_version=3` instead of snapshotting active runtime version 4."
    confirming_evidence:
      - "Deployment HEAD was `e3974aa`, while the fix is commit `46efcf5`."
      - "Direct inspection of `e3974aa` shows `expected_version=requirement.route_version`; direct inspection of `46efcf5` shows an operation snapshot from `current_route.version`."
      - "The two focused regressions pass at `46efcf5`."
    falsification_test: "After deployment is confirmed at `46efcf5` and restarted, a fresh operation still records base version 3 while active runtime is version 4."
    fix_rationale: "Deploying and restarting on `46efcf5` changes the executed request path to snapshot the current active version, directly removing the stale expected-version input."
    blind_spots: "This agent cannot observe the external deployment process, restart, or post-deploy operation record."
    candidate_causes:
      - "code: `e3974aa` uses persisted requirement version as the update precondition."
      - "environment: deployment was one commit behind the fixed source."
      - "data: persisted requirement version 3 and active runtime version 4 expose the old-code defect."
    and_gate: "yes — the exact recurrence requires the old handler to be deployed while requirement and runtime versions have diverged; both conditions are directly observed."
- tdd_checkpoint:

# Evidence

- timestamp: 2026-09-03
  checked: `.planning/debug/knowledge-base.md`
  found: No durable knowledge base exists in this repository.
  implication: There is no prior local resolution to seed the investigation; proceed from direct evidence.
- timestamp: 2026-09-03
  checked: Repository-wide search for the conflict text and version fields.
  found: The exception originates in `self_grow_agent/service.py:78`; `revise-and-implement` is in `self_grow_agent/api.py:1427`; operation base-version selection occurs in `api.py:1263`; focused regression coverage exists in `tests/test_api.py:491` and `tests/test_metadata.py:398`.
  implication: The failure path is localized and can be traced deterministically from endpoint snapshot creation to the service conflict check.
- timestamp: 2026-09-03
  checked: Commit `46efcf5` and the revise-to-worker call chain.
  found: The endpoint loads the linked route from `active_runtime`, passes `current_route.version` into `revise_and_create_operation`, persists that value in both the requirement and immutable operation, and starts the worker with the new operation id. The worker uses `operation.base_route_version` as `expected_version`.
  implication: In the checked-out code, a request observed while the active route is version 4 cannot naturally generate `expected route version 3` unless the route advances again after snapshot, the running server is stale, or the reported failure belongs to an earlier operation.
- timestamp: 2026-09-03
  checked: Initial focused-test invocation via `python -m pytest`.
  found: The shell has no `python` command, and an unmatched `requirements*.txt` zsh glob aborted the configuration probe.
  implication: This did not exercise the hypothesis; use the project's actual runner (likely `uv`) and avoid shell globs.
- timestamp: 2026-09-03
  checked: Project runner configuration.
  found: `pyproject.toml` declares Python >=3.12 and pytest; `uv` is available at `/opt/homebrew/bin/uv`; the repository also has `.venv`.
  implication: The focused tests can be run reproducibly through `uv run pytest`.
- timestamp: 2026-09-03
  checked: Focused API and metadata regressions via `uv run pytest`.
  found: Both `test_revise_and_implement_automatically_uses_the_current_route_version` and `test_revision_and_operation_creation_atomically_capture_current_route_version` passed (2 passed in 1.02s).
  implication: The checked-out handler and transaction store correctly capture the active version; the reported failure is not reproducible in current source under the exact stale-requirement/current-route condition.
- timestamp: 2026-09-03
  checked: Spectrum-based fault localization eligibility.
  found: The focused suite has no failing test under current source, so there is no failing/pass coverage spectrum to rank.
  implication: SBFL is skipped; deterministic differential debugging between current source and the live server is the appropriate next move.
- timestamp: 2026-09-03
  checked: Deployment and local Git revisions supplied by the orchestrator.
  found: Deployment host reports HEAD `e3974aa`; local HEAD is `46efcf5`; local `main` is ahead of `origin/main` by one commit.
  implication: The deployment cannot be running the fix in `46efcf5` because that commit has not reached the remote origin; this is direct environment/version-skew evidence.
- timestamp: 2026-09-03
  checked: Commit ancestry and old handler implementation.
  found: `e3974aa` is the direct parent and ancestor of `46efcf5`. At `e3974aa`, `revise-and-implement` only calls `update_content` and starts the worker by requirement id; the worker passes `requirement.route_version` directly as `expected_version`.
  implication: With persisted requirement version 3 and active runtime version 4, deployment commit `e3974aa` deterministically emits the exact reported error. Commit `46efcf5` changes this mechanism to snapshot the active runtime version into a new operation.
- timestamp: 2026-09-03
  checked: Local remote-tracking ref after ancestry inspection.
  found: The shared workspace currently reports both local `main` and `origin/main` at `46efcf5`, which is newer than the earlier deployment evidence that origin lacked the commit.
  implication: Another actor may have pushed the fix after the deployment check; verify the server-side remote ref directly before prescribing push versus pull/restart.
- timestamp: 2026-09-03
  checked: Server-side Git remote ref via `git ls-remote`.
  found: `origin/main` now resolves to `46efcf57f2cd15295b2675cd562ed2f0e45dc785`; local HEAD and remote-tracking `origin/main` match it.
  implication: No push or source edit remains. The deployment host can now pull commit `46efcf5`, restart, and retry a fresh operation.
- timestamp: 2026-09-03
  checked: Human checkpoint response.
  found: The user confirmed the issue is resolved and identified the missing push of `46efcf5` as the deployment-version mismatch.
  implication: End-to-end verification agrees with the diagnosed mechanism; the session can be finalized without source changes.
- timestamp: 2026-09-03
  checked: GSD archive configuration and semantic-memory availability.
  found: Documentation commits are enabled; MemPalace is disabled and its CLI is unavailable.
  implication: Commit the resolved session and durable Markdown knowledge-base entry; skip semantic indexing with this recorded reason.

# Eliminated

- hypothesis: The checked-out `revise-and-implement` code still snapshots the stale requirement version instead of the active runtime version.
  evidence: The endpoint code passes `current_route.version` into the atomic operation snapshot, and both focused regression tests pass.
  timestamp: 2026-09-03
- hypothesis: An additional independent code defect in commit `46efcf5` is needed to explain the reported version-3 conflict.
  evidence: Deployment was demonstrably running direct parent `e3974aa`; that version's exact call chain deterministically passes stale version 3 and yields the exact conflict against current version 4, while the fixed code passes both focused regressions.
  timestamp: 2026-09-03

# Resolution

- root_cause: "Deployment ran `e3974aa`, one commit behind the fix, so its old revise worker used persisted requirement version 3 against active runtime version 4; the divergent persisted/runtime versions triggered the deterministic conflict."
- fix: "No new code change. `origin/main` now contains `46efcf5`; update the deployment to that commit, restart the service, and submit a fresh operation."
- verification:
    self_verified: "Direct old/new code comparison proves the mechanism; two focused tests pass at `46efcf5`; remote `main` points to `46efcf5`."
    human_verified: "User confirmed the issue is resolved after pushing/deploying the previously missing commit."
    guardrail_note: "No new source fix was made in this session, so the source-fix acceptance guardrail is not applicable."
- files_changed: []

# Prevention

## Blameless causal branches

- environment/release branch: The deployment stayed on `e3974aa` because the fix commit initially existed only in the local checkout. A deployment-side `git pull --ff-only` correctly reported up to date relative to the then-current remote, so the discrepancy was not visible without comparing the deployment revision, remote revision, and expected fix commit.
- code/data branch: At `e3974aa`, the revise worker used persisted `requirement.route_version`. The valid persisted state had requirement version 3 while the active runtime route was version 4, so the old optimistic-concurrency precondition deterministically raised the reported conflict. Commit `46efcf5` already corrected this by snapshotting the active route version into a distinct operation.
- AND-gate: The recurrence required both conditions: deployment of the old handler and divergent persisted/runtime route versions. Neither alone would produce this exact post-fix report.

## Why this was not caught

No deployment-revision verification gate existed for this class. Focused tests correctly validated local commit `46efcf5`, but they could not establish that the commit had been pushed and loaded by the deployment.

## Recurrence guard

The durable knowledge-base entry for `revise-route-version-conflict` requires future matching investigations to compare deployment `HEAD`, remote `refs/heads/main`, and the expected fix commit before changing code. If this class recurs, version skew is tested first and source changes are withheld unless the deployment is already running the fixed revision.
