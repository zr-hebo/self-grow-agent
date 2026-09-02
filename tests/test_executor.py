from __future__ import annotations

import time

import pytest

from self_grow_agent.executor import (
    HandlerProcessError,
    HandlerTimeoutError,
    ProcessHandlerExecutor,
)

VALID_SOURCE = '''\
def handle(request):
    name = get(request["query"], "name", "world")
    return {"message": "hello " + str(name)}
'''


def _blocking_worker(
    source: str,
    module_name: str,
    request: dict[str, object],
) -> dict[str, str]:
    del source, module_name, request
    time.sleep(10)
    return {"status": "too late"}


def test_executes_reloaded_handler_in_subprocess() -> None:
    executor = ProcessHandlerExecutor(timeout_seconds=5)

    result = executor.execute(
        VALID_SOURCE,
        "hello_v1",
        {"query": {"name": "Ada"}},
    )

    assert result == {"message": "hello Ada"}


def test_returns_only_generic_error_for_handler_exception() -> None:
    executor = ProcessHandlerExecutor(timeout_seconds=5)
    source = '''\
def handle(request):
    return 1 / 0
'''

    with pytest.raises(HandlerProcessError) as raised:
        executor.execute(source, "broken_v1", {})

    assert str(raised.value) == "Generated handler process failed"
    assert "ZeroDivisionError" not in str(raised.value)
    assert raised.value.__cause__ is None


def test_terminates_worker_after_wall_clock_timeout() -> None:
    executor = ProcessHandlerExecutor(timeout_seconds=0.1, worker=_blocking_worker)
    started_at = time.monotonic()

    with pytest.raises(HandlerTimeoutError, match="timed out"):
        executor.execute(VALID_SOURCE, "slow_v1", {})

    assert time.monotonic() - started_at < 2


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"timeout_seconds": 0}, "timeout_seconds"),
        ({"timeout_seconds": float("inf")}, "timeout_seconds"),
        ({"timeout_seconds": True}, "timeout_seconds"),
        ({"timeout_seconds": 1, "memory_limit_bytes": 0}, "memory_limit_bytes"),
        ({"timeout_seconds": 1, "memory_limit_bytes": True}, "memory_limit_bytes"),
        ({"timeout_seconds": 1, "cpu_limit_seconds": 0}, "cpu_limit_seconds"),
        ({"timeout_seconds": 1, "cpu_limit_seconds": 1.5}, "cpu_limit_seconds"),
        ({"timeout_seconds": 1, "max_result_bytes": 0}, "max_result_bytes"),
        ({"timeout_seconds": 1, "max_result_bytes": True}, "max_result_bytes"),
    ],
)
def test_rejects_invalid_resource_configuration(
    kwargs: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        ProcessHandlerExecutor(**kwargs)  # type: ignore[arg-type]


def test_accepts_positive_resource_configuration() -> None:
    ProcessHandlerExecutor(
        timeout_seconds=1,
        memory_limit_bytes=128 * 1024 * 1024,
        cpu_limit_seconds=1,
        max_result_bytes=1024,
    )


def test_rejects_result_larger_than_configured_json_limit() -> None:
    executor = ProcessHandlerExecutor(timeout_seconds=5, max_result_bytes=64)
    source = '''\
def handle(request):
    return {"value": "x" * 200}
'''

    with pytest.raises(HandlerProcessError, match="process failed"):
        executor.execute(source, "large_result_v1", {})
