from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from self_grow_agent.plugin_models import GeneratedPlugin, PluginFile
from self_grow_agent.plugin_workspace import (
    PluginWorkspaceError,
    PluginWorkspaceManager,
)

OPERATION_ID = "a" * 32


def _plugin() -> GeneratedPlugin:
    return GeneratedPlugin(
        description="Example plugin",
        dependencies=("pymysql==1.1.1",),
        files=(
            PluginFile(
                path="handler.py",
                content="def handle(request):\n    return {'ok': True}\n",
            ),
            PluginFile(path="package/__init__.py", content=""),
            PluginFile(
                path="tests/test_handler.py",
                content="def test_handler():\n    assert True\n",
            ),
        ),
    )


def _manager(tmp_path: Path) -> PluginWorkspaceManager:
    return PluginWorkspaceManager(
        workspace_root=tmp_path / "external-workspaces",
        artifact_root=tmp_path / "generated" / "plugins",
    )


def test_creates_private_operation_workspace_and_materializes_bundle(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)

    workspace = manager.create(OPERATION_ID)
    source_dir = manager.materialize(OPERATION_ID, _plugin())

    assert workspace == tmp_path / "external-workspaces" / OPERATION_ID
    assert source_dir == workspace / "source"
    if os.name == "posix":
        assert stat.S_IMODE(workspace.stat().st_mode) == stat.S_IRWXU
        assert stat.S_IMODE((source_dir / "handler.py").stat().st_mode) == (
            stat.S_IRUSR | stat.S_IWUSR
        )
    assert (source_dir / "handler.py").read_text(encoding="utf-8").startswith(
        "def handle"
    )
    assert (source_dir / "package" / "__init__.py").read_text(encoding="utf-8") == ""
    metadata = json.loads((workspace / "plugin.json").read_text(encoding="utf-8"))
    assert metadata == {
        "dependencies": ["pymysql==1.1.1"],
        "description": "Example plugin",
        "entrypoint": "handler:handle",
        "files": [
            "handler.py",
            "package/__init__.py",
            "tests/test_handler.py",
        ],
    }


def test_refuses_to_reuse_existing_operation_workspace(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.create(OPERATION_ID)

    with pytest.raises(PluginWorkspaceError, match="already exists"):
        manager.create(OPERATION_ID)


@pytest.mark.parametrize(
    "operation_id",
    ["", "a" * 31, "A" * 32, "g" * 32, "../" + "a" * 29],
)
def test_rejects_invalid_operation_id(tmp_path: Path, operation_id: str) -> None:
    manager = _manager(tmp_path)

    with pytest.raises(ValueError, match="operation_id"):
        manager.create(operation_id)


def test_rejects_workspace_and_artifact_root_overlap(tmp_path: Path) -> None:
    root = tmp_path / "runtime"

    with pytest.raises(ValueError, match="must not overlap"):
        PluginWorkspaceManager(
            workspace_root=root,
            artifact_root=root / "artifacts",
        )


@pytest.mark.skipif(os.name != "posix", reason="symbolic link behavior is POSIX-specific")
def test_materialization_refuses_symbolic_link_parent(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    workspace = manager.create(OPERATION_ID)
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "source").mkdir()
    (workspace / "source" / "tests").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PluginWorkspaceError, match="symbolic link"):
        manager.materialize(OPERATION_ID, _plugin())

    assert list(outside.iterdir()) == []


def test_cleanup_removes_only_validated_operation_directory(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    workspace = manager.create(OPERATION_ID)
    sibling = manager.workspace_root / "keep-me"
    sibling.mkdir()

    assert manager.cleanup(OPERATION_ID) is True
    assert not workspace.exists()
    assert sibling.is_dir()
    assert manager.cleanup(OPERATION_ID) is False

    with pytest.raises(ValueError, match="operation_id"):
        manager.cleanup("../external-workspaces")
    assert manager.workspace_root.is_dir()


def test_materialize_requires_created_empty_source_directory(tmp_path: Path) -> None:
    manager = _manager(tmp_path)

    with pytest.raises(PluginWorkspaceError, match="does not exist"):
        manager.materialize(OPERATION_ID, _plugin())

    manager.create(OPERATION_ID)
    manager.materialize(OPERATION_ID, _plugin())
    with pytest.raises(PluginWorkspaceError, match="already materialized"):
        manager.materialize(OPERATION_ID, _plugin())
