"""Operation-scoped external workspaces for generated plugin bundles."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from self_grow_agent.plugin_models import GeneratedPlugin

_OPERATION_ID = re.compile(r"[0-9a-f]{32}\Z")


class PluginWorkspaceError(RuntimeError):
    """A plugin workspace could not be created or materialized safely."""


class PluginWorkspaceManager:
    """Create private workspaces without trusting generated filesystem paths."""

    def __init__(self, *, workspace_root: str | Path, artifact_root: str | Path) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.artifact_root = Path(artifact_root).expanduser().resolve()
        if _paths_overlap(self.workspace_root, self.artifact_root):
            raise ValueError("plugin workspace and artifact roots must not overlap")

    def create(self, operation_id: str) -> Path:
        """Create one new private workspace and return its absolute path."""

        operation_id = _validate_operation_id(operation_id)
        try:
            self.workspace_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.workspace_root.chmod(0o700)
            workspace = self.workspace_root / operation_id
            workspace.mkdir(mode=0o700, exist_ok=False)
            workspace.chmod(0o700)
        except FileExistsError:
            raise PluginWorkspaceError("plugin operation workspace already exists") from None
        except OSError:
            raise PluginWorkspaceError("plugin operation workspace could not be created") from None
        return workspace

    def materialize(self, operation_id: str, plugin: GeneratedPlugin) -> Path:
        """Write a parsed bundle into a fresh source directory atomically."""

        operation_id = _validate_operation_id(operation_id)
        if not isinstance(plugin, GeneratedPlugin):
            raise TypeError("plugin must be a GeneratedPlugin")
        workspace = self.workspace_root / operation_id
        if not workspace.is_dir() or workspace.is_symlink():
            raise PluginWorkspaceError("plugin operation workspace does not exist")
        source_dir = workspace / "source"
        metadata_path = workspace / "plugin.json"
        if source_dir.exists() or source_dir.is_symlink() or metadata_path.exists():
            if _contains_symbolic_link(source_dir):
                raise PluginWorkspaceError("plugin workspace contains a symbolic link")
            raise PluginWorkspaceError("plugin operation workspace is already materialized")

        staging_dir: Path | None = None
        try:
            staging_dir = Path(
                tempfile.mkdtemp(prefix=".source-", dir=workspace)
            )
            staging_dir.chmod(0o700)
            for file in plugin.files:
                target = staging_dir.joinpath(*file.path.split("/"))
                _create_private_parents(staging_dir, target.parent)
                with target.open("x", encoding="utf-8") as output:
                    output.write(file.content)
                    output.flush()
                    os.fsync(output.fileno())
                target.chmod(0o600)
            os.replace(staging_dir, source_dir)
            staging_dir = None
            _atomic_write_json(
                metadata_path,
                {
                    "dependencies": list(plugin.dependencies),
                    "description": plugin.description,
                    "entrypoint": plugin.entrypoint,
                    "files": [file.path for file in plugin.files],
                },
            )
        except PluginWorkspaceError:
            raise
        except OSError:
            raise PluginWorkspaceError("plugin bundle could not be materialized") from None
        finally:
            if staging_dir is not None:
                shutil.rmtree(staging_dir, ignore_errors=True)
        return source_dir

    def cleanup(self, operation_id: str) -> bool:
        """Remove exactly one validated operation workspace if it exists."""

        operation_id = _validate_operation_id(operation_id)
        workspace = self.workspace_root / operation_id
        if not workspace.exists() and not workspace.is_symlink():
            return False
        try:
            if workspace.is_symlink() or not workspace.is_dir():
                raise PluginWorkspaceError("plugin operation workspace is invalid")
            shutil.rmtree(workspace)
        except PluginWorkspaceError:
            raise
        except OSError:
            raise PluginWorkspaceError("plugin operation workspace could not be removed") from None
        return True


def _validate_operation_id(operation_id: str) -> str:
    if not isinstance(operation_id, str) or _OPERATION_ID.fullmatch(operation_id) is None:
        raise ValueError("operation_id must be 32 lowercase hexadecimal characters")
    return operation_id


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _contains_symbolic_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    if not path.is_dir():
        return False
    try:
        return any(child.is_symlink() for child in path.rglob("*"))
    except OSError:
        return True


def _create_private_parents(root: Path, parent: Path) -> None:
    relative = parent.relative_to(root)
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise PluginWorkspaceError("plugin workspace contains a symbolic link")
        current.mkdir(mode=0o700, exist_ok=True)
        current.chmod(0o700)


def _atomic_write_json(target: Path, value: object) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        temporary_path.chmod(0o600)
        os.replace(temporary_path, target)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


__all__ = ["PluginWorkspaceError", "PluginWorkspaceManager"]
