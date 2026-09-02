# Self-Growing Python Agent Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a Python HTTP agent that serves normal business requests and authenticated self-management requests, uses an LLM to generate or update dynamic API handlers, and activates validated handlers without restarting the process.

**Architecture:** FastAPI owns a stable control plane and a final catch-all business dispatcher. The management service asks an OpenAI-compatible LLM for a constrained Python handler, validates its AST, loads a versioned module, persists an atomic manifest, then swaps an immutable handler reference under a lock so in-flight requests keep the old version. Generated code receives plain JSON-like request data rather than a live FastAPI request and executes in a short-lived, resource-limited subprocess; this is defense in depth, not a substitute for container or VM isolation in an internet-facing production deployment.

**Tech Stack:** Python 3.12+, FastAPI, Uvicorn, official OpenAI Python SDK, Pydantic, pytest, HTTPX.

---

### Task 1: Project skeleton and configuration

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `.env.example`
- Create: `config.py`
- Create: `self_grow_agent/__init__.py`
- Create: `generated/.gitkeep`
- Test: `tests/test_config.py`

**Step 1: Write the failing configuration tests**

Verify that `load_settings()` reads service, management-key, LLM, and generated-directory values from environment variables without requiring a real LLM key merely to import or boot the app.

**Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_config.py -q`

Expected: FAIL because `config.py` does not exist.

**Step 3: Implement the project metadata and configuration**

Use an immutable dataclass with these fields and environment names:

```python
@dataclass(frozen=True, slots=True)
class Settings:
    host: str
    port: int
    management_api_key: str
    llm_api_key: str
    llm_base_url: str
    llm_model: str
    llm_timeout_seconds: float
    generated_dir: Path
```

Keep secrets out of source control; defaults may make local startup possible, but every management endpoint must still require the configured key.

**Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_config.py -q`

Expected: PASS.

### Task 2: LLM interface and constrained generated-code loader

**Files:**
- Create: `self_grow_agent/models.py`
- Create: `self_grow_agent/llm.py`
- Create: `self_grow_agent/code_loader.py`
- Test: `tests/test_code_loader.py`
- Test: `tests/test_llm.py`

**Step 1: Write failing tests for the generated handler contract**

Cover a valid handler, invalid syntax, imports, attribute access, loops, multiple top-level statements, an incorrect function signature, oversized source, and a handler returning JSON data.

Generated source must have exactly this shape:

```python
def handle(request):
    name = get(request["query"], "name", "world")
    return {"message": "hello " + str(name)}
```

**Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_code_loader.py tests/test_llm.py -q`

Expected: FAIL because the loader and LLM client do not exist.

**Step 3: Implement generation parsing and safe loading**

The LLM must return strict JSON with `source` and `description`. Parse fenced or unfenced JSON, validate with Pydantic, and use the Responses API through `AsyncOpenAI`. Validate the AST before compilation; reject imports, attributes, decorators, loops, async/generator behavior, exception handling, class/lambda definitions, private identifiers, and calls outside a small allowlist. Execute the versioned module with only safe builtins plus a side-effect-free `get(mapping, key, default)` helper.

**Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_code_loader.py tests/test_llm.py -q`

Expected: PASS.

### Task 3: Persistent versioned runtime and atomic hot swap

**Files:**
- Create: `self_grow_agent/runtime.py`
- Test: `tests/test_runtime.py`

**Step 1: Write failing runtime tests**

Test create, duplicate create, compare-and-swap update, successful version increments, failed-load rollback, exact method/path matching, reserved paths, and startup recovery from `routes.json`.

**Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_runtime.py -q`

Expected: FAIL because the runtime does not exist.

**Step 3: Implement the runtime**

Store records keyed by `(METHOD, normalized_path)`. Build and validate a new immutable record before entering a re-entrant lock. Inside the lock, re-check existence or `expected_version`, atomically write the versioned `.py` file and `routes.json`, then replace the record. A request resolves one record reference under the lock and calls it outside the lock, producing old-or-new behavior rather than a partial update.

Reserved roots: `/api/v1/manage`, `/healthz`, `/docs`, `/redoc`, and `/openapi.json`.

**Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_runtime.py -q`

Expected: PASS.

### Task 4: Management service and HTTP API

**Files:**
- Create: `self_grow_agent/service.py`
- Create: `self_grow_agent/api.py`
- Create: `self_grow_agent/executor.py`
- Create: `main.py`
- Test: `tests/test_api.py`
- Test: `tests/test_executor.py`

**Step 1: Write failing end-to-end API tests**

Test:

```text
POST /api/v1/manage/routes
X-Management-Key: <key>
{"path":"/hello","method":"GET","instruction":"Return {message: hello}"}

PUT /api/v1/manage/routes/get-hello
X-Management-Key: <key>
{"instruction":"Greet the name query parameter","expected_version":1}

GET /hello?name=Tom
```

Also cover missing/wrong keys, unsupported methods, route conflicts, LLM failures, invalid generations, version conflicts, unknown paths, and the health endpoint.

**Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_api.py -q`

Expected: FAIL because the ASGI app does not exist.

**Step 3: Implement the service and API**

Expose:

- `GET /healthz`
- `GET /api/v1/manage/routes`
- `POST /api/v1/manage/routes`
- `PUT /api/v1/manage/routes/{route_id}`
- A final `/{dynamic_path:path}` route supporting configured HTTP methods.

All management endpoints use `X-Management-Key`. Create returns 201 only after the handler is active; update returns 200 only after the new version is active. Convert domain errors into 401, 404, 409, 422, 502, or 503 without leaking secrets or generated tracebacks.

**Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_api.py -q`

Expected: PASS.

### Task 5: Documentation and full verification

**Files:**
- Create: `README.md`

**Step 1: Document setup and the complete hello flow**

Include `uv sync --dev`, environment variables, `uv run python main.py`, curl examples for create/update/list/call, the generated artifact layout, restart recovery, and the security boundary.

**Step 2: Run static and automated verification**

Run: `uv run ruff check .`

Expected: no diagnostics.

Run: `uv run pytest -q`

Expected: all tests pass.

Run: `uv run python -m compileall -q config.py main.py self_grow_agent tests`

Expected: exit code 0.

**Step 3: Smoke-test app creation without an LLM credential**

Run: `uv run python -c 'from main import app; print(app.title)'`

Expected: prints `Self-Growing Agent`.
