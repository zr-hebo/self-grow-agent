# Distinct Requirement Operations Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Give every asynchronous implementation attempt its own operation ID and make `revise-and-implement` bind automatically to the current route version.

**Architecture:** Keep `requirements` as stable editable definitions and add an SQLite `operations` table containing immutable execution inputs, the captured base route version, status, publication receipt, and error. `revise-and-implement` updates the requirement and creates its operation atomically; the background worker executes from the operation snapshot and performs optimistic concurrency only when publishing. Existing requirement endpoints remain available for the console, while `/api/v1/manage/operations/{id}` is the authoritative task-status endpoint.

**Tech Stack:** Python 3.12+, FastAPI, SQLite, pytest.

---

### Task 1: Persist independent operations

**Files:**

- Modify: `self_grow_agent/metadata.py`
- Test: `tests/test_metadata.py`

**Step 1: Write failing storage tests**

Verify that two sequential executions of one requirement receive different UUIDs, execution inputs and base versions are snapshotted, only one accepted/implementing operation can exist per requirement, and existing databases are upgraded automatically.

**Step 2: Run test to verify failure**

Run: `uv run pytest tests/test_metadata.py -q`

Expected: FAIL because there is no operations table or operation API.

**Step 3: Implement storage**

Add `OperationRecord`, the `operations` table, a partial unique index for active work, and transactional methods to create, claim, complete, fail, query, and list operation records. Add one transactional revision method that updates requirement content, binds the current route identity/version, and inserts a new accepted operation without exposing an intermediate draft race.

**Step 4: Verify pass**

Run: `uv run pytest tests/test_metadata.py -q`

Expected: PASS.

### Task 2: Execute from operation snapshots

**Files:**

- Modify: `self_grow_agent/api.py`
- Modify: `self_grow_agent/service.py`
- Test: `tests/test_api.py`

**Step 1: Write failing API tests**

Call `revise-and-implement` twice sequentially and assert both responses retain the same `requirement_id` but return different `operation_id` values. Advance the route between attempts and assert the second operation captures the current version automatically. Verify a route change during generation still produces a real version-conflict failure.

**Step 2: Run tests to verify failure**

Run: `uv run pytest tests/test_api.py -k 'revise_and_implement or operation' -q`

Expected: FAIL because `operation_id` currently equals the requirement ID.

**Step 3: Implement asynchronous operation execution**

Return `requirement_id`, a fresh `operation_id`, and `/api/v1/manage/operations/{operation_id}`. Execute using the operation's instruction/path/project/source route/base version snapshot. Mirror the latest status back to the requirement for console compatibility. Preserve the publication receipt recovery behavior and redact safe errors as before.

**Step 4: Verify pass**

Run: `uv run pytest tests/test_api.py -k 'revise_and_implement or operation' -q`

Expected: PASS.

### Task 3: Align all asynchronous route tasks

**Files:**

- Modify: `self_grow_agent/api.py`
- Modify: `cicd_case/test_agent_lifecycle.py`
- Modify: `cicd_case/test_pi_lifecycle.py`
- Test: `tests/test_api.py`

**Step 1: Update asynchronous create and move tests**

Verify route creation and route migration also return distinct operation IDs and use the operation status URL. Keep `requirement_id` available so users can continue editing the underlying requirement.

**Step 2: Implement consistent receipts**

Create an operation for every HTTP 202 workflow and make the operation URL authoritative. Keep the old requirement query route for requirement state and editing only.

**Step 3: Verify integration behavior**

Run: `uv run pytest tests/test_api.py cicd_case/test_agent_lifecycle.py -q`

Expected: PASS.

### Task 4: Document and verify

**Files:**

- Modify: `README.md`
- Modify: `docs/USAGE.md`

**Step 1: Update usage examples**

Document stable `requirement_id`, per-attempt `operation_id`, operation polling, automatic base-version capture, and genuine publish-time conflicts.

**Step 2: Run verification**

Run:

```bash
make test
make cicd
git diff --check
```

Expected: all tests pass.

**Step 3: Commit**

```bash
git add self_grow_agent tests cicd_case README.md docs
git commit -m "fix: track requirement executions as distinct operations"
```
