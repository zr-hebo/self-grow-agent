"""CICD preflight against a real agent TCP process."""

from __future__ import annotations

from datetime import UTC, datetime


def test_agent_health(agent_stack) -> None:
    response = agent_stack.require_client().get("/healthz")

    assert response.status_code == 200
    assert response.json()["code"] == 0
    assert response.json()["message"] == "OK"
    assert response.json()["data"]["status"] == "ok"
    event_time = datetime.fromisoformat(
        response.json()["data"]["event_time"].replace("Z", "+00:00")
    )
    assert event_time.tzinfo == UTC
    assert agent_stack.pid > 0
