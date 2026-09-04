# Full API Plugin Mode Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Let the agent generate a complete project-scoped Python API plugin—including handler code, imports, declared dependencies, and tests—in an external workspace, validate it, and atomically hot-publish it without breaking existing restricted handlers.

**Architecture:** Keep the current `restricted` single-file AST handler as the default compatibility mode and add an explicit `plugin` execution mode. A plugin generator returns a bounded multi-file bundle; the service materializes it under an operation-specific external workspace, runs policy and test gates through an executor abstraction, then atomically publishes an immutable version directory and updates the route manifest. Runtime requests execute the published plugin in an isolated subprocess with a sanitized environment; production deployments can replace the local executor with a container implementation without changing management APIs.

**Tech Stack:** Python 3.12+, FastAPI, Pydantic, SQLite, Pi RPC/DeepSeek, uv, pytest, subprocess isolation, atomic filesystem publication.

**Execution note:** Work remains on the current `main` branch at the user's request. Do not create implementation commits until the user explicitly requests `make git commit`.

---

### Task 1: Define the immutable plugin bundle contract

**Files:**
- Create: `self_grow_agent/plugin_models.py`
- Test: `tests/test_plugin_models.py`

**Step 1: Write failing contract tests**

Cover a valid bundle and rejection of absolute paths, `..`, backslashes, duplicate paths, symbolic-link metadata, unpinned dependencies, disallowed dependency names, missing `handler.py`, missing tests, excessive file count, oversized individual files, and oversized total source.

The accepted LLM payload is:

```json
{
  "description": "Restart replication for one validated instance",
  "entrypoint": "handler:handle",
  "dependencies": ["pymysql==1.1.1"],
  "files": [
    {"path": "handler.py", "content": "def handle(request): ..."},
    {"path": "tests/test_handler.py", "content": "def test_handler(): ..."}
  ]
}
```

**Step 2: Run the red test**

Run:

```bash
uv run pytest -q tests/test_plugin_models.py
```

Expected: FAIL because `plugin_models.py` does not exist.

**Step 3: Implement strict models and policy**

Add frozen strict Pydantic models:

```python
class PluginFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    path: str
    content: str


class GeneratedPlugin(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    description: str = ""
    entrypoint: Literal["handler:handle"] = "handler:handle"
    dependencies: tuple[str, ...] = ()
    files: tuple[PluginFile, ...]
```

Provide `PluginPolicy` with defaults of 32 files, 256 KiB per file, 1 MiB total, mandatory `handler.py`, and at least one `tests/test_*.py`. Dependency declarations must use exact `name==version` pins and must exist in the configured allowlist. Normalize paths as POSIX relative paths and reject empty/dot/private-control segments.

**Step 4: Run the green test**

Run:

```bash
uv run pytest -q tests/test_plugin_models.py
uv run ruff check self_grow_agent/plugin_models.py tests/test_plugin_models.py
```

Expected: PASS.

### Task 2: Create and clean operation-scoped external workspaces

**Files:**
- Create: `self_grow_agent/plugin_workspace.py`
- Test: `tests/test_plugin_workspace.py`
- Modify: `config.py`
- Modify: `.env.example`
- Modify: `tests/test_config.py`

**Step 1: Write failing workspace tests**

Verify:

- workspace root resolves outside `GENERATED_DIR`;
- operation IDs are validated as lowercase 32-character hex strings;
- materialization creates private `0700` directories and files without following links;
- existing operation workspaces are never silently reused;
- cleanup only removes the exact validated operation directory;
- plugin artifact root is distinct from workspace root.

**Step 2: Run the red test**

```bash
uv run pytest -q tests/test_plugin_workspace.py tests/test_config.py
```

Expected: FAIL for missing workspace manager/settings.

**Step 3: Implement workspace manager and settings**

Add settings:

```text
PLUGIN_WORKSPACE_ROOT=/var/lib/self-grow-agent/workspaces
PLUGIN_ARTIFACT_ROOT=generated/plugins
PLUGIN_ALLOWED_DEPENDENCIES=pymysql==1.1.1
PLUGIN_MAX_FILES=32
PLUGIN_MAX_FILE_BYTES=262144
PLUGIN_MAX_TOTAL_BYTES=1048576
PLUGIN_KEEP_FAILED_WORKSPACES=true
```

`PluginWorkspaceManager.create(operation_id)` must use an explicit validated child path, `mkdir(mode=0o700)`, and fail if it exists. `materialize()` writes only validated bundle paths using exclusive creation. No archive extraction, shell interpolation, or recursive copy from untrusted paths is allowed.

