"""Validate, test, and atomically publish immutable plugin versions."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from self_grow_agent.plugin_models import GeneratedPlugin, PluginFile
from self_grow_agent.plugin_test_runner import PluginTestRunner
from self_grow_agent.plugin_validator import PluginValidationError, PluginValidator
from self_grow_agent.plugin_workspace import PluginWorkspaceError, PluginWorkspaceManager

if TYPE_CHECKING:
    from self_grow_agent.runtime import RouteRecord, RouteRuntime

_ARTIFACT_SCHEMA_VERSION = 1
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_logger = logging.getLogger("uvicorn.error")


class PluginPublicationError(RuntimeError):
    """A plugin failed a safe pre-publication or persistence gate."""


class PluginPublisher:
    """Publish complete plugin versions without mutating an active artifact."""

    def __init__(
        self,
        *,
        runtime: RouteRuntime,
        workspace_manager: PluginWorkspaceManager,
        validator: PluginValidator,
        test_runner: PluginTestRunner,
        keep_failed_workspaces: bool,
    ) -> None:
        if runtime.plugin_artifact_root != workspace_manager.artifact_root:
            raise ValueError("runtime and workspace manager artifact roots must match")
        if not isinstance(validator, PluginValidator):
            raise TypeError("validator must be a PluginValidator")
        if not isinstance(test_runner, PluginTestRunner):
            raise TypeError("test_runner must be a PluginTestRunner")
        if not isinstance(keep_failed_workspaces, bool):
            raise TypeError("keep_failed_workspaces must be a boolean")
        self._runtime = runtime
        self._workspace = workspace_manager
        self._validator = validator
        self._test_runner = test_runner
        self._keep_failed_workspaces = keep_failed_workspaces

    def create(
        self,
        *,
        operation_id: str,
        path: str,
        method: str,
        project: str,
        plugin: GeneratedPlugin,
        before_activate: Callable[[str, int, str], None] | None = None,
    ) -> RouteRecord:
        """Build and atomically activate version one of a new plugin route."""

        route_id = self._runtime.route_id_for(path, method)
        normalized_project = self._runtime.normalize_project(project)
        return self._publish(
            operation_id=operation_id,
            route_id=route_id,
            version=1,
            project=normalized_project,
            plugin=plugin,
            before_activate=before_activate,
            activate=lambda artifact, digest: self._runtime.create_plugin(
                path,
                method,
                artifact_path=artifact,
                artifact_digest=digest,
                description=plugin.description,
                project=normalized_project,
            ),
        )

    def update(
        self,
        *,
        operation_id: str,
        route_id: str,
        expected_version: int,
        plugin: GeneratedPlugin,
        before_activate: Callable[[str, int, str], None] | None = None,
    ) -> RouteRecord:
        """Build and activate the next version if the active version still matches."""

        current = self._runtime.get(route_id)
        if current is None:
            from self_grow_agent.runtime import RouteNotFoundError

            raise RouteNotFoundError(f"route {route_id!r} was not found")
        return self._publish(
            operation_id=operation_id,
            route_id=current.route_id,
            version=expected_version + 1,
            project=current.project,
            plugin=plugin,
            before_activate=before_activate,
            activate=lambda artifact, digest: self._runtime.update_plugin(
                route_id,
                artifact_path=artifact,
                artifact_digest=digest,
                expected_version=expected_version,
                description=plugin.description,
            ),
        )

    def move(
        self,
        *,
        operation_id: str,
        route_id: str,
        path: str,
        project: str,
        expected_version: int,
        plugin: GeneratedPlugin,
        before_activate: Callable[[str, int, str], None] | None = None,
    ) -> RouteRecord:
        """Publish a regenerated plugin under a route's target identity."""

        current = self._runtime.get(route_id)
        if current is None:
            from self_grow_agent.runtime import RouteNotFoundError

            raise RouteNotFoundError(f"route {route_id!r} was not found")
        normalized_project = self._runtime.normalize_project(project)
        target_route_id = self._runtime.route_id_for(path, current.method)
        return self._publish(
            operation_id=operation_id,
            route_id=target_route_id,
            version=expected_version + 1,
            project=normalized_project,
            plugin=plugin,
            before_activate=before_activate,
            activate=lambda artifact, digest: self._runtime.move_plugin(
                route_id,
                path=path,
                project=normalized_project,
                artifact_path=artifact,
                artifact_digest=digest,
                expected_version=expected_version,
                description=plugin.description,
            ),
        )

    def rollback(
        self,
        *,
        operation_id: str,
        route_id: str,
        target_version: int,
        expected_version: int,
    ) -> RouteRecord:
        """Republish a previous immutable bundle as a new monotonic version."""

        current = self._runtime.get(route_id)
        if current is None:
            from self_grow_agent.runtime import RouteNotFoundError

            raise RouteNotFoundError(f"route {route_id!r} was not found")
        if current.execution_mode != "plugin":
            raise PluginPublicationError("route is not a plugin route")
        target = (
            self._runtime.plugin_artifact_root
            / current.project
            / current.route_id
            / f"v{target_version}"
        )
        plugin = load_published_plugin(target)
        return self.update(
            operation_id=operation_id,
            route_id=route_id,
            expected_version=expected_version,
            plugin=plugin,
            before_activate=None,
        )

    def _publish(
        self,
        *,
        operation_id: str,
        route_id: str,
        version: int,
        project: str,
        plugin: GeneratedPlugin,
        before_activate: Callable[[str, int, str], None] | None,
        activate: Any,
    ) -> RouteRecord:
        workspace_created = False
        published_path: Path | None = None
        activated = False
        started_at = time.monotonic()
        try:
            _logger.info(
                "plugin_publication validation_started operation_id=%s route_id=%s "
                "version=%s project=%s file_count=%s dependency_count=%s",
                operation_id,
                route_id,
                version,
                project,
                len(plugin.files),
                len(plugin.dependencies),
            )
            self._validator.validate(plugin)
            _logger.info(
                "plugin_publication validation_completed operation_id=%s route_id=%s "
                "version=%s elapsed_seconds=%.3f",
                operation_id,
                route_id,
                version,
                time.monotonic() - started_at,
            )
            self._workspace.create(operation_id)
            workspace_created = True
            source_dir = self._workspace.materialize(operation_id, plugin)
            _logger.info(
                "plugin_publication workspace_materialized operation_id=%s route_id=%s "
                "version=%s file_count=%s",
                operation_id,
                route_id,
                version,
                len(plugin.files),
            )
            test_result = self._test_runner.run(source_dir, plugin.dependencies)
            _logger.info(
                "plugin_publication tests_completed operation_id=%s route_id=%s "
                "version=%s passed=%s failure_category=%s exit_code=%s "
                "output_bytes=%s elapsed_seconds=%.3f",
                operation_id,
                route_id,
                version,
                test_result.passed,
                test_result.failure_category,
                test_result.exit_code,
                test_result.output_bytes,
                test_result.elapsed_seconds,
            )
            if not test_result.passed:
                category = test_result.failure_category or "unknown"
                raise PluginPublicationError(f"plugin tests failed: {category}")
            published_path, digest = _publish_artifact(
                artifact_root=self._runtime.plugin_artifact_root,
                project=project,
                route_id=route_id,
                version=version,
                plugin=plugin,
            )
            _logger.info(
                "plugin_publication artifact_published operation_id=%s route_id=%s "
                "version=%s digest=%s",
                operation_id,
                route_id,
                version,
                digest,
            )
            if before_activate is not None:
                before_activate(route_id, version, digest)
            record = activate(published_path, digest)
            activated = True
            _write_current_pointer(
                published_path.parent,
                version=record.version,
                digest=digest,
            )
            self._workspace.cleanup(operation_id)
            _logger.info(
                "plugin_publication activated operation_id=%s route_id=%s version=%s "
                "digest=%s elapsed_seconds=%.3f",
                operation_id,
                record.route_id,
                record.version,
                digest,
                time.monotonic() - started_at,
            )
            return record
        except (PluginPublicationError, PluginValidationError) as exc:
            _logger.warning(
                "plugin_publication failed operation_id=%s route_id=%s version=%s "
                "exception_type=%s elapsed_seconds=%.3f",
                operation_id,
                route_id,
                version,
                type(exc).__name__,
                time.monotonic() - started_at,
            )
            raise
        except PluginWorkspaceError as exc:
            raise PluginPublicationError(str(exc)) from None
        finally:
            if published_path is not None and not activated:
                shutil.rmtree(published_path, ignore_errors=True)
            if workspace_created and not activated and not self._keep_failed_workspaces:
                try:
                    self._workspace.cleanup(operation_id)
                except PluginWorkspaceError:
                    pass


