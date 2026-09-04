"""Run generated request handlers in short-lived, resource-limited processes."""

from __future__ import annotations

import json
import logging
import math
import multiprocessing
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from typing import Any, Protocol

from self_grow_agent.code_loader import GeneratedCodeLoader, HandlerExecutionError

__all__ = [
    "HandlerExecutor",
    "HandlerProcessError",
    "HandlerTimeoutError",
    "ProcessHandlerExecutor",
]

HandlerWorker = Callable[[str, str, dict[str, Any]], Any]

_ERROR_RESPONSE = b'{"status":"error","message":"generated handler process failed"}'
_PROCESS_ERROR_MESSAGE = "Generated handler process failed"
_logger = logging.getLogger("uvicorn.error")


@dataclass(frozen=True)
class _ProcessCleanup:
    process_id: int | None
    observed_exit_code: int | None
    action: str
    final_exit_code: int | None


class HandlerExecutor(Protocol):
    """Execution boundary used by the API's dynamic-route dispatcher."""

    def execute(
        self,
        source: str,
        module_name: str,
        request: dict[str, Any],
    ) -> Any:
        """Execute one generated handler and return a JSON-compatible result."""
        ...


class HandlerProcessError(HandlerExecutionError):
    """A generated handler subprocess failed without exposing implementation details."""


class HandlerTimeoutError(HandlerProcessError):
    """A generated handler subprocess exceeded its wall-clock deadline."""


class _HandlerCpuLimitExceeded(BaseException):
    """Interrupt generated code at the soft CPU limit before the hard kill."""


