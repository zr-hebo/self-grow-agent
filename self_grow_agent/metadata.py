"""SQLite-backed metadata for locally managed feature requirements."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from self_grow_agent.projects import DEFAULT_PROJECT, normalize_project

RequirementStatus = Literal["draft", "implementing", "active", "failed"]
OperationKind = Literal["create", "update", "move"]
OperationStatus = Literal["accepted", "implementing", "finish", "failed"]
INTERRUPTED_MESSAGE = "service restarted before implementation completed"
OPERATION_INTERRUPTED_MESSAGE = "service restarted before operation completed"
_REQUIREMENT_COLUMNS = {
    "project": "TEXT NOT NULL DEFAULT 'default'",
    "target_route_id": "TEXT",
    "target_route_version": "INTEGER",
    "target_source_sha256": "TEXT",
}


class RequirementStoreError(Exception):
    """Base class for requirement metadata errors."""


class RequirementNotFoundError(RequirementStoreError, LookupError):
    """The requested requirement does not exist."""


class OperationNotFoundError(RequirementStoreError, LookupError):
    """The requested implementation operation does not exist."""


class RequirementBusyError(RequirementStoreError):
    """A requirement or the SQLite database is busy."""


class RequirementStorageError(RequirementStoreError):
    """The requirement database could not complete an operation."""


@dataclass(frozen=True, slots=True)
class RequirementRecord:
    """One immutable requirement snapshot."""

    id: str
    title: str
    instruction: str
    path: str
    method: str
    project: str
    route_id: str | None
    route_version: int | None
    status: RequirementStatus
    last_error: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class RequirementEvent:
    """An append-only requirement status transition."""

    id: int
    requirement_id: str
    from_status: RequirementStatus | None
    to_status: RequirementStatus
    message: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class OperationRecord:
    """One immutable snapshot of an asynchronous implementation attempt."""

    id: str
    requirement_id: str
    kind: OperationKind
    instruction: str
    path: str
    method: str
    project: str
    base_route_id: str | None
    base_route_version: int | None
    target_route_id: str | None
    target_route_version: int | None
    target_source_sha256: str | None
    status: OperationStatus
    last_error: str | None
    created_at: datetime
    updated_at: datetime


def _now() -> datetime:
    return datetime.now(UTC)


def _serialize_time(value: datetime) -> str:
    return value.isoformat(timespec="microseconds")


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _validate_text(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _validate_route_link(
    route_id: str | None, route_version: int | None
) -> tuple[str | None, int | None]:
    if (route_id is None) != (route_version is None):
        raise ValueError("route_id and route_version must be provided together")
    if route_id is None:
        return None, None
    _validate_text("route_id", route_id)
    if isinstance(route_version, bool) or not isinstance(route_version, int) or route_version < 1:
        raise ValueError("route_version must be a positive integer")
    return route_id, route_version


def _validate_source_sha256(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or value != value.lower()
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("source_sha256 must be a lowercase SHA-256 digest")
    return value


def _validate_operation_kind(value: str) -> OperationKind:
    if value not in {"create", "update", "move"}:
        raise ValueError("operation kind is invalid")
    return value  # type: ignore[return-value]


class RequirementStore:
    """Persist requirement state with short, independently connected operations."""

    def __init__(self, database_path: str | Path, busy_timeout_ms: int = 5_000) -> None:
        if (
            isinstance(busy_timeout_ms, bool)
            or not isinstance(busy_timeout_ms, int)
            or busy_timeout_ms < 1
        ):
            raise ValueError("busy_timeout_ms must be a positive integer")

        self.database_path = Path(database_path).expanduser().resolve()
        self.busy_timeout_ms = busy_timeout_ms
        try:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RequirementStorageError("could not prepare requirement database") from exc
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                self.database_path,
                timeout=self.busy_timeout_ms / 1_000,
            )
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection
        except sqlite3.Error as exc:
            error_code = getattr(exc, "sqlite_errorcode", None)
            if error_code is not None and error_code & 0xFF in {
                sqlite3.SQLITE_BUSY,
                sqlite3.SQLITE_LOCKED,
            }:
                raise RequirementBusyError("requirement storage is busy") from exc
            raise RequirementStorageError("requirement storage operation failed") from exc
        finally:
            if connection is not None:
                connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            journal_mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if journal_mode.lower() != "wal":
                raise RequirementStorageError("requirement database could not enable WAL mode")

            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS requirements (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    instruction TEXT NOT NULL,
                    path TEXT NOT NULL,
                    method TEXT NOT NULL,
                    project TEXT NOT NULL DEFAULT 'default',
                    route_id TEXT,
                    route_version INTEGER,
                    target_route_id TEXT,
                    target_route_version INTEGER,
                    target_source_sha256 TEXT,
                    status TEXT NOT NULL CHECK (
                        status IN ('draft', 'implementing', 'active', 'failed')
                    ),
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK (
                        (route_id IS NULL AND route_version IS NULL)
                        OR (
                            route_id IS NOT NULL
                            AND route_version IS NOT NULL
                            AND route_version >= 1
                        )
                    ),
                    CHECK (
                        (
                            target_route_id IS NULL
                            AND target_route_version IS NULL
                            AND target_source_sha256 IS NULL
                        )
                        OR (
                            target_route_id IS NOT NULL
                            AND target_route_version IS NOT NULL
                            AND target_route_version >= 1
                            AND target_source_sha256 IS NOT NULL
                        )
                    )
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    requirement_id TEXT NOT NULL REFERENCES requirements(id) ON DELETE CASCADE,
                    from_status TEXT CHECK (
                        from_status IS NULL
                        OR from_status IN ('draft', 'implementing', 'active', 'failed')
                    ),
                    to_status TEXT NOT NULL CHECK (
                        to_status IN ('draft', 'implementing', 'active', 'failed')
                    ),
                    message TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS operations (
                    id TEXT PRIMARY KEY,
                    requirement_id TEXT NOT NULL REFERENCES requirements(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL CHECK (kind IN ('create', 'update', 'move')),
                    instruction TEXT NOT NULL,
                    path TEXT NOT NULL,
                    method TEXT NOT NULL,
                    project TEXT NOT NULL,
                    base_route_id TEXT,
                    base_route_version INTEGER,
                    target_route_id TEXT,
                    target_route_version INTEGER,
                    target_source_sha256 TEXT,
                    status TEXT NOT NULL CHECK (
                        status IN ('accepted', 'implementing', 'finish', 'failed')
                    ),
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK (
                        (base_route_id IS NULL AND base_route_version IS NULL)
                        OR (
                            base_route_id IS NOT NULL
                            AND base_route_version IS NOT NULL
                            AND base_route_version >= 1
                        )
                    ),
                    CHECK (
                        (
                            target_route_id IS NULL
                            AND target_route_version IS NULL
                            AND target_source_sha256 IS NULL
                        )
                        OR (
                            target_route_id IS NOT NULL
                            AND target_route_version IS NOT NULL
                            AND target_route_version >= 1
                            AND target_source_sha256 IS NOT NULL
                        )
                    )
                )
                """
            )
            self._ensure_requirement_columns(connection)
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS requirements_updated_at_idx
                ON requirements(updated_at DESC, id DESC)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS requirements_project_updated_at_idx
                ON requirements(project, updated_at DESC, id DESC)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS events_requirement_id_idx
                ON events(requirement_id, id)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS operations_requirement_created_at_idx
                ON operations(requirement_id, created_at DESC, id DESC)
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS operations_one_active_requirement_idx
                ON operations(requirement_id)
                WHERE status IN ('accepted', 'implementing')
                """
            )
            connection.commit()
            connection.execute("PRAGMA optimize")

    @staticmethod
    def _ensure_requirement_columns(connection: sqlite3.Connection) -> None:
        """Upgrade databases created before current requirement metadata fields."""

        existing = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(requirements)").fetchall()
        }
        for name, declaration in _REQUIREMENT_COLUMNS.items():
            if name not in existing:
                connection.execute(
                    f"ALTER TABLE requirements ADD COLUMN {name} {declaration}"
                )

    def recover_interrupted(
        self,
        active_publications: Mapping[str, tuple[int, str]],
    ) -> None:
        """Reconcile work left in ``implementing`` during an exclusive startup.

        A matching pre-publication receipt proves that the route commit completed
        before the previous process stopped. Work without a matching receipt is
        safely returned to the retryable ``failed`` state.
        """

        timestamp = _serialize_time(_now())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM requirements WHERE status = ?",
                ("implementing",),
            ).fetchall()
            for row in rows:
                if self._publication_matches(row, active_publications):
                    self._recover_published_row(connection, row, timestamp)
                else:
                    connection.execute(
                        """
                        UPDATE requirements
                        SET status = ?, last_error = ?, target_route_id = NULL,
                            target_route_version = NULL, target_source_sha256 = NULL,
                            updated_at = ?
                        WHERE id = ? AND status = ?
                        """,
                        (
                            "failed",
                            INTERRUPTED_MESSAGE,
                            timestamp,
                            row["id"],
                            "implementing",
                        ),
                    )
                    self._append_event(
                        connection,
                        requirement_id=row["id"],
                        from_status="implementing",
                        to_status="failed",
                        message=INTERRUPTED_MESSAGE,
                        timestamp=timestamp,
                    )
            operation_rows = connection.execute(
                "SELECT * FROM operations WHERE status IN (?, ?)",
                ("accepted", "implementing"),
            ).fetchall()
            for row in operation_rows:
                publication = active_publications.get(row["target_route_id"])
                published = (
                    row["status"] == "implementing"
                    and row["target_route_id"] is not None
                    and publication
                    == (row["target_route_version"], row["target_source_sha256"])
                )
                if published:
                    connection.execute(
                        """
                        UPDATE operations
                        SET status = ?, last_error = NULL, updated_at = ?
                        WHERE id = ? AND status = ?
                        """,
                        ("finish", timestamp, row["id"], "implementing"),
                    )
                else:
                    operation_error = (
                        f"{OPERATION_INTERRUPTED_MESSAGE} "
                        f"(previous status: {row['status']})"
                    )
                    connection.execute(
                        """
                        UPDATE operations
                        SET status = ?, last_error = ?, updated_at = ?
                        WHERE id = ? AND status IN (?, ?)
                        """,
                        (
                            "failed",
                            operation_error,
                            timestamp,
                            row["id"],
                            "accepted",
                            "implementing",
                        ),
                    )
            connection.commit()

    def create(
        self,
        title: str,
        instruction: str,
        path: str,
        method: str,
        *,
        project: str = DEFAULT_PROJECT,
        route_id: str | None = None,
        route_version: int | None = None,
    ) -> RequirementRecord:
        """Create a draft requirement, optionally linked to an active route."""

        title = _validate_text("title", title)
        instruction = _validate_text("instruction", instruction)
        path = _validate_text("path", path)
        method = _validate_text("method", method)
        project = normalize_project(project)
        route_id, route_version = _validate_route_link(route_id, route_version)

        requirement_id = uuid.uuid4().hex
        timestamp = _serialize_time(_now())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO requirements (
                    id, title, instruction, path, method, project, route_id, route_version,
                    status, last_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    requirement_id,
                    title,
                    instruction,
                    path,
                    method,
                    project,
                    route_id,
                    route_version,
                    "draft",
                    None,
                    timestamp,
                    timestamp,
                ),
            )
            self._append_event(
                connection,
                requirement_id=requirement_id,
                from_status=None,
                to_status="draft",
                message="created",
                timestamp=timestamp,
            )
            connection.commit()
        return self.get(requirement_id)

    def create_operation(
        self,
        requirement_id: str,
        *,
        kind: str,
        instruction: str,
        path: str,
        method: str,
        project: str,
        base_route_id: str | None = None,
        base_route_version: int | None = None,
    ) -> OperationRecord:
        """Persist an accepted execution snapshot for one requirement."""

        requirement_id = _validate_text("requirement_id", requirement_id)
        validated_kind = _validate_operation_kind(kind)
        instruction = _validate_text("instruction", instruction)
        path = _validate_text("path", path)
        method = _validate_text("method", method)
        project = normalize_project(project)
        base_route_id, base_route_version = _validate_route_link(
            base_route_id,
            base_route_version,
        )
        operation_id = uuid.uuid4().hex
        timestamp = _serialize_time(_now())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_row(connection, requirement_id)
            self._ensure_no_active_operation(connection, requirement_id)
            self._insert_operation(
                connection,
                operation_id=operation_id,
                requirement_id=requirement_id,
                kind=validated_kind,
                instruction=instruction,
                path=path,
                method=method,
                project=project,
                base_route_id=base_route_id,
                base_route_version=base_route_version,
                timestamp=timestamp,
            )
            connection.commit()
        return self.get_operation(operation_id)

    def revise_and_create_operation(
        self,
        requirement_id: str,
        *,
        title: str,
        instruction: str,
        kind: str,
        base_route_id: str | None = None,
        base_route_version: int | None = None,
    ) -> OperationRecord:
        """Atomically revise a requirement and snapshot its next execution."""

        requirement_id = _validate_text("requirement_id", requirement_id)
        title = _validate_text("title", title)
        instruction = _validate_text("instruction", instruction)
        validated_kind = _validate_operation_kind(kind)
        base_route_id, base_route_version = _validate_route_link(
            base_route_id,
            base_route_version,
        )
        operation_id = uuid.uuid4().hex
        timestamp = _serialize_time(_now())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._require_row(connection, requirement_id)
            if current["status"] == "implementing":
                raise RequirementBusyError(
                    f"requirement {requirement_id!r} is being implemented"
                )
            self._ensure_no_active_operation(connection, requirement_id)
            from_status: RequirementStatus = current["status"]
            connection.execute(
                """
                UPDATE requirements
                SET title = ?, instruction = ?, route_id = ?, route_version = ?,
                    status = ?, last_error = NULL, target_route_id = NULL,
                    target_route_version = NULL, target_source_sha256 = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    title,
                    instruction,
                    base_route_id,
                    base_route_version,
                    "draft",
                    timestamp,
                    requirement_id,
                ),
            )
            if from_status != "draft":
                self._append_event(
                    connection,
                    requirement_id=requirement_id,
                    from_status=from_status,
                    to_status="draft",
                    message="content updated",
                    timestamp=timestamp,
                )
            self._insert_operation(
                connection,
                operation_id=operation_id,
                requirement_id=requirement_id,
                kind=validated_kind,
                instruction=instruction,
                path=current["path"],
                method=current["method"],
                project=current["project"],
                base_route_id=base_route_id,
                base_route_version=base_route_version,
                timestamp=timestamp,
            )
            connection.commit()
        return self.get_operation(operation_id)

    def get_operation(self, operation_id: str) -> OperationRecord:
        operation_id = _validate_text("operation_id", operation_id)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM operations WHERE id = ?",
                (operation_id,),
            ).fetchone()
        if row is None:
            raise OperationNotFoundError(f"operation {operation_id!r} was not found")
        return self._operation_from_row(row)

    def list_operations(
        self,
        requirement_id: str | None = None,
    ) -> tuple[OperationRecord, ...]:
        if requirement_id is not None:
            requirement_id = _validate_text("requirement_id", requirement_id)
        with self._connection() as connection:
            if requirement_id is None:
                rows = connection.execute(
                    "SELECT * FROM operations ORDER BY created_at DESC, id DESC"
                ).fetchall()
            else:
                self._require_row(connection, requirement_id)
                rows = connection.execute(
                    "SELECT * FROM operations WHERE requirement_id = ? "
                    "ORDER BY created_at DESC, id DESC",
                    (requirement_id,),
                ).fetchall()
        return tuple(self._operation_from_row(row) for row in rows)

    def begin_operation(self, operation_id: str) -> OperationRecord:
        """Atomically claim an accepted operation and its requirement."""

        operation_id = _validate_text("operation_id", operation_id)
        timestamp = _serialize_time(_now())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            operation = self._require_operation_row(connection, operation_id)
            if operation["status"] != "accepted":
                raise RequirementBusyError(
                    f"operation {operation_id!r} cannot begin from status "
                    f"{operation['status']!r}"
                )
            requirement = self._require_row(connection, operation["requirement_id"])
            from_status: RequirementStatus = requirement["status"]
            if from_status not in {"draft", "failed"}:
                raise RequirementBusyError(
                    f"requirement {operation['requirement_id']!r} cannot begin "
                    f"from status {from_status!r}"
                )
            connection.execute(
                "UPDATE operations SET status = ?, updated_at = ? "
                "WHERE id = ? AND status = ?",
                ("implementing", timestamp, operation_id, "accepted"),
            )
            connection.execute(
                """
                UPDATE requirements
                SET status = ?, last_error = NULL, target_route_id = NULL,
                    target_route_version = NULL, target_source_sha256 = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                ("implementing", timestamp, operation["requirement_id"]),
            )
            self._append_event(
                connection,
                requirement_id=operation["requirement_id"],
                from_status=from_status,
                to_status="implementing",
                message="implementation started",
                timestamp=timestamp,
            )
            connection.commit()
        return self.get_operation(operation_id)

    def prepare_operation_publication(
        self,
        operation_id: str,
        *,
        route_id: str,
        route_version: int,
        source_sha256: str,
    ) -> OperationRecord:
        operation_id = _validate_text("operation_id", operation_id)
        route_id, route_version = _validate_route_link(route_id, route_version)
        assert route_id is not None and route_version is not None
        source_sha256 = _validate_source_sha256(source_sha256)
        timestamp = _serialize_time(_now())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            operation = self._require_operation_row(connection, operation_id)
            if operation["status"] != "implementing":
                raise RequirementBusyError(
                    f"operation {operation_id!r} is not implementing"
                )
            connection.execute(
                """
                UPDATE operations
                SET target_route_id = ?, target_route_version = ?,
                    target_source_sha256 = ?, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    route_id,
                    route_version,
                    source_sha256,
                    timestamp,
                    operation_id,
                    "implementing",
                ),
            )
            connection.commit()
        return self.get_operation(operation_id)

    def complete_operation(
        self,
        operation_id: str,
        *,
        route_id: str,
        route_version: int,
    ) -> OperationRecord:
        operation_id = _validate_text("operation_id", operation_id)
        route_id, route_version = _validate_route_link(route_id, route_version)
        assert route_id is not None and route_version is not None
        timestamp = _serialize_time(_now())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            operation = self._require_operation_row(connection, operation_id)
            if operation["status"] == "finish":
                connection.commit()
                return self._operation_from_row(operation)
            if operation["status"] != "implementing":
                raise RequirementBusyError(
                    f"operation {operation_id!r} is not implementing"
                )
            connection.execute(
                """
                UPDATE operations
                SET status = ?, target_route_id = ?, target_route_version = ?,
                    last_error = NULL, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    "finish",
                    route_id,
                    route_version,
                    timestamp,
                    operation_id,
                    "implementing",
                ),
            )
            connection.commit()
        return self.get_operation(operation_id)

    def fail_operation(self, operation_id: str, error: str) -> OperationRecord:
        operation_id = _validate_text("operation_id", operation_id)
        error = _validate_text("error", error)
        timestamp = _serialize_time(_now())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            operation = self._require_operation_row(connection, operation_id)
            if operation["status"] not in {"accepted", "implementing"}:
                raise RequirementBusyError(
                    f"operation {operation_id!r} cannot fail from status "
                    f"{operation['status']!r}"
                )
            connection.execute(
                """
                UPDATE operations
                SET status = ?, last_error = ?, updated_at = ?
                WHERE id = ? AND status IN (?, ?)
                """,
                ("failed", error, timestamp, operation_id, "accepted", "implementing"),
            )
            connection.commit()
        return self.get_operation(operation_id)
    def list(self, project: str | None = None) -> tuple[RequirementRecord, ...]:
        """Return requirements with the most recently updated first."""

        if project is not None:
            project = normalize_project(project)
        with self._connection() as connection:
            if project is None:
                rows = connection.execute(
                    "SELECT * FROM requirements ORDER BY updated_at DESC, id DESC"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM requirements WHERE project = ? "
                    "ORDER BY updated_at DESC, id DESC",
                    (project,),
                ).fetchall()
        return tuple(self._record_from_row(row) for row in rows)

    def get(self, requirement_id: str) -> RequirementRecord:
        """Return one requirement or raise ``RequirementNotFoundError``."""

        requirement_id = _validate_text("requirement_id", requirement_id)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM requirements WHERE id = ?",
                (requirement_id,),
            ).fetchone()
        if row is None:
            raise RequirementNotFoundError(f"requirement {requirement_id!r} was not found")
        return self._record_from_row(row)

    def update_content(
        self,
        requirement_id: str,
        *,
        title: str,
        instruction: str,
    ) -> RequirementRecord:
        """Update editable content and return completed work to draft state."""

        requirement_id = _validate_text("requirement_id", requirement_id)
        title = _validate_text("title", title)
        instruction = _validate_text("instruction", instruction)
        timestamp = _serialize_time(_now())

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._require_row(connection, requirement_id)
            from_status: RequirementStatus = current["status"]
            if from_status == "implementing":
                raise RequirementBusyError(
                    f"requirement {requirement_id!r} is being implemented"
                )
            to_status: RequirementStatus = (
                "draft" if from_status in {"active", "failed"} else from_status
            )
            connection.execute(
                """
                UPDATE requirements
                SET title = ?, instruction = ?, status = ?, last_error = NULL,
                    target_route_id = NULL, target_route_version = NULL,
                    target_source_sha256 = NULL, updated_at = ?
                WHERE id = ?
                """,
                (title, instruction, to_status, timestamp, requirement_id),
            )
            if to_status != from_status:
                self._append_event(
                    connection,
                    requirement_id=requirement_id,
                    from_status=from_status,
                    to_status=to_status,
                    message="content updated",
                    timestamp=timestamp,
                )
            connection.commit()
        return self.get(requirement_id)

    def begin_implementation(self, requirement_id: str) -> RequirementRecord:
        """Atomically claim a draft or failed requirement for implementation."""

        requirement_id = _validate_text("requirement_id", requirement_id)
        timestamp = _serialize_time(_now())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._require_row(connection, requirement_id)
            from_status: RequirementStatus = current["status"]
            if from_status not in {"draft", "failed"}:
                raise RequirementBusyError(
                    f"requirement {requirement_id!r} cannot begin from status {from_status!r}"
                )
            cursor = connection.execute(
                """
                UPDATE requirements
                SET status = ?, last_error = NULL, target_route_id = NULL,
                    target_route_version = NULL, target_source_sha256 = NULL,
                    updated_at = ?
                WHERE id = ? AND status IN (?, ?)
                """,
                ("implementing", timestamp, requirement_id, "draft", "failed"),
            )
            if cursor.rowcount != 1:
                raise RequirementBusyError(
                    f"requirement {requirement_id!r} was claimed concurrently"
                )
            self._append_event(
                connection,
                requirement_id=requirement_id,
                from_status=from_status,
                to_status="implementing",
                message="implementation started",
                timestamp=timestamp,
            )
            connection.commit()
        return self.get(requirement_id)

    def prepare_publication(
        self,
        requirement_id: str,
        *,
        route_id: str,
        route_version: int,
        source_sha256: str,
    ) -> RequirementRecord:
        """Persist the exact route candidate before it can become active."""

        requirement_id = _validate_text("requirement_id", requirement_id)
        validated_route_id, validated_route_version = _validate_route_link(
            route_id, route_version
        )
        assert validated_route_id is not None
        assert validated_route_version is not None
        validated_source_sha256 = _validate_source_sha256(source_sha256)
        timestamp = _serialize_time(_now())

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._require_row(connection, requirement_id)
            self._ensure_implementing(requirement_id, current["status"])
            current_receipt = (
                current["target_route_id"],
                current["target_route_version"],
                current["target_source_sha256"],
            )
            expected_receipt = (
                validated_route_id,
                validated_route_version,
                validated_source_sha256,
            )
            if any(value is not None for value in current_receipt):
                if current_receipt != expected_receipt:
                    raise RequirementBusyError(
                        f"requirement {requirement_id!r} already has a publication receipt"
                    )
                connection.commit()
                return self._record_from_row(current)

            connection.execute(
                """
                UPDATE requirements
                SET target_route_id = ?, target_route_version = ?,
                    target_source_sha256 = ?, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    validated_route_id,
                    validated_route_version,
                    validated_source_sha256,
                    timestamp,
                    requirement_id,
                    "implementing",
                ),
            )
            connection.commit()
        return self.get(requirement_id)

    def complete_implementation(
        self,
        requirement_id: str,
        *,
        route_id: str,
        route_version: int,
        source_sha256: str | None = None,
    ) -> RequirementRecord:
        """Mark the claimed requirement active at a concrete route version."""

        requirement_id = _validate_text("requirement_id", requirement_id)
        validated_route_id, validated_route_version = _validate_route_link(
            route_id, route_version
        )
        assert validated_route_id is not None
        assert validated_route_version is not None
        validated_source_sha256 = (
            _validate_source_sha256(source_sha256)
            if source_sha256 is not None
            else None
        )
        timestamp = _serialize_time(_now())

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._require_row(connection, requirement_id)
            if current["status"] == "active":
                if (
                    current["route_id"] == validated_route_id
                    and current["route_version"] == validated_route_version
                ):
                    connection.commit()
                    return self._record_from_row(current)
                raise RequirementBusyError(
                    f"requirement {requirement_id!r} is already active at another route version"
                )
            self._ensure_implementing(requirement_id, current["status"])
            receipt = (
                current["target_route_id"],
                current["target_route_version"],
                current["target_source_sha256"],
            )
            if validated_source_sha256 is not None and receipt != (
                validated_route_id,
                validated_route_version,
                validated_source_sha256,
            ):
                raise RequirementBusyError(
                    f"requirement {requirement_id!r} publication receipt does not match"
                )
            connection.execute(
                """
                UPDATE requirements
                SET route_id = ?, route_version = ?, status = ?, last_error = NULL,
                    target_route_id = NULL, target_route_version = NULL,
                    target_source_sha256 = NULL, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    validated_route_id,
                    validated_route_version,
                    "active",
                    timestamp,
                    requirement_id,
                    "implementing",
                ),
            )
            self._append_event(
                connection,
                requirement_id=requirement_id,
                from_status="implementing",
                to_status="active",
                message="implementation completed",
                timestamp=timestamp,
            )
            connection.commit()
        return self.get(requirement_id)

    def fail_implementation(
        self,
        requirement_id: str,
        error: str,
    ) -> RequirementRecord:
        """Record a caller-supplied safe failure message for a claimed requirement."""

        requirement_id = _validate_text("requirement_id", requirement_id)
        error = _validate_text("error", error)
        timestamp = _serialize_time(_now())

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._require_row(connection, requirement_id)
            self._ensure_implementing(requirement_id, current["status"])
            connection.execute(
                """
                UPDATE requirements
                SET status = ?, last_error = ?, target_route_id = NULL,
                    target_route_version = NULL, target_source_sha256 = NULL,
                    updated_at = ?
                WHERE id = ? AND status = ?
                """,
                ("failed", error, timestamp, requirement_id, "implementing"),
            )
            self._append_event(
                connection,
                requirement_id=requirement_id,
                from_status="implementing",
                to_status="failed",
                message=error,
                timestamp=timestamp,
            )
            connection.commit()
        return self.get(requirement_id)

    def reconcile_publication(
        self,
        requirement_id: str,
        active_publications: Mapping[str, tuple[int, str]],
    ) -> RequirementRecord:
        """Complete one matching published receipt without regenerating code."""

        requirement_id = _validate_text("requirement_id", requirement_id)
        timestamp = _serialize_time(_now())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._require_row(connection, requirement_id)
            if current["status"] == "implementing" and self._publication_matches(
                current, active_publications
            ):
                self._recover_published_row(connection, current, timestamp)
            connection.commit()
        return self.get(requirement_id)

    def rebase_route(
        self,
        requirement_id: str,
        *,
        route_id: str,
        route_version: int,
    ) -> RequirementRecord:
        """Explicitly move a linked requirement onto the active route version."""

        requirement_id = _validate_text("requirement_id", requirement_id)
        validated_route_id, validated_route_version = _validate_route_link(
            route_id, route_version
        )
        assert validated_route_id is not None
        assert validated_route_version is not None
        timestamp = _serialize_time(_now())

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._require_row(connection, requirement_id)
            from_status: RequirementStatus = current["status"]
            if from_status == "implementing":
                raise RequirementBusyError(
                    f"requirement {requirement_id!r} is being implemented"
                )
            if current["route_id"] != validated_route_id:
                raise RequirementBusyError(
                    f"requirement {requirement_id!r} is not linked to route "
                    f"{validated_route_id!r}"
                )
            connection.execute(
                """
                UPDATE requirements
                SET route_version = ?, status = ?, last_error = NULL,
                    target_route_id = NULL, target_route_version = NULL,
                    target_source_sha256 = NULL, updated_at = ?
                WHERE id = ?
                """,
                (validated_route_version, "draft", timestamp, requirement_id),
            )
            self._append_event(
                connection,
                requirement_id=requirement_id,
                from_status=from_status,
                to_status="draft",
                message=f"rebased to route version {validated_route_version}",
                timestamp=timestamp,
            )
            connection.commit()
        return self.get(requirement_id)

    def ensure_route_can_move(
        self,
        route_id: str,
        *,
        exclude_requirement_id: str | None = None,
    ) -> None:
        """Reject a route move while one of its linked requirements is active."""

        route_id = _validate_text("route_id", route_id)
        if exclude_requirement_id is not None:
            exclude_requirement_id = _validate_text(
                "exclude_requirement_id",
                exclude_requirement_id,
            )
        with self._connection() as connection:
            if exclude_requirement_id is None:
                row = connection.execute(
                    "SELECT id FROM requirements "
                    "WHERE route_id = ? AND status = ? LIMIT 1",
                    (route_id, "implementing"),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT id FROM requirements "
                    "WHERE route_id = ? AND status = ? AND id != ? LIMIT 1",
                    (route_id, "implementing", exclude_requirement_id),
                ).fetchone()
        if row is not None:
            raise RequirementBusyError(
                f"route {route_id!r} has a requirement being implemented"
            )

    def move_route_links(
        self,
        previous_route_id: str,
        *,
        route_id: str,
        route_version: int,
        path: str,
        project: str,
        exclude_requirement_id: str | None = None,
    ) -> None:
        """Move all inactive linked requirements to a route's new identity."""

        previous_route_id = _validate_text("previous_route_id", previous_route_id)
        route_id, route_version = _validate_route_link(route_id, route_version)
        assert route_id is not None
        assert route_version is not None
        path = _validate_text("path", path)
        project = normalize_project(project)
        if exclude_requirement_id is not None:
            exclude_requirement_id = _validate_text(
                "exclude_requirement_id",
                exclude_requirement_id,
            )
        timestamp = _serialize_time(_now())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if exclude_requirement_id is None:
                busy = connection.execute(
                    "SELECT id FROM requirements "
                    "WHERE route_id = ? AND status = ? LIMIT 1",
                    (previous_route_id, "implementing"),
                ).fetchone()
            else:
                busy = connection.execute(
                    "SELECT id FROM requirements "
                    "WHERE route_id = ? AND status = ? AND id != ? LIMIT 1",
                    (previous_route_id, "implementing", exclude_requirement_id),
                ).fetchone()
            if busy is not None:
                raise RequirementBusyError(
                    f"route {previous_route_id!r} has a requirement being implemented"
                )
            exclusion_sql = "" if exclude_requirement_id is None else " AND id != ?"
            parameters: tuple[object, ...] = (
                route_id,
                route_version,
                path,
                project,
                timestamp,
                previous_route_id,
            )
            if exclude_requirement_id is not None:
                parameters += (exclude_requirement_id,)
            connection.execute(
                f"""
                UPDATE requirements
                SET route_id = ?, route_version = ?, path = ?, project = ?,
                    target_route_id = NULL, target_route_version = NULL,
                    target_source_sha256 = NULL, updated_at = ?
                WHERE route_id = ?{exclusion_sql}
                """,
                parameters,
            )
            connection.commit()

    def list_events(self, requirement_id: str) -> tuple[RequirementEvent, ...]:
        """Return all status events for a requirement in append order."""

        requirement_id = _validate_text("requirement_id", requirement_id)
        with self._connection() as connection:
            self._require_row(connection, requirement_id)
            rows = connection.execute(
                "SELECT * FROM events WHERE requirement_id = ? ORDER BY id",
                (requirement_id,),
            ).fetchall()
        return tuple(self._event_from_row(row) for row in rows)

    @staticmethod
    def _publication_matches(
        row: sqlite3.Row,
        active_publications: Mapping[str, tuple[int, str]],
    ) -> bool:
        target_route_id = row["target_route_id"]
        target_route_version = row["target_route_version"]
        target_source_sha256 = row["target_source_sha256"]
        if (
            target_route_id is None
            or target_route_version is None
            or target_source_sha256 is None
        ):
            return False
        return active_publications.get(target_route_id) == (
            target_route_version,
            target_source_sha256,
        )

    @classmethod
    def _recover_published_row(
        cls,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        timestamp: str,
    ) -> None:
        connection.execute(
            """
            UPDATE requirements
            SET route_id = target_route_id, route_version = target_route_version,
                status = ?, last_error = NULL, target_route_id = NULL,
                target_route_version = NULL, target_source_sha256 = NULL,
                updated_at = ?
            WHERE id = ? AND status = ?
            """,
            ("active", timestamp, row["id"], "implementing"),
        )
        cls._append_event(
            connection,
            requirement_id=row["id"],
            from_status="implementing",
            to_status="active",
            message="published implementation recovered",
            timestamp=timestamp,
        )

    @staticmethod
    def _require_row(
        connection: sqlite3.Connection, requirement_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM requirements WHERE id = ?",
            (requirement_id,),
        ).fetchone()
        if row is None:
            raise RequirementNotFoundError(f"requirement {requirement_id!r} was not found")
        return row

    @staticmethod
    def _ensure_implementing(requirement_id: str, status: str) -> None:
        if status != "implementing":
            raise RequirementBusyError(
                f"requirement {requirement_id!r} is not being implemented"
            )

    @staticmethod
    def _ensure_no_active_operation(
        connection: sqlite3.Connection,
        requirement_id: str,
    ) -> None:
        active = connection.execute(
            "SELECT id FROM operations WHERE requirement_id = ? "
            "AND status IN (?, ?) LIMIT 1",
            (requirement_id, "accepted", "implementing"),
        ).fetchone()
        if active is not None:
            raise RequirementBusyError(
                f"requirement {requirement_id!r} already has an active operation"
            )

    @staticmethod
    def _insert_operation(
        connection: sqlite3.Connection,
        *,
        operation_id: str,
        requirement_id: str,
        kind: OperationKind,
        instruction: str,
        path: str,
        method: str,
        project: str,
        base_route_id: str | None,
        base_route_version: int | None,
        timestamp: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO operations (
                id, requirement_id, kind, instruction, path, method, project,
                base_route_id, base_route_version, status, last_error,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                operation_id,
                requirement_id,
                kind,
                instruction,
                path,
                method,
                project,
                base_route_id,
                base_route_version,
                "accepted",
                None,
                timestamp,
                timestamp,
            ),
        )

    @staticmethod
    def _require_operation_row(
        connection: sqlite3.Connection,
        operation_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM operations WHERE id = ?",
            (operation_id,),
        ).fetchone()
        if row is None:
            raise OperationNotFoundError(f"operation {operation_id!r} was not found")
        return row

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        *,
        requirement_id: str,
        from_status: RequirementStatus | None,
        to_status: RequirementStatus,
        message: str | None,
        timestamp: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO events (
                requirement_id, from_status, to_status, message, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (requirement_id, from_status, to_status, message, timestamp),
        )

    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> RequirementRecord:
        return RequirementRecord(
            id=row["id"],
            title=row["title"],
            instruction=row["instruction"],
            path=row["path"],
            method=row["method"],
            project=row["project"],
            route_id=row["route_id"],
            route_version=row["route_version"],
            status=row["status"],
            last_error=row["last_error"],
            created_at=_parse_time(row["created_at"]),
            updated_at=_parse_time(row["updated_at"]),
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> RequirementEvent:
        return RequirementEvent(
            id=row["id"],
            requirement_id=row["requirement_id"],
            from_status=row["from_status"],
            to_status=row["to_status"],
            message=row["message"],
            created_at=_parse_time(row["created_at"]),
        )

    @staticmethod
    def _operation_from_row(row: sqlite3.Row) -> OperationRecord:
        return OperationRecord(
            id=row["id"],
            requirement_id=row["requirement_id"],
            kind=row["kind"],
            instruction=row["instruction"],
            path=row["path"],
            method=row["method"],
            project=row["project"],
            base_route_id=row["base_route_id"],
            base_route_version=row["base_route_version"],
            target_route_id=row["target_route_id"],
            target_route_version=row["target_route_version"],
            target_source_sha256=row["target_source_sha256"],
            status=row["status"],
            last_error=row["last_error"],
            created_at=_parse_time(row["created_at"]),
            updated_at=_parse_time(row["updated_at"]),
        )
