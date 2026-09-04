"""Sanitized subprocess boundary for published plugin execution."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

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


class PluginProcessError(RuntimeError):
    """A plugin worker failed with a safe public category."""


class PluginTimeoutError(PluginProcessError):
    """A plugin worker exceeded its wall-clock deadline."""


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
                "runtime": {"environment": dict(self._allowed_environment)},
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
                protocol_limit = max(self._max_result_bytes + 1024, 4096)
                if len(stdout) > protocol_limit or len(stderr) > protocol_limit:
                    failure_stage = "worker_output"
                    raise PluginProcessError("plugin worker output exceeded byte limit")
                if process.returncode != 0:
                    failure_stage = "worker_exit"
                    raise PluginProcessError("plugin worker process failed")
                try:
                    response = json.loads(stdout)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    failure_stage = "protocol"
                    raise PluginProcessError("plugin worker returned invalid JSON") from None
                if not isinstance(response, dict) or response.get("status") not in {
                    "ok",
                    "error",
                }:
                    failure_stage = "protocol"
                    raise PluginProcessError("plugin worker returned invalid response")
                if response["status"] == "error":
                    failure_stage = "handler"
                    message = response.get("message")
                    if not isinstance(message, str) or not message.startswith("plugin "):
                        message = "plugin worker process failed"
                    raise PluginProcessError(message)
                if set(response) != {"status", "result"}:
                    failure_stage = "protocol"
                    raise PluginProcessError("plugin worker returned invalid response")
                return response["result"]
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


__all__ = ["PluginProcessError", "PluginProcessExecutor", "PluginTimeoutError"]