**Step 4: Verify**

```bash
uv run pytest -q tests/test_plugin_workspace.py tests/test_config.py
```

Expected: PASS.

### Task 3: Generate complete plugin bundles through Pi

**Files:**
- Create: `self_grow_agent/plugin_generator.py`
- Modify: `self_grow_agent/models.py`
- Test: `tests/test_plugin_generator.py`
- Modify: `self_grow_agent/pi_rpc.py`

**Step 1: Write failing generator tests**

Test create/update prompts, untrusted-data boundaries, strict JSON bundle parsing, current-plugin inclusion on update, size limits, safe Pi failures, and absence of credentials/source/output in logs.

**Step 2: Run the red test**

```bash
uv run pytest -q tests/test_plugin_generator.py
```

Expected: FAIL because the plugin generator is missing.

**Step 3: Implement bounded bundle generation**

Add a separate `PluginFeatureGenerator` protocol and `PiPluginGenerator`. Keep Pi tools disabled for this first safe slice; Pi returns the complete bundle as one strict JSON object. The prompt permits ordinary Python imports but requires declared dependencies, prohibits embedded credentials, and requires tests. Everything in the user instruction and current files remains bounded as untrusted task data.

The parser validates JSON first, then applies `PluginPolicy`; no files are written until the full bundle passes.

**Step 4: Verify**

```bash
uv run pytest -q tests/test_plugin_generator.py tests/test_pi_rpc.py
```

Expected: PASS.

### Task 4: Add plugin validation and test gates

**Files:**
- Create: `self_grow_agent/plugin_validator.py`
- Create: `self_grow_agent/plugin_test_runner.py`
- Test: `tests/test_plugin_validator.py`
- Test: `tests/test_plugin_test_runner.py`

**Step 1: Write failing validation tests**

Cover syntax errors, wrong entrypoint/signature, embedded credential-like literals, undeclared third-party imports, forbidden modules (`os`, `subprocess`, `socket`, `ctypes`, `pickle`), passing tests, failing tests, timeout, oversized output, and sanitized test environments.

**Step 2: Run the red tests**

```bash
uv run pytest -q tests/test_plugin_validator.py tests/test_plugin_test_runner.py
```

Expected: FAIL for missing modules.

**Step 3: Implement the gates**

Validation parses every Python file, requires exact `def handle(request)` for the entrypoint, rejects relative/dynamic imports, and verifies imported distributions against declared allowed pins. The local test runner launches an explicit Python command with a minimal environment, fixed cwd, output byte limit, process-group cleanup, and wall timeout. It receives no management or LLM credentials.

The runner returns structured metadata only:

```python
@dataclass(frozen=True, slots=True)
class PluginTestResult:
    passed: bool
    exit_code: int | None
    elapsed_seconds: float
    output_bytes: int
    failure_category: str | None
```

Do not persist raw test output in public API responses.

**Step 4: Verify**

```bash
uv run pytest -q tests/test_plugin_validator.py tests/test_plugin_test_runner.py
```

Expected: PASS.

### Task 5: Atomically publish immutable plugin versions

**Files:**
- Create: `self_grow_agent/plugin_runtime.py`
- Test: `tests/test_plugin_runtime.py`
- Modify: `self_grow_agent/runtime.py`
- Modify: `tests/test_runtime.py`

**Step 1: Write failing publication tests**

Test create, optimistic update, immutable version directories, manifest recovery, atomic current-version switch, failed validation/test rollback, concurrent publishers, digest verification, missing/tampered files, and explicit rollback.

Published layout:

```text
generated/plugins/<project>/<route-id>/
├── v1/
│   ├── plugin.json
│   ├── handler.py
│   └── tests/test_handler.py
├── v2/
└── current.json
```

**Step 2: Run the red test**

```bash
uv run pytest -q tests/test_plugin_runtime.py tests/test_runtime.py
```

Expected: FAIL for missing plugin publisher and route mode.

**Step 3: Implement publication and route manifest v2**

Add `execution_mode: Literal["restricted", "plugin"]` and optional `artifact_path`/digest to `RouteRecord`. Manifest schema v2 persists these fields; schema v1 restores as `restricted`. Build and verify a candidate completely before acquiring the route lock, then atomically rename the immutable version directory and compare-and-swap the route manifest. Never mutate an active version directory.

**Step 4: Verify**

```bash
uv run pytest -q tests/test_plugin_runtime.py tests/test_runtime.py
```

Expected: PASS, including schema-v1 compatibility.

