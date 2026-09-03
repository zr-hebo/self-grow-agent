"""Integration tests for the local development console and requirement API."""

from __future__ import annotations

import asyncio
import secrets
import threading
from collections import deque
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from config import Settings
from self_grow_agent.api import create_app
from self_grow_agent.code_loader import GeneratedCodeLoader
from self_grow_agent.llm import GenerationError
from self_grow_agent.metadata import RequirementBusyError, RequirementStore
from self_grow_agent.models import GeneratedHandler
from self_grow_agent.runtime import RouteRuntime

MANAGEMENT_KEY = secrets.token_urlsafe(32)
INCORRECT_MANAGEMENT_KEY = secrets.token_urlsafe(32)
LLM_API_KEY = secrets.token_urlsafe(32)


class FakeFeatureGenerator:
    """Return deterministic generated handlers while retaining generation inputs."""

    def __init__(self, *results: GeneratedHandler | Exception) -> None:
        self._results = deque(results)
        self.calls: list[dict[str, Any]] = []

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
        result = self._results.popleft()
        if isinstance(result, Exception):
            raise result
        return result


class InlineHandlerExecutor:
    """Execute validated generated handlers inline to keep API tests deterministic."""

    def execute(self, source: str, module_name: str, request: dict[str, Any]) -> object:
        handler = GeneratedCodeLoader().load(source, module_name)
        return handler(request)


class BusyOnceCompletionStore(RequirementStore):
    """Simulate one transient SQLite conflict after route publication."""

    def __init__(self, database_path: Path) -> None:
        super().__init__(database_path)
        self.complete_attempts = 0

    def complete_implementation(
        self,
        requirement_id: str,
        *,
        route_id: str,
        route_version: int,
        source_sha256: str | None = None,
    ):
        self.complete_attempts += 1
        if self.complete_attempts == 1:
            raise RequirementBusyError("requirement storage is busy")
        return super().complete_implementation(
            requirement_id,
            route_id=route_id,
            route_version=route_version,
            source_sha256=source_sha256,
        )


class CompletionSignallingStore(RequirementStore):
    """Expose finalization completion to cancellation tests without timing sleeps."""

    def __init__(self, database_path: Path) -> None:
        super().__init__(database_path)
        self.completed = threading.Event()

    def complete_implementation(self, *args, **kwargs):
        result = super().complete_implementation(*args, **kwargs)
        self.completed.set()
        return result


class BlockingFeatureGenerator:
    """Pause generation until the ASGI request that started it is cancelled."""

    def __init__(self, result: GeneratedHandler) -> None:
        self.result = result
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.call_count = 0

    async def generate(self, **_kwargs) -> GeneratedHandler:
        self.call_count += 1
        self.started.set()
        await self.release.wait()
        return self.result


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        host="127.0.0.1",
        port=8000,
        management_api_key=MANAGEMENT_KEY,
        llm_api_key=LLM_API_KEY,
        llm_base_url="https://llm.example.test/v1",
        llm_model="test-model",
        llm_timeout_seconds=5.0,
        generated_dir=tmp_path / "generated",
        metadata_db_path=tmp_path / "runtime-metadata.sqlite3",
    )


def build_client(
    tmp_path: Path,
    generator: FakeFeatureGenerator,
    *,
    runtime: RouteRuntime | None = None,
) -> tuple[TestClient, Settings]:
    settings = make_settings(tmp_path)
    app = create_app(
        settings=settings,
        generator=generator,
        runtime=runtime,
        handler_executor=InlineHandlerExecutor(),
    )
    return TestClient(app), settings


def management_headers() -> dict[str, str]:
    return {"X-Management-Key": MANAGEMENT_KEY}


def api_data(response: httpx.Response) -> Any:
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


