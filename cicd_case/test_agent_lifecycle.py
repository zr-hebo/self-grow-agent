"""Independent black-box cases for management and dynamic-route lifecycle."""

from __future__ import annotations

import json
import threading
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


def _update_hello(agent_stack, *, source: str, expected_version: int):
    agent_stack.stub.enqueue_handler(source, f"CICD hello v{expected_version + 1}")
    return agent_stack.require_client().put(
        "/api/v1/manage/routes/get-hello",
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
    assert authorized.json() == []
    assert agent_stack.stub.requests == []


def test_dynamic_hello_create(agent_stack) -> None:
    created = _create_hello(agent_stack)

    assert created.status_code == 201, created.text
    assert created.json() == {
        "route_id": "get-hello",
        "path": "/hello",
        "method": "GET",
        "version": 1,
        "description": "CICD hello handler",
    }
    hello = agent_stack.require_client().get("/hello")
    assert hello.status_code == 200
    assert hello.json() == {"message": "hello v1"}
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
    assert created.status_code == 201, created.text

    request_ids = [f"request-{index}" for index in range(8)]
    start_barrier = threading.Barrier(len(request_ids))
    client = agent_stack.require_client()

    def send_request(request_id: str):
        start_barrier.wait(timeout=5)
        return request_id, client.get(
            "/concurrent-echo",
            params={"request_id": request_id},
        )

    with ThreadPoolExecutor(max_workers=len(request_ids)) as executor:
        futures = [executor.submit(send_request, request_id) for request_id in request_ids]
        results = [future.result(timeout=15) for future in futures]

    for request_id, response in results:
        assert response.status_code == 200, response.text
        assert response.json() == {"request_id": request_id}

    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}


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
    requirement_id = created.json()["id"]
    assert created.json()["status"] == "draft"
    assert created.json()["route_id"] is None

    agent_stack.stub.enqueue_handler(HELLO_V1, "CICD console hello handler")
    implemented = client.post(
        f"/api/v1/manage/requirements/{requirement_id}/implement",
        headers=agent_stack.management_headers,
    )
    assert implemented.status_code == 200, implemented.text
    assert implemented.json()["status"] == "active"
    assert implemented.json()["route_version"] == 1
    assert client.get("/console-hello").json() == {"message": "hello v1"}

    events = client.get(
        f"/api/v1/manage/requirements/{requirement_id}/events",
        headers=agent_stack.management_headers,
    )
    assert events.status_code == 200
    assert [event["to_status"] for event in events.json()] == [
        "draft",
        "implementing",
        "active",
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
    assert len(restored.json()) == 1
    assert restored.json()[0]["id"] == requirement_id
    assert restored.json()[0]["status"] == "active"
    assert restored.json()[0]["route_version"] == 1
    assert agent_stack.require_client().get("/console-hello").json() == {
        "message": "hello v1"
    }


def test_hot_reload_update(agent_stack) -> None:
    created = _create_hello(agent_stack)
    assert created.status_code == 201, created.text
    original_pid = agent_stack.pid

    updated = _update_hello(agent_stack, source=HELLO_V2, expected_version=1)

    assert updated.status_code == 200, updated.text
    assert updated.json()["version"] == 2
    immediate = agent_stack.require_client().get("/hello", params={"name": "Codex"})
    default = agent_stack.require_client().get("/hello")
    assert immediate.status_code == 200
    assert immediate.json() == {"message": "hello Codex v2"}
    assert default.status_code == 200
    assert default.json() == {"message": "hello world v2"}
    assert agent_stack.pid == original_pid
    assert len(agent_stack.stub.requests) == 2
    assert "Current source" in agent_stack.stub.requests[1]["input"]
    assert HELLO_V1.strip() in agent_stack.stub.requests[1]["input"]

    llm_request_count = len(agent_stack.stub.requests)
    stale = agent_stack.require_client().put(
        "/api/v1/manage/routes/get-hello",
        headers=agent_stack.management_headers,
        json={"instruction": "This stale update must not run", "expected_version": 1},
    )
    assert stale.status_code == 409
    assert len(agent_stack.stub.requests) == llm_request_count
    assert agent_stack.require_client().get("/hello").json() == {"message": "hello world v2"}


def test_failed_update_rollback(agent_stack) -> None:
    created = _create_hello(agent_stack)
    assert created.status_code == 201, created.text
    updated = _update_hello(agent_stack, source=HELLO_V2, expected_version=1)
    assert updated.status_code == 200, updated.text

    failed = _update_hello(agent_stack, source=UNSAFE_V3, expected_version=2)

    assert failed.status_code == 422, failed.text
    active = agent_stack.require_client().get("/hello")
    assert active.status_code == 200
    assert active.json() == {"message": "hello world v2"}
    routes = agent_stack.require_client().get(
        "/api/v1/manage/routes", headers=agent_stack.management_headers
    )
    assert routes.status_code == 200
    assert routes.json()[0]["version"] == 2
    assert not (agent_stack.generated_dir / "get-hello.v3.py").exists()
    manifest = json.loads((agent_stack.generated_dir / "routes.json").read_text(encoding="utf-8"))
    assert manifest["routes"][0]["version"] == 2
    assert manifest["routes"][0]["source_file"] == "get-hello.v2.py"


def test_restart_recovery(agent_stack) -> None:
    created = _create_hello(agent_stack)
    assert created.status_code == 201, created.text
    updated = _update_hello(agent_stack, source=HELLO_V2, expected_version=1)
    assert updated.status_code == 200, updated.text
    original_pid = agent_stack.pid

    agent_stack.restart_without_llm()

    assert agent_stack.pid != original_pid
    assert not agent_stack.stub.running
    restored = agent_stack.require_client().get("/hello", params={"name": "Restart"})
    assert restored.status_code == 200
    assert restored.json() == {"message": "hello Restart v2"}
    routes = agent_stack.require_client().get(
        "/api/v1/manage/routes", headers=agent_stack.management_headers
    )
    assert routes.status_code == 200
    assert routes.json()[0]["version"] == 2
    unavailable = agent_stack.require_client().post(
        "/api/v1/manage/routes",
        headers=agent_stack.management_headers,
        json={"path": "/new", "method": "GET", "instruction": "Return new"},
    )
    assert unavailable.status_code == 503
    assert unavailable.json() == {"detail": "LLM is not configured"}
