"""Persistent dynamic-route registry with compare-and-swap hot updates."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any, Protocol

from self_grow_agent.projects import DEFAULT_PROJECT, normalize_project

MANIFEST_SCHEMA_VERSION = 1
MANIFEST_FILENAME = "routes.json"
SUPPORTED_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
RESERVED_ROOTS = (
    "/api/v1/manage",
    "/healthz",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/console",
)


class HandlerLoader(Protocol):
    """The small part of ``GeneratedCodeLoader`` needed by the runtime."""

    def load(
        self, source: str, module_name: str
    ) -> Callable[[dict[str, Any]], Any]: ...


class RouteRuntimeError(Exception):
    """Base class for route-registry errors."""


class RouteValidationError(RouteRuntimeError, ValueError):
    """A route definition is not safe or supported."""


class ReservedPathError(RouteValidationError):
    """A route overlaps the stable control plane."""


class RouteConflictError(RouteRuntimeError):
    """Base class for optimistic-concurrency conflicts."""


class RouteAlreadyExistsError(RouteConflictError):
    """The method/path pair is already registered."""


class RouteNotFoundError(RouteRuntimeError, LookupError):
    """The requested route id does not exist."""


class VersionConflictError(RouteConflictError):
    """The supplied version does not match the active route version."""

    def __init__(
        self, route_id: str, expected_version: int, actual_version: int
    ) -> None:
        self.route_id = route_id
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            f"route {route_id!r} is version {actual_version}, "
            f"not expected version {expected_version}"
        )


class RoutePersistenceError(RouteRuntimeError):
    """The persisted registry could not be read or atomically updated."""


@dataclass(frozen=True, slots=True)
class RouteRecord:
    """One immutable active-handler snapshot.

    The handler is intentionally excluded from equality and representation. It is
    process-local and never appears in the persisted manifest.
    """

    route_id: str
    path: str
    method: str
    project: str
    version: int
    description: str
    source_file: Path
    source: str = field(repr=False, compare=False)
    handler: Callable[[dict[str, Any]], Any] = field(repr=False, compare=False)


def normalize_method(method: str) -> str:
    """Validate and canonicalize a supported HTTP method."""

    if not isinstance(method, str) or not method or method != method.strip():
        raise RouteValidationError("method must be a non-empty string")
    normalized = method.upper()
    if normalized not in SUPPORTED_METHODS:
        supported = ", ".join(sorted(SUPPORTED_METHODS))
        raise RouteValidationError(f"unsupported method {method!r}; expected one of {supported}")
    return normalized


def normalize_path(path: str) -> str:
    """Validate and canonicalize an exact URL path."""

    if not isinstance(path, str) or not path or path != path.strip():
        raise RouteValidationError("path must be a non-empty string without outer whitespace")
    if not path.startswith("/"):
        raise RouteValidationError("path must start with '/'")
    if "?" in path or "#" in path or "\\" in path:
        raise RouteValidationError("path cannot contain a query, fragment, or backslash")
    if re.search(r"%[0-9a-fA-F]{2}", path):
        raise RouteValidationError("path must be URL-decoded, not percent-encoded")
    if any(ord(character) < 32 or ord(character) == 127 for character in path):
        raise RouteValidationError("path cannot contain control characters")
    if "//" in path:
        raise RouteValidationError("path cannot contain empty segments")

    normalized = path.rstrip("/") or "/"
    segments = normalized.split("/")[1:]
    if any(segment in {".", ".."} for segment in segments):
        raise RouteValidationError("path cannot contain '.' or '..' segments")
    return normalized


def _is_reserved(path: str) -> bool:
    return any(path == root or path.startswith(f"{root}/") for root in RESERVED_ROOTS)


def _route_id(method: str, path: str) -> str:
    raw_path = path.removeprefix("/")
    if not raw_path:
        digest = hashlib.sha256(f"{method} {path}".encode()).hexdigest()[:16]
        return f"{method.lower()}-h-root-{digest}"
    if (
        len(raw_path) <= 64
        and re.fullmatch(r"[a-z0-9_-]+", raw_path)
        and not raw_path.startswith("h-")
    ):
        return f"{method.lower()}-{raw_path}"

    slug = re.sub(r"[^a-z0-9]+", "-", raw_path.lower()).strip("-")[:36]
    slug = slug.rstrip("-") or "route"
    digest = hashlib.sha256(f"{method} {path}".encode()).hexdigest()[:16]
    return f"{method.lower()}-h-{slug}-{digest}"


def _module_name(route_id: str, version: int) -> str:
    safe_route_id = re.sub(r"[^a-zA-Z0-9_]", "_", route_id)
    return f"self_grow_generated_{safe_route_id}_v{version}"


class RouteRuntime:
    """Thread-safe, persistent registry of dynamically generated handlers."""

    def __init__(
        self,
        generated_dir: str | Path,
        loader: HandlerLoader | None = None,
    ) -> None:
        self.generated_dir = Path(generated_dir).expanduser().resolve()
        self.generated_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.generated_dir / MANIFEST_FILENAME
        if loader is None:
            # Keep the dependency lazy so callers can inject a policy-specific loader.
            from .code_loader import GeneratedCodeLoader

            loader = GeneratedCodeLoader()
        self._loader = loader
        self._lock = RLock()
        self._records: dict[tuple[str, str], RouteRecord] = {}
        self._records_by_id: dict[str, RouteRecord] = {}
        self._restore()

    @staticmethod
    def validate_route(path: str, method: str) -> tuple[str, str]:
        """Return ``(normalized_path, normalized_method)`` or raise validation error."""

        normalized_path = normalize_path(path)
        normalized_method = normalize_method(method)
        if _is_reserved(normalized_path):
            raise ReservedPathError(f"path {normalized_path!r} is reserved")
        return normalized_path, normalized_method

    @classmethod
    def route_id_for(cls, path: str, method: str) -> str:
        """Return the deterministic id a valid method/path pair will receive."""

        normalized_path, normalized_method = cls.validate_route(path, method)
        return _route_id(normalized_method, normalized_path)

    @staticmethod
    def normalize_project(project: str) -> str:
        """Validate and canonicalize a logical project identifier."""

        try:
            return normalize_project(project)
        except ValueError as exc:
            raise RouteValidationError(str(exc)) from None

    def create(
        self,
        path: str,
        method: str,
        source: str,
        description: str = "",
        project: str = DEFAULT_PROJECT,
    ) -> RouteRecord:
        """Validate, persist, and activate a new route at version one."""

        normalized_path, normalized_method = self.validate_route(path, method)
        source = self._validate_source(source)
        description = self._validate_description(description)
        project = self.normalize_project(project)
        key = (normalized_method, normalized_path)
        route_id = _route_id(normalized_method, normalized_path)

        # This early check avoids unnecessary compilation. The check in the commit
        # critical section remains authoritative for concurrent creators.
        with self._lock:
            self._ensure_create_available(key, route_id)

        candidate = self._build_record(
            route_id=route_id,
            path=normalized_path,
            method=normalized_method,
            project=project,
            version=1,
            source=source,
            description=description,
        )

        with self._lock:
            self._ensure_create_available(key, route_id)
            self._commit(candidate, source)
        return candidate

    def update(
        self,
        route_id: str,
        source: str,
        expected_version: int,
        description: str = "",
    ) -> RouteRecord:
        """Atomically replace a route if its active version matches the caller's."""

        route_id = self._validate_route_id(route_id)
        source = self._validate_source(source)
        description = self._validate_description(description)
        expected_version = self._validate_version(expected_version)

        with self._lock:
            current = self._records_by_id.get(route_id)
            if current is None:
                raise RouteNotFoundError(f"route {route_id!r} was not found")
            self._ensure_version(current, expected_version)

        candidate = self._build_record(
            route_id=current.route_id,
            path=current.path,
            method=current.method,
            project=current.project,
            version=expected_version + 1,
            source=source,
            description=description,
        )

        with self._lock:
            active = self._records_by_id.get(route_id)
            if active is None:
                raise RouteNotFoundError(f"route {route_id!r} was not found")
            self._ensure_version(active, expected_version)
            self._commit(candidate, source)
        return candidate

    def move(
        self,
        route_id: str,
        *,
        path: str,
        project: str,
        expected_version: int,
    ) -> RouteRecord:
        """Atomically move a route to a new public path and project."""

        route_id = self._validate_route_id(route_id)
        normalized_path, normalized_method = self.validate_route(path, "GET")
        project = self.normalize_project(project)
        expected_version = self._validate_version(expected_version)

        with self._lock:
            current = self._records_by_id.get(route_id)
            if current is None:
                raise RouteNotFoundError(f"route {route_id!r} was not found")
            self._ensure_version(current, expected_version)
            normalized_method = current.method
            normalized_path, _ = self.validate_route(normalized_path, normalized_method)
            if current.path == normalized_path and current.project == project:
                raise RouteValidationError("route already has the requested project and path")

            target_key = (normalized_method, normalized_path)
            target_route_id = _route_id(normalized_method, normalized_path)
            existing = self._records.get(target_key)
            if existing is not None and existing.route_id != current.route_id:
                raise RouteAlreadyExistsError(
                    f"route {existing.method} {existing.path} already exists"
                )
            existing_by_id = self._records_by_id.get(target_route_id)
            if existing_by_id is not None and existing_by_id.route_id != current.route_id:
                raise RouteAlreadyExistsError(f"route id {target_route_id!r} already exists")

        candidate = self._build_record(
            route_id=target_route_id,
            path=normalized_path,
            method=normalized_method,
            project=project,
            version=expected_version + 1,
            source=current.source,
            description=current.description,
        )

        with self._lock:
            active = self._records_by_id.get(route_id)
            if active is None:
                raise RouteNotFoundError(f"route {route_id!r} was not found")
            self._ensure_version(active, expected_version)
            target_key = (candidate.method, candidate.path)
            existing = self._records.get(target_key)
            if existing is not None and existing.route_id != active.route_id:
                raise RouteAlreadyExistsError(
                    f"route {existing.method} {existing.path} already exists"
                )
            existing_by_id = self._records_by_id.get(candidate.route_id)
            if existing_by_id is not None and existing_by_id.route_id != active.route_id:
                raise RouteAlreadyExistsError(f"route id {candidate.route_id!r} already exists")

            next_records = dict(self._records)
            next_records.pop((active.method, active.path))
            next_records[target_key] = candidate
            self._commit_records(next_records, candidate, candidate.source)
        return candidate

    def get(self, route_id: str) -> RouteRecord | None:
        """Return one active immutable record snapshot by id."""

        if not isinstance(route_id, str):
            return None
        with self._lock:
            return self._records_by_id.get(route_id)

    def list(self) -> tuple[RouteRecord, ...]:
        """Return a stable, deterministically ordered snapshot of active routes."""

        with self._lock:
            return tuple(
                sorted(
                    self._records_by_id.values(),
                    key=lambda item: (item.project, item.route_id),
                )
            )

    def resolve(self, method: str, path: str) -> RouteRecord | None:
        """Resolve an exact method/path pair without invoking its handler."""

        try:
            normalized_method = normalize_method(method)
            normalized_path = normalize_path(path)
        except RouteValidationError:
            return None
        with self._lock:
            return self._records.get((normalized_method, normalized_path))

    @staticmethod
    def _validate_source(source: str) -> str:
        if not isinstance(source, str) or not source.strip():
            raise RouteValidationError("source must be a non-empty string")
        return source

    @staticmethod
    def _validate_description(description: str) -> str:
        if not isinstance(description, str):
            raise RouteValidationError("description must be a string")
        return description

    @staticmethod
    def _validate_route_id(route_id: str) -> str:
        if not isinstance(route_id, str) or not re.fullmatch(r"[a-z0-9_-]+", route_id):
            raise RouteValidationError("route_id is invalid")
        return route_id

    @staticmethod
    def _validate_version(version: int) -> int:
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise RouteValidationError("expected_version must be a positive integer")
        return version

    def _ensure_create_available(self, key: tuple[str, str], route_id: str) -> None:
        existing = self._records.get(key)
        if existing is not None:
            raise RouteAlreadyExistsError(
                f"route {existing.method} {existing.path} already exists"
            )
        if route_id in self._records_by_id:
            raise RouteAlreadyExistsError(f"route id {route_id!r} already exists")

    @staticmethod
    def _ensure_version(record: RouteRecord, expected_version: int) -> None:
        if record.version != expected_version:
            raise VersionConflictError(record.route_id, expected_version, record.version)

    def _build_record(
        self,
        *,
        route_id: str,
        path: str,
        method: str,
        project: str,
        version: int,
        source: str,
        description: str,
    ) -> RouteRecord:
        source_file = self.generated_dir / f"{route_id}.v{version}.py"
        handler = self._loader.load(source, _module_name(route_id, version))
        if not callable(handler):
            raise RouteValidationError("generated-code loader did not return a callable")
        return RouteRecord(
            route_id=route_id,
            path=path,
            method=method,
            project=project,
            version=version,
            description=description,
            source_file=source_file,
            source=source,
            handler=handler,
        )

    def _commit(self, candidate: RouteRecord, source: str) -> None:
        key = (candidate.method, candidate.path)
        next_records = dict(self._records)
        next_records[key] = candidate
        self._commit_records(next_records, candidate, source)

    def _commit_records(
        self,
        next_records: dict[tuple[str, str], RouteRecord],
        candidate: RouteRecord,
        source: str,
    ) -> None:
        source_existed = candidate.source_file.exists()

        try:
            self._atomic_write_text(candidate.source_file, source)
            self._write_manifest(next_records.values())
        except Exception as exc:
            if not source_existed:
                try:
                    candidate.source_file.unlink(missing_ok=True)
                except OSError:
                    pass
            if isinstance(exc, RoutePersistenceError):
                raise
            raise RoutePersistenceError("could not persist dynamic route") from exc

        self._records = next_records
        self._records_by_id = {record.route_id: record for record in next_records.values()}

    def _write_manifest(self, records: Iterable[RouteRecord]) -> None:
        serialized = [self._serialize_record(record) for record in records]
        serialized.sort(key=lambda item: (item["project"], item["route_id"]))
        payload = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "routes": serialized,
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        self._atomic_write_text(self.manifest_path, text)

    @staticmethod
    def _serialize_record(record: RouteRecord) -> dict[str, str | int]:
        return {
            "route_id": record.route_id,
            "path": record.path,
            "method": record.method,
            "project": record.project,
            "version": record.version,
            "description": record.description,
            "source_file": record.source_file.name,
        }

    def _atomic_write_text(self, target: Path, content: str) -> None:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.generated_dir,
                prefix=f".{target.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, target)
        except OSError as exc:
            raise RoutePersistenceError(f"could not atomically write {target.name}") from exc
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def _restore(self) -> None:
        if not self.manifest_path.exists():
            return
        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            entries = self._validate_manifest(payload)
            restored: dict[tuple[str, str], RouteRecord] = {}
            restored_by_id: dict[str, RouteRecord] = {}
            for entry in entries:
                record = self._restore_record(entry)
                key = (record.method, record.path)
                if key in restored:
                    raise RoutePersistenceError(
                        f"manifest contains duplicate route {record.method} {record.path}"
                    )
                if record.route_id in restored_by_id:
                    raise RoutePersistenceError(
                        f"manifest contains duplicate route id {record.route_id!r}"
                    )
                restored[key] = record
                restored_by_id[record.route_id] = record
        except RoutePersistenceError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise RoutePersistenceError("could not restore routes.json") from exc

        with self._lock:
            self._records = restored
            self._records_by_id = restored_by_id

    @staticmethod
    def _validate_manifest(payload: object) -> list[object]:
        if not isinstance(payload, dict):
            raise RoutePersistenceError("routes.json must contain an object")
        if payload.get("schema_version") != MANIFEST_SCHEMA_VERSION:
            raise RoutePersistenceError("routes.json has an unsupported schema version")
        entries = payload.get("routes")
        if not isinstance(entries, list):
            raise RoutePersistenceError("routes.json routes must be a list")
        return entries

    def _restore_record(self, entry: object) -> RouteRecord:
        if not isinstance(entry, dict):
            raise RoutePersistenceError("manifest route entry must be an object")
        legacy_fields = {
            "route_id",
            "path",
            "method",
            "version",
            "description",
            "source_file",
        }
        supported_fields = legacy_fields | {"project"}
        if set(entry) != legacy_fields and set(entry) != supported_fields:
            raise RoutePersistenceError("manifest route entry has invalid fields")

        route_id = self._validate_route_id(entry["route_id"])
        path, method = self.validate_route(entry["path"], entry["method"])
        project = self.normalize_project(entry.get("project", DEFAULT_PROJECT))
        version = self._validate_version(entry["version"])
        description = self._validate_description(entry["description"])
        if route_id != _route_id(method, path):
            raise RoutePersistenceError("manifest route id does not match method and path")

        source_name = entry["source_file"]
        expected_source_name = f"{route_id}.v{version}.py"
        if not isinstance(source_name, str) or source_name != expected_source_name:
            raise RoutePersistenceError("manifest source_file is invalid")
        source_file = self.generated_dir / source_name
        if not source_file.is_file():
            raise RoutePersistenceError(f"generated source file {source_name!r} is missing")
        try:
            source = source_file.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise RoutePersistenceError(f"could not read generated source {source_name!r}") from exc

        handler = self._loader.load(source, _module_name(route_id, version))
        if not callable(handler):
            raise RoutePersistenceError("generated-code loader did not return a callable")
        return RouteRecord(
            route_id=route_id,
            path=path,
            method=method,
            project=project,
            version=version,
            description=description,
            source_file=source_file,
            source=source,
            handler=handler,
        )


__all__ = [
    "RESERVED_ROOTS",
    "SUPPORTED_METHODS",
    "ReservedPathError",
    "RouteAlreadyExistsError",
    "RouteConflictError",
    "RouteNotFoundError",
    "RoutePersistenceError",
    "RouteRecord",
    "RouteRuntime",
    "RouteRuntimeError",
    "RouteValidationError",
    "VersionConflictError",
    "normalize_method",
    "normalize_path",
]
