"""One-shot worker process for a verified generated plugin."""

from __future__ import annotations

import contextlib
import importlib.util
import json
import logging
import sys
import uuid
from pathlib import Path
from typing import Any

from self_grow_agent.executor import _apply_resource_limits
from self_grow_agent.plugin_runtime import verify_plugin_artifact

_MAX_LOG_EVENTS = 64
_MAX_LOG_MESSAGE_CHARS = 1_024


class _ProtocolLogHandler(logging.Handler):
    """Collect bounded plugin logs without writing on the JSON protocol streams."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.events: list[dict[str, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        if len(self.events) >= _MAX_LOG_EVENTS:
            return
        try:
            message = record.getMessage()
        except Exception:
            message = "plugin log message could not be formatted"
        self.events.append(
            {
                "level": record.levelname
                if record.levelname in {"INFO", "WARNING", "ERROR", "CRITICAL"}
                else "INFO",
                "message": message[:_MAX_LOG_MESSAGE_CHARS],
            }
        )


class _DiscardText:
    encoding = "utf-8"

    def write(self, value: str) -> int:
        return len(value)

    def flush(self) -> None:
        return None


def main() -> int:
    protocol_output = sys.stdout.buffer
    response: dict[str, Any]
    log_handler = _ProtocolLogHandler()
    try:
        artifact, digest, memory_text, cpu_text, result_limit_text = sys.argv[1:6]
        memory_limit = int(memory_text) or None
        cpu_limit = int(cpu_text) or None
        result_limit = int(result_limit_text)
        request = json.loads(sys.stdin.buffer.read())
        if not isinstance(request, dict):
            raise ValueError("request must be an object")
        _apply_resource_limits(memory_limit, cpu_limit)
        verify_plugin_artifact(artifact, digest)
        with contextlib.redirect_stdout(_DiscardText()), contextlib.redirect_stderr(
            _DiscardText()
        ):
            root_logger = logging.getLogger()
            previous_handlers = root_logger.handlers[:]
            previous_level = root_logger.level
            root_logger.handlers = [log_handler]
            root_logger.setLevel(logging.INFO)
            try:
                handler = _load_handler(Path(artifact))
                result = handler(request)
            finally:
                root_logger.handlers = previous_handlers
                root_logger.setLevel(previous_level)
        encoded_result = json.dumps(
            result,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded_result) > result_limit:
            response = {
                "status": "error",
                "message": "plugin handler result exceeded byte limit",
                "logs": log_handler.events,
            }
        else:
            response = {"status": "ok", "result": result, "logs": log_handler.events}
    except BaseException as exc:
        response = {
            "status": "error",
            "message": _safe_error(exc),
            "logs": log_handler.events,
        }

    try:
        protocol_output.write(
            json.dumps(
                response,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        protocol_output.flush()
        return 0
    except BaseException:
        return 1


def _load_handler(artifact: Path) -> Any:
    module_name = f"self_grow_plugin_{uuid.uuid4().hex}"
    handler_path = artifact / "handler.py"
    spec = importlib.util.spec_from_file_location(module_name, handler_path)
    if spec is None or spec.loader is None:
        raise ImportError("plugin entrypoint could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(artifact))
    try:
        spec.loader.exec_module(module)
    finally:
        try:
            sys.path.remove(str(artifact))
        except ValueError:
            pass
    handler = getattr(module, "handle", None)
    if not callable(handler):
        raise TypeError("plugin entrypoint is not callable")
    return handler


def _safe_error(exc: BaseException) -> str:
    if isinstance(exc, ModuleNotFoundError):
        return "plugin dependency is unavailable"
    if isinstance(exc, MemoryError):
        return "plugin handler exceeded memory limit"
    if isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError)):
        return "plugin worker received invalid JSON"
    if isinstance(exc, ValueError) and str(exc).startswith("plugin artifact"):
        return "plugin artifact validation failed"
    if type(exc).__name__ == "_HandlerCpuLimitExceeded":
        return "plugin handler exceeded CPU limit"
    return f"plugin handler raised {type(exc).__name__}"


if __name__ == "__main__":  # pragma: no cover - exercised through subprocess tests
    raise SystemExit(main())
