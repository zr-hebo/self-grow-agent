import asyncio
import logging
import secrets
import threading
from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from config import Settings
from self_grow_agent.api import _instruction_for_log
from self_grow_agent.api import create_app as build_app
from self_grow_agent.code_loader import GeneratedCodeLoader
from self_grow_agent.executor import HandlerProcessError, HandlerTimeoutError
from self_grow_agent.llm import GenerationCapacityError
from self_grow_agent.metadata import RequirementStore
from self_grow_agent.models import GeneratedHandler
from self_grow_agent.runtime import RoutePersistenceError, RouteRuntime


def _random_credential(*excluded: str) -> str:
    credential = secrets.token_urlsafe(32)
    while credential in excluded:
        credential = secrets.token_urlsafe(32)
    return credential


MANAGEMENT_KEY = _random_credential()
INCORRECT_MANAGEMENT_KEY = _random_credential(MANAGEMENT_KEY)
SENSITIVE_AUTH_TOKEN = _random_credential(MANAGEMENT_KEY, INCORRECT_MANAGEMENT_KEY)
SENSITIVE_COOKIE = _random_credential(
    MANAGEMENT_KEY,
    INCORRECT_MANAGEMENT_KEY,
    SENSITIVE_AUTH_TOKEN,
)
SENSITIVE_API_KEY = _random_credential(
    MANAGEMENT_KEY,
    INCORRECT_MANAGEMENT_KEY,
    SENSITIVE_AUTH_TOKEN,
    SENSITIVE_COOKIE,
)
SENSITIVE_MANAGEMENT_KEY = _random_credential(
    MANAGEMENT_KEY,
    INCORRECT_MANAGEMENT_KEY,
    SENSITIVE_AUTH_TOKEN,
    SENSITIVE_COOKIE,
    SENSITIVE_API_KEY,
)


class FakeFeatureGenerator:
    def __init__(self, *results: GeneratedHandler | Exception) -> None:
        self._results: Iterator[GeneratedHandler | Exception] = iter(results)
        self.calls: list[dict[str, object]] = []

    async def generate(
        self,
        *,
        instruction: str,
        path: str,
        method: str,
        current_source: str | None = None,
    ) -> GeneratedHandler:
        self.calls.append(
            {
                "instruction": instruction,
                "path": path,
                "method": method,
                "current_source": current_source,
            }
        )
        result = next(self._results)
        if isinstance(result, Exception):
            raise result
        return result


class AsyncBlockingFeatureGenerator:
    def __init__(self, result: GeneratedHandler) -> None:
        self._result = result
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def generate(
        self,
        *,
        instruction: str,
        path: str,
        method: str,
        current_source: str | None = None,
    ) -> GeneratedHandler:
        del instruction, path, method, current_source
        self.started.set()
        await self.release.wait()
        return self._result


class InlineHandlerExecutor:
    def execute(self, source: str, module_name: str, request: dict) -> object:
        return GeneratedCodeLoader().load(source, module_name)(request)


class FailingHandlerExecutor:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def execute(self, source: str, module_name: str, request: dict) -> object:
        del source, module_name, request
        raise self.error


class BlockingHandlerExecutor:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def execute(self, source: str, module_name: str, request: dict) -> object:
        del source, module_name, request
        self.started.set()
        self.release.wait(timeout=2)
        return {"ok": True}


class CoordinatedHandlerExecutor:
    def __init__(self, concurrent_calls: int) -> None:
        self._entry_barrier = threading.Barrier(concurrent_calls + 1)
        self.release = threading.Event()

    def wait_until_all_entered(self) -> None:
        self._entry_barrier.wait(timeout=5)

    def execute(self, source: str, module_name: str, request: dict) -> object:
        del source, module_name, request
        self._entry_barrier.wait(timeout=5)
        if not self.release.wait(timeout=5):
            raise AssertionError("coordinated handler was not released")
        return {"ok": True}


