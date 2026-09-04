from __future__ import annotations

import logging
import secrets
from typing import Any

import pytest

from self_grow_agent.capabilities import mysql_replication


class FakeCursor:
    def __init__(self, *, fail_on: str | None = None) -> None:
        self.fail_on = fail_on
        self.statements: list[str] = []
        self.closed = False

    def execute(self, statement: str) -> None:
        self.statements.append(statement)
        if statement == self.fail_on:
            raise RuntimeError("database rejected statement password=must-not-leak")

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor
        self.closed = False

    def cursor(self) -> FakeCursor:
        return self._cursor

    def close(self) -> None:
        self.closed = True


def _environment(password: str | None = None) -> dict[str, str]:
    return {
        "MYSQL_USER": "replication-operator",
        "MYSQL_PASSWORD": password or secrets.token_urlsafe(24),
    }


def test_executes_only_fixed_replica_statements_and_closes_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = FakeCursor()
    connection = FakeConnection(cursor)
    connection_arguments: list[dict[str, Any]] = []
    runtime_password = secrets.token_urlsafe(24)

    def connect(**kwargs: Any) -> FakeConnection:
        connection_arguments.append(kwargs)
        return connection

    monkeypatch.setattr(mysql_replication, "_connect", connect)
    monkeypatch.setenv("MYSQL_USER", "replication-operator")
    monkeypatch.setenv("MYSQL_PASSWORD", runtime_password)

    result = mysql_replication.rebuild_replication("10.20.30.40:6606")

    assert result == {
        "ok": True,
        "instance": "10.20.30.40:6606",
        "attempts": 1,
        "steps": [
            {"name": "stop_replica", "ok": True},
            {"name": "start_replica", "ok": True},
        ],
    }
    assert cursor.statements == ["STOP REPLICA", "START REPLICA"]
    assert cursor.closed is True
    assert connection.closed is True
    assert connection_arguments == [
        {
            "host": "10.20.30.40",
            "port": 6606,
            "user": "replication-operator",
            "password": runtime_password,
            "connection_timeout": 5,
            "autocommit": True,
        }
    ]


@pytest.mark.parametrize(
    "instance",
    [
        "",
        "db.internal:3306",
        "10.0.0.1",
        "10.0.0.1:0",
        "10.0.0.1:65536",
        "10.0.0.1:3306;DROP TABLE users",
        "[2001:db8::1]",
    ],
)
def test_rejects_invalid_instance_without_connecting(
    instance: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MYSQL_USER", "replication-operator")
    monkeypatch.setenv("MYSQL_PASSWORD", "runtime-only-secret")
    monkeypatch.setattr(
        mysql_replication,
        "_connect",
        lambda **kwargs: pytest.fail(f"unexpected connect: {kwargs}"),
    )

    result = mysql_replication.rebuild_replication(instance)

    assert result == {
        "ok": False,
        "error": "invalid MySQL instance; expected ip:port",
        "attempts": 0,
    }


def test_missing_credentials_returns_safe_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MYSQL_USER", raising=False)
    monkeypatch.delenv("MYSQL_PASSWORD", raising=False)

    result = mysql_replication.rebuild_replication("127.0.0.1:3306")

    assert result == {
        "ok": False,
        "error": "MySQL capability credentials are not configured",
        "attempts": 0,
    }


def test_retries_twice_and_never_logs_driver_error_or_password(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    password = secrets.token_urlsafe(24)
    attempts = 0
    connections: list[FakeConnection] = []

    def connect(**kwargs: Any) -> FakeConnection:
        nonlocal attempts
        attempts += 1
        assert kwargs["password"] == password
        cursor = FakeCursor(fail_on="START REPLICA")
        connection = FakeConnection(cursor)
        connections.append(connection)
        return connection

    monkeypatch.setattr(mysql_replication, "_connect", connect)
    monkeypatch.setenv("MYSQL_USER", "replication-operator")
    monkeypatch.setenv("MYSQL_PASSWORD", password)
    caplog.set_level(logging.INFO, logger="self_grow_agent.capability.mysql_replication")

    result = mysql_replication.rebuild_replication("127.0.0.1:3306", retries=2)

    assert result == {
        "ok": False,
        "error": "MySQL replication operation failed",
        "instance": "127.0.0.1:3306",
        "attempts": 3,
        "failed_step": "start_replica",
    }
    assert attempts == 3
    assert all(connection.closed for connection in connections)
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "step=connect" in log_text
    assert "step=stop_replica" in log_text
    assert "step=start_replica" in log_text
    assert "attempt=3" in log_text
    assert "RuntimeError" in log_text
    assert password not in log_text
    assert "must-not-leak" not in log_text


@pytest.mark.parametrize("retries", [-1, 3, True])
def test_rejects_retry_count_outside_policy(retries: object) -> None:
    with pytest.raises(ValueError, match="retries must be an integer between 0 and 2"):
        mysql_replication.rebuild_replication(
            "127.0.0.1:3306", retries=retries  # type: ignore[arg-type]
        )
