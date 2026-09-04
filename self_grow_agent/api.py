"""FastAPI application factory for the business and management planes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Generic, Literal, TypeVar
from zoneinfo import ZoneInfo

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    Response,
    status,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Receive, Scope, Send

from config import Settings, load_settings
from self_grow_agent.code_loader import CodeValidationError, HandlerContractError
from self_grow_agent.executor import (
    HandlerExecutor,
    HandlerProcessError,
    HandlerTimeoutError,
    ProcessHandlerExecutor,
)
from self_grow_agent.llm import FeatureGenerator, OpenAIFeatureGenerator
from self_grow_agent.metadata import (
    OperationNotFoundError,
    OperationRecord,
    RequirementBusyError,
    RequirementEvent,
    RequirementNotFoundError,
    RequirementRecord,
    RequirementStorageError,
    RequirementStore,
    RequirementStoreError,
)
from self_grow_agent.models import PluginFeatureGenerator
from self_grow_agent.observability import operation_log_context
from self_grow_agent.pi_generator import PiFeatureGenerator
from self_grow_agent.pi_rpc import PiRpcClient
from self_grow_agent.plugin_executor import (
    ContainerPluginExecutor,
    PluginExecutor,
    PluginProcessError,
    PluginProcessExecutor,
    PluginTimeoutError,
)
from self_grow_agent.plugin_generator import PiPluginGenerator
from self_grow_agent.plugin_models import PluginPolicy
from self_grow_agent.plugin_runtime import PluginPublicationError, PluginPublisher
from self_grow_agent.plugin_test_runner import PluginTestRunner
from self_grow_agent.plugin_validator import PluginValidator
from self_grow_agent.plugin_workspace import PluginWorkspaceManager
from self_grow_agent.projects import DEFAULT_PROJECT, normalize_project
from self_grow_agent.runtime import (
    ReservedPathError,
    RouteAlreadyExistsError,
    RouteNotFoundError,
    RoutePersistenceError,
    RouteRecord,
    RouteRuntime,
    RouteValidationError,
    VersionConflictError,
    normalize_path,
)
from self_grow_agent.service import (
    AgentManagementService,
    FeatureGenerationCapacityError,
    FeatureGenerationError,
    LLMUnavailableError,
    ManagedRouteNotFoundError,
    ManagedVersionConflictError,
)

_ALLOWED_BUSINESS_HEADERS = frozenset(
    {
        "accept",
        "accept-language",
        "content-type",
        "idempotency-key",
        "if-match",
        "traceparent",
        "user-agent",
        "x-request-id",
    }
)
_WEB_DIR = Path(__file__).with_name("web")
_CONSOLE_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
        "form-action 'self'; img-src 'self' data:; "
        "style-src 'self'; script-src 'self'; connect-src 'self'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}
_REQUIREMENT_FINALIZE_ATTEMPTS = 3
_BUSINESS_SUCCESS_RESPONSE = {"code": 0, "message": "OK"}
_BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")
ResponseData = TypeVar("ResponseData")
_logger = logging.getLogger("uvicorn.error")
_PUBLIC_FINISHED_STATUS = "finish"
_BACKGROUND_OPERATION_CANCELLED_MESSAGE = (
    "background operation cancelled before completion"
)
_LOGGED_INSTRUCTION_LIMIT = 1_024
_SENSITIVE_LOG_VALUE_PATTERN = re.compile(
    r"(?P<name>[\"']?(?:password|passwd|token|api[_ -]?key|secret|credential|"
    r"密码|口令|密钥)[\"']?)\s*(?P<separator>[:=：])\s*(?P<value>[^\s,，;；]+)",
    flags=re.IGNORECASE,
)
_SENSITIVE_LOG_FIELD_PARTS = frozenset(
    {
        "password",
        "passwd",
        "token",
        "apikey",
        "secret",
        "credential",
        "密码",
        "口令",
        "密钥",
    }
)


def _required_text(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("must not be blank")
    return normalized


def _api_success(data: Any) -> dict[str, Any]:
    """Wrap successful JSON API data in the public response envelope."""

    return {**_BUSINESS_SUCCESS_RESPONSE, "data": data}


def _api_error(code: int, message: str) -> dict[str, Any]:
    """Return a stable error response without exposing internal exception details."""

    return {"code": code, "message": message, "data": None}


def _instruction_for_log(instruction: str) -> str:
    """Return a bounded instruction string with common credential values redacted."""

    redacted = _SENSITIVE_LOG_VALUE_PATTERN.sub(
        lambda match: (
            f"{match.group('name')}{match.group('separator')}<redacted>"
        ),
        instruction,
    )
    return _bounded_log_text(redacted)


def _bounded_log_text(value: str) -> str:
    """Keep one log field bounded after it has been safely redacted."""

    if len(value) <= _LOGGED_INSTRUCTION_LIMIT:
        return value
    return f"{value[:_LOGGED_INSTRUCTION_LIMIT]}… [truncated]"


def _request_parameters_for_log(value: Any) -> str:
    """Serialize request parameters for logs while redacting credential values."""

    def redact(item: Any) -> Any:
        if isinstance(item, dict):
            redacted: dict[str, Any] = {}
            for key, nested_value in item.items():
                key_text = str(key)
                normalized_key = re.sub(r"[_ -]", "", key_text).casefold()
                if any(part in normalized_key for part in _SENSITIVE_LOG_FIELD_PARTS):
                    redacted[key_text] = "<redacted>"
                else:
                    redacted[key_text] = redact(nested_value)
            return redacted
        if isinstance(item, list):
            return [redact(nested_value) for nested_value in item]
        if isinstance(item, str):
            return _SENSITIVE_LOG_VALUE_PATTERN.sub(
                lambda match: (
                    f"{match.group('name')}{match.group('separator')}<redacted>"
                ),
                item,
            )
        return item

    try:
        serialized = json.dumps(
            redact(value),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError):
        serialized = "<unserializable request parameters>"
    return _bounded_log_text(serialized)


def _public_requirement_status(status_value: str) -> str:
    """Expose completed route-generation work as ``finish`` to API clients."""

    return _PUBLIC_FINISHED_STATUS if status_value == "active" else status_value


def _event_time() -> str:
    """Return the API event time in Beijing time using an ISO 8601 offset."""

    return datetime.now(_BEIJING_TIMEZONE).isoformat(timespec="milliseconds")


class RequestBodyLimitMiddleware:
    """Reject oversized bodies before FastAPI parses models or runs dependencies."""

    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        for name, value in scope.get("headers", []):
            if name.lower() != b"content-length":
                continue
            try:
                if int(value) > self.max_body_bytes:
                    await self._reject(scope, receive, send)
                    return
            except ValueError:
                pass
            break

        # FastAPI parses management Pydantic bodies before it evaluates the API-key
        # dependency. Buffer and replay only this stable control-plane prefix so an
        # unauthenticated chunked upload is bounded before FastAPI sees it. Dynamic
        # routes perform their own bounded stream read after admission control.
        path = scope.get("path", "")
        management_root = "/api/v1/manage"
        if path != management_root and not path.startswith(f"{management_root}/"):
            await self.app(scope, receive, send)
            return

        body = bytearray()
        disconnected = False
        chunk_count = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                disconnected = True
                break
            chunk_count += 1
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > self.max_body_bytes or chunk_count > 1024:
                await self._reject(scope, receive, send)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break

        replayed = False

        async def replay_receive():
            nonlocal replayed
            if disconnected or replayed:
                return {"type": "http.disconnect"}
            replayed = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            content=_api_error(
                status.HTTP_413_CONTENT_TOO_LARGE,
                "request body is too large",
            ),
            headers={"Connection": "close"},
        )
        await response(scope, receive, send)


class CreateRouteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=256)
    method: str = Field(min_length=1, max_length=16)
    project: str = Field(default=DEFAULT_PROJECT, min_length=1, max_length=63)
    execution_mode: Literal["restricted", "plugin"] = "restricted"
    instruction: str = Field(min_length=1, max_length=8_000)

    _normalize_instruction = field_validator("instruction")(_required_text)
    _normalize_project = field_validator("project")(normalize_project)


class UpdateRouteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instruction: str = Field(min_length=1, max_length=8_000)
    expected_version: int = Field(ge=1)
    execution_mode: Literal["restricted", "plugin"] | None = None

    _normalize_instruction = field_validator("instruction")(_required_text)


class MoveRouteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=256)
    project: str = Field(min_length=1, max_length=63)
    expected_version: int = Field(ge=1)
    instruction: str | None = Field(default=None, min_length=1, max_length=8_000)

    _normalize_project = field_validator("project")(normalize_project)

    @field_validator("instruction")
    @classmethod
    def normalize_instruction(cls, value: str | None) -> str | None:
        return None if value is None else _required_text(value)


class RollbackRouteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_version: int = Field(ge=1)
    expected_version: int = Field(ge=1)


class CreateRequirementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=120)
    instruction: str = Field(min_length=1, max_length=8_000)
    path: str = Field(min_length=1, max_length=256)
    method: str = Field(min_length=1, max_length=16)
    project: str = Field(default=DEFAULT_PROJECT, min_length=1, max_length=63)
    execution_mode: Literal["restricted", "plugin"] = "restricted"
    route_id: str | None = Field(default=None, min_length=1, max_length=128)

    _normalize_required_text = field_validator("title", "instruction")(_required_text)
    _normalize_project = field_validator("project")(normalize_project)


class UpdateRequirementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=120)
    instruction: str = Field(min_length=1, max_length=8_000)
    execution_mode: Literal["restricted", "plugin"] | None = None

    _normalize_required_text = field_validator("title", "instruction")(_required_text)


class RouteResponse(BaseModel):
    route_id: str
    path: str
    method: str
    project: str
    version: int
    description: str
    execution_mode: Literal["restricted", "plugin"]
    artifact_digest: str | None

    @classmethod
    def from_record(cls, record: RouteRecord) -> "RouteResponse":
        return cls(
            route_id=record.route_id,
            path=record.path,
            method=record.method,
            project=record.project,
            version=record.version,
            description=record.description,
            execution_mode=record.execution_mode,
            artifact_digest=record.artifact_digest,
        )


class ApiResponse(BaseModel, Generic[ResponseData]):
    """The response contract shared by JSON business and management endpoints."""

    code: int = 0
    message: str = "OK"
    data: ResponseData


class RouteTaskResponse(BaseModel):
    """Receipt for a route-generation task accepted for background execution."""

    requirement_id: str
    operation_id: str
    status: str = "accepted"
    project: str
    path: str
    method: str
    execution_mode: Literal["restricted", "plugin"]
    operation_url: str


class OperationResponse(BaseModel):
    id: str
    requirement_id: str
    kind: str
    path: str
    method: str
    project: str
    execution_mode: Literal["restricted", "plugin"]
    base_route_id: str | None
    base_route_version: int | None
    target_route_id: str | None
    target_route_version: int | None
    status: str
    last_error: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, record: OperationRecord) -> "OperationResponse":
        return cls(
            id=record.id,
            requirement_id=record.requirement_id,
            kind=record.kind,
            path=record.path,
            method=record.method,
            project=record.project,
            execution_mode=record.execution_mode,
            base_route_id=record.base_route_id,
            base_route_version=record.base_route_version,
            target_route_id=record.target_route_id,
            target_route_version=record.target_route_version,
            status=record.status,
            last_error=record.last_error,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


class RequirementResponse(BaseModel):
    id: str
    title: str
    instruction: str
    path: str
    method: str
    project: str
    execution_mode: Literal["restricted", "plugin"]
    route_id: str | None
    route_version: int | None
    status: str
    last_error: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, record: RequirementRecord) -> "RequirementResponse":
        return cls(
            id=record.id,
            title=record.title,
            instruction=record.instruction,
            path=record.path,
            method=record.method,
            project=record.project,
            execution_mode=record.execution_mode,
            route_id=record.route_id,
            route_version=record.route_version,
            status=_public_requirement_status(record.status),
            last_error=record.last_error,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


class RequirementEventResponse(BaseModel):
    id: int
    requirement_id: str
    from_status: str | None
    to_status: str
    message: str | None
    created_at: datetime

    @classmethod
    def from_event(cls, event: RequirementEvent) -> "RequirementEventResponse":
        return cls(
            id=event.id,
            requirement_id=event.requirement_id,
            from_status=(
                _public_requirement_status(event.from_status)
                if event.from_status is not None
                else None
            ),
            to_status=_public_requirement_status(event.to_status),
            message=event.message,
            created_at=event.created_at,
        )


def _build_generator(settings: Settings) -> FeatureGenerator | None:
    if not settings.llm_api_key:
        return None
    if settings.generation_backend == "pi":
        return PiFeatureGenerator(
            rpc_client=_build_pi_rpc_client(settings),
            max_concurrent_runs=settings.pi_max_concurrent_runs,
            admission_timeout_seconds=settings.pi_admission_timeout_seconds,
        )
    return OpenAIFeatureGenerator(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        timeout_seconds=settings.llm_timeout_seconds,
    )


def _build_pi_rpc_client(settings: Settings) -> PiRpcClient:
    return PiRpcClient(
        command=(settings.pi_executable,),
        provider=settings.pi_provider,
        model=settings.pi_model,
        thinking_level=settings.pi_thinking_level,
        api_key=settings.llm_api_key,
        provider_env_name=settings.pi_provider_env_name,
        timeout_seconds=settings.pi_timeout_seconds,
        max_event_stream_bytes=settings.pi_max_event_stream_bytes,
        workspace_root=settings.pi_workspace_root,
    )


def _build_plugin_generator(settings: Settings) -> PluginFeatureGenerator | None:
    if not settings.llm_api_key or settings.generation_backend != "pi":
        return None
    return PiPluginGenerator(
        rpc_client=_build_pi_rpc_client(settings),
        policy=PluginPolicy(
            allowed_dependencies=frozenset(settings.plugin_allowed_dependencies),
            max_files=settings.plugin_max_files,
            max_file_bytes=settings.plugin_max_file_bytes,
            max_total_bytes=settings.plugin_max_total_bytes,
        ),
        max_concurrent_runs=settings.pi_max_concurrent_runs,
        admission_timeout_seconds=settings.pi_admission_timeout_seconds,
    )


def _safe_requirement_error(exc: Exception) -> str:
    """Return a persistent failure message that cannot expose provider details."""

    if isinstance(exc, LLMUnavailableError):
        return str(exc)
    if isinstance(exc, FeatureGenerationError):
        return str(exc)
    if isinstance(exc, FeatureGenerationCapacityError):
        return "generation capacity is full"
    if isinstance(exc, RoutePersistenceError):
        return "route publication failed"
    if isinstance(
        exc,
        (
            RouteValidationError,
            ReservedPathError,
            CodeValidationError,
            RouteAlreadyExistsError,
            VersionConflictError,
            ManagedRouteNotFoundError,
            ManagedVersionConflictError,
        ),
    ):
        return str(exc)
    return "implementation failed"


def _safe_handler_error(exc: Exception) -> str:
    """Return the executor's bounded, non-secret runtime error summary."""

    message = str(exc).strip()
    if not message:
        return "dynamic handler failed"
    return f"dynamic handler failed: {message[:256]}"