def create_requirement(
    client: TestClient,
    *,
    title: str = "Greeting API",
    instruction: str = "Return a greeting",
    path: str = "/hello",
    method: str = "GET",
    project: str = "default",
    route_id: str | None = None,
):
    payload = {
        "title": title,
        "instruction": instruction,
        "path": path,
        "method": method,
        "project": project,
    }
    if route_id is not None:
        payload["route_id"] = route_id
    return client.post(
        "/api/v1/manage/requirements",
        headers=management_headers(),
        json=payload,
    )


def test_console_and_static_assets_are_public_but_do_not_expose_keys(
    tmp_path: Path,
) -> None:
    client, _settings = build_client(tmp_path, FakeFeatureGenerator())

    console = client.get("/console")
    javascript = client.get("/console/assets/app.js")
    stylesheet = client.get("/console/assets/styles.css")
    hidden_index = client.get("/console/assets/index.html")

    assert console.status_code == 200
    assert javascript.status_code == 200
    assert stylesheet.status_code == 200
    assert hidden_index.status_code != 200
    assert "text/html" not in hidden_index.headers.get("content-type", "")
    assert "text/html" in console.headers["content-type"]
    assert "javascript" in javascript.headers["content-type"]
    assert "text/css" in stylesheet.headers["content-type"]
    assert "/console/assets/app.js" in console.text
    assert "/console/assets/styles.css" in console.text
    assert 'id="route-project"' in console.text
    assert "route-project-heading" in javascript.text

    csp = console.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "connect-src 'self'" in csp
    assert "'unsafe-inline'" not in csp
    assert "'unsafe-eval'" not in csp
    public_assets = "\n".join((console.text, javascript.text, stylesheet.text))
    assert MANAGEMENT_KEY not in public_assets
    assert LLM_API_KEY not in public_assets


def test_console_requirement_keeps_project_when_implementing(tmp_path: Path) -> None:
    generator = FakeFeatureGenerator(
        GeneratedHandler(
            source='def handle(request):\n    return {"message": "orders"}\n',
            description="Orders API",
        )
    )
    client, _settings = build_client(tmp_path, generator)

    created = create_requirement(
        client,
        title="Orders API",
        path="/orders",
        project="Store",
    )
    requirement_id = api_data(created)["id"]
    implemented = client.post(
        f"/api/v1/manage/requirements/{requirement_id}/implement",
        headers=management_headers(),
    )

    assert created.status_code == 201
    assert api_data(created)["project"] == "store"
    assert implemented.status_code == 200
    assert api_data(implemented)["project"] == "store"
    routes = client.get(
        "/api/v1/manage/routes?project=store",
        headers=management_headers(),
    )
    assert [route["path"] for route in api_data(routes)] == ["/store/orders"]

@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("GET", "/api/v1/manage/operations", None),
        ("GET", "/api/v1/manage/operations/missing", None),
        ("GET", "/api/v1/manage/requirements", None),
        (
            "POST",
            "/api/v1/manage/requirements",
            {
                "title": "Greeting API",
                "instruction": "Return a greeting",
                "path": "/hello",
                "method": "GET",
            },
        ),
        (
            "PATCH",
            "/api/v1/manage/requirements/missing",
            {"title": "Updated greeting", "instruction": "Return a new greeting"},
        ),
        ("GET", "/api/v1/manage/requirements/missing/events", None),
        ("POST", "/api/v1/manage/requirements/missing/implement", None),
        (
            "POST",
            "/api/v1/manage/requirements/missing/revise-and-implement",
            {"title": "Updated greeting", "instruction": "Return a new greeting"},
        ),
        ("POST", "/api/v1/manage/requirements/missing/rebase", None),
    ],
)
def test_every_requirement_endpoint_requires_the_management_key(
    tmp_path: Path,
    method: str,
    path: str,
    payload: dict[str, str] | None,
) -> None:
    client, _settings = build_client(tmp_path, FakeFeatureGenerator())

    missing_key = client.request(method, path, json=payload)
    incorrect_key = client.request(
        method,
        path,
        headers={"X-Management-Key": INCORRECT_MANAGEMENT_KEY},
        json=payload,
    )

    assert missing_key.status_code == 401
    assert_api_error(missing_key, "invalid management key")
    assert incorrect_key.status_code == 401
    assert_api_error(incorrect_key, "invalid management key")