class CancellationBlockingHandlerExecutor:
    def __init__(self, first_error: Exception | None = None) -> None:
        self._lock = threading.Lock()
        self._call_count = 0
        self._first_error = first_error
        self.first_started = threading.Event()
        self.release_first = threading.Event()
        self.first_finished = threading.Event()

    @property
    def call_count(self) -> int:
        with self._lock:
            return self._call_count

    def execute(self, source: str, module_name: str, request: dict) -> object:
        del source, module_name, request
        with self._lock:
            self._call_count += 1
            call_number = self._call_count

        if call_number == 1:
            self.first_started.set()
            try:
                if not self.release_first.wait(timeout=5):
                    raise AssertionError("first handler was not released")
            finally:
                self.first_finished.set()
            if self._first_error is not None:
                raise self._first_error
        return {"call": call_number}


def create_test_app(
    *,
    settings: Settings,
    generator,
    runtime: RouteRuntime | None = None,
):
    return build_app(
        settings=settings,
        generator=generator,
        runtime=runtime,
        handler_executor=InlineHandlerExecutor(),
        async_route_creation=False,
    )


def make_settings(tmp_path: Path, *, llm_api_key: str = "") -> Settings:
    return Settings(
        host="127.0.0.1",
        port=8000,
        management_api_key=MANAGEMENT_KEY,
        llm_api_key=llm_api_key,
        llm_base_url="https://llm.example.test/v1",
        llm_model="test-model",
        llm_timeout_seconds=5.0,
        generated_dir=tmp_path / "generated",
        metadata_db_path=tmp_path / "runtime-metadata.sqlite3",
    )


def management_headers() -> dict[str, str]:
    return {"X-Management-Key": MANAGEMENT_KEY}


def api_data(response: httpx.Response) -> object:
    payload = response.json()
    assert payload["code"] == 0
    assert payload["message"] == "OK"
    return payload["data"]


def assert_api_error(response: httpx.Response, message: str) -> None:
    assert response.json() == {
        "code": response.status_code,
        "message": message,
        "data": None,
    }


def test_health_and_unknown_business_route(tmp_path: Path) -> None:
    client = TestClient(create_test_app(settings=make_settings(tmp_path), generator=None))

    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json()["code"] == 0
    assert health.json()["message"] == "OK"
    assert health.json()["data"]["status"] == "ok"
    event_time = datetime.fromisoformat(health.json()["data"]["event_time"])
    assert event_time.utcoffset() == timedelta(hours=8)
    response = client.get("/does-not-exist")
    assert response.status_code == 404
    assert_api_error(response, "dynamic route not found")


def test_management_endpoints_require_key(tmp_path: Path) -> None:
    client = TestClient(create_test_app(settings=make_settings(tmp_path), generator=None))

    missing = client.get("/api/v1/manage/routes")
    wrong = client.get(
        "/api/v1/manage/routes",
        headers={"X-Management-Key": INCORRECT_MANAGEMENT_KEY},
    )

    assert missing.status_code == 401
    assert wrong.status_code == 401


@pytest.mark.parametrize(
    "content",
    [
        b"x" * 17,
        (chunk for chunk in (b"x" * 9, b"y" * 8)),
    ],
)
def test_global_body_limit_runs_before_management_authentication(
    tmp_path: Path,
    content,
) -> None:
    settings = replace(make_settings(tmp_path), max_request_body_bytes=16)
    client = TestClient(create_test_app(settings=settings, generator=None))

    response = client.post("/api/v1/manage/routes", content=content)

    assert response.status_code == 413
    assert_api_error(response, "request body is too large")


def test_create_route_is_immediately_available(tmp_path: Path) -> None:
    generator = FakeFeatureGenerator(
        GeneratedHandler(
            source='def handle(request):\n    return {"message": "hello"}\n',
            description="Say hello",
        )
    )
    client = TestClient(
        create_test_app(settings=make_settings(tmp_path), generator=generator)
    )

    created = client.post(
        "/api/v1/manage/routes",
        headers=management_headers(),
        json={
            "path": "/hello",
            "method": "GET",
            "instruction": "Return a hello message",
        },
    )

    assert created.status_code == 201
    assert api_data(created) == {
        "route_id": "get-hello",
        "path": "/hello",
        "method": "GET",
        "project": "default",
        "version": 1,
        "description": "Say hello",
    }
    assert client.get("/hello").json() == {
        "code": 0,
        "message": "OK",
        "data": {"message": "hello"},
    }
    assert generator.calls[0]["path"] == "/hello"
    assert generator.calls[0]["method"] == "GET"


