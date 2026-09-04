"""Safe, one-shot client for Pi's JSONL RPC mode."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import signal
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from self_grow_agent.observability import current_generation_id, current_operation_id

_SAFE_PARENT_ENV = (
    "PATH",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "LANG",
    "LC_ALL",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)
_CREDENTIAL_ENV_NAME = re.compile(r"(?:[A-Z][A-Z0-9_]*_)?(?:API_KEY|TOKEN)\Z")
_MAX_JSONL_BYTES = 1_048_576
_DEFAULT_MAX_EVENT_STREAM_BYTES = 67_108_864
_MAX_RETAINED_EVENTS = 10_000
_STREAMING_EVENT_TYPES = frozenset(
    {
        "bash_execution_update",
        "message_update",
        "tool_execution_update",
    }
)
_GRACEFUL_EXIT_SECONDS = 0.25
_TERMINATE_SECONDS = 0.5
_logger = logging.getLogger("uvicorn.error")

SAFE_PI_RPC_FAILURE_MESSAGES = frozenset(
    {
        "Pi RPC assistant content is invalid",
        "Pi RPC assistant response is invalid",
        "Pi RPC assistant text is invalid",
        "Pi RPC emitted duplicate prompt responses",
        "Pi RPC emitted invalid JSON",
        "Pi RPC emitted too many events",
        "Pi RPC event has an invalid type",
        "Pi RPC event is too large",
        "Pi RPC event must be a JSON object",
        "Pi RPC event stream is too large",
        "Pi RPC message_end event is invalid",
        "Pi RPC pipes could not be opened",
        "Pi RPC process communication failed",
        "Pi RPC prompt is too large",
        "Pi RPC prompt response is invalid",
        "Pi RPC run timed out",
        "Pi RPC stream ended before agent_settled",
        "Pi RPC stream ended with an incomplete event",
        "Pi RPC workspace could not be prepared",
        "Pi agent did not complete successfully",
        "Pi agent did not produce a final assistant response",
        "Pi executable was not found",
        "Pi rejected the prompt command",
    }
)


class PiRpcError(RuntimeError):
    """Base class for safe-to-report Pi RPC failures."""


class PiRpcExecutableNotFound(PiRpcError):
    """The configured Pi command could not be found."""


class PiRpcProcessError(PiRpcError):
    """The Pi subprocess could not be started or communicated with."""


class PiRpcProtocolError(PiRpcError):
    """Pi emitted data that did not satisfy its RPC protocol."""


class PiRpcCommandError(PiRpcError):
    """Pi rejected the prompt before accepting it."""


class PiRpcAgentError(PiRpcError):
    """Pi accepted the prompt but the agent did not finish successfully."""


class PiRpcTimeoutError(PiRpcError):
    """Pi did not settle before the configured deadline."""


@dataclass(frozen=True, slots=True)
class PiRpcResult:
    """The final assistant text and bounded event stream from one Pi run."""

    final_text: str
    events: tuple[dict[str, object], ...] = ()


@dataclass(slots=True)
class _PiRpcProgress:
    event_count: int = 0
    retained_event_count: int = 0
    streaming_event_count: int = 0
    byte_count: int = 0
    prompt_accepted: bool = False
    agent_settled: bool = False
    assistant_messages: int = 0
    last_stop_reason_category: str = "none"


@dataclass(frozen=True, slots=True)
class _PiRpcCleanupResult:
    stderr_bytes: int | None
    action: str
    observed_exit_code: int | None
    final_exit_code: int | None


class PiRpcClient:
    """Run one isolated Pi RPC subprocess per prompt."""

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        provider_env_name: str,
        workspace_root: str | Path,
        command: tuple[str, ...] = ("pi",),
        api_key: str = "",
        timeout_seconds: float = 120.0,
        max_event_stream_bytes: int = _DEFAULT_MAX_EVENT_STREAM_BYTES,
    ) -> None:
        self._command = _validate_command(command)
        self._provider = _validate_cli_value(provider, "provider")
        self._model = _validate_cli_value(model, "model")
        self._provider_env_name = _validate_provider_env_name(provider_env_name)
        if not isinstance(api_key, str) or "\0" in api_key:
            raise ValueError("api_key must be a string without NUL characters")
        self._api_key = api_key
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a positive finite number")
        self._timeout_seconds = float(timeout_seconds)
        if (
            isinstance(max_event_stream_bytes, bool)
            or not isinstance(max_event_stream_bytes, int)
            or max_event_stream_bytes <= 0
        ):
            raise ValueError("max_event_stream_bytes must be a positive integer")
        self._max_event_stream_bytes = max_event_stream_bytes
        self._workspace_root = Path(workspace_root).expanduser().resolve()

    async def run(self, prompt: str) -> PiRpcResult:
        """Run ``prompt`` in a fresh workspace and wait until Pi fully settles."""

        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be a non-empty string")

        run_id = uuid.uuid4().hex
        operation_id = current_operation_id()
        generation_id = current_generation_id()
        started_at = time.monotonic()
        _logger.info(
            "pi_rpc run_queued operation_id=%s generation_id=%s run_id=%s "
            "provider=%r model=%r timeout_seconds=%.3f max_event_stream_bytes=%s "
            "prompt_chars=%s prompt_bytes=%s",
            operation_id,
            generation_id,
            run_id,
            self._provider,
            self._model,
            self._timeout_seconds,
            self._max_event_stream_bytes,
            len(prompt),
            len(prompt.encode("utf-8")),
        )
        try:
            self._workspace_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(
                prefix="pi-run-",
                dir=self._workspace_root,
            ) as run_dir_name:
                run_dir = Path(run_dir_name)
                workspace_dir = run_dir / "workspace"
                agent_dir = run_dir / "pi-agent"
                workspace_dir.mkdir(mode=0o700)
                agent_dir.mkdir(mode=0o700)
                workspace_dir.chmod(0o700)
                agent_dir.chmod(0o700)
                return await self._run_process(
                    prompt,
                    workspace_dir,
                    agent_dir,
                    run_dir,
                    operation_id=operation_id,
                    generation_id=generation_id,
                    run_id=run_id,
                    started_at=started_at,
                )
        except PiRpcError:
            raise
        except asyncio.CancelledError:
            raise
        except OSError:
            _logger.warning(
                "pi_rpc run_failed operation_id=%s generation_id=%s run_id=%s "
                "category=workspace_preparation_failed "
                "stage=workspace elapsed_seconds=%.3f",
                operation_id,
                generation_id,
                run_id,
                time.monotonic() - started_at,
            )
            raise PiRpcProcessError("Pi RPC workspace could not be prepared") from None

    async def _run_process(
        self,
        prompt: str,
        workspace_dir: Path,
        agent_dir: Path,
        run_dir: Path,
        *,
        operation_id: str | None,
        generation_id: str | None,
        run_id: str,
        started_at: float,
    ) -> PiRpcResult:
        request_id = f"prompt_{run_id}"
        command_line = _encode_command({"id": request_id, "type": "prompt", "message": prompt})
        if len(command_line) > _MAX_JSONL_BYTES + 1:
            _logger.warning(
                "pi_rpc run_failed operation_id=%s generation_id=%s run_id=%s "
                "category=prompt_too_large "
                "stage=encoding event_count=0 byte_count=0 stderr_bytes=0 "
                "elapsed_seconds=%.3f",
                operation_id,
                generation_id,
                run_id,
                time.monotonic() - started_at,
            )
            raise PiRpcProtocolError("Pi RPC prompt is too large")

        process: asyncio.subprocess.Process | None = None
        stderr_task: asyncio.Task[int] | None = None
        progress = _PiRpcProgress()
        outcome = "failed"
        category = "unexpected"
        try:
            async with asyncio.timeout(self._timeout_seconds):
                process = await asyncio.create_subprocess_exec(
                    *self._argv(),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=workspace_dir,
                    env=self._environment(agent_dir, run_dir),
                    limit=_MAX_JSONL_BYTES + 1,
                    **_process_group_options(),
                )
                _logger.info(
                    "pi_rpc process_started operation_id=%s generation_id=%s "
                    "run_id=%s pid=%s "
                    "provider=%r model=%r "
                    "timeout_seconds=%.3f",
                    operation_id,
                    generation_id,
                    run_id,
                    process.pid,
                    self._provider,
                    self._model,
                    self._timeout_seconds,
                )
                if process.stdin is None or process.stdout is None or process.stderr is None:
                    raise PiRpcProcessError("Pi RPC pipes could not be opened")
                stderr_task = asyncio.create_task(_count_stderr_bytes(process.stderr))
                process.stdin.write(command_line)
                await process.stdin.drain()
                result = await _read_result(
                    process.stdout,
                    request_id,
                    operation_id=operation_id,
                    generation_id=generation_id,
                    run_id=run_id,
                    progress=progress,
                    max_event_stream_bytes=self._max_event_stream_bytes,
                )
                outcome = "completed"
                category = "success"
                return result
        except TimeoutError:
            category = "timeout"
            raise PiRpcTimeoutError("Pi RPC run timed out") from None
        except FileNotFoundError:
            category = "executable_not_found"
            raise PiRpcExecutableNotFound("Pi executable was not found") from None
        except PiRpcError as exc:
            category = _safe_pi_rpc_failure_category(exc)
            raise
        except asyncio.CancelledError:
            category = "cancelled"
            raise
        except (BrokenPipeError, ConnectionError, OSError):
            category = "process_communication_failed"
            raise PiRpcProcessError("Pi RPC process communication failed") from None
        finally:
            cleanup = _PiRpcCleanupResult(
                stderr_bytes=0 if stderr_task is None else None,
                action="not_started",
                observed_exit_code=process.returncode if process is not None else None,
                final_exit_code=process.returncode if process is not None else None,
            )
            if process is not None:
                try:
                    cleanup = await _finish_cleanup(
                        process,
                        stderr_task,
                        operation_id=operation_id,
                        generation_id=generation_id,
                        run_id=run_id,
                    )
                except asyncio.CancelledError:
                    _log_pi_rpc_outcome(
                        outcome="failed",
                        operation_id=operation_id,
                        generation_id=generation_id,
                        run_id=run_id,
                        category="cancelled",
                        process=process,
                        progress=progress,
                        cleanup=cleanup,
                        started_at=started_at,
                    )
                    raise
            _log_pi_rpc_outcome(
                outcome=outcome,
                operation_id=operation_id,
                generation_id=generation_id,
                run_id=run_id,
                category=category,
                process=process,
                progress=progress,
                cleanup=cleanup,
                started_at=started_at,
            )

    def _argv(self) -> tuple[str, ...]:
        return (
            *self._command,
            "--mode",
            "rpc",
            "--no-session",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-context-files",
            "--no-approve",
            "--no-tools",
            "--provider",
            self._provider,
            "--model",
            self._model,
        )

    def _environment(self, agent_dir: Path, run_dir: Path) -> dict[str, str]:
        env = {name: os.environ[name] for name in _SAFE_PARENT_ENV if name in os.environ}
        env.setdefault("PATH", os.defpath)
        env.update(
            {
                "PI_CODING_AGENT_DIR": str(agent_dir),
                "PI_SKIP_VERSION_CHECK": "1",
                "PI_TELEMETRY": "0",
                "TMPDIR": str(run_dir),
                "TEMP": str(run_dir),
                "TMP": str(run_dir),
            }
        )
        if self._api_key:
            env[self._provider_env_name] = self._api_key
        return env


def _safe_pi_rpc_failure_category(exc: PiRpcError) -> str:
    """Return a stable category without trusting provider-authored exception text."""

    message = str(exc)
    if message in SAFE_PI_RPC_FAILURE_MESSAGES:
        return re.sub(r"[^a-z0-9]+", "_", message.casefold()).strip("_")
    if isinstance(exc, PiRpcProtocolError):
        return "protocol_error"
    if isinstance(exc, PiRpcCommandError):
        return "command_error"
    if isinstance(exc, PiRpcAgentError):
        return "agent_error"
    if isinstance(exc, PiRpcProcessError):
        return "process_error"
    return "rpc_error"


def _assistant_message_categories(stop_reason: object) -> tuple[str, str]:
    categories = {
        "stop": ("stop", "completed"),
        "toolUse": ("tool_use", "continuation"),
        "error": ("error", "failed"),
        "aborted": ("aborted", "failed"),
        "length": ("length", "incomplete"),
    }
    if isinstance(stop_reason, str):
        return categories.get(stop_reason, ("other", "unknown"))
    return "invalid", "invalid"


def _log_pi_rpc_outcome(
    *,
    outcome: str,
    operation_id: str | None,
    generation_id: str | None,
    run_id: str,
    category: str,
    process: asyncio.subprocess.Process | None,
    progress: _PiRpcProgress,
    cleanup: _PiRpcCleanupResult,
    started_at: float,
) -> None:
    log = _logger.info if outcome == "completed" else _logger.warning
    log(
        "pi_rpc run_%s operation_id=%s generation_id=%s run_id=%s category=%s "
        "pid=%s observed_exit_code=%s final_exit_code=%s final_signal=%s "
        "cleanup_action=%s "
        "prompt_accepted=%s agent_settled=%s assistant_messages=%s "
        "last_stop_reason_category=%s event_count=%s retained_event_count=%s "
        "streaming_event_count=%s byte_count=%s "
        "stderr_bytes=%s elapsed_seconds=%.3f",
        outcome,
        operation_id,
        generation_id,
        run_id,
        category,
        process.pid if process is not None else None,
        cleanup.observed_exit_code,
        cleanup.final_exit_code,
        _exit_signal_name(cleanup.final_exit_code),
        cleanup.action,
        progress.prompt_accepted,
        progress.agent_settled,
        progress.assistant_messages,
        progress.last_stop_reason_category,
        progress.event_count,
        progress.retained_event_count,
        progress.streaming_event_count,
        progress.byte_count,
        cleanup.stderr_bytes,
        time.monotonic() - started_at,
    )


async def _read_result(
    stdout: asyncio.StreamReader,
    request_id: str,
    *,
    operation_id: str | None,
    generation_id: str | None,
    run_id: str,
    progress: _PiRpcProgress,
    max_event_stream_bytes: int,
) -> PiRpcResult:
    events: list[dict[str, object]] = []
    acknowledged = False
    settled = False
    final_assistant: dict[str, object] | None = None
    total_bytes = 0
    observed_event_count = 0

    while not (acknowledged and settled):
        event, event_bytes = await _read_event(stdout)
        total_bytes += event_bytes
        observed_event_count += 1
        progress.event_count = observed_event_count
        progress.byte_count = total_bytes
        if total_bytes > max_event_stream_bytes:
            raise PiRpcProtocolError("Pi RPC event stream is too large")

        event_type = event.get("type")
        if not isinstance(event_type, str):
            raise PiRpcProtocolError("Pi RPC event has an invalid type")
        if event_type in _STREAMING_EVENT_TYPES:
            progress.streaming_event_count += 1
        else:
            events.append(event)
            progress.retained_event_count = len(events)
            if len(events) > _MAX_RETAINED_EVENTS:
                raise PiRpcProtocolError("Pi RPC emitted too many events")

        if event_type == "response" and event.get("id") == request_id:
            if acknowledged:
                raise PiRpcProtocolError("Pi RPC emitted duplicate prompt responses")
            success = event.get("success")
            if success is False:
                raise PiRpcCommandError("Pi rejected the prompt command")
            if success is not True:
                raise PiRpcProtocolError("Pi RPC prompt response is invalid")
            if event.get("command") != "prompt":
                raise PiRpcProtocolError("Pi RPC prompt response is invalid")
            acknowledged = True
            progress.prompt_accepted = True
            _logger.info(
                "pi_rpc prompt_accepted operation_id=%s generation_id=%s run_id=%s "
                "event_count=%s byte_count=%s",
                operation_id,
                generation_id,
                run_id,
                progress.event_count,
                progress.byte_count,
            )
        elif event_type == "message_end":
            message = event.get("message")
            if not isinstance(message, dict):
                raise PiRpcProtocolError("Pi RPC message_end event is invalid")
            if message.get("role") == "assistant":
                final_assistant = message
                progress.assistant_messages += 1
                stop_reason_category, message_category = _assistant_message_categories(
                    message.get("stopReason")
                )
                progress.last_stop_reason_category = stop_reason_category
                _logger.info(
                    "pi_rpc assistant_message operation_id=%s generation_id=%s "
                    "run_id=%s stop_reason_category=%s "
                    "message_category=%s assistant_messages=%s event_count=%s byte_count=%s",
                    operation_id,
                    generation_id,
                    run_id,
                    stop_reason_category,
                    message_category,
                    progress.assistant_messages,
                    progress.event_count,
                    progress.byte_count,
                )
        elif event_type == "agent_settled":
            settled = True
            progress.agent_settled = True
            _logger.info(
                "pi_rpc agent_settled operation_id=%s generation_id=%s run_id=%s "
                "event_count=%s byte_count=%s "
                "assistant_messages=%s",
                operation_id,
                generation_id,
                run_id,
                progress.event_count,
                progress.byte_count,
                progress.assistant_messages,
            )

    if final_assistant is None:
        raise PiRpcAgentError("Pi agent did not produce a final assistant response")
    stop_reason = final_assistant.get("stopReason")
    if not isinstance(stop_reason, str):
        raise PiRpcProtocolError("Pi RPC assistant response is invalid")
    if stop_reason != "stop":
        raise PiRpcAgentError("Pi agent did not complete successfully")

    return PiRpcResult(
        final_text=_assistant_text(final_assistant),
        events=tuple(events),
    )


async def _read_event(stdout: asyncio.StreamReader) -> tuple[dict[str, object], int]:
    try:
        raw_line = await stdout.readline()
    except (ValueError, asyncio.LimitOverrunError):
        raise PiRpcProtocolError("Pi RPC event is too large") from None
    if not raw_line:
        raise PiRpcProtocolError("Pi RPC stream ended before agent_settled")
    if not raw_line.endswith(b"\n"):
        raise PiRpcProtocolError("Pi RPC stream ended with an incomplete event")
    if len(raw_line) > _MAX_JSONL_BYTES + 1:
        raise PiRpcProtocolError("Pi RPC event is too large")

    try:
        value = json.loads(
            raw_line[:-1].decode("utf-8"),
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError, RecursionError):
        raise PiRpcProtocolError("Pi RPC emitted invalid JSON") from None
    if not isinstance(value, dict):
        raise PiRpcProtocolError("Pi RPC event must be a JSON object")
    return value, len(raw_line)


def _assistant_text(message: dict[str, object]) -> str:
    content = message.get("content")
    if not isinstance(content, list):
        raise PiRpcProtocolError("Pi RPC assistant content is invalid")
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            raise PiRpcProtocolError("Pi RPC assistant content is invalid")
        if block.get("type") == "text":
            text = block.get("text")
            if not isinstance(text, str):
                raise PiRpcProtocolError("Pi RPC assistant text is invalid")
            parts.append(text)
    return "".join(parts)


async def _count_stderr_bytes(stderr: asyncio.StreamReader) -> int:
    total_bytes = 0
    while chunk := await stderr.read(8192):
        total_bytes += len(chunk)
    return total_bytes


async def _finish_cleanup(
    process: asyncio.subprocess.Process,
    stderr_task: asyncio.Task[int] | None,
    *,
    operation_id: str | None,
    generation_id: str | None,
    run_id: str,
) -> _PiRpcCleanupResult:
    cleanup_started_at = time.monotonic()
    observed_exit_code = process.returncode
    _logger.info(
        "pi_rpc cleanup_started operation_id=%s generation_id=%s run_id=%s pid=%s "
        "observed_exit_code=%s observed_signal=%s",
        operation_id,
        generation_id,
        run_id,
        process.pid,
        observed_exit_code,
        _exit_signal_name(observed_exit_code),
    )
    cleanup_task = asyncio.create_task(
        _cleanup_process(
            process,
            stderr_task,
            operation_id=operation_id,
            generation_id=generation_id,
            run_id=run_id,
            observed_exit_code=observed_exit_code,
        )
    )
    interrupted = False
    while not cleanup_task.done():
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            interrupted = True
    try:
        result = cleanup_task.result()
    except Exception:
        result = _PiRpcCleanupResult(
            stderr_bytes=None,
            action="cleanup_error",
            observed_exit_code=observed_exit_code,
            final_exit_code=process.returncode,
        )
    _logger.info(
        "pi_rpc cleanup_completed operation_id=%s generation_id=%s run_id=%s pid=%s "
        "action=%s observed_exit_code=%s final_exit_code=%s final_signal=%s "
        "stderr_bytes=%s elapsed_seconds=%.3f interrupted=%s",
        operation_id,
        generation_id,
        run_id,
        process.pid,
        result.action,
        result.observed_exit_code,
        result.final_exit_code,
        _exit_signal_name(result.final_exit_code),
        result.stderr_bytes,
        time.monotonic() - cleanup_started_at,
        interrupted,
    )
    if interrupted:
        raise asyncio.CancelledError
    return result


async def _cleanup_process(
    process: asyncio.subprocess.Process,
    stderr_task: asyncio.Task[int] | None,
    *,
    operation_id: str | None,
    generation_id: str | None,
    run_id: str,
    observed_exit_code: int | None,
) -> _PiRpcCleanupResult:
    action = "already_exited" if observed_exit_code is not None else "graceful"
    stdin = process.stdin
    if stdin is not None and not stdin.is_closing() and process.returncode is None:
        try:
            stdin.write(
                _encode_command({"id": f"cleanup_{uuid.uuid4().hex}", "type": "clear_queue"})
            )
            stdin.write(_encode_command({"id": f"cleanup_{uuid.uuid4().hex}", "type": "abort"}))
            await asyncio.wait_for(stdin.drain(), timeout=0.1)
        except (BrokenPipeError, ConnectionError, OSError, TimeoutError):
            pass
    if stdin is not None and not stdin.is_closing():
        stdin.close()
        try:
            await asyncio.wait_for(stdin.wait_closed(), timeout=0.1)
        except (BrokenPipeError, ConnectionError, OSError, TimeoutError):
            pass

    wait_task = asyncio.create_task(process.wait())
    exited = await _wait_for_process_tree(wait_task, process, _GRACEFUL_EXIT_SECONDS)
    if not exited:
        action = "terminate"
        _logger.info(
            "pi_rpc cleanup_signal operation_id=%s generation_id=%s run_id=%s "
            "pid=%s action=terminate wait_seconds=%.3f",
            operation_id,
            generation_id,
            run_id,
            process.pid,
            _GRACEFUL_EXIT_SECONDS,
        )
        _signal_process_tree(process, force=False)
        exited = await _wait_for_process_tree(wait_task, process, _TERMINATE_SECONDS)
    if not exited:
        action = "kill"
        _logger.warning(
            "pi_rpc cleanup_signal operation_id=%s generation_id=%s run_id=%s "
            "pid=%s action=kill wait_seconds=%.3f",
            operation_id,
            generation_id,
            run_id,
            process.pid,
            _TERMINATE_SECONDS,
        )
        _signal_process_tree(process, force=True)
        exited = await _wait_for_process_tree(wait_task, process, _TERMINATE_SECONDS)
        if not exited:
            action = "kill_unreaped"
    if not wait_task.done():
        wait_task.cancel()
        try:
            await wait_task
        except asyncio.CancelledError:
            pass

    if stderr_task is not None:
        if not stderr_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(stderr_task), timeout=0.1)
            except TimeoutError:
                stderr_task.cancel()
                try:
                    await stderr_task
                except asyncio.CancelledError:
                    pass
        if stderr_task.done():
            try:
                stderr_bytes = stderr_task.result()
            except (Exception, asyncio.CancelledError):
                stderr_bytes = None
        else:
            stderr_bytes = None
    else:
        stderr_bytes = 0
    return _PiRpcCleanupResult(
        stderr_bytes=stderr_bytes,
        action=action,
        observed_exit_code=observed_exit_code,
        final_exit_code=process.returncode,
    )


def _exit_signal_name(exit_code: int | None) -> str | None:
    if exit_code is None or exit_code >= 0 or os.name != "posix":
        return None
    try:
        return signal.Signals(-exit_code).name
    except ValueError:
        return "UNKNOWN"


def _process_group_options() -> dict[str, object]:
    if os.name == "posix":
        return {"start_new_session": True}
    if os.name == "nt":
        creation_flag = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return {"creationflags": creation_flag}
    return {}


async def _wait_for_process_tree(
    wait_task: asyncio.Task[int],
    process: asyncio.subprocess.Process,
    timeout: float,
) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        leader_reaped = wait_task.done()
        if leader_reaped and not _process_tree_exists(process):
            return True
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(0.01, remaining))


def _process_tree_exists(process: asyncio.subprocess.Process) -> bool:
    if os.name != "posix":
        return process.returncode is None
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_process_tree(process: asyncio.subprocess.Process, *, force: bool) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
        elif force:
            process.kill()
        else:
            process.terminate()
    except ProcessLookupError:
        pass


def _encode_command(command: dict[str, object]) -> bytes:
    return (
        json.dumps(command, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(
            "utf-8"
        )
        + b"\n"
    )


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Non-standard JSON constant: {value}")


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _validate_command(command: tuple[str, ...]) -> tuple[str, ...]:
    if (
        not isinstance(command, tuple)
        or not command
        or any(not isinstance(value, str) or not value or "\0" in value for value in command)
    ):
        raise ValueError("command must be a non-empty tuple of valid arguments")
    return command


def _validate_cli_value(value: str, name: str) -> str:
    if not isinstance(value, str) or not value or value.startswith("-") or "\0" in value:
        raise ValueError(f"{name} must be a non-empty CLI value")
    return value


def _validate_provider_env_name(value: str) -> str:
    reserved = {
        *_SAFE_PARENT_ENV,
        "HOME",
        "PI_CODING_AGENT_DIR",
        "PI_SKIP_VERSION_CHECK",
        "PI_TELEMETRY",
    }
    if (
        not isinstance(value, str)
        or _CREDENTIAL_ENV_NAME.fullmatch(value) is None
        or value in reserved
        or value in {"TMPDIR", "TEMP", "TMP"}
    ):
        raise ValueError("provider_env_name must end with API_KEY or TOKEN")
    return value


__all__ = [
    "SAFE_PI_RPC_FAILURE_MESSAGES",
    "PiRpcAgentError",
    "PiRpcClient",
    "PiRpcCommandError",
    "PiRpcError",
    "PiRpcExecutableNotFound",
    "PiRpcProcessError",
    "PiRpcProtocolError",
    "PiRpcResult",
    "PiRpcTimeoutError",
]