@pytest.mark.parametrize("blank_field", ["title", "instruction"])
def test_create_requirement_rejects_whitespace_only_content(
    tmp_path: Path,
    blank_field: str,
) -> None:
    generator = FakeFeatureGenerator()
    client, settings = build_client(tmp_path, generator)
    values = {
        "title": "Greeting API",
        "instruction": "Return a greeting",
    }
    values[blank_field] = " \t\n "

    response = create_requirement(client, **values)

    assert response.status_code == 422
    assert RequirementStore(settings.metadata_db_path).list() == ()
    assert generator.calls == []


@pytest.mark.parametrize("blank_field", ["title", "instruction"])
def test_update_requirement_rejects_whitespace_only_content(
    tmp_path: Path,
    blank_field: str,
) -> None:
    generator = FakeFeatureGenerator()
    client, settings = build_client(tmp_path, generator)
    created = create_requirement(client)
    requirement_id = api_data(created)["id"]
    values = {
        "title": "Updated greeting",
        "instruction": "Return an updated greeting",
    }
    values[blank_field] = " \t\n "

    response = client.patch(
        f"/api/v1/manage/requirements/{requirement_id}",
        headers=management_headers(),
        json=values,
    )

    assert response.status_code == 422
    persisted = RequirementStore(settings.metadata_db_path).get(requirement_id)
    assert persisted.title == "Greeting API"
    assert persisted.instruction == "Return a greeting"
    assert generator.calls == []


def test_new_requirement_persists_and_implementation_hot_loads_version_one(
    tmp_path: Path,
) -> None:
    generated = GeneratedHandler(
        source='def handle(request):\n    return {"message": "hello-v1"}\n',
        description="Greeting version one",
    )
    generator = FakeFeatureGenerator(generated)
    client, settings = build_client(tmp_path, generator)

    created = create_requirement(client)

    assert created.status_code == 201
    requirement_id = api_data(created)["id"]
    persisted_draft = RequirementStore(settings.metadata_db_path).get(requirement_id)
    assert persisted_draft.status == "draft"
    assert persisted_draft.route_id is None

    implemented = client.post(
        f"/api/v1/manage/requirements/{requirement_id}/implement",
        headers=management_headers(),
    )

    assert implemented.status_code == 200
    assert api_data(implemented)["status"] == "finish"
    default_route_id = RouteRuntime.route_id_for("/default/hello", "GET")
    assert api_data(implemented)["route_id"] == default_route_id
    assert api_data(implemented)["route_version"] == 1
    assert client.get("/default/hello").json() == {
        "code": 0,
        "message": "OK",
        "data": {"message": "hello-v1"},
    }

    reopened_store = RequirementStore(settings.metadata_db_path)
    persisted_active = reopened_store.get(requirement_id)
    assert persisted_active.status == "active"
    assert persisted_active.route_id == default_route_id
    assert persisted_active.route_version == 1
    assert [event.to_status for event in reopened_store.list_events(requirement_id)] == [
        "draft",
        "implementing",
        "active",
    ]