def _publish_artifact(
    *,
    artifact_root: Path,
    project: str,
    route_id: str,
    version: int,
    plugin: GeneratedPlugin,
) -> tuple[Path, str]:
    route_root = artifact_root / project / route_id
    route_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    route_root.chmod(0o700)
    target = route_root / f"v{version}"
    staging = Path(tempfile.mkdtemp(prefix=f".v{version}-", dir=route_root))
    staging.chmod(0o700)
    try:
        file_entries: list[dict[str, str | int]] = []
        for plugin_file in plugin.files:
            destination = staging.joinpath(*plugin_file.path.split("/"))
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            destination.parent.chmod(0o700)
            encoded = plugin_file.content.encode("utf-8")
            with destination.open("xb") as output:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            destination.chmod(0o600)
            file_entries.append(
                {
                    "path": plugin_file.path,
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                    "bytes": len(encoded),
                }
            )
        descriptor = {
            "schema_version": _ARTIFACT_SCHEMA_VERSION,
            "description": plugin.description,
            "entrypoint": plugin.entrypoint,
            "dependencies": list(plugin.dependencies),
            "files": file_entries,
        }
        digest = _descriptor_digest(descriptor)
        metadata = dict(descriptor, digest=digest)
        _atomic_write_json(staging / "plugin.json", metadata, staging)
        os.replace(staging, target)
        staging = Path()
        return target, digest
    except FileExistsError:
        raise PluginPublicationError("plugin version already exists") from None
    except OSError:
        raise PluginPublicationError("plugin artifact could not be published") from None
    finally:
        if staging != Path():
            shutil.rmtree(staging, ignore_errors=True)


