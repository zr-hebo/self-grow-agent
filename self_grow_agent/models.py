"""Shared models and protocols for generated features."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class GeneratedHandler(BaseModel):
    """Strict, structured output returned by a feature generator."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source: str = Field(min_length=1)
    description: str = ""


@runtime_checkable
class FeatureGenerator(Protocol):
    """Dependency-injection boundary between the service and an LLM provider."""

    async def generate(
        self,
        *,
        instruction: str,
        path: str,
        method: str,
        current_source: str | None = None,
    ) -> GeneratedHandler:
        """Generate a complete replacement for one dynamic handler."""
        ...


Handler = Any