### Task 6: Execute full plugins in a sanitized subprocess

**Files:**
- Create: `self_grow_agent/plugin_worker.py`
- Create: `self_grow_agent/plugin_executor.py`
- Test: `tests/test_plugin_executor.py`
- Modify: `self_grow_agent/api.py`
- Modify: `tests/test_api.py`

**Step 1: Write failing execution tests**

Test JSON request/response IPC, ordinary allowed imports, missing dependency, exception categories, logs, timeout, CPU/memory/result limits, concurrent old/new versions, environment-secret exclusion, process-tree cleanup, and rejection of tampered artifacts.

**Step 2: Run the red tests**

```bash
uv run pytest -q tests/test_plugin_executor.py tests/test_api.py
```

Expected: FAIL because plugin routes are not executable.

**Step 3: Implement execution dispatch**

The API dispatches `restricted` records to `ProcessHandlerExecutor` and `plugin` records to `PluginProcessExecutor`. The plugin worker loads only the immutable artifact entrypoint, accepts one JSON request on stdin, and emits one JSON envelope on stdout. The parent starts it without a shell, uses a sanitized environment, enforces configured resource limits, and logs operation-safe stage metadata without payloads or secrets.

Production credentials are introduced only through an explicit per-project environment allowlist; generation and tests never receive them. Default allowlist is empty.

**Step 4: Verify**

```bash
uv run pytest -q tests/test_plugin_executor.py tests/test_api.py
```

Expected: PASS.

### Task 7: Persist plugin mode through requirements and operations

**Files:**
- Modify: `self_grow_agent/metadata.py`
- Modify: `self_grow_agent/api.py`
- Modify: `self_grow_agent/service.py`
- Modify: `tests/test_metadata.py`
- Modify: `tests/test_console_api.py`
- Modify: `tests/test_api.py`

**Step 1: Write failing metadata/API tests**

Add `execution_mode` to create, revise, retry, move, response, and recovery cases. Verify old SQLite databases migrate to `restricted`; an operation snapshots its mode; retries preserve it; restricted and plugin routes can coexist.

**Step 2: Run the red tests**

```bash
uv run pytest -q tests/test_metadata.py tests/test_console_api.py tests/test_api.py
```

Expected: FAIL for missing mode fields and migration.

**Step 3: Implement compatible migration and orchestration**

Management requests accept:

```json
{
  "path": "/rebuild_replication",
  "method": "POST",
  "project": "binlog-server",
  "execution_mode": "plugin",
  "instruction": "..."
}
```

Default remains `restricted`. The asynchronous task creates a workspace, generates and validates a bundle, runs tests, records a publication receipt, publishes atomically, then transitions to `finish`. Failures keep the old route/version active and preserve a safe category plus workspace/test metadata.

**Step 4: Verify**

```bash
uv run pytest -q tests/test_metadata.py tests/test_console_api.py tests/test_api.py
```

Expected: PASS.

### Task 8: Add console workflow, observability, and integration coverage

**Files:**
- Modify: `self_grow_agent/web/index.html`
- Modify: `self_grow_agent/web/app.js`
- Modify: `self_grow_agent/web/styles.css`
- Modify: `self_grow_agent/api.py`
- Modify: `cicd_case/test_pi_lifecycle.py`
- Create: `cicd_case/test_plugin_lifecycle.py`
- Modify: `cicd_case/run_tests.py`
- Modify: `README.md`
- Modify: `docs/USAGE.md`

**Step 1: Write failing black-box lifecycle tests**

Verify plugin create, import, tests, publication, invocation, update, failed-test rollback, restart recovery, retry, and rollback. Use local deterministic generation and plugin-runner stubs; CI must not contact an external LLM or package registry.

**Step 2: Implement console/API visibility**

Show execution mode, workspace state, validation/test stages, artifact digest, changed files, active plugin version, and rollback action. Do not display secrets, raw model reasoning, or unrestricted test output.

**Step 3: Update documentation**

Document local development mode versus production container sandbox, dependency allowlists, workspace cleanup, import policy, secrets, full curl examples, and the fact that platform-core changes still require deployment/restart.

**Step 4: Run all gates**

```bash
make test
make cicd
uv run ruff check .
git diff --check
```

Expected: all unit and integration tests pass, no credentials are present in generated fixtures or logs, and existing restricted-route cases remain green.

**Step 5: Commit only with explicit user authorization**

When the user requests `make git commit`, stage only product, test, and documentation files. Exclude `.planning/debug/`, generated workspaces, plugin artifacts, logs, databases, virtual environments, and secrets.