def verify_plugin_artifact(artifact_path: str | Path, expected_digest: str) -> dict[str, Any]:
    """Verify an immutable artifact and return its parsed metadata."""

    artifact = Path(artifact_path).expanduser().resolve()
    if not _DIGEST.fullmatch(expected_digest):
        raise ValueError("plugin artifact digest is invalid")
    if not artifact.is_dir() or Path(artifact_path).is_symlink():
        raise ValueError("plugin artifact directory is missing or invalid")
    metadata_path = artifact / "plugin.json"
    if not metadata_path.is_file() or metadata_path.is_symlink():
        raise ValueError("plugin artifact metadata is missing or invalid")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("plugin artifact metadata is invalid") from None
    if not isinstance(metadata, dict) or set(metadata) != {
        "schema_version",
        "description",
        "entrypoint",
        "dependencies",
        "files",
        "digest",
    }:
        raise ValueError("plugin artifact metadata is invalid")
    if metadata["schema_version"] != _ARTIFACT_SCHEMA_VERSION:
        raise ValueError("plugin artifact schema is unsupported")
    if metadata["digest"] != expected_digest:
        raise ValueError("plugin artifact digest does not match")
    files = metadata["files"]
    if not isinstance(files, list) or not files:
        raise ValueError("plugin artifact file manifest is invalid")
    seen: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256", "bytes"}:
            raise ValueError("plugin artifact file manifest is invalid")
        path = entry["path"]
        try:
            PluginFile(path=path, content="")
        except Exception:
            raise ValueError("plugin artifact file path is invalid") from None
        if path in seen:
            raise ValueError("plugin artifact contains duplicate files")
        seen.add(path)
        candidate = artifact.joinpath(*path.split("/"))
        if not candidate.is_file() or candidate.is_symlink():
            raise ValueError("plugin artifact file is missing or invalid")
        try:
            content = candidate.read_bytes()
        except OSError:
            raise ValueError("plugin artifact file could not be read") from None
        if (
            entry["bytes"] != len(content)
            or entry["sha256"] != hashlib.sha256(content).hexdigest()
        ):
            raise ValueError("plugin artifact file digest does not match")
    descriptor = {key: value for key, value in metadata.items() if key != "digest"}
    if _descriptor_digest(descriptor) != expected_digest:
        raise ValueError("plugin artifact digest does not match")
    return metadata


def load_published_plugin(artifact_path: str | Path) -> GeneratedPlugin:
    """Load one verified bundle for monotonic rollback publication."""

    artifact = Path(artifact_path).expanduser().resolve()
    try:
        metadata = json.loads((artifact / "plugin.json").read_text(encoding="utf-8"))
        digest = metadata["digest"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
        raise PluginPublicationError("plugin rollback artifact is invalid") from None
    try:
        verified = verify_plugin_artifact(artifact, digest)
        files = tuple(
            PluginFile(
                path=entry["path"],
                content=artifact.joinpath(*entry["path"].split("/")).read_text(
                    encoding="utf-8"
                ),
            )
            for entry in verified["files"]
        )
        return GeneratedPlugin(
            description=verified["description"],
            entrypoint=verified["entrypoint"],
            dependencies=tuple(verified["dependencies"]),
            files=files,
        )
    except (ValueError, OSError, UnicodeError, TypeError):
        raise PluginPublicationError("plugin rollback artifact is invalid") from None


def _descriptor_digest(descriptor: dict[str, Any]) -> str:
    encoded = json.dumps(
        descriptor,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write_current_pointer(route_root: Path, *, version: int, digest: str) -> None:
    _atomic_write_json(
        route_root / "current.json",
        {"version": version, "digest": digest},
        route_root,
    )


def _atomic_write_json(target: Path, payload: object, parent: Path) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(text)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        temporary_path.chmod(0o600)
        os.replace(temporary_path, target)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


__all__ = [
    "PluginPublicationError",
    "PluginPublisher",
    "load_published_plugin",
    "verify_plugin_artifact",
]
