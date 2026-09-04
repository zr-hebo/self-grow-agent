"""Black-box full-plugin lifecycle using a deterministic local Pi RPC stub."""

from __future__ import annotations

import time
from pathlib import Path

from cicd_case.conftest import AgentStack

PI_STUB = Path(__file__).with_name("pi_rpc_stub.py").resolve()


def _data(response):
    payload = response.json()
    assert payload["code"] == 0, payload
    return payload["data"]


def _wait(stack: AgentStack, operation_url: str) -> dict:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        response = stack.require_client().get(
            operation_url, headers=stack.management_headers
        )
        assert response.status_code == 200, response.text
        operation = _data(response)
        if operation["status"] in {"finish", "failed"}:
            return operation
        time.sleep(0.05)
    raise AssertionError("plugin operation did not finish")


def _revise(stack: AgentStack, requirement_id: str, instruction: str):
    return stack.require_client().post(
        f"/api/v1/manage/requirements/{requirement_id}/revise-and-implement",
        headers=stack.management_headers,
        json={"title": "CICD plugin", "instruction": instruction},
    )


def test_plugin_lifecycle(tmp_path: Path) -> None:
    stack = AgentStack(tmp_path)
    plugin_workspace = tmp_path / "plugin-workspaces"
    try:
        stack.start(
            extra_env={
                "GENERATION_BACKEND": "pi",
                "PI_EXECUTABLE": str(PI_STUB),
                "PI_PROVIDER": "deepseek",
                "PI_MODEL": "deepseek-v4-pro",
                "PI_TIMEOUT_SECONDS": "5",
                "PI_WORKSPACE_ROOT": str(tmp_path / "pi-workspaces"),
                "PI_PROVIDER_ENV_NAME": "DEEPSEEK_API_KEY",
                "PLUGIN_WORKSPACE_ROOT": str(plugin_workspace),
                "PLUGIN_KEEP_FAILED_WORKSPACES": "false",
            }
        )
        client = stack.require_client()
        created = client.post(
            "/api/v1/manage/routes",
            headers=stack.management_headers,
            json={
                "path": "/echo",
                "method": "POST",
                "project": "cicd",
                "execution_mode": "plugin",
                "instruction": "Return plugin version one",
            },
        )
        assert created.status_code == 202, created.text
        receipt = _data(created)
        requirement_id = receipt["requirement_id"]
        assert _wait(stack, receipt["operation_url"])["status"] == "finish"
        assert _data(client.post("/cicd/echo", json={})) == {"value": "plugin-v1"}

        updated = _revise(stack, requirement_id, "Return plugin version two")
        assert updated.status_code == 202, updated.text
        assert _wait(stack, _data(updated)["operation_url"])["status"] == "finish"
        assert _data(client.post("/cicd/echo", json={})) == {"value": "plugin-v2"}

        failed = _revise(stack, requirement_id, "Generate a failing test candidate")
        failed_operation = _wait(stack, _data(failed)["operation_url"])
        assert failed_operation["status"] == "failed"
        assert failed_operation["last_error"] == "plugin tests failed: tests_failed"
        assert _data(client.post("/cicd/echo", json={})) == {"value": "plugin-v2"}

        retried = client.post(
            f"/api/v1/manage/operations/{failed_operation['id']}/retry",
            headers=stack.management_headers,
        )
        retry_operation = _wait(stack, _data(retried)["operation_url"])
        assert retry_operation["id"] != failed_operation["id"]
        assert retry_operation["execution_mode"] == "plugin"
        assert retry_operation["status"] == "failed"
        assert _data(client.post("/cicd/echo", json={})) == {"value": "plugin-v2"}

        original_pid = stack.pid
        stack.restart_without_llm()
        assert stack.pid != original_pid
        client = stack.require_client()
        assert _data(client.post("/cicd/echo", json={})) == {"value": "plugin-v2"}

        route = _data(
            client.get("/api/v1/manage/routes?project=cicd", headers=stack.management_headers)
        )[0]
        assert route["execution_mode"] == "plugin"
        assert route["version"] == 2
        rolled_back = client.post(
            f"/api/v1/manage/routes/{route['route_id']}/rollback",
            headers=stack.management_headers,
            json={"target_version": 1, "expected_version": 2},
        )
        assert rolled_back.status_code == 200, rolled_back.text
        assert _data(rolled_back)["version"] == 3
        assert _data(client.post("/cicd/echo", json={})) == {"value": "plugin-v1"}

        service_log = stack.service_log_path.read_text(encoding="utf-8")
        assert "plugin_publication validation_started" in service_log
        assert "plugin_publication tests_completed" in service_log
        assert "plugin_publication activated" in service_log
        assert not plugin_workspace.exists() or not list(plugin_workspace.iterdir())
    finally:
        stack.close()
