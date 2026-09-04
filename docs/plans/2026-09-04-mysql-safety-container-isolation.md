# MySQL Safety And Container Isolation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a real MySQL replication integration case, prevent generated plugins from executing arbitrary SQL, and provide a production-oriented container execution backend.

**Architecture:** MySQL access is owned by a narrow platform capability that validates `ip:port`, reads credentials only from the configured runtime environment, and executes only fixed replication control statements with bounded retries. Generated plugins may import that capability but may not import MySQL drivers directly. A configurable Docker executor runs immutable artifacts with a read-only root filesystem, dropped Linux capabilities, no-new-privileges, resource limits, and either no network or one explicitly mapped project network.

**Tech Stack:** Python 3.12+, FastAPI, mysql-connector-python, Docker CLI, pytest, GitHub Actions.

---

### Task 1: Controlled MySQL replication capability

**Files:**
- Create: `self_grow_agent/capabilities/__init__.py`
- Create: `self_grow_agent/capabilities/mysql_replication.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Test: `tests/test_mysql_replication.py`

**Steps:**
1. Write unit tests for target parsing, missing credentials, fixed SQL order, retry bounds, connection cleanup, and secret-free errors/logs.
2. Run `uv run pytest -q tests/test_mysql_replication.py` and verify the tests fail because the capability is missing.
3. Implement `rebuild_replication(instance, environment, retries=2)` using `mysql.connector.connect`, fixed `STOP REPLICA` and `START REPLICA` statements, strict input validation, timeouts, structured logs, and dependency injection for tests.
4. Add the official connector dependency and refresh the lock file.
5. Run the focused test and expect all cases to pass.

### Task 2: Generated-code SQL policy

**Files:**
- Modify: `self_grow_agent/plugin_validator.py`
- Modify: `self_grow_agent/plugin_generator.py`
- Test: `tests/test_plugin_validator.py`
- Test: `tests/test_plugin_generator.py`

**Steps:**
1. Add failing tests proving direct `mysql`, `pymysql`, and other SQL-driver imports are rejected even when dependency pins are allowed, while the exact controlled capability import is accepted.
2. Run the focused tests and verify the new policy tests fail.
3. Permit only `from self_grow_agent.capabilities.mysql_replication import rebuild_replication`; reject broad internal-package imports and direct database-driver imports.
4. Update the generation contract to require the controlled capability for replication work and prohibit generated SQL strings or database-driver dependencies.
5. Run the focused tests and expect all cases to pass.

### Task 3: Container-isolated plugin executor

**Files:**
- Create: `docker/plugin-runtime.Dockerfile`
- Modify: `self_grow_agent/plugin_executor.py`
- Modify: `self_grow_agent/api.py`
- Modify: `config.py`
- Test: `tests/test_plugin_executor.py`
- Test: `tests/test_config.py`
- Test: `tests/test_api.py`

**Steps:**
1. Add failing tests for backend selection, safe Docker arguments, default `--network none`, explicit per-project network, read-only artifact mounts, secret delivery through environment rather than argv, response parsing, timeout cleanup, and invalid settings.
2. Run the focused tests and verify failure before implementation.
3. Refactor shared worker-response validation and add `ContainerPluginExecutor` using an exact container name and bounded cleanup.
4. Add `PLUGIN_EXECUTION_BACKEND`, `PLUGIN_CONTAINER_RUNTIME`, `PLUGIN_CONTAINER_IMAGE`, and `PLUGIN_PROJECT_CONTAINER_NETWORKS` settings with strict validation.
5. Wire executor selection per project in `create_app` and run the focused tests.

### Task 4: Real MySQL and container integration cases

**Files:**
- Create: `cicd_case/test_mysql_replication.py`
- Create: `cicd_case/test_container_plugin.py`
- Modify: `cicd_case/run_tests.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `Makefile`

**Steps:**
1. Add an opt-in real-MySQL test that connects over the MySQL protocol, configures a disposable replica in the fixture, and verifies the controlled capability stops and starts it.
2. Add a Docker smoke test that builds the runtime image and executes a published artifact under the hardened arguments.
3. Add dedicated `mysql` and `container` CICD groups and Make targets; keep ordinary `make cicd` deterministic while CI runs the real infrastructure groups explicitly.
4. Configure ephemeral CI services or Docker setup without committing passwords; credentials must come from CI-generated runtime environment values.
5. Run the infrastructure tests locally with Docker and expect success.

### Task 5: Documentation and complete verification

**Files:**
- Modify: `README.md`
- Modify: `docs/USAGE.md`

**Steps:**
1. Document controlled MySQL usage, required environment mappings, least-privilege `REPLICATION_SLAVE_ADMIN`, current replica statements, container image build, project network mapping, and security boundaries.
2. Run `uv run ruff check .` and expect success.
3. Run `make test` and expect all unit tests to pass.
4. Run `make cicd` and expect all deterministic integration groups to pass.
5. Run the Docker-backed MySQL/container CICD targets and expect both real infrastructure cases to pass.
6. Inspect `git diff --check` and `git status --short`; do not commit until the user asks.
