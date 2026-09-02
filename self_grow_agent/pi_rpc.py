"""Safe, one-shot client for Pi's JSONL RPC mode."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import signal
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
_MAX_EVENT_STREAM_BYTES = 8_388_608
_MAX_EVENTS = 10_000
_MAX_STDERR_BYTES = 16_384
_GRACEFUL_EXIT_SECONDS = 0.25
_TERMINATE_SECONDS = 0.5


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
        self._workspace_root = Path(workspace_root).expanduser().resolve()

    async def run(self, prompt: str) -> PiRpcResult:
        """Run ``prompt`` in a fresh workspace and wait until Pi fully settles."""

        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be a non-empty string")

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
                return await self._run_process(prompt, workspace_dir, agent_dir, run_dir)
        except PiRpcError:
            raise
        except asyncio.CancelledError:
            raise
        except OSError:
            raise PiRpcProcessError("Pi RPC workspace could not be prepared") from None

    async def _run_process(
        self,
        prompt: str,
        workspace_dir: Path,
        agent_dir: Path,
        run_dir: Path,
    ) -> PiRpcResult:
        request_id = f"prompt_{uuid.uuid4().hex}"
        command_line = _encode_command({"id": request_id, "type": "prompt", "message": prompt})
        if len(command_line) > _MAX_JSONL_BYTES + 1:
            raise PiRpcProtocolError("Pi RPC prompt is too large")

        process: asyncio.subprocess.Process | None = None
        stderr_task: asyncio.Task[bytes] | None = None
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
                if process.stdin is None or process.stdout is None or process.stderr is None:
                    raise PiRpcProcessError("Pi RPC pipes could not be opened")
                stderr_task = asyncio.create_task(_read_bounded_stderr(process.stderr))
                process.stdin.write(command_line)
                await process.stdin.drain()
                return await _read_result(process.stdout, request_id)
        except TimeoutError:
            raise PiRpcTimeoutError("Pi RPC run timed out") from None
        except FileNotFoundError:
            raise PiRpcExecutableNotFound("Pi executable was not found") from None
        except PiRpcError:
            raise
        except asyncio.CancelledError:
            raise
        except (BrokenPipeError, ConnectionError, OSError):
            raise PiRpcProcessError("Pi RPC process communication failed") from None
        finally:
            if process is not None:
                await _finish_cleanup(process, stderr_task)

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


async def _read_result(
    stdout: asyncio.StreamReader,
    request_id: str,
) -> PiRpcResult:
    events: list[dict[str, object]] = []
    acknowledged = False
    settled = False
    final_assistant: dict[str, object] | None = None
    total_bytes = 0

    while not (acknowledged and settled):
        event, event_bytes = await _read_event(stdout)
        total_bytes += event_bytes
        if total_bytes > _MAX_EVENT_STREAM_BYTES:
            raise PiRpcProtocolError("Pi RPC event stream is too large")
        events.append(event)
        if len(events) > _MAX_EVENTS:
            raise PiRpcProtocolError("Pi RPC emitted too many events")

        event_type = event.get("type")
        if not isinstance(event_type, str):
            raise PiRpcProtocolError("Pi RPC event has an invalid type")

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
        elif event_type == "message_end":
            message = event.get("message")
            if not isinstance(message, dict):
                raise PiRpcProtocolError("Pi RPC message_end event is invalid")
            if message.get("role") == "assistant":
                final_assistant = message
        elif event_type == "agent_settled":
            settled = True

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


async def _read_bounded_stderr(stderr: asyncio.StreamReader) -> bytes:
    retained = bytearray()
    while chunk := await stderr.read(8192):
        remaining = _MAX_STDERR_BYTES - len(retained)
        if remaining > 0:
            retained.extend(chunk[:remaining])
    return bytes(retained)


async def _finish_cleanup(
    process: asyncio.subprocess.Process,
    stderr_task: asyncio.Task[bytes] | None,
) -> None:
    cleanup_task = asyncio.create_task(_cleanup_process(process, stderr_task))
    interrupted = False
    while not cleanup_task.done():
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            interrupted = True
    try:
        cleanup_task.result()
    except Exception:
        pass
    if interrupted:
        raise asyncio.CancelledError


async def _cleanup_process(
    process: asyncio.subprocess.Process,
    stderr_task: asyncio.Task[bytes] | None,
) -> None:
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
        _signal_process_tree(process, force=False)
        exited = await _wait_for_process_tree(wait_task, process, _TERMINATE_SECONDS)
    if not exited:
        _signal_process_tree(process, force=True)
        await _wait_for_process_tree(wait_task, process, _TERMINATE_SECONDS)
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
                stderr_task.result()
            except (Exception, asyncio.CancelledError):
                pass


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
