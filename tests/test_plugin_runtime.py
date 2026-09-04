from __future__ import annotations

import json
from pathlib import Path

import pytest

from self_grow_agent.plugin_models import GeneratedPlugin, PluginFile, PluginPolicy
from self_grow_agent.plugin_runtime import PluginPublicationError, PluginPublisher
from self_grow_agent.plugin_test_runner import PluginTestRunner
from self_grow_agent.plugin_validator import PluginValidator
from self_grow_agent.plugin_workspace import PluginWorkspaceManager
from self_grow_agent.runtime import RoutePersistenceError, RouteRuntime, VersionConflictError


def _plugin(value: str, *, passing: bool = True) -> GeneratedPlugin:
    return GeneratedPlugin(
        description=f"returns {value}",
        files=(
            PluginFile(
                path="handler.py",
                content=f"def handle(request):\n    return {{'value': {value!r}}}\n",
            ),
            PluginFile(
                path="tests/test_handler.py",
                content=(
                    "from handler import handle\n\n"
                    f"def test_handler():\n    assert handle({{}}) == {{'value': {value!r}}}\n"
                    if passing
                    else "def test_handler():\n    assert False\n"
                ),
            ),
        ),
    )


def _publisher(tmp_path: Path) -> tuple[PluginPublisher, RouteRuntime]:
    generated = tmp_path / "generated"
    artifacts = generated / "plugins"
    runtime = RouteRuntime(generated, plugin_artifact_root=artifacts)
    workspace = PluginWorkspaceManager(
        workspace_root=tmp_path / "external-workspaces",
        artifact_root=artifacts,
    )
    publisher = PluginPublisher(
        runtime=runtime,
        workspace_manager=workspace,
        validator=PluginValidator(PluginPolicy()),
        test_runner=PluginTestRunner(timeout_seconds=5),
        keep_failed_workspaces=True,
    )
    return publisher, runtime


def test_create_and_update_publish_immutable_versions_and_manifest_v2(
    tmp_path: Path,
) -> None:
    publisher, runtime = _publisher(tmp_path)

    first = publisher.create(
        operation_id="1" * 32,
        path="/demo/hello",
        method="POST",
        project="demo",
        plugin=_plugin("v1"),
    )
    second = publisher.update(
        operation_id="2" * 32,
        route_id=first.route_id,
        expected_version=1,
        plugin=_plugin("v2"),
    )

    assert first.execution_mode == "plugin"
    assert first.version == 1
    assert second.version == 2
    assert first.artifact_path is not None and first.artifact_path.is_dir()
    assert second.artifact_path is not None and second.artifact_path.is_dir()
    assert first.artifact_path != second.artifact_path
    assert "v1" in (first.artifact_path / "handler.py").read_text()
    assert "v2" in (second.artifact_path / "handler.py").read_text()
    assert first.artifact_digest and len(first.artifact_digest) == 64

    manifest = json.loads(runtime.manifest_path.read_text())
    assert manifest["schema_version"] == 2
    assert manifest["routes"][0]["execution_mode"] == "plugin"
    assert manifest["routes"][0]["source_file"] is None
    assert manifest["routes"][0]["artifact_path"].endswith("/v2")

    restored = RouteRuntime(
        runtime.generated_dir,
        plugin_artifact_root=runtime.plugin_artifact_root,
    )
    recovered = restored.get(second.route_id)
    assert recovered is not None
    assert recovered.execution_mode == "plugin"
    assert recovered.artifact_digest == second.artifact_digest


def test_failed_plugin_tests_leave_active_version_unchanged(tmp_path: Path) -> None:
    publisher, runtime = _publisher(tmp_path)
    current = publisher.create(
        operation_id="3" * 32,
        path="/demo/hello",
        method="POST",
        project="demo",
        plugin=_plugin("good"),
    )

    with pytest.raises(PluginPublicationError, match="plugin tests failed"):
        publisher.update(
            operation_id="4" * 32,
            route_id=current.route_id,
            expected_version=1,
            plugin=_plugin("bad", passing=False),
        )

    active = runtime.get(current.route_id)
    assert active is not None and active.version == 1
    assert not (runtime.plugin_artifact_root / "demo" / current.route_id / "v2").exists()


def test_publish_update_uses_compare_and_swap(tmp_path: Path) -> None:
    publisher, runtime = _publisher(tmp_path)
    current = publisher.create(
        operation_id="5" * 32,
        path="/demo/hello",
        method="POST",
        project="demo",
        plugin=_plugin("v1"),
    )

    with pytest.raises(VersionConflictError):
        publisher.update(
            operation_id="6" * 32,
            route_id=current.route_id,
            expected_version=2,
            plugin=_plugin("v3"),
        )

    assert runtime.get(current.route_id) == current


def test_tampered_or_missing_plugin_artifact_is_rejected_on_restore(
    tmp_path: Path,
) -> None:
    publisher, runtime = _publisher(tmp_path)
    record = publisher.create(
        operation_id="7" * 32,
        path="/demo/hello",
        method="POST",
        project="demo",
        plugin=_plugin("safe"),
    )
    assert record.artifact_path is not None
    (record.artifact_path / "handler.py").write_text("def handle(request): return 'tampered'\n")

    with pytest.raises(RoutePersistenceError, match="artifact"):
        RouteRuntime(
            runtime.generated_dir,
            plugin_artifact_root=runtime.plugin_artifact_root,
        )


def test_explicit_rollback_republishes_old_content_as_new_version(tmp_path: Path) -> None:
    publisher, runtime = _publisher(tmp_path)
    first = publisher.create(
        operation_id="8" * 32,
        path="/demo/hello",
        method="POST",
        project="demo",
        plugin=_plugin("v1"),
    )
    second = publisher.update(
        operation_id="9" * 32,
        route_id=first.route_id,
        expected_version=1,
        plugin=_plugin("v2"),
    )

    rolled_back = publisher.rollback(
        operation_id="a" * 32,
        route_id=first.route_id,
        target_version=1,
        expected_version=second.version,
    )

    assert rolled_back.version == 3
    assert rolled_back.artifact_path is not None
    assert "v1" in (rolled_back.artifact_path / "handler.py").read_text()


def test_schema_v1_restricted_manifest_remains_compatible(tmp_path: Path) -> None:
    generated = tmp_path / "generated"
    generated.mkdir()
    (generated / "get-hello.v1.py").write_text(
        "def handle(request):\n    return {'hello': 'world'}\n"
    )
    (generated / "routes.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "routes": [
                    {
                        "route_id": "get-hello",
                        "path": "/hello",
                        "method": "GET",
                        "project": "default",
                        "version": 1,
                        "description": "legacy",
                        "source_file": "get-hello.v1.py",
                    }
                ],
            }
        )
    )

    restored = RouteRuntime(generated)

    record = restored.get("get-hello")
    assert record is not None
    assert record.execution_mode == "restricted"
    assert record.handler is not None
