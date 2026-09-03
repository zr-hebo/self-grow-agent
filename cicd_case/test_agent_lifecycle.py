"""Independent black-box cases for management and dynamic-route lifecycle."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

HELLO_V1 = 'def handle(request):\n    return {"message": "hello v1"}\n'
HELLO_V2 = """\
def handle(request):
    query = get(request, "query", {})
    name = str(get(query, "name", "world"))
    return {"message": "hello " + name + " v2"}
"""
UNSAFE_V3 = 'import os\n\ndef handle(request):\n    return {"message": "hello v3"}\n'
CONCURRENT_ECHO = """\
def handle(request):
    query = get(request, "query", {})
    request_id = str(get(query, "request_id", ""))
    return {"request_id": request_id}
"""


def api_data(response):
    payload = response.json()
    assert payload["code"] == 0
    assert payload["message"] == "OK"
    return payload["data"]


def assert_api_error(response, message: str) -> None:
    assert response.json() == {
        "code": response.status_code,
        "message": message,
        "data": None,
    }


def _create_hello(agent_stack, source: str = HELLO_V1):
    agent_stack.stub.enqueue_handler(source, "CICD hello handler")
    return agent_stack.require_client().post(
        "/api/v1/manage/routes",
        headers=agent_stack.management_headers,
        json={
            "path": "/hello",
            "method": "GET",
            "instruction": "Return the CICD hello message",
        },
    )


def _wait_for_route_task(agent_stack, accepted):
    """Poll a direct-route receipt until its background implementation finishes."""

    assert accepted.status_code == 202, accepted.text
    operation = api_data(accepted)
    assert operation["status"] == "accepted"
    deadline = time.monotonic() + 10
    client = agent_stack.require_client()
    while time.monotonic() < deadline:
        operation_response = client.get(
            operation["operation_url"],
            headers=agent_stack.management_headers,
        )
        assert operation_response.status_code == 200, operation_response.text
        result = api_data(operation_response)
        if result["status"] in {"finish", "failed"}:
            assert result["status"] == "finish", result
            return result
        time.sleep(0.05)
    raise AssertionError(f"route task did not finish: {operation}")


def _update_hello(agent_stack, *, source: str, expected_version: int):
    agent_stack.stub.enqueue_handler(source, f"CICD hello v{expected_version + 1}")
    client = agent_stack.require_client()
    routes = api_data(
        client.get("/api/v1/manage/routes", headers=agent_stack.management_headers)
    )
    hello_route = next(route for route in routes if route["path"] == "/default/hello")
    return client.put(
        f"/api/v1/manage/routes/{hello_route['route_id']}",
        headers=agent_stack.management_headers,
        json={
            "instruction": f"Update the CICD greeting to v{expected_version + 1}",
            "expected_version": expected_version,
        },
    )


def test_management_auth(agent_stack) -> None:
    client = agent_stack.require_client()

    missing = client.get("/api/v1/manage/routes")
    wrong = client.get(
        "/api/v1/manage/routes",
        headers=agent_stack.incorrect_management_headers,
    )
    authorized = client.get("/api/v1/manage/routes", headers=agent_stack.management_headers)

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert authorized.status_code == 200
    assert api_data(authorized) == []
    assert agent_stack.stub.requests == []


def test_dynamic_hello_create(agent_stack) -> None:
    accepted = _create_hello(agent_stack)

    assert accepted.status_code == 202, accepted.text
    operation = api_data(accepted)
    assert operation == {
        "requirement_id": operation["requirement_id"],
        "operation_id": operation["operation_id"],
        "status": "accepted",
        "project": "default",
        "path": "/default/hello",
        "method": "GET",
        "operation_url": f"/api/v1/manage/operations/{operation['operation_id']}",
    }
    assert operation["requirement_id"] != operation["operation_id"]
    completed = _wait_for_route_task(agent_stack, accepted)
    assert completed["target_route_id"]
    assert completed["target_route_version"] == 1
    hello = agent_stack.require_client().get("/default/hello")
    assert hello.status_code == 200
    assert hello.json() == {
        "code": 0,
        "message": "OK",
        "data": {"message": "hello v1"},
    }
    assert len(agent_stack.stub.requests) == 1


def test_concurrent_business_requests(agent_stack) -> None:
    agent_stack.stub.enqueue_handler(CONCURRENT_ECHO, "CICD concurrent echo handler")
    created = agent_stack.require_client().post(
        "/api/v1/manage/routes",
        headers=agent_stack.management_headers,
        json={
            "path": "/concurrent-echo",
            "method": "GET",
            "instruction": "Echo the request_id query parameter",
        },
    )
    _wait_for_route_task(agent_stack, created)

    request_ids = [f"request-{index}" for index in range(8)]
    start_barrier = threading.Barrier(len(request_ids))
    client = agent_stack.require_client()

    def send_request(request_id: str):
        start_barrier.wait(timeout=5)
        return request_id, client.get(
            "/default/concurrent-echo",
            params={"request_id": request_id},
        )

    with ThreadPoolExecutor(max_workers=len(request_ids)) as executor:
        futures = [executor.submit(send_request, request_id) for request_id in request_ids]
        results = [future.result(timeout=15) for future in futures]

    for request_id, response in results:
        assert response.status_code == 200, response.text
        assert response.json() == {
            "code": 0,
            "message": "OK",
            "data": {"request_id": request_id},
        }

    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json()["code"] == 0
    assert health.json()["message"] == "OK"
    assert health.json()["data"]["status"] == "ok"
    assert health.json()["data"]["event_time"].endswith("+08:00")


def test_console_requirement_metadata_survives_restart(agent_stack) -> None:
    client = agent_stack.require_client()
    console = client.get("/console")
    assert console.status_code == 200
    assert "Agent Forge" in console.text

    created = client.post(
        "/api/v1/manage/requirements",
        headers=agent_stack.management_headers,
        json={
            "title": "CICD console greeting",
            "instruction": "Return the CICD hello message",
            "path": "/console-hello",
            "method": "GET",
        },
    )
    assert created.status_code == 201, created.text
    requirement_id = api_data(created)["id"]
    assert api_data(created)["status"] == "draft"
    assert api_data(created)["route_id"] is None

    agent_stack.stub.enqueue_handler(HELLO_V1, "CICD console hello handler")
    implemented = client.post(
        f"/api/v1/manage/requirements/{requirement_id}/revise-and-implement",
        headers=agent_stack.management_headers,
        json={
            "title": "CICD console greeting (revised)",
            "instruction": "Return the CICD hello message after revision",
        },
    )
    completed = _wait_for_route_task(agent_stack, implemented)
    assert completed["status"] == "finish"
    assert completed["target_route_version"] == 1
    assert client.get("/default/console-hello").json() == {
        "code": 0,
        "message": "OK",
        "data": {"message": "hello v1"},
    }

    events = client.get(
        f"/api/v1/manage/requirements/{requirement_id}/events",
        headers=agent_stack.management_headers,
    )
    assert events.status_code == 200
    assert [event["to_status"] for event in api_data(events)] == [
        "draft",
        "implementing",
        "finish",
    ]
    assert (agent_stack.generated_dir / "runtime-metadata.sqlite3").is_file()

    original_pid = agent_stack.pid
    agent_stack.restart_without_llm()

    assert agent_stack.pid != original_pid
    restored = agent_stack.require_client().get(
        "/api/v1/manage/requirements",
        headers=agent_stack.management_headers,
    )
    assert restored.status_code == 200
    assert len(api_data(restored)) == 1
    assert api_data(restored)[0]["id"] == requirement_id
    assert api_data(restored)[0]["status"] == "finish"
    assert api_data(restored)[0]["route_version"] == 1
    assert agent_stack.require_client().get("/default/console-hello").json() == {
        "code": 0,
        "message": "OK",
        "data": {"message": "hello v1"},
    }


def test_hot_reload_update(agent_stack) -> None:
    created = _create_hello(agent_stack)
    _wait_for_route_task(agent_stack, created)
    original_pid = agent_stack.pid

    updated = _update_hello(agent_stack, source=HELLO_V2, expected_version=1)

    assert updated.status_code == 200, updated.text
    assert api_data(updated)["version"] == 2
    immediate = agent_stack.require_client().get(
        "/default/hello", params={"name": "Codex"}
    )
    default = agent_stack.require_client().get("/default/hello")
    assert immediate.status_code == 200
    assert immediate.json() == {
        "code": 0,
        "message": "OK",
        "data": {"message": "hello Codex v2"},
    }
    assert default.status_code == 200
    assert default.json() == {
        "code": 0,
        "message": "OK",
        "data": {"message": "hello world v2"},
    }
    assert agent_stack.pid == original_pid
    assert len(agent_stack.stub.requests) == 2
    assert "Current source" in agent_stack.stub.requests[1]["input"]
    assert HELLO_V1.strip() in agent_stack.stub.requests[1]["input"]

    llm_request_count = len(agent_stack.stub.requests)
    routes = api_data(
        agent_stack.require_client().get(
            "/api/v1/manage/routes", headers=agent_stack.management_headers
        )
    )
    route_id = next(route["route_id"] for route in routes if route["path"] == "/default/hello")
    stale = agent_stack.require_client().put(
        f"/api/v1/manage/routes/{route_id}",
        headers=agent_stack.management_headers,
        json={"instruction": "This stale update must not run", "expected_version": 1},
    )
    assert stale.status_code == 409
    assert len(agent_stack.stub.requests) == llm_request_count
    assert agent_stack.require_client().get("/default/hello").json() == {
        "code": 0,
        "message": "OK",
        "data": {"message": "hello world v2"},
    }


def test_failed_update_rollback(agent_stack) -> None:
    created = _create_hello(agent_stack)
    _wait_for_route_task(agent_stack, created)
    updated = _update_hello(agent_stack, source=HELLO_V2, expected_version=1)
    assert updated.status_code == 200, updated.text

    failed = _update_hello(agent_stack, source=UNSAFE_V3, expected_version=2)

    assert failed.status_code == 422, failed.text
    active = agent_stack.require_client().get("/default/hello")
    assert active.status_code == 200
    assert active.json() == {
        "code": 0,
        "message": "OK",
        "data": {"message": "hello world v2"},
    }
    routes = agent_stack.require_client().get(
        "/api/v1/manage/routes", headers=agent_stack.management_headers
    )
    assert routes.status_code == 200
    assert api_data(routes)[0]["version"] == 2
    route_id = api_data(routes)[0]["route_id"]
    assert not (agent_stack.generated_dir / f"{route_id}.v3.py").exists()
    manifest = json.loads((agent_stack.generated_dir / "routes.json").read_text(encoding="utf-8"))
    assert manifest["routes"][0]["version"] == 2
    assert manifest["routes"][0]["source_file"] == f"{route_id}.v2.py"


def test_restart_recovery(agent_stack) -> None:
    created = _create_hello(agent_stack)
    _wait_for_route_task(agent_stack, created)
    updated = _update_hello(agent_stack, source=HELLO_V2, expected_version=1)
    assert updated.status_code == 200, updated.text
    original_pid = agent_stack.pid

    agent_stack.restart_without_llm()

    assert agent_stack.pid != original_pid
    assert not agent_stack.stub.running
    restored = agent_stack.require_client().get(
        "/default/hello", params={"name": "Restart"}
    )
    assert restored.status_code == 200
    assert restored.json() == {
        "code": 0,
        "message": "OK",
        "data": {"message": "hello Restart v2"},
    }
    routes = agent_stack.require_client().get(
        "/api/v1/manage/routes", headers=agent_stack.management_headers
    )
    assert routes.status_code == 200
    assert api_data(routes)[0]["version"] == 2
    unavailable = agent_stack.require_client().post(
        "/api/v1/manage/routes",
        headers=agent_stack.management_headers,
        json={"path": "/new", "method": "GET", "instruction": "Return new"},
    )
    assert unavailable.status_code == 503
    assert_api_error(unavailable, "LLM is not configured")
