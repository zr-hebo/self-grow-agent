"""CICD preflight against a real agent TCP process."""

from __future__ import annotations


def test_agent_health(agent_stack) -> None:
    response = agent_stack.require_client().get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert agent_stack.pid > 0
