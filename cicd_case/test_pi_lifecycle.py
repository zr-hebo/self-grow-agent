"""Black-box Pi RPC generation backend case without real model traffic."""

from __future__ import annotations

import time
from pathlib import Path

from cicd_case.conftest import LLM_API_KEY, MANAGEMENT_KEY, AgentStack

PI_STUB = Path(__file__).with_name("pi_rpc_stub.py").resolve()


def api_data(response):
    payload = response.json()
    assert payload["code"] == 0
    assert payload["message"] == "OK"
    return payload["data"]


def test_pi_rpc_backend_generates_and_hot_loads_handler(tmp_path: Path) -> None:
    workspace_root = tmp_path / "pi-workspaces"
    stack = AgentStack(tmp_path)
    try:
        stack.start(
            extra_env={
                "GENERATION_BACKEND": "pi",
                "PI_EXECUTABLE": str(PI_STUB),
                "PI_PROVIDER": "deepseek",
                "PI_MODEL": "deepseek-v4-pro",
                "PI_TIMEOUT_SECONDS": "5",
                "PI_MAX_CONCURRENT_RUNS": "1",
                "PI_WORKSPACE_ROOT": str(workspace_root),
                "PI_PROVIDER_ENV_NAME": "DEEPSEEK_API_KEY",
            }
        )
        client = stack.require_client()

        created = client.post(
            "/api/v1/manage/routes",
            headers=stack.management_headers,
            json={
                "path": "/pi-hello",
                "method": "GET",
                "instruction": "Return a greeting generated through Pi",
            },
        )

        assert created.status_code == 202, created.text
        operation = api_data(created)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            completed = client.get(
                operation["operation_url"],
                headers=stack.management_headers,
            )
            assert completed.status_code == 200, completed.text
            if api_data(completed)["status"] in {"finish", "failed"}:
                break
            time.sleep(0.05)
        else:
            raise AssertionError(f"Pi route task did not finish: {operation}")
        assert api_data(completed)["status"] == "finish", completed.text
        assert api_data(completed)["route_id"] == "get-pi-hello"
        response = client.get("/pi-hello")
        assert response.status_code == 200
        assert response.json() == {
            "code": 0,
            "message": "OK",
            "data": {"message": "hello from pi"},
        }
        assert stack.stub.requests == []
        assert not workspace_root.exists() or list(workspace_root.iterdir()) == []
        service_log = stack.service_log_path.read_text(encoding="utf-8", errors="replace")
        assert "Management API key configured (fingerprint=sha256:" in service_log
        assert LLM_API_KEY not in service_log
        assert MANAGEMENT_KEY not in service_log
    finally:
        stack.close()