def test_editing_an_active_requirement_and_reimplementing_publishes_version_two(
    tmp_path: Path,
) -> None:
    first_source = 'def handle(request):\n    return {"message": "hello-v1"}\n'
    second_source = 'def handle(request):\n    return {"message": "hello-v2"}\n'
    generator = FakeFeatureGenerator(
        GeneratedHandler(source=first_source, description="Greeting version one"),
        GeneratedHandler(source=second_source, description="Greeting version two"),
    )
    client, settings = build_client(tmp_path, generator)
    created = create_requirement(client)
    requirement_id = api_data(created)["id"]
    first_implementation = client.post(
        f"/api/v1/manage/requirements/{requirement_id}/implement",
        headers=management_headers(),
    )
    assert first_implementation.status_code == 200

    edited = client.patch(
        f"/api/v1/manage/requirements/{requirement_id}",
        headers=management_headers(),
        json={
            "title": "Greeting API v2",
            "instruction": "Return the revised greeting",
        },
    )
    second_implementation = client.post(
        f"/api/v1/manage/requirements/{requirement_id}/implement",
        headers=management_headers(),
    )

    assert edited.status_code == 200
    assert api_data(edited)["status"] == "draft"
    assert api_data(edited)["route_id"] == RouteRuntime.route_id_for(
        "/default/hello", "GET"
    )
    assert api_data(edited)["route_version"] == 1
    assert second_implementation.status_code == 200
    assert api_data(second_implementation)["status"] == "finish"
    assert api_data(second_implementation)["route_version"] == 2
    assert client.get("/default/hello").json() == {
        "code": 0,
        "message": "OK",
        "data": {"message": "hello-v2"},
    }
    assert generator.calls[1]["current_source"] == first_source
    assert RequirementStore(settings.metadata_db_path).get(requirement_id).route_version == 2


