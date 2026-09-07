"""Execution-mode defaults shared by management and persistence layers."""

from typing import Literal, TypeAlias

ExecutionMode: TypeAlias = Literal["restricted", "plugin"]

# New and revised APIs use full Pi-generated plugins unless callers explicitly
# opt into the legacy single-file restricted handler.
DEFAULT_EXECUTION_MODE: ExecutionMode = "plugin"
