from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

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


def _abrupt_exit_worker(
    source: str,
    module_name: str,
    request: dict[str, object],
) -> None:
    del source, module_name, request
    os._exit(7)


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

    assert str(raised.value) == "generated handler raised ZeroDivisionError"
    assert "return 1 / 0" not in str(raised.value)
    assert raised.value.__cause__ is None


def test_terminates_worker_after_wall_clock_timeout() -> None:
    executor = ProcessHandlerExecutor(timeout_seconds=0.1, worker=_blocking_worker)
    started_at = time.monotonic()

    with pytest.raises(HandlerTimeoutError, match="timed out"):
        executor.execute(VALID_SOURCE, "slow_v1", {})

    assert time.monotonic() - started_at < 2


def test_logs_process_stage_and_exit_code_when_worker_exits_before_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    executor = ProcessHandlerExecutor(
        timeout_seconds=5,
        worker=_abrupt_exit_worker,
    )
    caplog.set_level(logging.INFO, logger="uvicorn.error")

    with pytest.raises(HandlerProcessError) as raised:
        executor.execute(VALID_SOURCE, "abrupt_exit_v1", {})

    assert str(raised.value) == "Generated handler process failed"
    process_logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "generated_handler_process failed stage=receive" in process_logs
    assert "exit_code=7" in process_logs
    assert "signal=None" in process_logs
    assert "error_type=EOFError" in process_logs


def test_spawned_handler_import_does_not_recover_active_operation(
    tmp_path: Path,
) -> None:
    entrypoint = tmp_path / "spawn_entrypoint.py"
    entrypoint.write_text(
        textwrap.dedent(
            '''\
            import json

            from config import load_settings
            from self_grow_agent.api import create_app
            from self_grow_agent.executor import ProcessHandlerExecutor
            from self_grow_agent.metadata import RequirementStore

            settings = load_settings()
            app = create_app(settings=settings)


            def run():
                store = RequirementStore(settings.metadata_db_path)
                requirement = store.create("Greeting", "Say hello", "/hello", "GET")
                operation = store.create_operation(
                    requirement.id,
                    kind="create",
                    instruction=requirement.instruction,
                    path=requirement.path,
                    method=requirement.method,
                    project=requirement.project,
                )
                store.begin_operation(operation.id)
                result = ProcessHandlerExecutor(timeout_seconds=5).execute(
                    'def handle(request):\\n    return {"message": "hello"}\\n',
                    "spawn_import_v1",
                    {},
                )
                print(json.dumps({
                    "operation_status": store.get_operation(operation.id).status,
                    "result": result,
                }))


            if __name__ == "__main__":
                run()
            '''
        ),
        encoding="utf-8",
    )
    environment = os.environ.copy()
    repository_root = Path(__file__).resolve().parents[1]
    python_path = [str(repository_root)]
    if environment.get("PYTHONPATH"):
        python_path.append(environment["PYTHONPATH"])
    environment.update(
        {
            "GENERATED_DIR": str(tmp_path / "generated"),
            "METADATA_DB_PATH": str(tmp_path / "runtime-metadata.sqlite3"),
            "LLM_API_KEY": "",
            "PYTHONPATH": os.pathsep.join(python_path),
        }
    )

    completed = subprocess.run(
        [sys.executable, str(entrypoint)],
        cwd=repository_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout.strip().splitlines()[-1]) == {
        "operation_status": "implementing",
        "result": {"message": "hello"},
    }


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