def test_requirement_linked_to_an_existing_route_updates_that_route(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    runtime = RouteRuntime(settings.generated_dir)
    original_source = 'def handle(request):\n    return {"message": "existing"}\n'
    existing = runtime.create(
        "/hello",
        "GET",
        original_source,
        description="Existing greeting",
    )
    replacement_source = 'def handle(request):\n    return {"message": "improved"}\n'
    generator = FakeFeatureGenerator(
        GeneratedHandler(source=replacement_source, description="Improved greeting")
    )
    app = create_app(
        settings=settings,
        generator=generator,
        runtime=runtime,
        handler_executor=InlineHandlerExecutor(),
    )
    client = TestClient(app)

    created = create_requirement(
        client,
        title="Improve greeting",
        instruction="Improve the existing greeting",
        route_id=existing.route_id,
    )
    requirement_id = api_data(created)["id"]
    implemented = client.post(
        f"/api/v1/manage/requirements/{requirement_id}/implement",
        headers=management_headers(),
    )

    assert created.status_code == 201
    assert api_data(created)["route_id"] == existing.route_id
    assert api_data(created)["route_version"] == 1
    assert implemented.status_code == 200
    assert api_data(implemented)["route_id"] == existing.route_id
    assert api_data(implemented)["route_version"] == 2
    assert client.get("/hello").json() == {
        "code": 0,
        "message": "OK",
        "data": {"message": "improved"},
    }
    assert generator.calls == [
        {
            "instruction": "Improve the existing greeting",
            "path": "/hello",
            "method": "GET",
            "current_source": original_source,
        }
    ]


def test_requirement_can_rebase_after_route_is_updated_directly(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    runtime = RouteRuntime(settings.generated_dir)
    first_source = 'def handle(request):\n    return {"message": "existing-v1"}\n'
    existing = runtime.create(
        "/hello",
        "GET",
        first_source,
        description="Existing greeting",
    )
    direct_source = 'def handle(request):\n    return {"message": "direct-v2"}\n'
    requirement_source = 'def handle(request):\n    return {"message": "requirement-v3"}\n'
    generator = FakeFeatureGenerator(
        GeneratedHandler(source=direct_source, description="Direct route update"),
        GeneratedHandler(source=requirement_source, description="Requirement update"),
    )
    app = create_app(
        settings=settings,
        generator=generator,
        runtime=runtime,
        handler_executor=InlineHandlerExecutor(),
    )
    client = TestClient(app)
    created = create_requirement(
        client,
        title="Improve greeting",
        instruction="Apply the requirement after synchronizing",
        route_id=existing.route_id,
    )
    requirement_id = api_data(created)["id"]

    direct_update = client.put(
        f"/api/v1/manage/routes/{existing.route_id}",
        headers=management_headers(),
        json={"instruction": "Update the route directly", "expected_version": 1},
    )
    rebased = client.post(
        f"/api/v1/manage/requirements/{requirement_id}/rebase",
        headers=management_headers(),
    )
    implemented = client.post(
        f"/api/v1/manage/requirements/{requirement_id}/implement",
        headers=management_headers(),
    )

    assert direct_update.status_code == 200
    assert api_data(direct_update)["version"] == 2
    assert rebased.status_code == 200
    assert api_data(rebased)["status"] == "draft"
    assert api_data(rebased)["route_id"] == existing.route_id
    assert api_data(rebased)["route_version"] == 2
    assert implemented.status_code == 200
    assert api_data(implemented)["status"] == "finish"
    assert api_data(implemented)["route_version"] == 3
    assert client.get("/hello").json() == {
        "code": 0,
        "message": "OK",
        "data": {"message": "requirement-v3"},
    }
    assert generator.calls == [
        {
            "instruction": "Update the route directly",
            "path": "/hello",
            "method": "GET",
            "current_source": first_source,
        },
        {
            "instruction": "Apply the requirement after synchronizing",
            "path": "/hello",
            "method": "GET",
            "current_source": direct_source,
        },
    ]


def test_transient_completion_conflict_is_retried_without_regenerating(
    tmp_path: Path,
) -> None:
    generated = GeneratedHandler(
        source='def handle(request):\n    return {"message": "hello"}\n',
        description="Greeting",
    )
    generator = FakeFeatureGenerator(generated)
    settings = make_settings(tmp_path)
    store = BusyOnceCompletionStore(settings.metadata_db_path)
    app = create_app(
        settings=settings,
        generator=generator,
        handler_executor=InlineHandlerExecutor(),
        requirement_store=store,
    )
    client = TestClient(app)
    created = create_requirement(client)
    requirement_id = api_data(created)["id"]

    implemented = client.post(
        f"/api/v1/manage/requirements/{requirement_id}/implement",
        headers=management_headers(),
    )

    assert implemented.status_code == 200
    assert api_data(implemented)["status"] == "finish"
    assert api_data(implemented)["route_version"] == 1
    assert store.complete_attempts == 2
    assert len(generator.calls) == 1
    assert client.get("/default/hello").json() == {
        "code": 0,
        "message": "OK",
        "data": {"message": "hello"},
    }


def test_published_receipt_is_reconciled_without_regenerating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generated = GeneratedHandler(
        source='def handle(request):\n    return {"message": "hello"}\n',
        description="Greeting",
    )
    generator = FakeFeatureGenerator(generated)
    settings = make_settings(tmp_path)
    store = RequirementStore(settings.metadata_db_path)
    app = create_app(
        settings=settings,
        generator=generator,
        handler_executor=InlineHandlerExecutor(),
        requirement_store=store,
    )
    client = TestClient(app)
    created = create_requirement(client)
    requirement_id = api_data(created)["id"]
    actual_complete = store.complete_implementation

    def reject_completion(*_args, **_kwargs):
        raise RequirementBusyError("requirement storage is busy")

    monkeypatch.setattr(store, "complete_implementation", reject_completion)
    interrupted = client.post(
        f"/api/v1/manage/requirements/{requirement_id}/implement",
        headers=management_headers(),
    )

    assert interrupted.status_code == 409
    assert store.get(requirement_id).status == "implementing"
    assert client.get("/default/hello").json() == {
        "code": 0,
        "message": "OK",
        "data": {"message": "hello"},
    }
    assert len(generator.calls) == 1

    monkeypatch.setattr(store, "complete_implementation", actual_complete)
    reconciled = client.post(
        f"/api/v1/manage/requirements/{requirement_id}/implement",
        headers=management_headers(),
    )

    assert reconciled.status_code == 200
    assert api_data(reconciled)["status"] == "finish"
    assert api_data(reconciled)["route_version"] == 1
    assert len(generator.calls) == 1


def test_cancelled_implement_request_continues_to_a_consistent_result(
    tmp_path: Path,
) -> None:
    generated = GeneratedHandler(
        source='def handle(request):\n    return {"message": "hello"}\n',
        description="Greeting",
    )
    generator = BlockingFeatureGenerator(generated)
    settings = make_settings(tmp_path)
    store = CompletionSignallingStore(settings.metadata_db_path)
    app = create_app(
        settings=settings,
        generator=generator,  # type: ignore[arg-type]
        handler_executor=InlineHandlerExecutor(),
        requirement_store=store,
    )

    async def run_scenario() -> tuple[dict[str, Any], httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            created = await client.post(
                "/api/v1/manage/requirements",
                headers=management_headers(),
                json={
                    "title": "Greeting API",
                    "instruction": "Return a greeting",
                    "path": "/hello",
                    "method": "GET",
                },
            )
            requirement_id = api_data(created)["id"]
            implementation = asyncio.create_task(
                client.post(
                    f"/api/v1/manage/requirements/{requirement_id}/implement",
                    headers=management_headers(),
                )
            )
            await asyncio.wait_for(generator.started.wait(), timeout=2)
            implementation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await implementation

            generator.release.set()
            assert await asyncio.to_thread(store.completed.wait, 2)
            requirement_response = await client.get(
                "/api/v1/manage/requirements",
                headers=management_headers(),
            )
            requirement = api_data(requirement_response)[0]
            business = await client.get("/default/hello")
            return requirement, business

    requirement, business = asyncio.run(run_scenario())

    assert requirement["status"] == "finish"
    assert requirement["route_version"] == 1
    assert generator.call_count == 1
    assert business.status_code == 200
    assert business.json() == {
        "code": 0,
        "message": "OK",
        "data": {"message": "hello"},
    }


def test_llm_failure_persists_only_a_safe_requirement_error(tmp_path: Path) -> None:
    provider_detail = secrets.token_urlsafe(32)
    generator = FakeFeatureGenerator(RuntimeError(f"provider failure: {provider_detail}"))
    client, settings = build_client(tmp_path, generator)
    created = create_requirement(client)
    requirement_id = api_data(created)["id"]

    response = client.post(
        f"/api/v1/manage/requirements/{requirement_id}/implement",
        headers=management_headers(),
    )

    assert response.status_code == 502
    assert_api_error(response, "LLM generation failed")
    assert provider_detail not in response.text

    store = RequirementStore(settings.metadata_db_path)
    failed = store.get(requirement_id)
    events = store.list_events(requirement_id)
    assert failed.status == "failed"
    assert failed.last_error == "LLM generation failed"
    assert events[-1].to_status == "failed"
    assert events[-1].message == "LLM generation failed"
    assert provider_detail not in failed.last_error
    assert all(event.message is None or provider_detail not in event.message for event in events)


def test_llm_failure_persists_a_safe_diagnostic_category(tmp_path: Path) -> None:
    generator = FakeFeatureGenerator(
        GenerationError("LLM returned invalid generated-handler JSON")
    )
    client, settings = build_client(tmp_path, generator)
    created = create_requirement(client)
    requirement_id = api_data(created)["id"]

    response = client.post(
        f"/api/v1/manage/requirements/{requirement_id}/implement",
        headers=management_headers(),
    )

    assert response.status_code == 502
    assert_api_error(response, "LLM returned invalid generated-handler JSON")
    failed = RequirementStore(settings.metadata_db_path).get(requirement_id)
    assert failed.status == "failed"
    assert failed.last_error == "LLM returned invalid generated-handler JSON"


@pytest.mark.parametrize("reserved_path", ["/console", "/console/assets/override"])
def test_console_paths_cannot_be_created_as_dynamic_routes(
    tmp_path: Path,
    reserved_path: str,
) -> None:
    generator = FakeFeatureGenerator()
    client, settings = build_client(tmp_path, generator)

    response = create_requirement(client, path=reserved_path)

    assert response.status_code == 422
    assert_api_error(response, f"path {reserved_path!r} is reserved")
    assert RequirementStore(settings.metadata_db_path).list() == ()
    assert generator.calls == []
