"""Controlled MySQL replication operations for generated API plugins."""

from __future__ import annotations

import ipaddress
import logging
import os
import time
from typing import Any

_LOGGER = logging.getLogger("self_grow_agent.capability.mysql_replication")
_STATEMENTS = (
    ("stop_replica", "STOP REPLICA"),
    ("start_replica", "START REPLICA"),
)
_MAX_RETRIES = 2


def rebuild_replication(instance: str, *, retries: int = 2) -> dict[str, Any]:
    """Restart replication on one validated address using only fixed SQL statements.

    Credentials are intentionally read from the worker process environment instead of
    accepting them from generated code. The return value is JSON-compatible and never
    includes credentials or raw database exception messages.
    """

    if (
        isinstance(retries, bool)
        or not isinstance(retries, int)
        or not 0 <= retries <= _MAX_RETRIES
    ):
        raise ValueError("retries must be an integer between 0 and 2")
    target = _parse_instance(instance)
    if target is None:
        return {
            "ok": False,
            "error": "invalid MySQL instance; expected ip:port",
            "attempts": 0,
        }
    host, port = target
    normalized_instance = f"{host}:{port}"
    user = os.environ.get("MYSQL_USER", "")
    password = os.environ.get("MYSQL_PASSWORD", "")
    if not user or not password:
        return {
            "ok": False,
            "error": "MySQL capability credentials are not configured",
            "attempts": 0,
        }

    for attempt in range(1, retries + 2):
        connection: Any | None = None
        cursor: Any | None = None
        failed_step = "connect"
        attempt_started = time.monotonic()
        _log_step(normalized_instance, failed_step, attempt, "started", attempt_started)
        try:
            connection = _connect(
                host=host,
                port=port,
                user=user,
                password=password,
                connection_timeout=5,
                autocommit=True,
            )
            _log_step(normalized_instance, failed_step, attempt, "succeeded", attempt_started)
            cursor = connection.cursor()
            steps: list[dict[str, Any]] = []
            for step, statement in _STATEMENTS:
                failed_step = step
                step_started = time.monotonic()
                _log_step(normalized_instance, step, attempt, "started", step_started)
                cursor.execute(statement)
                steps.append({"name": step, "ok": True})
                _log_step(normalized_instance, step, attempt, "succeeded", step_started)
            return {
                "ok": True,
                "instance": normalized_instance,
                "attempts": attempt,
                "steps": steps,
            }
        except Exception as exc:
            _LOGGER.warning(
                "mysql_replication instance=%s step=%s attempt=%s outcome=failed "
                "error_type=%s elapsed_seconds=%.3f",
                normalized_instance,
                failed_step,
                attempt,
                type(exc).__name__,
                time.monotonic() - attempt_started,
            )
            if attempt > retries:
                return {
                    "ok": False,
                    "error": "MySQL replication operation failed",
                    "instance": normalized_instance,
                    "attempts": attempt,
                    "failed_step": failed_step,
                }
        finally:
            _close(cursor)
            _close(connection)

    raise AssertionError("retry loop did not return")  # pragma: no cover


def _parse_instance(instance: object) -> tuple[str, int] | None:
    if not isinstance(instance, str) or instance.count(":") != 1:
        return None
    host_text, separator, port_text = instance.partition(":")
    if not separator or not port_text.isascii() or not port_text.isdecimal():
        return None
    try:
        host = str(ipaddress.ip_address(host_text))
        port = int(port_text)
    except ValueError:
        return None
    if not 1 <= port <= 65_535:
        return None
    return host, port


def _connect(**kwargs: Any) -> Any:
    import mysql.connector

    return mysql.connector.connect(**kwargs)


def _close(resource: Any | None) -> None:
    if resource is None:
        return
    try:
        resource.close()
    except Exception as exc:
        _LOGGER.warning(
            "mysql_replication step=cleanup outcome=failed error_type=%s",
            type(exc).__name__,
        )


def _log_step(
    instance: str,
    step: str,
    attempt: int,
    outcome: str,
    started_at: float,
) -> None:
    _LOGGER.info(
        "mysql_replication instance=%s step=%s attempt=%s outcome=%s elapsed_seconds=%.3f",
        instance,
        step,
        attempt,
        outcome,
        time.monotonic() - started_at,
    )


__all__ = ["rebuild_replication"]
