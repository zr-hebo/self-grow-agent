"""Sanitized subprocess boundary for published plugin execution."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from self_grow_agent.plugin_runtime import verify_plugin_artifact

_ENVIRONMENT_NAME = re.compile(r"[A-Z][A-Z0-9_]*\Z")
_RESERVED_ENVIRONMENT = frozenset(
    {
        "HOME",
        "PATH",
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTHONINSPECT",
        "PYTHONUSERBASE",
        "LD_PRELOAD",
        "DYLD_INSERT_LIBRARIES",
    }
)
_logger = logging.getLogger("uvicorn.error")
_MAX_PLUGIN_LOG_EVENTS = 64
_MAX_PLUGIN_LOG_MESSAGE_CHARS = 1_024
_PLUGIN_LOG_CREDENTIAL = re.compile(
    r"(?P<name>(?:password|passwd|token|api[_ -]?key|secret|credential))"
    r"(?P<separator>\s*[:=]\s*)(?P<value>[^\s,;]+)",
    flags=re.IGNORECASE,
)
_SENSITIVE_FIELD = re.compile(
    r"(?:password|passwd|token|api[_ -]?key|secret|credential)", re.IGNORECASE
)


class PluginProcessError(RuntimeError):
    """A plugin worker failed with a safe public category."""


class PluginTimeoutError(PluginProcessError):
    """A plugin worker exceeded its wall-clock deadline."""


class PluginExecutor(Protocol):
    """Execution boundary shared by local-process and container backends."""

    def execute(
        self,
        artifact_path: str | Path,
        artifact_digest: str,
        request: dict[str, Any],
    ) -> Any: ...


class PluginProcessExecutor:
    """Run one immutable plugin version with JSON stdin/stdout IPC."""

    def __init__(
        self,
        timeout_seconds: float,
        memory_limit_bytes: int | None = None,
        cpu_limit_seconds: int | None = None,
        max_result_bytes: int = 1_048_576,
        *,
        max_request_bytes: int = 1_048_576,
        allowed_environment: Mapping[str, str] | None = None,
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a positive finite number")
        for name, value in {
            "memory_limit_bytes": memory_limit_bytes,
            "cpu_limit_seconds": cpu_limit_seconds,
        }.items():
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
            ):
                raise ValueError(f"{name} must be a positive integer or None")
        for name, value in {
            "max_result_bytes": max_result_bytes,
            "max_request_bytes": max_request_bytes,
        }.items():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        environment = dict(allowed_environment or {})
        for name, value in environment.items():
            if (
                not isinstance(name, str)
                or _ENVIRONMENT_NAME.fullmatch(name) is None
                or name in _RESERVED_ENVIRONMENT
                or not isinstance(value, str)
            ):
                raise ValueError("plugin environment contains an invalid entry")
        self._timeout_seconds = float(timeout_seconds)
        self._memory_limit_bytes = memory_limit_bytes
        self._cpu_limit_seconds = cpu_limit_seconds
        self._max_result_bytes = max_result_bytes
        self._max_request_bytes = max_request_bytes
        self._allowed_environment = environment
        self._worker_path = Path(__file__).with_name("plugin_worker.py").resolve()
        self._project_root = self._worker_path.parent.parent

    def execute(
        self,
        artifact_path: str | Path,
        artifact_digest: str,
        request: dict[str, Any],
    ) -> Any:
        """Verify and invoke one plugin artifact, returning JSON-compatible data."""

        started_at = time.monotonic()
        artifact = Path(artifact_path).expanduser().resolve()
        try:
            verify_plugin_artifact(artifact, artifact_digest)
        except ValueError:
            raise PluginProcessError("plugin artifact validation failed") from None
        try:
            worker_request = {
                **request,
                "runtime": {"environment": _public_environment(self._allowed_environment)},
            }
            encoded_request = json.dumps(
                worker_request,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError):
            raise PluginProcessError("plugin request is not JSON-compatible") from None
        if len(encoded_request) > self._max_request_bytes:
            raise PluginProcessError("plugin request exceeded byte limit")

        command = [
            sys.executable,
            str(self._worker_path),
            str(artifact),
            artifact_digest,
            str(self._memory_limit_bytes or 0),
            str(self._cpu_limit_seconds or 0),
            str(self._max_result_bytes),
        ]
        failure_stage: str | None = None
        with tempfile.TemporaryDirectory(prefix="self-grow-agent-plugin-run-") as home:
            environment = {
                "HOME": home,
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PATH": os.defpath,
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONNOUSERSITE": "1",
                "PYTHONPATH": str(self._project_root),
                **self._allowed_environment,
            }
            try:
                process = subprocess.Popen(
                    command,
                    cwd=artifact,
                    env=environment,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
            except OSError:
                raise PluginProcessError("plugin worker could not start") from None
            try:
                try:
                    stdout, stderr = process.communicate(
                        encoded_request,
                        timeout=self._timeout_seconds,
                    )
                except subprocess.TimeoutExpired:
                    failure_stage = "timeout"
                    _terminate_process_group(process)
                    process.communicate()
                    raise PluginTimeoutError("plugin handler timed out") from None
                protocol_limit = max(
                    self._max_result_bytes
                    + (6 * _MAX_PLUGIN_LOG_EVENTS * _MAX_PLUGIN_LOG_MESSAGE_CHARS)
                    + 8192,
                    8192,
                )
                if len(stdout) > protocol_limit or len(stderr) > protocol_limit:
                    failure_stage = "worker_output"
                    raise PluginProcessError("plugin worker output exceeded byte limit")
                if process.returncode != 0:
                    failure_stage = "worker_exit"
                    raise PluginProcessError("plugin worker process failed")
                try:
                    return _decode_worker_response(
                        stdout,
                        stderr,
                        artifact=artifact,
                        request=request,
                        allowed_environment=self._allowed_environment,
                    )
                except PluginProcessError:
                    failure_stage = "protocol_or_handler"
                    raise
            finally:
                if process.poll() is None:
                    _terminate_process_group(process)
                if failure_stage is not None:
                    _logger.warning(
                        "plugin_handler_process failed stage=%s pid=%s exit_code=%s "
                        "artifact_version=%s timeout_seconds=%.3f memory_limit_bytes=%s "
                        "cpu_limit_seconds=%s elapsed_seconds=%.3f",
                        failure_stage,
                        process.pid,
                        process.returncode,
                        artifact.name,
                        self._timeout_seconds,
                        self._memory_limit_bytes,
                        self._cpu_limit_seconds,
                        time.monotonic() - started_at,
                    )


class ContainerPluginExecutor:
    """Run a verified plugin inside a hardened one-shot OCI container."""

    def __init__(
        self,
        timeout_seconds: float,
        *,
        runtime: str = "docker",
        image: str = "self-grow-agent-plugin-runtime:latest",
        network: str | None = None,
        memory_limit_bytes: int | None = None,
        cpu_limit_seconds: int | None = None,
        max_result_bytes: int = 1_048_576,
        max_request_bytes: int = 1_048_576,
        allowed_environment: Mapping[str, str] | None = None,
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a positive finite number")
        if not runtime or runtime != runtime.strip() or "\x00" in runtime:
            raise ValueError("container runtime must be a non-empty command")
        resolved_runtime = shutil.which(runtime)
        if resolved_runtime is None:
            raise ValueError("container runtime command was not found")
        if not image or image != image.strip() or re.search(r"\s", image):
            raise ValueError("container image must be a non-empty reference")
        if network is not None and (
            not network
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", network) is None
        ):
            raise ValueError("container network name is invalid")
        for name, value in {
            "memory_limit_bytes": memory_limit_bytes,
            "cpu_limit_seconds": cpu_limit_seconds,
            "max_result_bytes": max_result_bytes,
            "max_request_bytes": max_request_bytes,
        }.items():
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
            ):
                raise ValueError(f"{name} must be a positive integer or None")
        environment = dict(allowed_environment or {})
        for name, value in environment.items():
            if (
                not isinstance(name, str)
                or _ENVIRONMENT_NAME.fullmatch(name) is None
                or name in _RESERVED_ENVIRONMENT
                or not isinstance(value, str)
            ):
                raise ValueError("plugin environment contains an invalid entry")
        self._timeout_seconds = float(timeout_seconds)
        self._runtime = resolved_runtime
        self._image = image
        self._network = network
        self._memory_limit_bytes = memory_limit_bytes
        self._cpu_limit_seconds = cpu_limit_seconds
        self._max_result_bytes = max_result_bytes
        self._max_request_bytes = max_request_bytes
        self._allowed_environment = environment
        self._project_root = Path(__file__).resolve().parent.parent

    def execute(
        self,
        artifact_path: str | Path,
        artifact_digest: str,
        request: dict[str, Any],
    ) -> Any:
        """Verify and invoke one plugin in an isolated container."""

        started_at = time.monotonic()
        artifact = Path(artifact_path).expanduser().resolve()
        try:
            verify_plugin_artifact(artifact, artifact_digest)
        except ValueError:
            raise PluginProcessError("plugin artifact validation failed") from None
        try:
            encoded_request = json.dumps(
                {
                    **request,
                    "runtime": {
                        "environment": _public_environment(self._allowed_environment)
                    },
                },
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError):
            raise PluginProcessError("plugin request is not JSON-compatible") from None
        if len(encoded_request) > self._max_request_bytes:
            raise PluginProcessError("plugin request exceeded byte limit")

        container_name = f"self-grow-agent-plugin-{uuid.uuid4().hex}"
        command = [
            self._runtime,
            "run",
            "--rm",
            "--interactive",
            "--name",
            container_name,
            "--network",
            self._network or "none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=64",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=16m",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--env=HOME=/tmp",
            "--env=PYTHONDONTWRITEBYTECODE=1",
            "--env=PYTHONNOUSERSITE=1",
            "--env=PYTHONPATH=/opt/self-grow-agent",
            "--mount",
            f"type=bind,src={artifact},dst=/plugin,readonly",
            "--mount",
            f"type=bind,src={self._project_root},dst=/opt/self-grow-agent,readonly",
            "--workdir=/plugin",
        ]
        if self._memory_limit_bytes is not None:
            command.append(f"--memory={self._memory_limit_bytes}")
        command.append("--cpus=1.0")
        for name in sorted(self._allowed_environment):
            command.append(f"--env={name}")
        command.extend(
            [
                self._image,
                "python",
                "/opt/self-grow-agent/self_grow_agent/plugin_worker.py",
                "/plugin",
                artifact_digest,
                "0",
                str(self._cpu_limit_seconds or 0),
                str(self._max_result_bytes),
            ]
        )
        environment = {
            "HOME": os.environ.get("HOME", tempfile.gettempdir()),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": os.defpath,
            **self._allowed_environment,
        }
        for name in (
            "DOCKER_CONFIG",
            "DOCKER_CONTEXT",
            "DOCKER_HOST",
            "DOCKER_TLS_VERIFY",
            "DOCKER_CERT_PATH",
        ):
            if name in os.environ:
                environment[name] = os.environ[name]
        process: subprocess.Popen[bytes] | None = None
        failure_stage: str | None = None
        try:
            try:
                process = subprocess.Popen(
                    command,
                    cwd=self._project_root,
                    env=environment,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
            except OSError:
                raise PluginProcessError("plugin container could not start") from None
            try:
                stdout, stderr = process.communicate(
                    encoded_request, timeout=self._timeout_seconds
                )
            except subprocess.TimeoutExpired:
                failure_stage = "timeout"
                _terminate_process_group(process)
                _remove_container(self._runtime, container_name, environment)
                process.communicate()
                raise PluginTimeoutError("plugin handler timed out") from None
            protocol_limit = max(
                self._max_result_bytes
                + (6 * _MAX_PLUGIN_LOG_EVENTS * _MAX_PLUGIN_LOG_MESSAGE_CHARS)
                + 8192,
                8192,
            )
            if len(stdout) > protocol_limit or len(stderr) > protocol_limit:
                failure_stage = "worker_output"
                raise PluginProcessError("plugin worker output exceeded byte limit")
            if process.returncode != 0:
                failure_stage = "container_exit"
                _logger.warning(
                    "plugin_handler_container runtime_error container_name=%s detail=%r",
                    container_name,
                    _redact_text(
                        stderr.decode("utf-8", errors="replace")[:2048],
                        frozenset(self._allowed_environment.values()),
                    ),
                )
                raise PluginProcessError("plugin container process failed")
            try:
                return _decode_worker_response(
                    stdout,
                    stderr,
                    artifact=artifact,
                    request=request,
                    allowed_environment=self._allowed_environment,
                )
            except PluginProcessError:
                failure_stage = "protocol_or_handler"
                raise
        finally:
            if process is not None and process.poll() is None:
                _terminate_process_group(process)
                _remove_container(self._runtime, container_name, environment)
            if failure_stage is not None:
                _logger.warning(
                    "plugin_handler_container failed stage=%s container_name=%s "
                    "exit_code=%s artifact_version=%s network=%s timeout_seconds=%.3f "
                    "memory_limit_bytes=%s cpu_limit_seconds=%s elapsed_seconds=%.3f",
                    failure_stage,
                    container_name,
                    process.returncode if process is not None else None,
                    artifact.name,
                    self._network or "none",
                    self._timeout_seconds,
                    self._memory_limit_bytes,
                    self._cpu_limit_seconds,
                    time.monotonic() - started_at,
                )


def _public_environment(environment: Mapping[str, str]) -> dict[str, str]:
    return {
        name: value
        for name, value in environment.items()
        if _SENSITIVE_FIELD.search(name) is None
    }


def _redact_text(value: str, sensitive_values: frozenset[str]) -> str:
    for sensitive_value in sensitive_values:
        if sensitive_value:
            value = value.replace(sensitive_value, "<redacted>")
    return _PLUGIN_LOG_CREDENTIAL.sub(
        lambda match: f"{match.group('name')}{match.group('separator')}<redacted>",
        value,
    )


def _decode_worker_response(
    stdout: bytes,
    stderr: bytes,
    *,
    artifact: Path,
    request: Mapping[str, Any],
    allowed_environment: Mapping[str, str],
) -> Any:
    del stderr
    try:
        response = json.loads(stdout)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise PluginProcessError("plugin worker returned invalid JSON") from None
    if not isinstance(response, dict) or response.get("status") not in {"ok", "error"}:
        raise PluginProcessError("plugin worker returned invalid response")
    logs = response.get("logs")
    if not _valid_plugin_logs(logs):
        raise PluginProcessError("plugin worker returned invalid response")
    _emit_plugin_logs(
        logs,
        artifact=artifact,
        redacted_values=_sensitive_values(request, allowed_environment),
    )
    if response["status"] == "error":
        message = response.get("message")
        if not isinstance(message, str) or not message.startswith("plugin "):
            message = "plugin worker process failed"
        raise PluginProcessError(message)
    if set(response) != {"status", "result", "logs"}:
        raise PluginProcessError("plugin worker returned invalid response")
    return response["result"]


def _remove_container(runtime: str, name: str, environment: Mapping[str, str]) -> None:
    try:
        subprocess.run(
            [runtime, "rm", "--force", name],
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        _logger.warning("plugin_handler_container cleanup_failed container_name=%s", name)


def _valid_plugin_logs(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) <= _MAX_PLUGIN_LOG_EVENTS
        and all(
            isinstance(event, dict)
            and set(event) == {"level", "message"}
            and event["level"] in {"INFO", "WARNING", "ERROR", "CRITICAL"}
            and isinstance(event["message"], str)
            and len(event["message"]) <= _MAX_PLUGIN_LOG_MESSAGE_CHARS
            for event in value
        )
    )


def _sensitive_values(
    request: Mapping[str, Any], environment: Mapping[str, str]
) -> frozenset[str]:
    values = {value for value in environment.values() if value}

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                if _SENSITIVE_FIELD.search(str(key)) and isinstance(nested, str) and nested:
                    values.add(nested)
                else:
                    visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(request)
    return frozenset(values)


def _emit_plugin_logs(
    events: list[dict[str, str]], *, artifact: Path, redacted_values: frozenset[str]
) -> None:
    for event in events:
        message = _redact_text(event["message"], redacted_values)
        log_method = {
            "INFO": _logger.info,
            "WARNING": _logger.warning,
            "ERROR": _logger.error,
            "CRITICAL": _logger.critical,
        }[event["level"]]
        log_method(
            "plugin_handler event project=%s route_id=%s artifact_version=%s "
            "level=%s message=%r",
            artifact.parent.parent.name,
            artifact.parent.name,
            artifact.name,
            event["level"],
            message,
        )


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        if hasattr(os, "killpg"):
            os.killpg(process.pid, signal.SIGTERM)
        else:  # pragma: no cover - Windows fallback
            process.terminate()
        process.wait(timeout=0.5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            if hasattr(os, "killpg"):
                os.killpg(process.pid, signal.SIGKILL)
            else:  # pragma: no cover - Windows fallback
                process.kill()
            process.wait(timeout=0.5)
        except (OSError, subprocess.TimeoutExpired):
            pass


__all__ = [
    "ContainerPluginExecutor",
    "PluginExecutor",
    "PluginProcessError",
    "PluginProcessExecutor",
    "PluginTimeoutError",
]
