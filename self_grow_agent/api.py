"""FastAPI application factory for the business and management planes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
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
    RequirementBusyError,
    RequirementEvent,
    RequirementNotFoundError,
    RequirementRecord,
    RequirementStorageError,
    RequirementStore,
    RequirementStoreError,
)
from self_grow_agent.pi_generator import PiFeatureGenerator
from self_grow_agent.pi_rpc import PiRpcClient
from self_grow_agent.runtime import (
    ReservedPathError,
    RouteAlreadyExistsError,
    RouteNotFoundError,
    RoutePersistenceError,
    RouteRecord,
    RouteRuntime,
    RouteValidationError,
    VersionConflictError,
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


def _required_text(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("must not be blank")
    return normalized


def _business_success(data: Any) -> dict[str, Any]:
    """Wrap a generated-handler result in the public business API envelope."""

    return {**_BUSINESS_SUCCESS_RESPONSE, "data": data}


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
            content={"detail": "request body is too large"},
            headers={"Connection": "close"},
        )
        await response(scope, receive, send)


class CreateRouteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=256)
    method: str = Field(min_length=1, max_length=16)
    instruction: str = Field(min_length=1, max_length=8_000)

    _normalize_instruction = field_validator("instruction")(_required_text)


class UpdateRouteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instruction: str = Field(min_length=1, max_length=8_000)
    expected_version: int = Field(ge=1)

    _normalize_instruction = field_validator("instruction")(_required_text)


class CreateRequirementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=120)
    instruction: str = Field(min_length=1, max_length=8_000)
    path: str = Field(min_length=1, max_length=256)
    method: str = Field(min_length=1, max_length=16)
    route_id: str | None = Field(default=None, min_length=1, max_length=128)

    _normalize_required_text = field_validator("title", "instruction")(_required_text)


class UpdateRequirementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=120)
    instruction: str = Field(min_length=1, max_length=8_000)

    _normalize_required_text = field_validator("title", "instruction")(_required_text)


class RouteResponse(BaseModel):
    route_id: str
    path: str
    method: str
    version: int
    description: str

    @classmethod
    def from_record(cls, record: RouteRecord) -> "RouteResponse":
        return cls(
            route_id=record.route_id,
            path=record.path,
            method=record.method,
            version=record.version,
            description=record.description,
        )


class RequirementResponse(BaseModel):
    id: str
    title: str
    instruction: str
    path: str
    method: str
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
            route_id=record.route_id,
            route_version=record.route_version,
            status=record.status,
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
            from_status=event.from_status,
            to_status=event.to_status,
            message=event.message,
            created_at=event.created_at,
        )


def _build_generator(settings: Settings) -> FeatureGenerator | None:
    if not settings.llm_api_key:
        return None
    if settings.generation_backend == "pi":
        rpc_client = PiRpcClient(
            command=(settings.pi_executable,),
            provider=settings.pi_provider,
            model=settings.pi_model,
            api_key=settings.llm_api_key,
            provider_env_name=settings.pi_provider_env_name,
            timeout_seconds=settings.pi_timeout_seconds,
            workspace_root=settings.pi_workspace_root,
        )
        return PiFeatureGenerator(
            rpc_client=rpc_client,
            max_concurrent_runs=settings.pi_max_concurrent_runs,
            admission_timeout_seconds=settings.pi_admission_timeout_seconds,
        )
    return OpenAIFeatureGenerator(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        timeout_seconds=settings.llm_timeout_seconds,
    )


def _safe_requirement_error(exc: Exception) -> str:
    """Return a persistent failure message that cannot expose provider details."""

    if isinstance(exc, LLMUnavailableError):
        return "LLM is not configured"
    if isinstance(exc, FeatureGenerationError):
        return "LLM generation failed"
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


def _source_sha256(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _active_publications(runtime: RouteRuntime) -> dict[str, tuple[int, str]]:
    return {
        route.route_id: (route.version, _source_sha256(route.source))
        for route in runtime.list()
    }


def create_app(
    *,
    settings: Settings | None = None,
    generator: FeatureGenerator | None = None,
    runtime: RouteRuntime | None = None,
    handler_executor: HandlerExecutor | None = None,
    requirement_store: RequirementStore | None = None,
) -> FastAPI:
    """Build one application instance, allowing deterministic dependency injection."""

    active_settings = settings or load_settings()
    active_runtime = runtime or RouteRuntime(active_settings.generated_dir)
    active_generator = generator
    if active_generator is None and active_settings.llm_api_key:
        active_generator = _build_generator(active_settings)
    service = AgentManagementService(active_runtime, active_generator)
    owns_requirement_store = requirement_store is None
    active_requirement_store = requirement_store or RequirementStore(
        active_settings.metadata_db_path
    )
    if owns_requirement_store:
        # The documented single-worker startup owns recovery. Merely opening a
        # second RequirementStore for inspection must never mutate active work.
        active_requirement_store.recover_interrupted(
            _active_publications(active_runtime)
        )
    active_handler_executor = handler_executor or ProcessHandlerExecutor(
        timeout_seconds=active_settings.handler_timeout_seconds,
        memory_limit_bytes=active_settings.handler_memory_limit_mb * 1024 * 1024,
        cpu_limit_seconds=active_settings.handler_cpu_limit_seconds,
        max_result_bytes=active_settings.max_handler_result_bytes,
    )
    handler_slots = asyncio.Semaphore(active_settings.max_concurrent_handlers)

    app = FastAPI(title="Self-Growing Agent", version="0.1.0")
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_body_bytes=active_settings.max_request_body_bytes,
    )
    app.state.settings = active_settings
    app.state.runtime = active_runtime
    app.state.management_service = service
    app.state.handler_executor = active_handler_executor
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
            content={"detail": str(exc)},
        )

    @app.exception_handler(RouteAlreadyExistsError)
    @app.exception_handler(VersionConflictError)
    @app.exception_handler(ManagedVersionConflictError)
    async def conflict_error_handler(request: Request, exc: Exception) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": str(exc)},
        )

    @app.exception_handler(RouteNotFoundError)
    @app.exception_handler(ManagedRouteNotFoundError)
    async def not_found_error_handler(request: Request, exc: Exception) -> JSONResponse:
        del request, exc
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"detail": "managed route not found"},
        )

    @app.exception_handler(LLMUnavailableError)
    async def llm_unavailable_handler(request: Request, exc: Exception) -> JSONResponse:
        del request, exc
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": "LLM is not configured"},
        )

    @app.exception_handler(FeatureGenerationError)
    async def generation_error_handler(request: Request, exc: Exception) -> JSONResponse:
        del request, exc
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"detail": "LLM generation failed"},
        )

    @app.exception_handler(FeatureGenerationCapacityError)
    async def generation_capacity_error_handler(
        request: Request,
        exc: Exception,
    ) -> JSONResponse:
        del request, exc
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"detail": "generation capacity is full"},
            headers={"Retry-After": "1"},
        )

    @app.exception_handler(RoutePersistenceError)
    async def persistence_error_handler(request: Request, exc: Exception) -> JSONResponse:
        del request, exc
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": "route publication failed"},
        )

    @app.exception_handler(RequirementNotFoundError)
    async def requirement_not_found_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        del request, exc
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"detail": "requirement not found"},
        )

    @app.exception_handler(RequirementBusyError)
    async def requirement_busy_handler(request: Request, exc: Exception) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": str(exc)},
        )

    @app.exception_handler(RequirementStorageError)
    async def requirement_storage_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        del request, exc
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": "requirement metadata is unavailable"},
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
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get(
        "/api/v1/manage/routes",
        response_model=list[RouteResponse],
        dependencies=[management_auth],
        tags=["management"],
    )
    async def list_routes() -> list[RouteResponse]:
        return [RouteResponse.from_record(record) for record in active_runtime.list()]

    @app.post(
        "/api/v1/manage/routes",
        response_model=RouteResponse,
        status_code=status.HTTP_201_CREATED,
        dependencies=[management_auth],
        tags=["management"],
    )
    async def create_route(payload: CreateRouteRequest) -> RouteResponse:
        record = await service.create_route(
            path=payload.path,
            method=payload.method,
            instruction=payload.instruction,
        )
        return RouteResponse.from_record(record)

    @app.put(
        "/api/v1/manage/routes/{route_id}",
        response_model=RouteResponse,
        dependencies=[management_auth],
        tags=["management"],
    )
    async def update_route(route_id: str, payload: UpdateRouteRequest) -> RouteResponse:
        record = await service.update_route(
            route_id=route_id,
            instruction=payload.instruction,
            expected_version=payload.expected_version,
        )
        return RouteResponse.from_record(record)

    @app.get(
        "/api/v1/manage/requirements",
        response_model=list[RequirementResponse],
        dependencies=[management_auth],
        tags=["management"],
    )
    async def list_requirements() -> list[RequirementResponse]:
        records = await asyncio.to_thread(active_requirement_store.list)
        return [RequirementResponse.from_record(record) for record in records]

    @app.post(
        "/api/v1/manage/requirements",
        response_model=RequirementResponse,
        status_code=status.HTTP_201_CREATED,
        dependencies=[management_auth],
        tags=["management"],
    )
    async def create_requirement(
        payload: CreateRequirementRequest,
    ) -> RequirementResponse:
        normalized_path, normalized_method = active_runtime.validate_route(
            payload.path,
            payload.method,
        )
        route_id = payload.route_id
        route_version: int | None = None
        active_route = active_runtime.resolve(normalized_method, normalized_path)
        if route_id is None:
            if active_route is not None:
                raise RouteAlreadyExistsError(
                    f"route {normalized_method} {normalized_path} already exists; "
                    "link the requirement to that route"
                )
        else:
            linked_route = active_runtime.get(route_id)
            if linked_route is None:
                raise ManagedRouteNotFoundError("managed route not found")
            if (
                linked_route.path != normalized_path
                or linked_route.method != normalized_method
            ):
                raise RouteValidationError(
                    "linked route does not match the requirement method and path"
                )
            route_version = linked_route.version

        record = await asyncio.to_thread(
            active_requirement_store.create,
            payload.title,
            payload.instruction,
            normalized_path,
            normalized_method,
            route_id=route_id,
            route_version=route_version,
        )
        return RequirementResponse.from_record(record)

    @app.patch(
        "/api/v1/manage/requirements/{requirement_id}",
        response_model=RequirementResponse,
        dependencies=[management_auth],
        tags=["management"],
    )
    async def update_requirement(
        requirement_id: str,
        payload: UpdateRequirementRequest,
    ) -> RequirementResponse:
        record = await asyncio.to_thread(
            active_requirement_store.update_content,
            requirement_id,
            title=payload.title,
            instruction=payload.instruction,
        )
        return RequirementResponse.from_record(record)

    @app.get(
        "/api/v1/manage/requirements/{requirement_id}/events",
        response_model=list[RequirementEventResponse],
        dependencies=[management_auth],
        tags=["management"],
    )
    async def list_requirement_events(
        requirement_id: str,
    ) -> list[RequirementEventResponse]:
        events = await asyncio.to_thread(
            active_requirement_store.list_events,
            requirement_id,
        )
        return [RequirementEventResponse.from_event(event) for event in events]

    @app.post(
        "/api/v1/manage/requirements/{requirement_id}/rebase",
        response_model=RequirementResponse,
        dependencies=[management_auth],
        tags=["management"],
    )
    async def rebase_requirement(requirement_id: str) -> RequirementResponse:
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
        ):
            raise RouteValidationError(
                "linked route does not match the requirement method and path"
            )
        rebased = await asyncio.to_thread(
            active_requirement_store.rebase_route,
            requirement_id,
            route_id=active_route.route_id,
            route_version=active_route.version,
        )
        return RequirementResponse.from_record(rebased)

    async def finalize_requirement(
        requirement_id: str,
        route: RouteRecord,
    ) -> RequirementRecord:
        source_sha256 = _source_sha256(route.source)
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
    ) -> RequirementRecord:
        requirement = await asyncio.to_thread(
            active_requirement_store.begin_implementation,
            requirement_id,
        )

        async def prepare_publication(
            route_id: str,
            route_version: int,
            source: str,
        ) -> None:
            await asyncio.to_thread(
                active_requirement_store.prepare_publication,
                requirement_id,
                route_id=route_id,
                route_version=route_version,
                source_sha256=_source_sha256(source),
            )

        try:
            if requirement.route_id is None:
                route = await service.create_route(
                    path=requirement.path,
                    method=requirement.method,
                    instruction=requirement.instruction,
                    before_publish=prepare_publication,
                )
            else:
                if requirement.route_version is None:
                    raise RequirementStorageError(
                        "linked requirement has no route version"
                    )
                route = await service.update_route(
                    route_id=requirement.route_id,
                    instruction=requirement.instruction,
                    expected_version=requirement.route_version,
                    before_publish=prepare_publication,
                )
        except Exception as exc:
            await fail_requirement(requirement_id, _safe_requirement_error(exc))
            raise
        # Once the route is published, retain its receipt if SQLite finalization
        # cannot finish. Startup or a repeated implement request can then reconcile
        # the exact version and source hash without another LLM call.
        return await finalize_requirement(requirement_id, route)

    def consume_implementation_result(task: asyncio.Task[RequirementRecord]) -> None:
        if not task.cancelled():
            task.exception()

    @app.post(
        "/api/v1/manage/requirements/{requirement_id}/implement",
        response_model=RequirementResponse,
        dependencies=[management_auth],
        tags=["management"],
    )
    async def implement_requirement(requirement_id: str) -> RequirementResponse:
        current = await asyncio.to_thread(
            active_requirement_store.get,
            requirement_id,
        )
        if current.status == "implementing":
            current = await asyncio.to_thread(
                active_requirement_store.reconcile_publication,
                requirement_id,
                _active_publications(active_runtime),
            )
            if current.status == "active":
                return RequirementResponse.from_record(current)

        implementation_task = asyncio.create_task(
            run_requirement_implementation(requirement_id)
        )
        implementation_task.add_done_callback(consume_implementation_result)
        completed = await asyncio.shield(implementation_task)
        return RequirementResponse.from_record(completed)

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
            module_name = f"dynamic_{record.route_id.replace('-', '_')}_v{record.version}"
            try:
                execution = asyncio.get_running_loop().run_in_executor(
                    None,
                    active_handler_executor.execute,
                    record.source,
                    module_name,
                    context,
                )
                execution.add_done_callback(release_handler_slot)
                return _business_success(await asyncio.shield(execution))
            except HandlerTimeoutError:
                raise HTTPException(
                    status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                    detail="dynamic handler timed out",
                ) from None
            except HandlerContractError:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="dynamic handler returned an invalid response",
                ) from None
            except HandlerProcessError:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="dynamic handler failed",
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
        media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
        if media_type == "application/json" or media_type.endswith("+json"):
            try:
                body = json.loads(raw_body)
            except (UnicodeDecodeError, json.JSONDecodeError):
                body = raw_body.decode("utf-8", errors="replace")
        else:
            body = raw_body.decode("utf-8", errors="replace")

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
