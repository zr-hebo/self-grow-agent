"""Controlled MySQL replication operations for generated API plugins."""

from __future__ import annotations

import ipaddress
import logging
import os
import time
from typing import Any

from self_grow_agent.capabilities.errors import CapabilityError

_LOGGER = logging.getLogger("self_grow_agent.capability.mysql_replication")
_STATEMENTS = (
    ("stop_replica", "STOP REPLICA"),
    ("start_replica", "START REPLICA"),
)
_MAX_RETRIES = 2
_MAX_INSTANCES_PER_MESSAGE = 16


def rebuild_replication_from_message(
    raw_message: object, *, retries: int = 2
) -> dict[str, Any]:
    """Extract unique instances from an alert message and restart replication.

    Generated handlers should use this entry point for ``raw-message`` requests so
    parsing, validation, database access, retries, and logs all remain platform-owned.
    A single-instance message retains the original response shape. Aggregated messages
    return an ordered result for every unique instance.
    """

    _validate_retries(retries)
    started_at = time.monotonic()
    message_chars = len(raw_message) if isinstance(raw_message, str) else 0
    _LOGGER.info(
        "mysql_replication step=parse_instance outcome=started message_chars=%s",
        message_chars,
    )
    instances = _instances_from_message(raw_message)
    if instances is None:
        _LOGGER.warning(
            "mysql_replication step=parse_instance outcome=failed "
            "reason=missing_invalid_or_too_many max_instances=%s "
            "elapsed_seconds=%.3f",
            _MAX_INSTANCES_PER_MESSAGE,
            time.monotonic() - started_at,
        )
        raise CapabilityError("mysql_alert_instance_invalid")
    _LOGGER.info(
        "mysql_replication step=parse_instance outcome=succeeded instance_count=%s "
        "elapsed_seconds=%.3f",
        len(instances),
        time.monotonic() - started_at,
    )
    for instance in instances:
        _LOGGER.info(
            "mysql_replication instance=%s step=parse_instance outcome=succeeded",
            instance,
        )
    if len(instances) == 1:
        return rebuild_replication(instances[0], retries=retries)

    results: list[dict[str, Any]] = []
    for index, instance in enumerate(instances, start=1):
        instance_started = time.monotonic()
        _LOGGER.info(
            "mysql_replication instance=%s step=batch_instance instance_index=%s "
            "instance_count=%s outcome=started",
            instance,
            index,
            len(instances),
        )
        try:
            result = rebuild_replication(instance, retries=retries)
        except CapabilityError:
            _LOGGER.warning(
                "mysql_replication instance=%s step=batch_instance instance_index=%s "
                "instance_count=%s outcome=failed elapsed_seconds=%.3f",
                instance,
                index,
                len(instances),
                time.monotonic() - instance_started,
            )
            raise
        results.append(result)
        _LOGGER.info(
            "mysql_replication instance=%s step=batch_instance instance_index=%s "
            "instance_count=%s outcome=succeeded elapsed_seconds=%.3f",
            instance,
            index,
            len(instances),
            time.monotonic() - instance_started,
        )
    return {
        "ok": True,
        "instance_count": len(instances),
        "results": results,
    }


def rebuild_replication(instance: str, *, retries: int = 2) -> dict[str, Any]:
    """Restart replication on one validated address using only fixed SQL statements.

    Credentials are intentionally read from the worker process environment instead of
    accepting them from generated code. The return value is JSON-compatible and never
    includes credentials or raw database exception messages.
    """

    _validate_retries(retries)
    target = _parse_instance(instance)
    if target is None:
        _LOGGER.warning(
            "mysql_replication step=validate_instance outcome=failed "
            "reason=invalid_ip_or_port"
        )
        raise CapabilityError("mysql_instance_invalid")
    host, port = target
    normalized_instance = f"{host}:{port}"
    _LOGGER.info(
        "mysql_replication instance=%s step=validate_instance outcome=succeeded",
        normalized_instance,
    )
    user = os.environ.get("MYSQL_USER", "")
    password = os.environ.get("MYSQL_PASSWORD", "")
    if not user or not password:
        missing = ",".join(
            name
            for name, value in (("MYSQL_USER", user), ("MYSQL_PASSWORD", password))
            if not value
        )
        _LOGGER.warning(
            "mysql_replication instance=%s step=credentials outcome=failed missing=%s",
            normalized_instance,
            missing,
        )
        raise CapabilityError("mysql_credentials_not_configured")
    _LOGGER.info(
        "mysql_replication instance=%s step=credentials outcome=succeeded",
        normalized_instance,
    )

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
                raise CapabilityError("mysql_replication_failed") from None
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


def _validate_retries(retries: object) -> None:
    if (
        isinstance(retries, bool)
        or not isinstance(retries, int)
        or not 0 <= retries <= _MAX_RETRIES
    ):
        raise ValueError("retries must be an integer between 0 and 2")


def _instances_from_message(raw_message: object) -> tuple[str, ...] | None:
    if not isinstance(raw_message, str):
        return None
    candidates: list[str] = []
    for line in raw_message.splitlines():
        label, separator, value = line.partition(":")
        if separator and label.strip().casefold() == "instance":
            candidates.append(value.strip())
    if not candidates:
        return None
    instances: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        target = _parse_instance(candidate)
        if target is None:
            return None
        host, port = target
        instance = f"{host}:{port}"
        if instance in seen:
            continue
        seen.add(instance)
        instances.append(instance)
        if len(instances) > _MAX_INSTANCES_PER_MESSAGE:
            return None
    return tuple(instances)


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


__all__ = ["rebuild_replication", "rebuild_replication_from_message"]