def _source_sha256(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _route_publication_digest(route: RouteRecord) -> str:
    if route.execution_mode == "plugin":
        if route.artifact_digest is None:
            raise RoutePersistenceError("plugin route has no artifact digest")
        return route.artifact_digest
    if route.source is None:
        raise RoutePersistenceError("restricted route has no generated source")
    return _source_sha256(route.source)


def _route_requirement_title(project: str, method: str, path: str) -> str:
    """Return a deterministic, bounded title for a direct route request."""

    return f"{project}: {method} {path}"[:120]


def _public_route_path(project: str, path: str) -> str:
    """Map a project-relative management path to its public business path."""

    # Reject management/system paths in the caller's project-relative namespace
    # before adding a project prefix. This keeps `/healthz` and `/console`
    # unavailable as user-defined business API names even though the final URL
    # would not collide with their root endpoints.
    normalized_path, _ = RouteRuntime.validate_route(path, "GET")
    return f"/{project}{normalized_path}"


def _route_move_instruction(
    current: RouteRecord,
    target_path: str,
    additional_instruction: str | None,
) -> str:
    """Describe a behavior-preserving handler regeneration for a route move."""

    instruction = (
        f"Migrate the existing {current.method} handler from {current.path} to "
        f"{target_path}. Preserve its business behavior and response contract. "
        f"Replace any hard-coded validation or reference to {current.path} with "
        f"{target_path}. Return a complete handler for the target path."
    )
    if additional_instruction is not None:
        instruction += f"\n\nAdditional requirement:\n{additional_instruction}"
    return instruction


def _active_publications(runtime: RouteRuntime) -> dict[str, tuple[int, str]]:
    return {
        route.route_id: (route.version, _route_publication_digest(route))
        for route in runtime.list()
    }


def _recover_interrupted_at_startup(
    requirement_store: RequirementStore,
    runtime: RouteRuntime,
) -> None:
    """Recover unfinished work only from the real application startup owner."""

    pending_requirements = tuple(
        requirement
        for requirement in requirement_store.list()
        if requirement.status == "implementing"
    )
    pending_operations = tuple(
        operation
        for operation in requirement_store.list_operations()
        if operation.status in {"accepted", "implementing"}
    )
    requirement_store.recover_interrupted(_active_publications(runtime))
    operation_requirement_ids = {
        operation.requirement_id for operation in pending_operations
    }
    for pending_operation in pending_operations:
        recovered_operation = requirement_store.get_operation(pending_operation.id)
        log_recovery = (
            _logger.info
            if recovered_operation.status == "finish"
            else _logger.warning
        )
        log_recovery(
            "route_task recovered operation_id=%s requirement_id=%s "
            "project=%s method=%s path=%s cause=service_restart "
            "previous_status=%s outcome=%s error=%r",
            recovered_operation.id,
            recovered_operation.requirement_id,
            recovered_operation.project,
            recovered_operation.method,
            recovered_operation.path,
            pending_operation.status,
            recovered_operation.status,
            recovered_operation.last_error,
        )
    for pending_requirement in pending_requirements:
        if pending_requirement.id in operation_requirement_ids:
            continue
        recovered_requirement = requirement_store.get(pending_requirement.id)
        log_recovery = (
            _logger.info
            if recovered_requirement.status == "active"
            else _logger.warning
        )
        log_recovery(
            "route_task recovered requirement_id=%s project=%s method=%s "
            "path=%s cause=service_restart previous_status=%s outcome=%s "
            "error=%r",
            recovered_requirement.id,
            recovered_requirement.project,
            recovered_requirement.method,
            recovered_requirement.path,
            pending_requirement.status,
            recovered_requirement.status,
            recovered_requirement.last_error,
        )


def create_app(
    *,
    settings: Settings | None = None,
    generator: FeatureGenerator | None = None,
    plugin_generator: PluginFeatureGenerator | None = None,
    plugin_publisher: PluginPublisher | None = None,
    runtime: RouteRuntime | None = None,
    handler_executor: HandlerExecutor | None = None,
    plugin_executor: PluginExecutor | None = None,
    requirement_store: RequirementStore | None = None,
    lifespan: Any | None = None,
    async_route_creation: bool = True,
) -> FastAPI:
    """Build one application instance, allowing deterministic dependency injection."""

    active_settings = settings or load_settings()
    active_runtime = runtime or RouteRuntime(
        active_settings.generated_dir,
        plugin_artifact_root=active_settings.plugin_artifact_root,
    )
    active_generator = generator
    if active_generator is None and active_settings.llm_api_key:
        active_generator = _build_generator(active_settings)
    active_plugin_generator = plugin_generator
    if active_plugin_generator is None and active_settings.llm_api_key:
        active_plugin_generator = _build_plugin_generator(active_settings)
    plugin_policy = PluginPolicy(
        allowed_dependencies=frozenset(active_settings.plugin_allowed_dependencies),
        max_files=active_settings.plugin_max_files,
        max_file_bytes=active_settings.plugin_max_file_bytes,
        max_total_bytes=active_settings.plugin_max_total_bytes,
    )
    active_plugin_publisher = plugin_publisher or PluginPublisher(
        runtime=active_runtime,
        workspace_manager=PluginWorkspaceManager(
            workspace_root=active_settings.plugin_workspace_root,
            artifact_root=active_runtime.plugin_artifact_root,
        ),
        validator=PluginValidator(plugin_policy),
        test_runner=PluginTestRunner(timeout_seconds=30),
        keep_failed_workspaces=active_settings.plugin_keep_failed_workspaces,
    )
    service = AgentManagementService(
        active_runtime,
        active_generator,
        active_plugin_generator,
        active_plugin_publisher,
    )
    owns_requirement_store = requirement_store is None
    active_requirement_store = requirement_store or RequirementStore(
        active_settings.metadata_db_path
    )
    active_handler_executor = handler_executor or ProcessHandlerExecutor(
        timeout_seconds=active_settings.handler_timeout_seconds,
        memory_limit_bytes=active_settings.handler_memory_limit_mb * 1024 * 1024,
        cpu_limit_seconds=active_settings.handler_cpu_limit_seconds,
        max_result_bytes=active_settings.max_handler_result_bytes,
    )
    plugin_environments: dict[str, dict[str, str]] = {}
    for entry in active_settings.plugin_project_env_allowlist:
        project, environment_name = entry.split(":", 1)
        if environment_name in os.environ:
            plugin_environments.setdefault(project, {})[environment_name] = os.environ[
                environment_name
            ]
    plugin_networks = dict(
        entry.split(":", 1) for entry in active_settings.plugin_project_container_networks
    )
    plugin_executors: dict[str, PluginExecutor] = {}

    def plugin_executor_for(project: str) -> PluginExecutor:
        if plugin_executor is not None:
            return plugin_executor
        if project not in plugin_executors:
            executor_options = {
                "timeout_seconds": active_settings.handler_timeout_seconds,
                "memory_limit_bytes": active_settings.handler_memory_limit_mb * 1024 * 1024,
                "cpu_limit_seconds": active_settings.handler_cpu_limit_seconds,
                "max_result_bytes": active_settings.max_handler_result_bytes,
                "max_request_bytes": active_settings.max_request_body_bytes,
                "allowed_environment": plugin_environments.get(project),
            }
            if active_settings.plugin_execution_backend == "container":
                plugin_executors[project] = ContainerPluginExecutor(
                    runtime=active_settings.plugin_container_runtime,
                    image=active_settings.plugin_container_image,
                    network=plugin_networks.get(project),
                    **executor_options,
                )
            else:
                plugin_executors[project] = PluginProcessExecutor(**executor_options)
        return plugin_executors[project]

    active_plugin_executor = plugin_executor_for(DEFAULT_PROJECT)
    handler_slots = asyncio.Semaphore(active_settings.max_concurrent_handlers)

    def generation_is_available(execution_mode: str) -> bool:
        return (
            active_plugin_generator is not None
            if execution_mode == "plugin"
            else active_generator is not None
        )

    @asynccontextmanager
    async def application_lifespan(lifespan_app: FastAPI) -> AsyncIterator[Any]:
        # A spawn worker re-imports the service entrypoint before running its
        # target. Keeping recovery inside ASGI lifespan makes app construction
        # safe while retaining single-worker startup reconciliation.
        if owns_requirement_store:
            _recover_interrupted_at_startup(
                active_requirement_store,
                active_runtime,
            )
        if lifespan is None:
            yield None
        else:
            async with lifespan(lifespan_app) as lifespan_state:
                yield lifespan_state

    app = FastAPI(
        title="Self-Growing Agent",
        version="0.1.0",
        lifespan=application_lifespan,
    )
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_body_bytes=active_settings.max_request_body_bytes,
    )
    app.state.settings = active_settings
    app.state.runtime = active_runtime
    app.state.management_service = service
    app.state.plugin_generator = active_plugin_generator
    app.state.plugin_publisher = active_plugin_publisher
    app.state.handler_executor = active_handler_executor
    app.state.plugin_executor = active_plugin_executor
    app.state.requirement_store = active_requirement_store

    def release_handler_slot(completed: asyncio.Future[Any]) -> None:
        """Release capacity only after the submitted handler has really stopped."""

        handler_slots.release()
        if not completed.cancelled():
            # A disconnected client no longer awaits the shielded future. Retrieve
            # its exception here so asyncio does not report an unhandled failure.
            completed.exception()

    async def require_management_key(
        provided_key: Annotated[
            str | None,
            Header(alias="X-Management-Key"),
        ] = None,
    ) -> None:
        expected_key = active_settings.management_api_key
        if (
            provided_key is None
            or not expected_key
            or not secrets.compare_digest(provided_key, expected_key)
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid management key",
            )

    management_auth = Depends(require_management_key)

    @app.exception_handler(RouteValidationError)
    @app.exception_handler(ReservedPathError)
    @app.exception_handler(CodeValidationError)
    async def validation_error_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=_api_error(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)),
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        del request, exc
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=_api_error(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "request validation failed",
            ),
        )

    @app.exception_handler(RouteAlreadyExistsError)
    @app.exception_handler(VersionConflictError)
    @app.exception_handler(ManagedVersionConflictError)
    async def conflict_error_handler(request: Request, exc: Exception) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=_api_error(status.HTTP_409_CONFLICT, str(exc)),
        )

    @app.exception_handler(RouteNotFoundError)
    @app.exception_handler(ManagedRouteNotFoundError)
    async def not_found_error_handler(request: Request, exc: Exception) -> JSONResponse:
        del request, exc
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content=_api_error(status.HTTP_404_NOT_FOUND, "managed route not found"),
        )

    @app.exception_handler(LLMUnavailableError)
    async def llm_unavailable_handler(request: Request, exc: Exception) -> JSONResponse:
        del request
        message = str(exc)
        if message not in {"LLM is not configured", "plugin generation requires the Pi backend"}:
            message = "LLM is not configured"
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=_api_error(status.HTTP_503_SERVICE_UNAVAILABLE, message),
        )

    @app.exception_handler(FeatureGenerationError)
    async def generation_error_handler(request: Request, exc: Exception) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content=_api_error(
                status.HTTP_502_BAD_GATEWAY,
                _safe_requirement_error(exc),
            ),
        )

    @app.exception_handler(FeatureGenerationCapacityError)
    async def generation_capacity_error_handler(
        request: Request,
        exc: Exception,
    ) -> JSONResponse:
        del request, exc
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content=_api_error(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "generation capacity is full",
            ),
            headers={"Retry-After": "1"},
        )

    @app.exception_handler(RoutePersistenceError)
    async def persistence_error_handler(request: Request, exc: Exception) -> JSONResponse:
        del request, exc
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=_api_error(status.HTTP_503_SERVICE_UNAVAILABLE, "route publication failed"),
        )

    @app.exception_handler(PluginPublicationError)
    async def plugin_publication_error_handler(
        request: Request, exc: PluginPublicationError
    ) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=_api_error(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)[:256]),
        )

    @app.exception_handler(RequirementNotFoundError)
    async def requirement_not_found_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        del request, exc
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content=_api_error(status.HTTP_404_NOT_FOUND, "requirement not found"),
        )

    @app.exception_handler(OperationNotFoundError)
    async def operation_not_found_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        del request, exc
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content=_api_error(status.HTTP_404_NOT_FOUND, "operation not found"),
        )

    @app.exception_handler(RequirementBusyError)
    async def requirement_busy_handler(request: Request, exc: Exception) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=_api_error(status.HTTP_409_CONFLICT, str(exc)),
        )

    @app.exception_handler(RequirementStorageError)
    async def requirement_storage_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        del request, exc
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=_api_error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "requirement metadata is unavailable",
            ),
        )

    @app.exception_handler(HTTPException)
    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(
        request: Request,
        exc: StarletteHTTPException,
    ) -> JSONResponse:
        del request
        message = exc.detail if isinstance(exc.detail, str) else "request failed"
        return JSONResponse(
            status_code=exc.status_code,
            content=_api_error(exc.status_code, message),
            headers=exc.headers,
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
        del request, exc
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_api_error(status.HTTP_500_INTERNAL_SERVER_ERROR, "internal server error"),
        )

    @app.get("/console", include_in_schema=False)
    @app.get("/console/", include_in_schema=False)
    async def development_console() -> FileResponse:
        return FileResponse(
            _WEB_DIR / "index.html",
            headers=_CONSOLE_SECURITY_HEADERS,
        )

    @app.get("/console/assets/app.js", include_in_schema=False)
    async def development_console_javascript() -> FileResponse:
        return FileResponse(
            _WEB_DIR / "app.js",
            media_type="application/javascript",
            headers=_CONSOLE_SECURITY_HEADERS,
        )

    @app.get("/console/assets/styles.css", include_in_schema=False)
    async def development_console_stylesheet() -> FileResponse:
        return FileResponse(
            _WEB_DIR / "styles.css",
            media_type="text/css",
            headers=_CONSOLE_SECURITY_HEADERS,
        )

    @app.get("/healthz", tags=["system"])
    async def health() -> dict[str, Any]:
        return _api_success({"status": "ok", "event_time": _event_time()})

    @app.get(
        "/api/v1/manage/routes",
        response_model=ApiResponse[list[RouteResponse]],
        dependencies=[management_auth],
        tags=["management"],
    )
    async def list_routes(project: str | None = None) -> dict[str, Any]:
        normalized_project = (
            active_runtime.normalize_project(project) if project is not None else None
        )
        records = active_runtime.list()
        if normalized_project is not None:
            records = tuple(
                record for record in records if record.project == normalized_project
            )
        return _api_success([RouteResponse.from_record(record) for record in records])

    @app.post(
        "/api/v1/manage/routes",
        response_model=ApiResponse[RouteTaskResponse | RouteResponse],
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[management_auth],
        tags=["management"],
    )
    async def create_route(
        payload: CreateRouteRequest,
        response: Response,
    ) -> dict[str, Any]:
        public_path = _public_route_path(payload.project, payload.path)
        if not async_route_creation:
            record = await service.create_route(
                path=public_path,
                method=payload.method,
                project=payload.project,
                instruction=payload.instruction,
                execution_mode=payload.execution_mode,
            )
            response.status_code = status.HTTP_201_CREATED
            return _api_success(RouteResponse.from_record(record))

        if not generation_is_available(payload.execution_mode):
            raise LLMUnavailableError(
                "plugin generation requires the Pi backend"
                if payload.execution_mode == "plugin"
                else "LLM is not configured"
            )
        normalized_path, normalized_method = active_runtime.validate_route(
            public_path,
            payload.method,
        )
        if active_runtime.resolve(normalized_method, normalized_path) is not None:
            raise RouteAlreadyExistsError(
                f"route {normalized_method} {normalized_path} already exists"
            )
        requirement = await asyncio.to_thread(
            active_requirement_store.create,
            _route_requirement_title(payload.project, normalized_method, normalized_path),
            payload.instruction,
            normalized_path,
            normalized_method,
            project=payload.project,
            execution_mode=payload.execution_mode,
        )
        operation = await asyncio.to_thread(
            active_requirement_store.create_operation,
            requirement.id,
            kind="create",
            instruction=requirement.instruction,
            path=requirement.path,
            method=requirement.method,
            project=requirement.project,
            execution_mode=requirement.execution_mode,
        )
        _logger.info(
            "route_task state_transition operation_id=%s requirement_id=%s kind=create "
            "from_status=none to_status=accepted project=%s method=%s path=%s "
            "instruction_chars=%s",
            operation.id,
            requirement.id,
            requirement.project,
            requirement.method,
            requirement.path,
            len(payload.instruction),
        )
        start_requirement_implementation(requirement.id, operation.id)
        return _api_success(
            RouteTaskResponse(
                requirement_id=requirement.id,
                operation_id=operation.id,
                project=requirement.project,
                path=requirement.path,
                method=requirement.method,
                execution_mode=requirement.execution_mode,
                operation_url=f"/api/v1/manage/operations/{operation.id}",
            )
        )

    @app.put(
        "/api/v1/manage/routes/{route_id}",
        response_model=ApiResponse[RouteResponse],
        dependencies=[management_auth],
        tags=["management"],
    )
    async def update_route(route_id: str, payload: UpdateRouteRequest) -> dict[str, Any]:
        record = await service.update_route(
            route_id=route_id,
            instruction=payload.instruction,
            expected_version=payload.expected_version,
            execution_mode=payload.execution_mode,
        )
        return _api_success(RouteResponse.from_record(record))

    @app.post(
        "/api/v1/manage/routes/{route_id}/move",
        response_model=ApiResponse[RouteTaskResponse],
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[management_auth],
        tags=["management"],
    )
    async def move_route(route_id: str, payload: MoveRouteRequest) -> dict[str, Any]:
        current = active_runtime.get(route_id)
        if current is None:
            raise ManagedRouteNotFoundError("managed route not found")
        if not generation_is_available(current.execution_mode):
            raise LLMUnavailableError(
                "plugin generation requires the Pi backend"
                if current.execution_mode == "plugin"
                else "LLM is not configured"
            )
        if current.version != payload.expected_version:
            raise ManagedVersionConflictError(payload.expected_version, current.version)
        public_path = _public_route_path(payload.project, payload.path)
        normalized_path, normalized_method = active_runtime.validate_route(
            public_path,
            current.method,
        )
        if current.path == normalized_path and current.project == payload.project:
            raise RouteValidationError("route already has the requested project and path")
        existing = active_runtime.resolve(normalized_method, normalized_path)
        if existing is not None and existing.route_id != current.route_id:
            raise RouteAlreadyExistsError(
                f"route {normalized_method} {normalized_path} already exists"
            )
        await asyncio.to_thread(active_requirement_store.ensure_route_can_move, route_id)
        instruction = _route_move_instruction(
            current,
            normalized_path,
            payload.instruction,
        )
        requirement = await asyncio.to_thread(
            active_requirement_store.create,
            _route_requirement_title(payload.project, normalized_method, normalized_path),
            instruction,
            normalized_path,
            normalized_method,
            project=payload.project,
            execution_mode=current.execution_mode,
            route_id=current.route_id,
            route_version=current.version,
        )
        operation = await asyncio.to_thread(
            active_requirement_store.create_operation,
            requirement.id,
            kind="move",
            instruction=requirement.instruction,
            path=requirement.path,
            method=requirement.method,
            project=requirement.project,
            execution_mode=requirement.execution_mode,
            base_route_id=current.route_id,
            base_route_version=current.version,
        )
        _logger.info(
            "route_task state_transition operation_id=%s requirement_id=%s kind=move "
            "from_status=none to_status=accepted base_route_id=%s "
            "base_route_version=%s project=%s method=%s path=%s instruction_chars=%s",
            operation.id,
            requirement.id,
            current.route_id,
            current.version,
            requirement.project,
            requirement.method,
            requirement.path,
            len(requirement.instruction),
        )
        start_requirement_implementation(requirement.id, operation.id)
        return _api_success(
            RouteTaskResponse(
                requirement_id=requirement.id,
                operation_id=operation.id,
                project=requirement.project,
                path=requirement.path,
                method=requirement.method,
                execution_mode=requirement.execution_mode,
                operation_url=f"/api/v1/manage/operations/{operation.id}",
            )
        )

    @app.post(
        "/api/v1/manage/routes/{route_id}/rollback",
        response_model=ApiResponse[RouteResponse],
        dependencies=[management_auth],
        tags=["management"],
    )
    async def rollback_plugin_route(
        route_id: str, payload: RollbackRouteRequest
    ) -> dict[str, Any]:
        current = active_runtime.get(route_id)
        if current is None:
            raise ManagedRouteNotFoundError("managed route not found")
        if current.execution_mode != "plugin":
            raise RouteValidationError("only plugin routes can be rolled back")
        if current.version != payload.expected_version:
            raise ManagedVersionConflictError(payload.expected_version, current.version)
        if payload.target_version >= current.version:
            raise RouteValidationError("target version must be older than current version")
        operation_id = uuid.uuid4().hex
        _logger.info(
            "plugin_rollback accepted operation_id=%s route_id=%s "
            "from_version=%s target_version=%s",
            operation_id,
            route_id,
            current.version,
            payload.target_version,
        )
        record = await asyncio.to_thread(
            active_plugin_publisher.rollback,
            operation_id=operation_id,
            route_id=route_id,
            target_version=payload.target_version,
            expected_version=payload.expected_version,
        )
        return _api_success(RouteResponse.from_record(record))

    @app.get(
        "/api/v1/manage/operations",
        response_model=ApiResponse[list[OperationResponse]],
        dependencies=[management_auth],
        tags=["management"],
    )
    async def list_operations(
        requirement_id: str | None = None,
    ) -> dict[str, Any]:
        operations = await asyncio.to_thread(
            active_requirement_store.list_operations,
            requirement_id,
        )
        return _api_success(
            [OperationResponse.from_record(operation) for operation in operations]
        )

    @app.get(
        "/api/v1/manage/operations/{operation_id}",
        response_model=ApiResponse[OperationResponse],
        dependencies=[management_auth],
        tags=["management"],
    )
    async def get_operation(operation_id: str) -> dict[str, Any]:
        operation = await asyncio.to_thread(
            active_requirement_store.get_operation,
            operation_id,
        )
        return _api_success(OperationResponse.from_record(operation))

    @app.post(
        "/api/v1/manage/operations/{operation_id}/retry",
        response_model=ApiResponse[RouteTaskResponse],
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[management_auth],
        tags=["management"],
    )
    async def retry_operation(operation_id: str) -> dict[str, Any]:
        """Create a fresh execution snapshot from one failed operation."""

        previous = await asyncio.to_thread(
            active_requirement_store.get_operation,
            operation_id,
        )
        if previous.status != "failed":
            raise RequirementBusyError(
                f"operation {operation_id!r} cannot retry from status "
                f"{previous.status!r}"
            )
        if not generation_is_available(previous.execution_mode):
            raise LLMUnavailableError(
                "plugin generation requires the Pi backend"
                if previous.execution_mode == "plugin"
                else "LLM is not configured"
            )

        requirement = await asyncio.to_thread(
            active_requirement_store.get,
            previous.requirement_id,
        )
        retry_path = previous.path
        retry_method = previous.method
        retry_project = previous.project
        retry_execution_mode = previous.execution_mode
        base_route_id: str | None = None
        base_route_version: int | None = None

        if previous.kind in {"update", "move"}:
            current_route_id = requirement.route_id or previous.base_route_id
            current_route = (
                active_runtime.get(current_route_id)
                if current_route_id is not None
                else None
            )
            if current_route is None:
                raise ManagedRouteNotFoundError("managed route not found")
            base_route_id = current_route.route_id
            base_route_version = current_route.version
            if previous.kind == "update":
                retry_path = current_route.path
                retry_method = current_route.method
                retry_project = current_route.project
            elif current_route.method != previous.method:
                raise RouteValidationError(
                    "a route move cannot change the HTTP method"
                )

        retry = await asyncio.to_thread(
            active_requirement_store.create_operation,
            requirement.id,
            kind=previous.kind,
            instruction=previous.instruction,
            path=retry_path,
            method=retry_method,
            project=retry_project,
            execution_mode=retry_execution_mode,
            base_route_id=base_route_id,
            base_route_version=base_route_version,
        )
        _logger.info(
            "route_task retry_accepted operation_id=%s source_operation_id=%s "
            "requirement_id=%s kind=%s base_route_id=%s base_route_version=%s "
            "project=%s method=%s path=%s instruction_chars=%s",
            retry.id,
            previous.id,
            requirement.id,
            retry.kind,
            retry.base_route_id,
            retry.base_route_version,
            retry.project,
            retry.method,
            retry.path,
            len(retry.instruction),
        )
        start_requirement_implementation(requirement.id, retry.id)
        return _api_success(
            RouteTaskResponse(
                requirement_id=requirement.id,
                operation_id=retry.id,
                project=retry.project,
                path=retry.path,
                method=retry.method,
                execution_mode=retry.execution_mode,
                operation_url=f"/api/v1/manage/operations/{retry.id}",
            )
        )

    @app.get(
        "/api/v1/manage/requirements",
        response_model=ApiResponse[list[RequirementResponse]],
        dependencies=[management_auth],
        tags=["management"],
    )
    async def list_requirements(project: str | None = None) -> dict[str, Any]:
        normalized_project = (
            active_runtime.normalize_project(project) if project is not None else None
        )
        records = await asyncio.to_thread(active_requirement_store.list, normalized_project)
        return _api_success(
            [RequirementResponse.from_record(record) for record in records]
        )

    @app.post(
        "/api/v1/manage/requirements",
        response_model=ApiResponse[RequirementResponse],
        status_code=status.HTTP_201_CREATED,
        dependencies=[management_auth],
        tags=["management"],
    )
    async def create_requirement(
        payload: CreateRequirementRequest,
    ) -> dict[str, Any]:
        route_id = payload.route_id
        route_version: int | None = None
        linked_route: RouteRecord | None = None
        if route_id is not None:
            linked_route = active_runtime.get(route_id)
            if linked_route is None:
                raise ManagedRouteNotFoundError("managed route not found")
            supplied_path = normalize_path(payload.path)
            public_path = (
                supplied_path
                if supplied_path == linked_route.path
                else _public_route_path(payload.project, supplied_path)
            )
        else:
            public_path = _public_route_path(payload.project, payload.path)
        normalized_path, normalized_method = active_runtime.validate_route(
            public_path,
            payload.method,
        )
        active_route = active_runtime.resolve(normalized_method, normalized_path)
        if route_id is None:
            if active_route is not None:
                raise RouteAlreadyExistsError(
                    f"route {normalized_method} {normalized_path} already exists; "
                    "link the requirement to that route"
                )
        else:
            assert linked_route is not None
            if (
                linked_route.path != normalized_path
                or linked_route.method != normalized_method
                or linked_route.project != payload.project
            ):
                raise RouteValidationError(
                    "linked route does not match the requirement project, method, and path"
                )
            route_version = linked_route.version

        record = await asyncio.to_thread(
            active_requirement_store.create,
            payload.title,
            payload.instruction,
            normalized_path,
            normalized_method,
            project=payload.project,
            execution_mode=(
                linked_route.execution_mode
                if linked_route is not None
                else payload.execution_mode
            ),
            route_id=route_id,
            route_version=route_version,
        )
        return _api_success(RequirementResponse.from_record(record))

    @app.patch(
        "/api/v1/manage/requirements/{requirement_id}",
        response_model=ApiResponse[RequirementResponse],
        dependencies=[management_auth],
        tags=["management"],
    )
    async def update_requirement(
        requirement_id: str,
        payload: UpdateRequirementRequest,
    ) -> dict[str, Any]:
        record = await asyncio.to_thread(
            active_requirement_store.update_content,
            requirement_id,
            title=payload.title,
            instruction=payload.instruction,
            execution_mode=payload.execution_mode,
        )
        return _api_success(RequirementResponse.from_record(record))

    @app.get(
        "/api/v1/manage/requirements/{requirement_id}",
        response_model=ApiResponse[RequirementResponse],
        dependencies=[management_auth],
        tags=["management"],
    )
    async def get_requirement(requirement_id: str) -> dict[str, Any]:
        record = await asyncio.to_thread(active_requirement_store.get, requirement_id)
        return _api_success(RequirementResponse.from_record(record))

    @app.get(
        "/api/v1/manage/requirements/{requirement_id}/events",
        response_model=ApiResponse[list[RequirementEventResponse]],
        dependencies=[management_auth],
        tags=["management"],
    )
    async def list_requirement_events(
        requirement_id: str,
    ) -> dict[str, Any]:
        events = await asyncio.to_thread(
            active_requirement_store.list_events,
            requirement_id,
        )
        return _api_success(
            [RequirementEventResponse.from_event(event) for event in events]
        )

    @app.post(
        "/api/v1/manage/requirements/{requirement_id}/rebase",
        response_model=ApiResponse[RequirementResponse],
        dependencies=[management_auth],
        tags=["management"],
    )
    async def rebase_requirement(requirement_id: str) -> dict[str, Any]:
        requirement = await asyncio.to_thread(
            active_requirement_store.get,
            requirement_id,
        )
        if requirement.route_id is None:
            raise RequirementBusyError(
                f"requirement {requirement_id!r} is not linked to a route"
            )
        active_route = active_runtime.get(requirement.route_id)
        if active_route is None:
            raise ManagedRouteNotFoundError("managed route not found")
        if (
            active_route.path != requirement.path
            or active_route.method != requirement.method
            or active_route.project != requirement.project
        ):
            raise RouteValidationError(
                "linked route does not match the requirement project, method, and path"
            )
        rebased = await asyncio.to_thread(
            active_requirement_store.rebase_route,
            requirement_id,
            route_id=active_route.route_id,
            route_version=active_route.version,
        )
        return _api_success(RequirementResponse.from_record(rebased))

    async def finalize_requirement(
        requirement_id: str,
        route: RouteRecord,
    ) -> RequirementRecord:
        source_sha256 = _route_publication_digest(route)
        for attempt in range(_REQUIREMENT_FINALIZE_ATTEMPTS):
            try:
                return await asyncio.to_thread(
                    active_requirement_store.complete_implementation,
                    requirement_id,
                    route_id=route.route_id,
                    route_version=route.version,
                    source_sha256=source_sha256,
                )
            except RequirementBusyError:
                if attempt + 1 == _REQUIREMENT_FINALIZE_ATTEMPTS:
                    raise
                await asyncio.sleep(0.01 * (2**attempt))
        raise AssertionError("requirement finalization attempts were exhausted")

    async def fail_requirement(requirement_id: str, message: str) -> None:
        try:
            await asyncio.to_thread(
                active_requirement_store.fail_implementation,
                requirement_id,
                message,
            )
        except RequirementStoreError:
            # A persisted receipt lets startup or a later request reconcile a
            # route that was committed while SQLite was temporarily unavailable.
            pass

    async def run_requirement_implementation(
        requirement_id: str,
        operation_id: str | None = None,
    ) -> RequirementRecord:
        started_at = time.monotonic()
        operation: OperationRecord | None = None
        try:
            if operation_id is None:
                requirement = await asyncio.to_thread(
                    active_requirement_store.begin_implementation,
                    requirement_id,
                )
            else:
                operation = await asyncio.to_thread(
                    active_requirement_store.begin_operation,
                    operation_id,
                )
                requirement = await asyncio.to_thread(
                    active_requirement_store.get,
                    requirement_id,
                )
        except Exception as exc:
            _logger.warning(
                "route_task claim_failed operation_id=%s requirement_id=%s "
                "exception_type=%s elapsed_seconds=%.3f",
                operation_id or requirement_id,
                requirement_id,
                type(exc).__name__,
                time.monotonic() - started_at,
            )
            raise
        work_id = operation.id if operation is not None else requirement.id
        work_instruction = (
            operation.instruction if operation is not None else requirement.instruction
        )
        work_path = operation.path if operation is not None else requirement.path
        work_method = operation.method if operation is not None else requirement.method
        work_project = operation.project if operation is not None else requirement.project
        work_execution_mode = (
            operation.execution_mode if operation is not None else requirement.execution_mode
        )
        base_route_id = (
            operation.base_route_id if operation is not None else requirement.route_id
        )
        base_route_version = (
            operation.base_route_version
            if operation is not None
            else requirement.route_version
        )
        _logger.info(
            "route_task state_transition operation_id=%s requirement_id=%s kind=%s "
            "from_status=%s to_status=implementing base_route_id=%s "
            "base_route_version=%s",
            work_id,
            requirement_id,
            operation.kind if operation is not None else "requirement",
            "accepted" if operation is not None else "draft_or_failed",
            base_route_id,
            base_route_version,
        )
        _logger.info(
            "route_task generation_started operation_id=%s requirement_id=%s "
            "project=%s method=%s path=%s mode=%s execution_mode=%s instruction_chars=%s",
            work_id,
            requirement_id,
            work_project,
            work_method,
            work_path,
            operation.kind
            if operation is not None
            else ("update" if base_route_id is not None else "create"),
            work_execution_mode,
            len(work_instruction),
        )

        async def prepare_publication(
            route_id: str,
            route_version: int,
            source: str,
        ) -> None:
            publication_digest = (
                source
                if work_execution_mode == "plugin"
                else _source_sha256(source)
            )
            await asyncio.to_thread(
                active_requirement_store.prepare_publication,
                requirement_id,
                route_id=route_id,
                route_version=route_version,
                source_sha256=publication_digest,
            )
            if operation is not None:
                await asyncio.to_thread(
                    active_requirement_store.prepare_operation_publication,
                    operation.id,
                    route_id=route_id,
                    route_version=route_version,
                    source_sha256=publication_digest,
                )
            _logger.info(
                "route_task publication_prepared operation_id=%s requirement_id=%s "
                "route_id=%s version=%s source_chars=%s",
                work_id,
                requirement_id,
                route_id,
                route_version,
                len(source),
            )

        try:
            if base_route_id is None:
                route = await service.create_route(
                    path=work_path,
                    method=work_method,
                    project=work_project,
                    instruction=work_instruction,
                    execution_mode=work_execution_mode,
                    operation_id=work_id,
                    before_publish=prepare_publication,
                )
            else:
                if base_route_version is None:
                    raise RequirementStorageError(
                        "linked requirement has no route version"
                    )
                current_route = active_runtime.get(base_route_id)
                if current_route is None:
                    raise ManagedRouteNotFoundError("managed route not found")
                is_route_move = (
                    operation.kind == "move"
                    if operation is not None
                    else (
                        current_route.path != work_path
                        or current_route.project != work_project
                    )
                )
                if is_route_move:
                    if current_route.method != work_method:
                        raise RouteValidationError(
                            "a route move cannot change the HTTP method"
                        )
                    await asyncio.to_thread(
                        active_requirement_store.ensure_route_can_move,
                        current_route.route_id,
                        exclude_requirement_id=requirement.id,
                    )
                    route = await service.move_route(
                        route_id=current_route.route_id,
                        path=work_path,
                        project=work_project,
                        instruction=work_instruction,
                        expected_version=base_route_version,
                        execution_mode=work_execution_mode,
                        operation_id=work_id,
                        before_publish=prepare_publication,
                    )
                    await asyncio.to_thread(
                        active_requirement_store.move_route_links,
                        current_route.route_id,
                        route_id=route.route_id,
                        route_version=route.version,
                        path=route.path,
                        project=route.project,
                        exclude_requirement_id=requirement.id,
                    )
                else:
                    route = await service.update_route(
                        route_id=base_route_id,
                        instruction=work_instruction,
                        expected_version=base_route_version,
                        execution_mode=work_execution_mode,
                        operation_id=work_id,
                        before_publish=prepare_publication,
                    )
        except asyncio.CancelledError:
            persistence_errors: list[str] = []
            try:
                active_requirement_store.fail_implementation(
                    requirement_id,
                    _BACKGROUND_OPERATION_CANCELLED_MESSAGE,
                )
            except RequirementStoreError as exc:
                persistence_errors.append(type(exc).__name__)
            if operation is not None:
                try:
                    active_requirement_store.fail_operation(
                        operation.id,
                        _BACKGROUND_OPERATION_CANCELLED_MESSAGE,
                    )
                except RequirementStoreError as exc:
                    persistence_errors.append(type(exc).__name__)
            _logger.warning(
                "route_task cancelled operation_id=%s requirement_id=%s "
                "stage=generation_or_publication cause=asyncio_cancelled "
                "persistence_errors=%s elapsed_seconds=%.3f",
                work_id,
                requirement_id,
                persistence_errors or None,
                time.monotonic() - started_at,
            )
            raise
        except Exception as exc:
            safe_error = _safe_requirement_error(exc)
            await fail_requirement(requirement_id, safe_error)
            if operation is not None:
                await asyncio.to_thread(
                    active_requirement_store.fail_operation,
                    operation.id,
                    safe_error,
                )
            _logger.warning(
                "route_task failed operation_id=%s requirement_id=%s "
                "stage=generation_or_publication exception_type=%s error=%s "
                "elapsed_seconds=%.3f",
                work_id,
                requirement_id,
                type(exc).__name__,
                safe_error,
                time.monotonic() - started_at,
            )
            if operation is not None:
                _logger.warning(
                    "route_task state_transition operation_id=%s requirement_id=%s "
                    "kind=%s from_status=implementing to_status=failed error=%s",
                    operation.id,
                    requirement_id,
                    operation.kind,
                    safe_error,
                )
            raise
        # Once the route is published, retain its receipt if SQLite finalization
        # cannot finish. Startup or a repeated implement request can then reconcile
        # the exact version and source hash without another LLM call.
        try:
            completed = await finalize_requirement(requirement_id, route)
            if operation is not None:
                await asyncio.to_thread(
                    active_requirement_store.complete_operation,
                    operation.id,
                    route_id=route.route_id,
                    route_version=route.version,
                )
        except Exception as exc:
            _logger.warning(
                "route_task finalization_pending operation_id=%s requirement_id=%s "
                "route_id=%s version=%s exception_type=%s elapsed_seconds=%.3f",
                work_id,
                requirement_id,
                route.route_id,
                route.version,
                type(exc).__name__,
                time.monotonic() - started_at,
            )
            raise
        if operation is not None:
            _logger.info(
                "route_task state_transition operation_id=%s requirement_id=%s kind=%s "
                "from_status=implementing to_status=finish route_id=%s version=%s",
                operation.id,
                requirement_id,
                operation.kind,
                route.route_id,
                route.version,
            )
        _logger.info(
            "route_task completed operation_id=%s requirement_id=%s route_id=%s "
            "version=%s elapsed_seconds=%.3f",
            work_id,
            requirement_id,
            completed.route_id,
            completed.route_version,
            time.monotonic() - started_at,
        )
        return completed

    def consume_implementation_result(task: asyncio.Task[RequirementRecord]) -> None:
        if not task.cancelled():
            task.exception()

    def start_requirement_implementation(
        requirement_id: str,
        operation_id: str | None = None,
    ) -> asyncio.Task[RequirementRecord]:
        """Schedule persisted requirement work without holding a management request."""

        with operation_log_context(operation_id or requirement_id):
            implementation_task = asyncio.create_task(
                run_requirement_implementation(requirement_id, operation_id)
            )
        implementation_task.add_done_callback(consume_implementation_result)
        return implementation_task

    @app.post(
        "/api/v1/manage/requirements/{requirement_id}/revise-and-implement",
        response_model=ApiResponse[RouteTaskResponse],
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[management_auth],
        tags=["management"],
    )
    async def revise_and_implement_requirement(
        requirement_id: str,
        payload: UpdateRequirementRequest,
    ) -> dict[str, Any]:
        """Persist a revision and start its implementation as one async operation."""

        current_requirement = await asyncio.to_thread(
            active_requirement_store.get,
            requirement_id,
        )
        current_route: RouteRecord | None = None
        operation_kind = "create"
        if current_requirement.route_id is not None:
            current_route = active_runtime.get(current_requirement.route_id)
            if current_route is None:
                raise ManagedRouteNotFoundError("managed route not found")
            if (
                current_route.path != current_requirement.path
                or current_route.method != current_requirement.method
                or current_route.project != current_requirement.project
            ):
                raise RouteValidationError(
                    "linked route does not match the requirement project, method, and path"
                )
            operation_kind = "update"
        target_execution_mode = (
            payload.execution_mode or current_requirement.execution_mode
        )
        if not generation_is_available(target_execution_mode):
            raise LLMUnavailableError(
                "plugin generation requires the Pi backend"
                if target_execution_mode == "plugin"
                else "LLM is not configured"
            )
        operation = await asyncio.to_thread(
            active_requirement_store.revise_and_create_operation,
            requirement_id,
            title=payload.title,
            instruction=payload.instruction,
            kind=operation_kind,
            execution_mode=target_execution_mode,
            base_route_id=current_route.route_id if current_route is not None else None,
            base_route_version=(
                current_route.version if current_route is not None else None
            ),
        )
        requirement = await asyncio.to_thread(
            active_requirement_store.get,
            requirement_id,
        )
        _logger.info(
            "route_task state_transition operation_id=%s requirement_id=%s kind=%s "
            "from_status=none to_status=accepted base_route_id=%s "
            "base_route_version=%s project=%s method=%s path=%s instruction_chars=%s",
            operation.id,
            requirement.id,
            operation.kind,
            operation.base_route_id,
            operation.base_route_version,
            requirement.project,
            requirement.method,
            requirement.path,
            len(requirement.instruction),
        )
        start_requirement_implementation(requirement.id, operation.id)
        return _api_success(
            RouteTaskResponse(
                requirement_id=requirement.id,
                operation_id=operation.id,
                project=requirement.project,
                path=requirement.path,
                method=requirement.method,
                execution_mode=operation.execution_mode,
                operation_url=f"/api/v1/manage/operations/{operation.id}",
            )
        )

    @app.post(
        "/api/v1/manage/requirements/{requirement_id}/implement",
        response_model=ApiResponse[RequirementResponse],
        dependencies=[management_auth],
        tags=["management"],
    )
    async def implement_requirement(requirement_id: str) -> dict[str, Any]:
        current = await asyncio.to_thread(
            active_requirement_store.get,
            requirement_id,
        )
        if not generation_is_available(current.execution_mode):
            raise LLMUnavailableError(
                "plugin generation requires the Pi backend"
                if current.execution_mode == "plugin"
                else "LLM is not configured"
            )
        if current.status == "implementing":
            current = await asyncio.to_thread(
                active_requirement_store.reconcile_publication,
                requirement_id,
                _active_publications(active_runtime),
            )
            if current.status == "active":
                return _api_success(RequirementResponse.from_record(current))

        implementation_task = start_requirement_implementation(requirement_id)
        completed = await asyncio.shield(implementation_task)
        return _api_success(RequirementResponse.from_record(completed))

    @app.api_route(
        "/{dynamic_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        include_in_schema=False,
    )
    async def dispatch_dynamic_route(dynamic_path: str, request: Request) -> Any:
        path = f"/{dynamic_path}"
        record = active_runtime.resolve(request.method, path)
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="dynamic route not found",
            )

        try:
            await asyncio.wait_for(
                handler_slots.acquire(),
                timeout=active_settings.handler_admission_timeout_seconds,
            )
        except TimeoutError:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="dynamic handler capacity is full",
                headers={"Retry-After": "1"},
            ) from None

        execution: asyncio.Future[Any] | None = None
        try:
            context = await _build_request_context(
                request,
                max_body_bytes=active_settings.max_request_body_bytes,
            )
            _logger.info(
                "dynamic_route request route_id=%s method=%s path=%s query=%s body=%s",
                record.route_id,
                request.method,
                path,
                _request_parameters_for_log(context["query"]),
                _request_parameters_for_log(context["body"]),
            )
            try:
                if record.execution_mode == "plugin":
                    if record.artifact_path is None or record.artifact_digest is None:
                        raise PluginProcessError("plugin artifact is unavailable")
                    execution = asyncio.get_running_loop().run_in_executor(
                        None,
                        plugin_executor_for(record.project).execute,
                        record.artifact_path,
                        record.artifact_digest,
                        context,
                    )
                else:
                    if record.source is None:
                        raise HandlerProcessError("Generated handler source is unavailable")
                    module_name = (
                        f"dynamic_{record.route_id.replace('-', '_')}_v{record.version}"
                    )
                    execution = asyncio.get_running_loop().run_in_executor(
                        None,
                        active_handler_executor.execute,
                        record.source,
                        module_name,
                        context,
                    )
                execution.add_done_callback(release_handler_slot)
                return _api_success(await asyncio.shield(execution))
            except (HandlerTimeoutError, PluginTimeoutError):
                raise HTTPException(
                    status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                    detail="dynamic handler timed out",
                ) from None
            except HandlerContractError as exc:
                message = _safe_handler_error(exc)
                _logger.warning(
                    "dynamic_route failed route_id=%s method=%s path=%s error=%s",
                    record.route_id,
                    request.method,
                    path,
                    message,
                )
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=message,
                ) from None
            except (HandlerProcessError, PluginProcessError) as exc:
                message = _safe_handler_error(exc)
                _logger.warning(
                    "dynamic_route failed route_id=%s method=%s path=%s error=%s",
                    record.route_id,
                    request.method,
                    path,
                    message,
                )
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=message,
                ) from None
            except Exception:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="dynamic handler failed",
                ) from None
        finally:
            if execution is None:
                handler_slots.release()

    return app


async def _build_request_context(
    request: Request,
    *,
    max_body_bytes: int,
) -> dict[str, Any]:
    body: Any = None
    raw_body = await _read_limited_body(request, max_body_bytes=max_body_bytes)
    if raw_body:
        # Dynamic APIs use JSON as their body contract.  Parse valid JSON even
        # when a simple curl ``-d '{...}'`` omits Content-Type, while still
        # documenting application/json as the interoperable client behaviour.
        try:
            body = json.loads(raw_body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="dynamic request body must be valid JSON",
            ) from None

    headers = {
        name: value
        for name, value in request.headers.items()
        if name.lower() in _ALLOWED_BUSINESS_HEADERS
    }
    return {
        "method": request.method,
        "path": request.url.path,
        "query": dict(request.query_params),
        "headers": headers,
        "body": body,
    }


async def _read_limited_body(request: Request, *, max_body_bytes: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > max_body_bytes:
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail="request body is too large",
                )
        except ValueError:
            pass

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > max_body_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="request body is too large",
            )
        body.extend(chunk)
    return bytes(body)