class ProcessHandlerExecutor:
    """Execute one generated handler in a fresh subprocess.

    ``execute`` is synchronous so an async API should invoke it in a worker thread.
    A spawn context is used deliberately because forking an active API process from
    one of its threads can copy locks in an unsafe state.
    """

    def __init__(
        self,
        timeout_seconds: float,
        memory_limit_bytes: int | None = None,
        cpu_limit_seconds: int | None = None,
        max_result_bytes: int = 1024 * 1024,
        *,
        worker: HandlerWorker | None = None,
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a positive finite number")
        if memory_limit_bytes is not None and (
            isinstance(memory_limit_bytes, bool)
            or not isinstance(memory_limit_bytes, int)
            or memory_limit_bytes <= 0
        ):
            raise ValueError("memory_limit_bytes must be a positive integer or None")
        if cpu_limit_seconds is not None and (
            isinstance(cpu_limit_seconds, bool)
            or not isinstance(cpu_limit_seconds, int)
            or cpu_limit_seconds <= 0
        ):
            raise ValueError("cpu_limit_seconds must be a positive integer or None")
        if (
            isinstance(max_result_bytes, bool)
            or not isinstance(max_result_bytes, int)
            or max_result_bytes <= 0
        ):
            raise ValueError("max_result_bytes must be a positive integer")
        if worker is not None and not callable(worker):
            raise TypeError("worker must be callable or None")

        self._timeout_seconds = float(timeout_seconds)
        self._memory_limit_bytes = memory_limit_bytes
        self._cpu_limit_seconds = cpu_limit_seconds
        self._max_result_bytes = max_result_bytes
        self._worker = worker or _execute_generated_handler
        self._context = multiprocessing.get_context("spawn")

    def execute(
        self,
        source: str,
        module_name: str,
        request: dict[str, Any],
    ) -> Any:
        """Reload and invoke a handler, returning only its JSON-compatible result."""

        receive_connection, send_connection = self._context.Pipe(duplex=False)
        process = self._context.Process(
            target=_process_main,
            args=(
                send_connection,
                self._worker,
                source,
                module_name,
                request,
                self._memory_limit_bytes,
                self._cpu_limit_seconds,
                self._max_result_bytes,
            ),
            daemon=True,
        )
        started_at = time.monotonic()
        deadline = time.monotonic() + self._timeout_seconds
        started = False
        failure_stage: str | None = None
        failure_error_type: str | None = None
        try:
            try:
                process.start()
                started = True
            except Exception as exc:
                failure_stage = "start"
                failure_error_type = type(exc).__name__
                raise HandlerProcessError(_PROCESS_ERROR_MESSAGE) from None
            finally:
                send_connection.close()

            remaining = max(0.0, deadline - time.monotonic())
            if not receive_connection.poll(remaining):
                failure_stage = "timeout"
                raise HandlerTimeoutError(
                    "Generated handler execution timed out"
                ) from None

            try:
                encoded_response = receive_connection.recv_bytes(
                    maxlength=self._max_result_bytes
                )
                response = json.loads(encoded_response)
            except (EOFError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                failure_stage = "receive"
                failure_error_type = type(exc).__name__
                raise HandlerProcessError(_PROCESS_ERROR_MESSAGE) from None

            if type(response) is dict and response.get("status") == "error":
                message = response.get("message")
                if not isinstance(message, str) or not message:
                    message = _PROCESS_ERROR_MESSAGE
                failure_stage = "worker_response"
                failure_error_type = "worker_reported_error"
                raise HandlerProcessError(message) from None
            if (
                type(response) is not dict
                or set(response) != {"status", "result"}
                or response["status"] != "ok"
            ):
                failure_stage = "protocol"
                raise HandlerProcessError(_PROCESS_ERROR_MESSAGE) from None
            return response["result"]
        finally:
            receive_connection.close()
            cleanup = _ProcessCleanup(
                process_id=process.pid if started else None,
                observed_exit_code=None,
                action="none",
                final_exit_code=None,
            )
            if started:
                cleanup = _reap_process(process)
            if failure_stage is not None:
                _logger.warning(
                    "generated_handler_process failed stage=%s pid=%s "
                    "observed_exit_code=%s observed_signal=%s "
                    "cleanup_action=%s final_exit_code=%s final_signal=%s "
                    "error_type=%s timeout_seconds=%.3f memory_limit_bytes=%s "
                    "cpu_limit_seconds=%s elapsed_seconds=%.3f",
                    failure_stage,
                    cleanup.process_id,
                    cleanup.observed_exit_code,
                    _exit_signal_name(cleanup.observed_exit_code),
                    cleanup.action,
                    cleanup.final_exit_code,
                    _exit_signal_name(cleanup.final_exit_code),
                    failure_error_type,
                    self._timeout_seconds,
                    self._memory_limit_bytes,
                    self._cpu_limit_seconds,
                    time.monotonic() - started_at,
                )


def _execute_generated_handler(
    source: str,
    module_name: str,
    request: dict[str, Any],
) -> Any:
    handler = GeneratedCodeLoader().load(source, module_name)
    return handler(request)


def _process_main(
    connection: Connection,
    worker: HandlerWorker,
    source: str,
    module_name: str,
    request: dict[str, Any],
    memory_limit_bytes: int | None,
    cpu_limit_seconds: int | None,
    max_result_bytes: int,
) -> None:
    response = _ERROR_RESPONSE
    try:
        _apply_resource_limits(memory_limit_bytes, cpu_limit_seconds)
        result = worker(source, module_name, request)
        response = json.dumps(
            {"status": "ok", "result": result},
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(response) > max_result_bytes:
            response = _ERROR_RESPONSE
    except BaseException as exc:
        # Do not print generated-code tracebacks to stderr or return exception
        # messages, which could include request values. The exception class still
        # gives clients an actionable, non-secret failure category.
        response = json.dumps(
            {"status": "error", "message": _safe_worker_error_message(exc)},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

    try:
        connection.send_bytes(response)
    except BaseException:
        pass
    finally:
        connection.close()


def _safe_worker_error_message(exc: BaseException) -> str:
    """Return a non-secret, specific category for a worker exception."""

    if isinstance(exc, _HandlerCpuLimitExceeded):
        return "generated handler exceeded CPU limit"
    if isinstance(exc, HandlerExecutionError):
        return str(exc)
    return f"generated handler raised {type(exc).__name__}"


def _apply_resource_limits(
    memory_limit_bytes: int | None,
    cpu_limit_seconds: int | None,
) -> None:
    try:
        import resource
    except ImportError:  # pragma: no cover - Windows has no resource module
        return

    if memory_limit_bytes is not None:
        memory_resource = getattr(resource, "RLIMIT_AS", None)
        if memory_resource is None:
            memory_resource = getattr(resource, "RLIMIT_DATA", None)
        if memory_resource is not None:
            _lower_resource_limit(resource, memory_resource, memory_limit_bytes)

    if cpu_limit_seconds is not None and hasattr(resource, "RLIMIT_CPU"):
        _install_cpu_limit_handler()
        _lower_cpu_resource_limit(
            resource,
            resource.RLIMIT_CPU,
            cpu_limit_seconds,
        )


def _lower_resource_limit(resource_module: Any, resource_id: int, limit: int) -> None:
    try:
        current_soft, current_hard = resource_module.getrlimit(resource_id)
        infinity = resource_module.RLIM_INFINITY
        candidates = [limit]
        if current_soft != infinity:
            candidates.append(current_soft)
        if current_hard != infinity:
            candidates.append(current_hard)
        effective_limit = min(candidates)
        resource_module.setrlimit(resource_id, (effective_limit, effective_limit))
    except (OSError, OverflowError, ValueError):
        # Platforms differ in which limits they support and permit. The external
        # wall-clock timeout remains mandatory even when a kernel limit is not.
        pass


def _lower_cpu_resource_limit(
    resource_module: Any,
    resource_id: int,
    budget_seconds: int,
) -> None:
    """Apply a CPU budget relative to work already spent during spawn/import."""

    try:
        usage = resource_module.getrusage(resource_module.RUSAGE_SELF)
        consumed_seconds = max(0.0, float(usage.ru_utime) + float(usage.ru_stime))
        requested_soft = math.ceil(consumed_seconds) + budget_seconds
        requested_hard = requested_soft + 1
        current_soft, current_hard = resource_module.getrlimit(resource_id)
        infinity = resource_module.RLIM_INFINITY

        soft_candidates = [requested_soft]
        if current_soft != infinity:
            soft_candidates.append(current_soft)
        if current_hard != infinity:
            soft_candidates.append(current_hard)
        effective_soft = min(soft_candidates)

        hard_candidates = [requested_hard]
        if current_hard != infinity:
            hard_candidates.append(current_hard)
        effective_hard = max(effective_soft, min(hard_candidates))
        resource_module.setrlimit(
            resource_id,
            (effective_soft, effective_hard),
        )
    except (AttributeError, OSError, OverflowError, TypeError, ValueError):
        # The wall-clock timeout remains mandatory when CPU accounting is not
        # supported. Never fail a request merely because a platform cannot set it.
        pass


def _install_cpu_limit_handler() -> None:
    sigxcpu = getattr(signal, "SIGXCPU", None)
    if sigxcpu is None:
        return
    try:
        signal.signal(sigxcpu, _raise_cpu_limit_exceeded)
    except (OSError, ValueError):
        pass


def _raise_cpu_limit_exceeded(signum: int, frame: object) -> None:
    del signum, frame
    raise _HandlerCpuLimitExceeded


def _exit_signal_name(exit_code: int | None) -> str | None:
    if exit_code is None or exit_code >= 0:
        return None
    try:
        return signal.Signals(-exit_code).name
    except ValueError:
        return f"SIG{-exit_code}"


def _reap_process(process: BaseProcess) -> _ProcessCleanup:
    process_id = process.pid
    observed_exit_code = process.exitcode
    cleanup_action = "none"
    if observed_exit_code is None:
        cleanup_action = "terminate"
        process.terminate()
        process.join(timeout=0.2)
    else:
        process.join()

    if process.is_alive() and hasattr(process, "kill"):
        cleanup_action = "terminate_then_kill"
        process.kill()
        process.join(timeout=0.2)
    final_exit_code = process.exitcode
    if not process.is_alive():
        process.close()
    return _ProcessCleanup(
        process_id=process_id,
        observed_exit_code=observed_exit_code,
        action=cleanup_action,
        final_exit_code=final_exit_code,
    )
