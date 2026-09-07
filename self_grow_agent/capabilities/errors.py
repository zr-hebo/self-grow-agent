"""Safe, platform-authored error protocol for controlled capabilities."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Final

_ERROR_DETAILS: Final[dict[str, tuple[int, str]]] = {
    "mysql_alert_instance_invalid": (
        422,
        "raw-message must contain exactly one valid Instance: ip:port line",
    ),
    "mysql_instance_invalid": (422, "invalid MySQL instance; expected ip:port"),
    "mysql_credentials_not_configured": (
        503,
        "MySQL capability credentials are not configured",
    ),
    "mysql_replication_failed": (502, "MySQL replication operation failed"),
}

_active_capability_errors: list[str] | None = None


class CapabilityError(RuntimeError):
    """A known capability failure safe to cross the plugin process boundary."""

    def __init__(self, code: str) -> None:
        status_code, message = capability_error_details(code)
        if _active_capability_errors is not None and not _active_capability_errors:
            _active_capability_errors.append(code)
        self.code = code
        self.status_code = status_code
        self.safe_message = message
        super().__init__(message)


def capability_error_details(code: object) -> tuple[int, str]:
    """Resolve only registered error codes; reject worker-controlled messages."""

    if not isinstance(code, str) or code not in _ERROR_DETAILS:
        raise ValueError("unknown capability error code")
    return _ERROR_DETAILS[code]


@contextmanager
def capture_capability_errors() -> Iterator[list[str]]:
    """Record the first trusted capability failure even if plugin code catches it."""

    global _active_capability_errors
    previous = _active_capability_errors
    captured: list[str] = []
    _active_capability_errors = captured
    try:
        yield captured
    finally:
        _active_capability_errors = previous


__all__ = ["CapabilityError", "capability_error_details"]
