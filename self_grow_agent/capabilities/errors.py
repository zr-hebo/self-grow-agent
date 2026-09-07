"""Safe, platform-authored error protocol for controlled capabilities."""

from __future__ import annotations

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


class CapabilityError(RuntimeError):
    """A known capability failure safe to cross the plugin process boundary."""

    def __init__(self, code: str) -> None:
        status_code, message = capability_error_details(code)
        self.code = code
        self.status_code = status_code
        self.safe_message = message
        super().__init__(message)


def capability_error_details(code: object) -> tuple[int, str]:
    """Resolve only registered error codes; reject worker-controlled messages."""

    if not isinstance(code, str) or code not in _ERROR_DETAILS:
        raise ValueError("unknown capability error code")
    return _ERROR_DETAILS[code]


__all__ = ["CapabilityError", "capability_error_details"]
