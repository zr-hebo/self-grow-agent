"""Strict data contract for complete generated API plugins."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_DEPENDENCY_PIN = re.compile(
    r"(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)=="
    r"(?P<version>[A-Za-z0-9][A-Za-z0-9._+-]*)\Z"
)
_MAX_PATH_CHARS = 200


class PluginPolicyError(ValueError):
    """A structurally valid plugin violates a deployment policy."""


class PluginFile(BaseModel):
    """One regular Python source file in a generated plugin bundle."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str = Field(min_length=1, max_length=_MAX_PATH_CHARS)
    content: str

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        if (
            value.startswith("/")
            or "\\" in value
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError("plugin file path must be a safe POSIX relative path")
        path = PurePosixPath(value)
        parts = value.split("/")
        if (
            str(path) != value
            or any(not part or part in {".", ".."} or part.startswith(".") for part in parts)
            or path.suffix != ".py"
        ):
            raise ValueError("plugin file path must name a regular Python source file")
        return value


class GeneratedPlugin(BaseModel):
    """Complete, bounded file bundle produced by an untrusted generator."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    description: str = Field(default="", max_length=4_000)
    entrypoint: Literal["handler:handle"] = "handler:handle"
    dependencies: tuple[str, ...] = ()
    files: tuple[PluginFile, ...]

    @field_validator("dependencies")
    @classmethod
    def normalize_dependencies(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_normalize_dependency_pin(value) for value in values)
        if len(normalized) != len(set(normalized)):
            raise ValueError("duplicate plugin dependency")
        return normalized

    @model_validator(mode="after")
    def reject_duplicate_paths(self) -> Self:
        paths = [file.path for file in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("duplicate plugin file path")
        return self


@dataclass(frozen=True, slots=True)
class PluginPolicy:
    """Deployment-specific limits applied after strict bundle parsing."""

    allowed_dependencies: frozenset[str] = frozenset()
    max_files: int = 32
    max_file_bytes: int = 262_144
    max_total_bytes: int = 1_048_576

    def __post_init__(self) -> None:
        for field_name in ("max_files", "max_file_bytes", "max_total_bytes"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")
        normalized_dependencies = frozenset(
            _normalize_dependency_pin(value) for value in self.allowed_dependencies
        )
        object.__setattr__(self, "allowed_dependencies", normalized_dependencies)

    def validate(self, plugin: GeneratedPlugin) -> GeneratedPlugin:
        """Return ``plugin`` when it satisfies file and dependency policy."""

        if not isinstance(plugin, GeneratedPlugin):
            raise TypeError("plugin must be a GeneratedPlugin")
        if len(plugin.files) > self.max_files:
            raise PluginPolicyError("plugin contains too many files")
        if "handler.py" not in {file.path for file in plugin.files}:
            raise PluginPolicyError("plugin must include handler.py")
        if not any(
            file.path.startswith("tests/test_") and file.path.endswith(".py")
            for file in plugin.files
        ):
            raise PluginPolicyError("plugin must include tests/test_*.py")

        total_bytes = 0
        for file in plugin.files:
            file_bytes = len(file.content.encode("utf-8"))
            if file_bytes > self.max_file_bytes:
                raise PluginPolicyError(
                    f"plugin file {file.path!r} exceeds byte limit"
                )
            total_bytes += file_bytes
        if total_bytes > self.max_total_bytes:
            raise PluginPolicyError("plugin total source exceeds byte limit")

        disallowed = [
            dependency
            for dependency in plugin.dependencies
            if dependency not in self.allowed_dependencies
        ]
        if disallowed:
            raise PluginPolicyError("plugin dependency is not allowed")
        return plugin


def _normalize_dependency_pin(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("plugin dependency must be an exact name==version pin")
    match = _DEPENDENCY_PIN.fullmatch(value)
    if match is None:
        raise ValueError("plugin dependency must be an exact name==version pin")
    name = re.sub(r"[-_.]+", "-", match.group("name")).lower()
    return f"{name}=={match.group('version')}"


__all__ = [
    "GeneratedPlugin",
    "PluginFile",
    "PluginPolicy",
    "PluginPolicyError",
]
