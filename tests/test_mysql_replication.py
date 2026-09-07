from __future__ import annotations

import logging
import secrets
import threading
import time
from typing import Any

import pytest

from self_grow_agent.capabilities import mysql_replication
from self_grow_agent.capabilities.errors import CapabilityError


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

    with pytest.raises(CapabilityError, match="invalid MySQL instance") as raised:
        mysql_replication.rebuild_replication(instance)

    assert raised.value.code == "mysql_instance_invalid"
    assert raised.value.status_code == 422


def test_missing_credentials_returns_safe_error_and_logs_only_missing_names(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("MYSQL_USER", raising=False)
    monkeypatch.delenv("MYSQL_PASSWORD", raising=False)
    caplog.set_level(logging.INFO, logger="self_grow_agent.capability.mysql_replication")

    with pytest.raises(CapabilityError, match="credentials are not configured") as raised:
        mysql_replication.rebuild_replication("127.0.0.1:3306")

    assert raised.value.code == "mysql_credentials_not_configured"
    assert raised.value.status_code == 503
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "step=validate_instance outcome=succeeded" in log_text
    assert "step=credentials outcome=failed" in log_text
    assert "missing=MYSQL_USER,MYSQL_PASSWORD" in log_text


def test_extracts_instance_from_real_alert_message_and_runs_operation(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw_message = """\
[active][error] 11:10AM
Name:ShopeeBinlogServer_Transactions_Total_Zero
Deploy: live
Message:Event : Binlog Server 事务未推进
AZ : ap-sg-1-general-b
Instance: 10.159.21.16:6606
Binlog Server UUID : 3eeb368b48a7f433
RDS Cluster UUID : ab95fc1a268dffc8
"""
    cursor = FakeCursor()
    connection = FakeConnection(cursor)
    monkeypatch.setattr(mysql_replication, "_connect", lambda **kwargs: connection)
    monkeypatch.setenv("MYSQL_USER", "replication-operator")
    monkeypatch.setenv("MYSQL_PASSWORD", secrets.token_urlsafe(24))
    caplog.set_level(logging.INFO, logger="self_grow_agent.capability.mysql_replication")

    result = mysql_replication.rebuild_replication_from_message(raw_message)

    assert result["ok"] is True
    assert result["instance"] == "10.159.21.16:6606"
    assert cursor.statements == ["STOP REPLICA", "START REPLICA"]
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "step=parse_instance outcome=started" in log_text
    assert "instance=10.159.21.16:6606 step=parse_instance outcome=succeeded" in log_text
    assert "step=credentials outcome=succeeded" in log_text


def test_processes_each_unique_instance_from_aggregated_alert_message(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw_message = """\
[active][error]
Instance: 10.159.21.16:6606
[active][error]
Instance: 10.241.147.122:6606
[active][error]
Instance: 10.159.21.16:6606
"""
    connections: list[tuple[str, FakeConnection]] = []

    def connect(**kwargs: Any) -> FakeConnection:
        connection = FakeConnection(FakeCursor())
        connections.append((f"{kwargs['host']}:{kwargs['port']}", connection))
        return connection

    monkeypatch.setattr(mysql_replication, "_connect", connect)
    monkeypatch.setenv("MYSQL_USER", "replication-operator")
    monkeypatch.setenv("MYSQL_PASSWORD", secrets.token_urlsafe(24))
    caplog.set_level(logging.INFO, logger="self_grow_agent.capability.mysql_replication")

    result = mysql_replication.rebuild_replication_from_message(raw_message)

    assert result == {
        "ok": True,
        "instance_count": 2,
        "results": [
            {
                "ok": True,
                "instance": "10.159.21.16:6606",
                "attempts": 1,
                "steps": [
                    {"name": "stop_replica", "ok": True},
                    {"name": "start_replica", "ok": True},
                ],
            },
            {
                "ok": True,
                "instance": "10.241.147.122:6606",
                "attempts": 1,
                "steps": [
                    {"name": "stop_replica", "ok": True},
                    {"name": "start_replica", "ok": True},
                ],
            },
        ],
    }
    assert sorted(instance for instance, _ in connections) == sorted([
        "10.159.21.16:6606",
        "10.241.147.122:6606",
    ])
    assert all(connection.closed for _, connection in connections)
    assert all(
        connection._cursor.statements == ["STOP REPLICA", "START REPLICA"]
        for _, connection in connections
    )
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "step=parse_instance outcome=succeeded instance_count=2" in log_text
    assert "step=batch_instance instance_index=1 instance_count=2 outcome=started" in log_text
    assert "step=batch_instance instance_index=2 instance_count=2 outcome=succeeded" in log_text


def test_processes_aggregated_instances_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_message = "\n".join(
        f"[active][error]\nInstance: 10.0.0.{index}:3306"
        for index in range(1, 5)
    )
    lock = threading.Lock()
    active_connections = 0
    max_active_connections = 0

    def connect(**kwargs: Any) -> FakeConnection:
        nonlocal active_connections, max_active_connections
        with lock:
            active_connections += 1
            max_active_connections = max(max_active_connections, active_connections)
        try:
            time.sleep(0.05)
            return FakeConnection(FakeCursor())
        finally:
            with lock:
                active_connections -= 1

    monkeypatch.setattr(mysql_replication, "_connect", connect)
    monkeypatch.setenv("MYSQL_USER", "replication-operator")
    monkeypatch.setenv("MYSQL_PASSWORD", secrets.token_urlsafe(24))

    result = mysql_replication.rebuild_replication_from_message(raw_message)

    assert result["instance_count"] == 4
    assert [item["instance"] for item in result["results"]] == [
        f"10.0.0.{index}:3306" for index in range(1, 5)
    ]
    assert 1 < max_active_connections <= 4


def test_resolved_alert_is_skipped_without_connecting(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw_message = """\
[resolved][error] 2:52PM
Message:Event : Binlog Server 事务推进已恢复
Instance: 10.0.0.1:3306
"""
    monkeypatch.setattr(
        mysql_replication,
        "_connect",
        lambda **kwargs: pytest.fail(f"unexpected connect: {kwargs}"),
    )
    caplog.set_level(logging.INFO, logger="self_grow_agent.capability.mysql_replication")

    result = mysql_replication.rebuild_replication_from_message(raw_message)

    assert result == {
        "ok": True,
        "skipped": True,
        "reason": "alert is resolved",
        "instance_count": 0,
    }
    assert "outcome=skipped reason=alert_resolved" in "\n".join(
        record.getMessage() for record in caplog.records
    )


def test_mixed_alert_processes_only_active_instances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_message = """\
[resolved][error]
Instance: 10.0.0.1:3306
[active][error]
Instance: 10.0.0.2:3306
"""
    connected: list[str] = []

    def connect(**kwargs: Any) -> FakeConnection:
        connected.append(f"{kwargs['host']}:{kwargs['port']}")
        return FakeConnection(FakeCursor())

    monkeypatch.setattr(mysql_replication, "_connect", connect)
    monkeypatch.setenv("MYSQL_USER", "replication-operator")
    monkeypatch.setenv("MYSQL_PASSWORD", secrets.token_urlsafe(24))

    result = mysql_replication.rebuild_replication_from_message(raw_message)

    assert result["instance"] == "10.0.0.2:3306"
    assert connected == ["10.0.0.2:3306"]


@pytest.mark.parametrize(
    "raw_message",
    [
        None,
        "no instance here",
        "Instance: db.internal:3306",
        "Instance: 10.0.0.1:3306\nInstance: db.internal:3306",
    ],
)
def test_rejects_missing_or_invalid_instance_in_message(
    raw_message: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        mysql_replication,
        "_connect",
        lambda **kwargs: pytest.fail(f"unexpected connect: {kwargs}"),
    )

    with pytest.raises(CapabilityError, match="raw-message must contain") as raised:
        mysql_replication.rebuild_replication_from_message(raw_message)

    assert raised.value.code == "mysql_alert_instance_invalid"
    assert raised.value.status_code == 422


def test_rejects_aggregated_message_above_instance_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_message = "\n".join(
        f"Instance: 10.0.0.{index}:3306" for index in range(1, 18)
    )
    monkeypatch.setattr(
        mysql_replication,
        "_connect",
        lambda **kwargs: pytest.fail(f"unexpected connect: {kwargs}"),
    )

    with pytest.raises(CapabilityError, match="1 to 16 valid Instance") as raised:
        mysql_replication.rebuild_replication_from_message(raw_message)

    assert raised.value.code == "mysql_alert_instance_invalid"
    assert raised.value.status_code == 422


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

    with pytest.raises(CapabilityError, match="MySQL replication operation failed") as raised:
        mysql_replication.rebuild_replication("127.0.0.1:3306", retries=2)

    assert raised.value.code == "mysql_replication_failed"
    assert raised.value.status_code == 502
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
    with pytest.raises(ValueError, match="retries must be an integer between 0 and 2"):
        mysql_replication.rebuild_replication_from_message(
            "Instance: 127.0.0.1:3306", retries=retries  # type: ignore[arg-type]
        )
