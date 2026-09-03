# Project URL Prefix Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Publish named-project dynamic APIs under `/{project}`; for example, `binlog-server` plus `/rebuild_replication` serves `/binlog-server/rebuild_replication`.

**Architecture:** Management endpoints accept project-relative paths and convert them once at the API boundary to a public path stored in `RouteRuntime` and SQLite. Every project, including `default`, receives an explicit `/{project}` prefix. Existing persisted root routes are migrated through an asynchronous move operation: the current route remains active while Pi/LLM regenerates its handler for the target path, then the runtime atomically switches to the generated handler and linked requirements follow the new route identity.

**Tech Stack:** Python 3.12+, FastAPI, SQLite, pytest.

---

### Task 1: Add project-path conversion

**Files:**

- Modify: `self_grow_agent/api.py`
- Test: `tests/test_api.py`

**Step 1: Write a failing API test**

```python
assert api_data(accepted)["path"] == "/binlog-server/rebuild_replication"
assert client.post("/binlog-server/rebuild_replication", json={}).status_code == 200
```

**Step 2: Verify failure**

Run: `uv run pytest tests/test_api.py::test_named_project_routes_are_served_below_the_project_prefix -q`

Expected: FAIL because projects currently publish at the root.

**Step 3: Implement minimal conversion**

```python
def _public_route_path(project: str, path: str) -> str:
    return f"/{project}{path}"
```

Normalize the relative input first, then validate the composed public path through `RouteRuntime`.

**Step 4: Verify pass**

Run: `uv run pytest tests/test_api.py::test_named_project_routes_are_served_below_the_project_prefix -q`

Expected: PASS.

### Task 2: Support linked requirements without a double prefix

**Files:**

- Modify: `self_grow_agent/api.py`
- Test: `tests/test_console_api.py`

**Step 1: Write a failing linked-requirement test**

```python
assert requirement.path == "/store/orders"
assert client.get("/store/orders").status_code == 200
```

**Step 2: Verify failure**

Run: `uv run pytest tests/test_console_api.py::test_requirement_linked_to_named_project_route_uses_its_public_path -q`

Expected: FAIL before linked public paths are accepted.

**Step 3: Implement linked-route handling**

When `route_id` is supplied, accept the linked route's public path as returned by the console/API. For unlinked requirements, convert the project-relative input once. Keep method and project validation.

**Step 4: Verify pass**

Run: `uv run pytest tests/test_console_api.py::test_requirement_linked_to_named_project_route_uses_its_public_path -q`

Expected: PASS.

### Task 3: Add an explicit existing-route migration API

**Files:**

- Modify: `self_grow_agent/runtime.py`
- Modify: `self_grow_agent/api.py`
- Modify: `self_grow_agent/metadata.py`
- Test: `tests/test_api.py`
- Test: `tests/test_metadata.py`

**Step 1: Write failing asynchronous migration tests**

```python
accepted = client.post(
    "/api/v1/manage/routes/post-rebuild_replication/move",
    headers=management_headers(),
    json={"project": "binlog-server", "path": "/rebuild_replication", "expected_version": 1},
)
assert accepted.status_code == 202
assert client.post("/rebuild_replication").status_code == 200
# Poll api_data(accepted)["operation_url"] until status == "finish".
assert client.post("/rebuild_replication").status_code == 404
assert client.post("/binlog-server/rebuild_replication").status_code == 200
```

Also verify that a generation failure records `failed`, leaves the old route active,
and does not publish the target route.

**Step 2: Verify failure**

Run: `uv run pytest tests/test_api.py::test_existing_route_can_move_to_a_named_project_prefix -q`

Expected: FAIL because the current migration is synchronous and retains the old source.

**Step 3: Implement asynchronous regenerate-and-move**

`POST /api/v1/manage/routes/{route_id}/move` requires management authentication, the target `project`, a project-relative `path`, and `expected_version`, and returns an HTTP 202 operation receipt. Create a persisted requirement and regenerate from the current source for the target public path in a background task. Keep the old route active until generation and validation succeed. `RouteRuntime` then atomically publishes the generated source under the target path, removes the old method/path record, and increments the route version. Update linked SQLite requirements to the new `route_id`, public path, and version; reject conflicts instead of overwriting another route. If generation fails, mark the operation failed and keep the old route unchanged.

**Step 4: Verify pass**

Run: `uv run pytest tests/test_api.py::test_existing_route_can_move_to_a_named_project_prefix tests/test_metadata.py -q`

Expected: PASS; the old path no longer resolves and all linked requirements target the moved route.

### Task 4: Update integration coverage and documentation

**Files:**

- Modify: `cicd_case/test_agent_lifecycle.py`
- Modify: `README.md`
- Modify: `docs/USAGE.md`

**Step 1: Add a named-project lifecycle assertion**

```python
assert client.get("/binlog-server/rebuild_replication").status_code == 200
```

**Step 2: Document the contract**

Explain that a request `path` is project-relative and every project, including `default`, publishes beneath `/{project}`. Update all `/hello` examples to `/default/hello`.

**Step 3: Validate**

Run:

```bash
make test
make cicd
git diff --check
```

Expected: all tests pass.

**Step 4: Commit**

```bash
git add self_grow_agent/api.py tests/test_api.py tests/test_console_api.py cicd_case/test_agent_lifecycle.py README.md docs/USAGE.md
git commit -m "feat: prefix named project routes and migrate existing routes"
```