def test_create_route_returns_an_accepted_task_before_generation_finishes(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    generator = AsyncBlockingFeatureGenerator(
        GeneratedHandler(source='def handle(request):\n    return {"message": "hello"}\n')
    )
    app = build_app(
        settings=make_settings(tmp_path),
        generator=generator,
        handler_executor=InlineHandlerExecutor(),
    )
    caplog.set_level(logging.INFO, logger="uvicorn.error")

    async def run_scenario() -> tuple[httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            accepted = await client.post(
                "/api/v1/manage/routes",
                headers=management_headers(),
                json={
                    "path": "/hello",
                    "method": "GET",
                    "project": "demo",
                    "instruction": "Return hello",
                },
            )
            assert accepted.status_code == 202
            assert await asyncio.wait_for(generator.started.wait(), timeout=1)
            operation = api_data(accepted)
            in_progress = await client.get(
                operation["operation_url"],
                headers=management_headers(),
            )
            assert api_data(in_progress)["status"] in {"draft", "implementing"}
            generator.release.set()
            for _ in range(100):
                completed = await client.get(
                    operation["operation_url"],
                    headers=management_headers(),
                )
                if api_data(completed)["status"] == "finish":
                    return accepted, completed
                await asyncio.sleep(0.01)
        raise AssertionError("background route task did not complete")

    accepted, completed = asyncio.run(run_scenario())

    assert api_data(accepted) == {
        "operation_id": api_data(completed)["id"],
        "status": "accepted",
        "project": "demo",
        "path": "/hello",
        "method": "GET",
        "operation_url": f"/api/v1/manage/requirements/{api_data(completed)['id']}",
    }
    assert api_data(completed)["status"] == "finish"
    assert api_data(completed)["route_id"] == "get-hello"

    task_logs = "\n".join(record.getMessage() for record in caplog.records)
    operation_id = api_data(completed)["id"]
    assert f"route_task accepted operation_id={operation_id}" in task_logs
    assert f"route_task generation_started operation_id={operation_id}" in task_logs
    assert f"route_task generation_completed operation_id={operation_id}" in task_logs
    assert "instruction='Return hello'" in task_logs


def test_revise_and_implement_saves_the_draft_and_returns_before_generation(
    tmp_path: Path,
) -> None:
    original_source = 'def handle(request):\n    return {"message": "hello-v1"}\n'
    revised_source = 'def handle(request):\n    return {"message": "hello-v2"}\n'
    settings = make_settings(tmp_path)
    runtime = RouteRuntime(settings.generated_dir)
    existing = runtime.create("/hello", "GET", original_source, "Greeting v1")
    store = RequirementStore(settings.metadata_db_path)
    requirement = store.create(
        "Greeting API",
        "Return hello v1",
        "/hello",
        "GET",
        route_id=existing.route_id,
        route_version=existing.version,
    )
    store.begin_implementation(requirement.id)
    store.complete_implementation(
        requirement.id,
        route_id=existing.route_id,
        route_version=existing.version,
    )
    generator = AsyncBlockingFeatureGenerator(
        GeneratedHandler(source=revised_source, description="Greeting v2")
    )
    app = build_app(
        settings=settings,
        generator=generator,
        runtime=runtime,
        handler_executor=InlineHandlerExecutor(),
        requirement_store=store,
    )

    async def run_scenario() -> tuple[httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            accepted = await client.post(
                f"/api/v1/manage/requirements/{requirement.id}/revise-and-implement",
                headers=management_headers(),
                json={
                    "title": "Greeting API v2",
                    "instruction": "Return hello v2",
                },
            )
            assert accepted.status_code == 202
            assert await asyncio.wait_for(generator.started.wait(), timeout=1)
            in_progress = await client.get(
                f"/api/v1/manage/requirements/{requirement.id}",
                headers=management_headers(),
            )
            assert api_data(in_progress)["status"] == "implementing"
            generator.release.set()
            for _ in range(100):
                completed = await client.get(
                    f"/api/v1/manage/requirements/{requirement.id}",
                    headers=management_headers(),
                )
                if api_data(completed)["status"] == "finish":
                    return accepted, completed
                await asyncio.sleep(0.01)
        raise AssertionError("revised requirement did not finish")

    accepted, completed = asyncio.run(run_scenario())

    assert api_data(accepted) == {
        "operation_id": requirement.id,
        "status": "accepted",
        "project": "default",
        "path": "/hello",
        "method": "GET",
        "operation_url": f"/api/v1/manage/requirements/{requirement.id}",
    }
    assert api_data(completed)["title"] == "Greeting API v2"
    assert api_data(completed)["instruction"] == "Return hello v2"
    assert api_data(completed)["route_version"] == 2
    assert [event.to_status for event in store.list_events(requirement.id)] == [
        "draft",
        "implementing",
        "active",
        "draft",
        "implementing",
        "active",
    ]


def test_instruction_log_redacts_credential_values() -> None:
    credential = secrets.token_urlsafe(32)

    instruction = _instruction_for_log(
        f"Connect using password={credential}; then return a health response"
    )

    assert credential not in instruction
    assert "password=<redacted>" in instruction


def test_routes_can_be_filtered_and_grouped_by_project(tmp_path: Path) -> None:
    generator = FakeFeatureGenerator(
        GeneratedHandler(source='def handle(request):\n    return {"name": "orders"}\n'),
        GeneratedHandler(source='def handle(request):\n    return {"name": "billing"}\n'),
    )
    client = TestClient(
        create_test_app(settings=make_settings(tmp_path), generator=generator)
    )

    for path, project in (("/orders", "Store"), ("/billing", "billing")):
        response = client.post(
            "/api/v1/manage/routes",
            headers=management_headers(),
            json={
                "path": path,
                "method": "GET",
                "project": project,
                "instruction": f"Create {project}",
            },
        )
        assert response.status_code == 201

    routes = client.get("/api/v1/manage/routes", headers=management_headers())
    filtered = client.get(
        "/api/v1/manage/routes?project=STORE",
        headers=management_headers(),
    )

    assert [route["project"] for route in api_data(routes)] == ["billing", "store"]
    assert api_data(filtered) == [
        {
            "route_id": "get-orders",
            "path": "/orders",
            "method": "GET",
            "project": "store",
            "version": 1,
            "description": "",
        }
    ]


def test_max_length_path_can_be_created_and_called(tmp_path: Path) -> None:
    path = "/" + ("a" * 255)
    generator = FakeFeatureGenerator(
        GeneratedHandler(source='def handle(request):\n    return {"ok": True}\n')
    )
    client = TestClient(
        create_test_app(settings=make_settings(tmp_path), generator=generator)
    )

    created = client.post(
        "/api/v1/manage/routes",
        headers=management_headers(),
        json={"path": path, "method": "GET", "instruction": "Return ok"},
    )

    assert created.status_code == 201
    assert len(api_data(created)["route_id"]) <= 64
    assert client.get(path).json() == {
        "code": 0,
        "message": "OK",
        "data": {"ok": True},
    }


def test_new_app_instance_recovers_real_generated_handler(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    generator = FakeFeatureGenerator(
        GeneratedHandler(
            source='def handle(request):\n    return {"message": "restored"}\n'
        )
    )
    first_client = TestClient(create_test_app(settings=settings, generator=generator))
    assert (
        first_client.post(
            "/api/v1/manage/routes",
            headers=management_headers(),
            json={"path": "/hello", "method": "GET", "instruction": "hello"},
        ).status_code
        == 201
    )

    restarted_client = TestClient(create_test_app(settings=settings, generator=None))

    assert restarted_client.get("/hello").json() == {
        "code": 0,
        "message": "OK",
        "data": {"message": "restored"},
    }


def test_default_app_executes_business_handler_in_subprocess(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    runtime = RouteRuntime(settings.generated_dir)
    runtime.create(
        "/isolated",
        "GET",
        'def handle(request):\n    return {"message": "isolated"}\n',
    )
    client = TestClient(
        build_app(settings=settings, generator=None, runtime=runtime)
    )

    response = client.get("/isolated")

    assert response.status_code == 200
    assert response.json() == {
        "code": 0,
        "message": "OK",
        "data": {"message": "isolated"},
    }


def test_business_response_wraps_null_handler_result(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    runtime = RouteRuntime(settings.generated_dir)
    runtime.create(
        "/empty",
        "GET",
        "def handle(request):\n    return None\n",
    )
    client = TestClient(build_app(settings=settings, generator=None, runtime=runtime))

    response = client.get("/empty")

    assert response.status_code == 200
    assert response.json() == {"code": 0, "message": "OK", "data": None}


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_detail"),
    [
        (HandlerTimeoutError("timeout"), 504, "dynamic handler timed out"),
        (HandlerProcessError("failed"), 500, "dynamic handler failed: failed"),
    ],
)
def test_handler_process_failures_are_mapped_to_safe_http_errors(
    tmp_path: Path,
    error: Exception,
    expected_status: int,
    expected_detail: str,
) -> None:
    settings = make_settings(tmp_path)
    runtime = RouteRuntime(settings.generated_dir)
    runtime.create(
        "/isolated",
        "GET",
        'def handle(request):\n    return {"message": "isolated"}\n',
    )
    client = TestClient(
        build_app(
            settings=settings,
            generator=None,
            runtime=runtime,
            handler_executor=FailingHandlerExecutor(error),
        )
    )

    response = client.get("/isolated")

    assert response.status_code == expected_status
    assert_api_error(response, expected_detail)


def test_handler_capacity_rejects_waiters_before_reading_business_body(
    tmp_path: Path,
) -> None:
    settings = replace(
        make_settings(tmp_path),
        max_concurrent_handlers=1,
        handler_admission_timeout_seconds=0.02,
    )
    runtime = RouteRuntime(settings.generated_dir)
    runtime.create(
        "/busy",
        "GET",
        'def handle(request):\n    return {"ok": True}\n',
    )
    executor = BlockingHandlerExecutor()
    app = build_app(
        settings=settings,
        generator=None,
        runtime=runtime,
        handler_executor=executor,
    )

    async def run_requests() -> tuple[httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            first_task = asyncio.create_task(client.get("/busy"))
            try:
                assert await asyncio.to_thread(executor.started.wait, 1)
                rejected = await client.get("/busy")
            finally:
                executor.release.set()
            return await first_task, rejected

    first, rejected = asyncio.run(run_requests())

    assert first.status_code == 200
    assert rejected.status_code == 429
    assert_api_error(rejected, "dynamic handler capacity is full")
    assert rejected.headers["Retry-After"] == "1"


def test_handler_capacity_allows_configured_calls_to_run_concurrently(
    tmp_path: Path,
) -> None:
    settings = replace(
        make_settings(tmp_path),
        max_concurrent_handlers=2,
        handler_admission_timeout_seconds=0.02,
    )
    runtime = RouteRuntime(settings.generated_dir)
    runtime.create(
        "/parallel",
        "GET",
        'def handle(request):\n    return {"ok": True}\n',
    )
    executor = CoordinatedHandlerExecutor(concurrent_calls=2)
    app = build_app(
        settings=settings,
        generator=None,
        runtime=runtime,
        handler_executor=executor,
    )

    async def run_requests() -> list[httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            tasks = [
                asyncio.create_task(client.get("/parallel")),
                asyncio.create_task(client.get("/parallel")),
            ]
            try:
                await asyncio.to_thread(executor.wait_until_all_entered)
            finally:
                executor.release.set()
            return await asyncio.gather(*tasks)

    responses = asyncio.run(run_requests())

    assert [response.status_code for response in responses] == [200, 200]
    assert [response.json() for response in responses] == [
        {"code": 0, "message": "OK", "data": {"ok": True}},
        {"code": 0, "message": "OK", "data": {"ok": True}},
    ]


@pytest.mark.parametrize(
    "first_error",
    [None, HandlerProcessError("cancelled background handler failed")],
)
def test_cancelled_request_keeps_capacity_until_background_handler_finishes(
    tmp_path: Path,
    first_error: Exception | None,
) -> None:
    settings = replace(
        make_settings(tmp_path),
        max_concurrent_handlers=1,
        handler_admission_timeout_seconds=0.02,
    )
    runtime = RouteRuntime(settings.generated_dir)
    runtime.create(
        "/cancelled",
        "GET",
        'def handle(request):\n    return {"ok": True}\n',
    )
    executor = CancellationBlockingHandlerExecutor(first_error)
    app = build_app(
        settings=settings,
        generator=None,
        runtime=runtime,
        handler_executor=executor,
    )

    async def run_requests() -> tuple[httpx.Response, httpx.Response, int]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            first_task = asyncio.create_task(client.get("/cancelled"))
            try:
                assert await asyncio.to_thread(executor.first_started.wait, 2)
                first_task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await first_task

                rejected = await client.get("/cancelled")
                calls_before_release = executor.call_count
            finally:
                executor.release_first.set()

            assert await asyncio.to_thread(executor.first_finished.wait, 2)
            recovered = await client.get("/cancelled")
            return rejected, recovered, calls_before_release

    rejected, recovered, calls_before_release = asyncio.run(run_requests())

    assert rejected.status_code == 429
    assert_api_error(rejected, "dynamic handler capacity is full")
    assert rejected.headers["Retry-After"] == "1"
    assert calls_before_release == 1
    assert recovered.status_code == 200
    assert recovered.json() == {
        "code": 0,
        "message": "OK",
        "data": {"call": 2},
    }


def test_update_route_hot_swaps_handler_with_version_check(tmp_path: Path) -> None:
    first = GeneratedHandler(
        source='def handle(request):\n    return {"message": "hello"}\n'
    )
    second = GeneratedHandler(
        source=(
            'def handle(request):\n'
            '    name = get(request["query"], "name", "world")\n'
            '    return {"message": "hello " + str(name)}\n'
        ),
        description="Greet by name",
    )
    generator = FakeFeatureGenerator(first, second)
    client = TestClient(
        create_test_app(settings=make_settings(tmp_path), generator=generator)
    )
    create_response = client.post(
        "/api/v1/manage/routes",
        headers=management_headers(),
        json={"path": "/hello", "method": "GET", "instruction": "Say hello"},
    )
    assert create_response.status_code == 201

    updated = client.put(
        "/api/v1/manage/routes/get-hello",
        headers=management_headers(),
        json={"instruction": "Greet the name query parameter", "expected_version": 1},
    )

    assert updated.status_code == 200
    assert api_data(updated)["version"] == 2
    assert client.get("/hello?name=Tom").json() == {
        "code": 0,
        "message": "OK",
        "data": {"message": "hello Tom"},
    }
    assert generator.calls[1]["current_source"] == first.source

    stale = client.put(
        "/api/v1/manage/routes/get-hello",
        headers=management_headers(),
        json={"instruction": "Another change", "expected_version": 1},
    )
    assert stale.status_code == 409


def test_post_business_route_receives_json_body(tmp_path: Path) -> None:
    generator = FakeFeatureGenerator(
        GeneratedHandler(
            source=(
                'def handle(request):\n'
                '    return {"received": get(request, "body", None)}\n'
            )
        )
    )
    client = TestClient(
        create_test_app(settings=make_settings(tmp_path), generator=generator)
    )
    created = client.post(
        "/api/v1/manage/routes",
        headers=management_headers(),
        json={"path": "/echo", "method": "POST", "instruction": "Echo JSON body"},
    )

    response = client.post("/echo", json={"name": "Tom"})

    assert created.status_code == 201
    assert response.status_code == 200
    assert response.json() == {
        "code": 0,
        "message": "OK",
        "data": {"received": {"name": "Tom"}},
    }


def test_post_business_route_defaults_to_json_body_without_content_type(
    tmp_path: Path,
) -> None:
    generator = FakeFeatureGenerator(
        GeneratedHandler(
            source=(
                'def handle(request):\n'
                '    body = get(request, "body", {})\n'
                '    return {"name": get(body, "name", "missing")}\n'
            )
        )
    )
    client = TestClient(
        create_test_app(settings=make_settings(tmp_path), generator=generator)
    )
    assert (
        client.post(
            "/api/v1/manage/routes",
            headers=management_headers(),
            json={"path": "/echo", "method": "POST", "instruction": "Echo name"},
        ).status_code
        == 201
    )

    response = client.post("/echo", content='{"name":"Tom"}')

    assert response.status_code == 200
    assert response.json() == {
        "code": 0,
        "message": "OK",
        "data": {"name": "Tom"},
    }


def test_dynamic_request_body_rejects_non_json_parameters(tmp_path: Path) -> None:
    runtime = RouteRuntime(make_settings(tmp_path).generated_dir)
    runtime.create(
        "/echo",
        "POST",
        'def handle(request):\n    return {"ok": True}\n',
    )
    client = TestClient(
        create_test_app(
            settings=make_settings(tmp_path),
            generator=None,
            runtime=runtime,
        )
    )

    response = client.post("/echo", content="name=Tom")

    assert response.status_code == 422
    assert_api_error(response, "dynamic request body must be valid JSON")


def test_json_suffix_media_type_is_parsed_as_json(tmp_path: Path) -> None:
    generator = FakeFeatureGenerator(
        GeneratedHandler(
            source=(
                'def handle(request):\n'
                '    return {"received": get(request, "body", None)}\n'
            )
        )
    )
    client = TestClient(
        create_test_app(settings=make_settings(tmp_path), generator=generator)
    )
    assert (
        client.post(
            "/api/v1/manage/routes",
            headers=management_headers(),
            json={"path": "/patch", "method": "PATCH", "instruction": "Echo patch"},
        ).status_code
        == 201
    )

    response = client.patch(
        "/patch",
        content='{"name":"Tom"}',
        headers={"Content-Type": "application/merge-patch+json"},
    )

    assert response.json() == {
        "code": 0,
        "message": "OK",
        "data": {"received": {"name": "Tom"}},
    }


def test_dynamic_request_body_limit_is_enforced(tmp_path: Path) -> None:
    settings = replace(make_settings(tmp_path), max_request_body_bytes=16)
    runtime = RouteRuntime(settings.generated_dir)
    runtime.create(
        "/upload",
        "POST",
        'def handle(request):\n    return {"ok": True}\n',
    )
    client = TestClient(
        create_test_app(settings=settings, generator=None, runtime=runtime)
    )

    response = client.post("/upload", content=b"x" * 17)

    assert response.status_code == 413
    assert_api_error(response, "request body is too large")


def test_sensitive_headers_are_not_exposed_to_generated_handler(tmp_path: Path) -> None:
    generator = FakeFeatureGenerator(
        GeneratedHandler(
            source='def handle(request):\n    return request["headers"]\n'
        )
    )
    client = TestClient(
        create_test_app(settings=make_settings(tmp_path), generator=generator)
    )
    assert (
        client.post(
            "/api/v1/manage/routes",
            headers=management_headers(),
            json={"path": "/headers", "method": "GET", "instruction": "Show headers"},
        ).status_code
        == 201
    )

    response = client.get(
        "/headers",
        headers={
            "Authorization": f"Bearer {SENSITIVE_AUTH_TOKEN}",
            "Cookie": f"session={SENSITIVE_COOKIE}",
            "X-Api-Key": SENSITIVE_API_KEY,
            "X-Management-Key": SENSITIVE_MANAGEMENT_KEY,
            "X-Request-Id": "visible",
        },
    )

    headers = response.json()["data"]
    assert headers["x-request-id"] == "visible"
    assert "authorization" not in headers
    assert "cookie" not in headers
    assert "x-api-key" not in headers
    assert "x-management-key" not in headers


def test_update_unknown_route_returns_not_found_without_calling_llm(
    tmp_path: Path,
) -> None:
    generator = FakeFeatureGenerator()
    client = TestClient(
        create_test_app(settings=make_settings(tmp_path), generator=generator)
    )

    response = client.put(
        "/api/v1/manage/routes/get-missing",
        headers=management_headers(),
        json={"instruction": "change", "expected_version": 1},
    )

    assert response.status_code == 404
    assert generator.calls == []


def test_invalid_generated_update_keeps_old_handler(tmp_path: Path) -> None:
    generator = FakeFeatureGenerator(
        GeneratedHandler(source='def handle(request):\n    return {"message": "old"}\n'),
        GeneratedHandler(source='import os\n\ndef handle(request):\n    return {}\n'),
    )
    client = TestClient(
        create_test_app(settings=make_settings(tmp_path), generator=generator)
    )
    assert (
        client.post(
            "/api/v1/manage/routes",
            headers=management_headers(),
            json={"path": "/hello", "method": "GET", "instruction": "old"},
        ).status_code
        == 201
    )

    rejected = client.put(
        "/api/v1/manage/routes/get-hello",
        headers=management_headers(),
        json={"instruction": "unsafe", "expected_version": 1},
    )

    assert rejected.status_code == 422
    assert client.get("/hello").json() == {
        "code": 0,
        "message": "OK",
        "data": {"message": "old"},
    }


def test_create_conflict_and_route_listing(tmp_path: Path) -> None:
    generator = FakeFeatureGenerator(
        GeneratedHandler(source='def handle(request):\n    return {"ok": True}\n'),
    )
    client = TestClient(
        create_test_app(settings=make_settings(tmp_path), generator=generator)
    )
    body = {"path": "/hello", "method": "GET", "instruction": "hello"}

    assert (
        client.post(
            "/api/v1/manage/routes", headers=management_headers(), json=body
        ).status_code
        == 201
    )
    duplicate = client.post(
        "/api/v1/manage/routes", headers=management_headers(), json=body
    )
    routes = client.get("/api/v1/manage/routes", headers=management_headers())

    assert duplicate.status_code == 409
    assert len(generator.calls) == 1
    assert routes.status_code == 200
    assert [route["route_id"] for route in api_data(routes)] == ["get-hello"]


def test_management_rejects_reserved_path_and_unsupported_method(
    tmp_path: Path,
) -> None:
    generator = FakeFeatureGenerator()
    client = TestClient(
        create_test_app(settings=make_settings(tmp_path), generator=generator)
    )

    reserved = client.post(
        "/api/v1/manage/routes",
        headers=management_headers(),
        json={"path": "/healthz", "method": "GET", "instruction": "replace"},
    )
    unsupported = client.post(
        "/api/v1/manage/routes",
        headers=management_headers(),
        json={"path": "/hello", "method": "TRACE", "instruction": "trace"},
    )

    assert reserved.status_code == 422
    assert unsupported.status_code == 422
    assert generator.calls == []


def test_management_rejects_percent_encoded_path_before_generation(
    tmp_path: Path,
) -> None:
    generator = FakeFeatureGenerator()
    client = TestClient(
        create_test_app(settings=make_settings(tmp_path), generator=generator)
    )

    response = client.post(
        "/api/v1/manage/routes",
        headers=management_headers(),
        json={
            "path": "/hello%20world",
            "method": "GET",
            "instruction": "hello",
        },
    )

    assert response.status_code == 422
    assert generator.calls == []


def test_management_reports_unconfigured_or_failed_llm(tmp_path: Path) -> None:
    without_llm = TestClient(
        create_test_app(settings=make_settings(tmp_path / "none"), generator=None)
    )
    unavailable = without_llm.post(
        "/api/v1/manage/routes",
        headers=management_headers(),
        json={"path": "/hello", "method": "GET", "instruction": "hello"},
    )
    assert unavailable.status_code == 503

    generator = FakeFeatureGenerator(RuntimeError("upstream unavailable"))
    failed = TestClient(
        create_test_app(settings=make_settings(tmp_path / "error"), generator=generator)
    ).post(
        "/api/v1/manage/routes",
        headers=management_headers(),
        json={"path": "/hello", "method": "GET", "instruction": "hello"},
    )
    assert failed.status_code == 502


def test_management_reports_generation_capacity_with_retry_hint(tmp_path: Path) -> None:
    generator = FakeFeatureGenerator(
        GenerationCapacityError("private backend capacity detail")
    )
    response = TestClient(
        create_test_app(settings=make_settings(tmp_path), generator=generator)
    ).post(
        "/api/v1/manage/routes",
        headers=management_headers(),
        json={"path": "/hello", "method": "GET", "instruction": "hello"},
    )

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "1"
    assert_api_error(response, "generation capacity is full")


def test_route_publication_failure_returns_service_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    settings = make_settings(tmp_path)
    runtime = RouteRuntime(settings.generated_dir)
    generator = FakeFeatureGenerator(
        GeneratedHandler(source='def handle(request):\n    return {"ok": True}\n')
    )

    def fail_write(target: Path, content: str) -> None:
        del target, content
        raise RoutePersistenceError("disk unavailable")

    monkeypatch.setattr(runtime, "_atomic_write_text", fail_write)
    client = TestClient(
        create_test_app(settings=settings, generator=generator, runtime=runtime)
    )

    response = client.post(
        "/api/v1/manage/routes",
        headers=management_headers(),
        json={"path": "/hello", "method": "GET", "instruction": "hello"},
    )

    assert response.status_code == 503
    assert_api_error(response, "route publication failed")
