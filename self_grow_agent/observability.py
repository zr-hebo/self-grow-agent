"""Task-local correlation values used by operational logs."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_operation_id: ContextVar[str | None] = ContextVar("operation_id", default=None)
_generation_id: ContextVar[str | None] = ContextVar("generation_id", default=None)


def current_operation_id() -> str | None:
    """Return the operation associated with the current async task, if any."""

    return _operation_id.get()


def current_generation_id() -> str | None:
    """Return the generation associated with the current async task, if any."""

    return _generation_id.get()


@contextmanager
def operation_log_context(operation_id: str) -> Iterator[None]:
    """Bind an operation ID while creating work that inherits task context."""

    token = _operation_id.set(operation_id)
    try:
        yield
    finally:
        _operation_id.reset(token)


@contextmanager
def generation_log_context(generation_id: str) -> Iterator[None]:
    """Bind a generation ID while its RPC call inherits task context."""

    token = _generation_id.set(generation_id)
    try:
        yield
    finally:
        _generation_id.reset(token)


__all__ = [
    "current_generation_id",
    "current_operation_id",
    "generation_log_context",
    "operation_log_context",
]
