"""Application service that turns management instructions into active routes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import NoReturn

from self_grow_agent.llm import FeatureGenerator, GenerationCapacityError, GenerationError
from self_grow_agent.projects import DEFAULT_PROJECT
from self_grow_agent.runtime import RouteAlreadyExistsError, RouteRecord, RouteRuntime

PublicationHook = Callable[[str, int, str], Awaitable[None]]


class AgentServiceError(Exception):
    """Base class for errors owned by the management service."""


class LLMUnavailableError(AgentServiceError):
    """Raised when route generation is requested without an LLM configuration."""


class FeatureGenerationError(AgentServiceError):
    """Raised when the configured LLM cannot produce a generation."""


class FeatureGenerationCapacityError(AgentServiceError):
    """Raised when the generation backend has no available execution slot."""


_SAFE_GENERATION_FAILURE_MESSAGES = frozenset(
    {
        "LLM provider request failed",
        "LLM provider request timed out",
        "LLM provider authentication failed",
        "LLM provider rate limit exceeded",
        "LLM provider connection failed",
        "LLM provider returned an error",
        "LLM returned an empty generated-handler response",
        "LLM returned an invalid generated-handler response",
        "LLM returned invalid generated-handler JSON",
        "Pi executable was not found",
        "Pi generation timed out",
        "Pi RPC emitted invalid JSON",
        "Pi RPC stream ended before agent_settled",
        "Pi RPC stream ended with an incomplete event",
        "Pi returned invalid generated-handler JSON",
        "Pi returned an invalid generated-handler response",
        "Pi RPC protocol error",
        "Pi rejected the generation request",
        "Pi agent did not complete generation",
        "Pi process failed",
        "Pi generation failed",
    }
)


def _safe_generation_failure_message(exc: GenerationError) -> str:
    """Keep provider data out of a persisted generation failure message."""

    message = str(exc)
    if message in _SAFE_GENERATION_FAILURE_MESSAGES:
        return message
    return "LLM generation failed"


class ManagedRouteNotFoundError(AgentServiceError):
    """Raised when an update targets a route that does not exist."""


class ManagedVersionConflictError(AgentServiceError):
    """Raised before generation when the caller's route version is stale."""

    def __init__(self, expected_version: int, actual_version: int) -> None:
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            f"expected route version {expected_version}, current version is {actual_version}"
        )


class AgentManagementService:
    """Coordinate validation, LLM generation, and atomic runtime publication."""

    def __init__(
        self,
        runtime: RouteRuntime,
        generator: FeatureGenerator | None,
    ) -> None:
        self.runtime = runtime
        self.generator = generator

    async def create_route(
        self,
        *,
        path: str,
        method: str,
        instruction: str,
        project: str = DEFAULT_PROJECT,
        before_publish: PublicationHook | None = None,
    ) -> RouteRecord:
        normalized_path, normalized_method = self.runtime.validate_route(path, method)
        project = self.runtime.normalize_project(project)
        existing = self.runtime.resolve(normalized_method, normalized_path)
        if existing is not None:
            raise RouteAlreadyExistsError(
                f"route {normalized_method} {normalized_path} already exists"
            )
        generated = await self._generate(
            instruction=instruction,
            path=normalized_path,
            method=normalized_method,
        )
        if before_publish is not None:
            await before_publish(
                self.runtime.route_id_for(normalized_path, normalized_method),
                1,
                generated.source,
            )
        return self.runtime.create(
            path=normalized_path,
            method=normalized_method,
            source=generated.source,
            description=generated.description,
            project=project,
        )

    async def update_route(
        self,
        *,
        route_id: str,
        instruction: str,
        expected_version: int,
        before_publish: PublicationHook | None = None,
    ) -> RouteRecord:
        current = self.runtime.get(route_id)
        if current is None:
            raise ManagedRouteNotFoundError("managed route not found")
        if current.version != expected_version:
            raise ManagedVersionConflictError(expected_version, current.version)

        current_source = current.source
        generated = await self._generate(
            instruction=instruction,
            path=current.path,
            method=current.method,
            current_source=current_source,
        )
        if before_publish is not None:
            await before_publish(route_id, expected_version + 1, generated.source)
        return self.runtime.update(
            route_id=route_id,
            source=generated.source,
            expected_version=expected_version,
            description=generated.description,
        )

    async def move_route(
        self,
        *,
        route_id: str,
        path: str,
        project: str,
        instruction: str,
        expected_version: int,
        before_publish: PublicationHook | None = None,
    ) -> RouteRecord:
        """Regenerate a handler for its target path, then atomically move it."""

        current = self.runtime.get(route_id)
        if current is None:
            raise ManagedRouteNotFoundError("managed route not found")
        if current.version != expected_version:
            raise ManagedVersionConflictError(expected_version, current.version)

        normalized_path, _ = self.runtime.validate_route(path, current.method)
        normalized_project = self.runtime.normalize_project(project)
        generated = await self._generate(
            instruction=instruction,
            path=normalized_path,
            method=current.method,
            current_source=current.source,
        )
        target_route_id = self.runtime.route_id_for(normalized_path, current.method)
        if before_publish is not None:
            await before_publish(
                target_route_id,
                expected_version + 1,
                generated.source,
            )
        return self.runtime.move(
            route_id,
            path=normalized_path,
            project=normalized_project,
            expected_version=expected_version,
            source=generated.source,
            description=generated.description,
        )

    async def _generate(
        self,
        *,
        instruction: str,
        path: str,
        method: str,
        current_source: str | None = None,
    ):
        if self.generator is None:
            raise LLMUnavailableError("LLM is not configured")
        try:
            return await self.generator.generate(
                instruction=instruction,
                path=path,
                method=method,
                current_source=current_source,
            )
        except GenerationCapacityError as exc:
            raise FeatureGenerationCapacityError("generation capacity is full") from exc
        except GenerationError as exc:
            raise FeatureGenerationError(_safe_generation_failure_message(exc)) from exc
        except Exception as exc:
            self._raise_generation_error(exc)

    @staticmethod
    def _raise_generation_error(exc: Exception) -> NoReturn:
        # Provider errors can contain credentials or upstream response bodies. Keep
        # the public domain error intentionally generic and retain details only in
        # the exception chain for local diagnostics.
        raise FeatureGenerationError("LLM generation failed") from exc
