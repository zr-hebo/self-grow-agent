from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import pytest

from self_grow_agent.runtime import (
    ReservedPathError,
    RouteAlreadyExistsError,
    RoutePersistenceError,
    RouteRuntime,
    RouteValidationError,
    VersionConflictError,
)


class FakeLoader:
    """Small deterministic loader that keeps runtime tests independent of AST policy."""

    def load(self, source: str, module_name: str) -> Callable[[dict[str, Any]], Any]:
        del module_name
        if source == "INVALID":
            raise ValueError("generated source is invalid")

        message = source

        def handle(request: dict[str, Any]) -> dict[str, Any]:
            return {"message": message, "request": request}

        return handle


@pytest.fixture
def runtime(tmp_path: Path) -> RouteRuntime:
    return RouteRuntime(tmp_path / "generated", loader=FakeLoader())


def test_create_activates_and_persists_route(runtime: RouteRuntime) -> None:
    record = runtime.create(
        path="/hello/",
        method="get",
        source="hello v1",
        description="A greeting",
    )

    assert record.route_id == "get-hello"
    assert record.path == "/hello"
    assert record.method == "GET"
    assert record.version == 1
    assert record.description == "A greeting"
    assert record.source_file.read_text(encoding="utf-8") == "hello v1"
    assert runtime.resolve("GET", "/hello") is record
    assert record.handler({"query": {"name": "Ada"}})["message"] == "hello v1"

    manifest = json.loads((record.source_file.parent / "routes.json").read_text())
    assert manifest["schema_version"] == 1
    assert manifest["routes"] == [
        {
            "description": "A greeting",
            "method": "GET",
            "path": "/hello",
            "route_id": "get-hello",
            "source_file": "get-hello.v1.py",
            "version": 1,
        }
    ]


def test_long_simple_path_uses_bounded_hashed_route_id(runtime: RouteRuntime) -> None:
    path = "/" + ("a" * 255)

    record = runtime.create(path, "GET", "long route")

    assert record.path == path
    assert len(record.route_id) <= 64
    assert record.route_id.startswith("get-")
    assert record.source_file.is_file()
    assert runtime.resolve("GET", path) is record


def test_hashed_route_id_namespace_cannot_collide_with_simple_path(
    runtime: RouteRuntime,
) -> None:
    nested = runtime.create("/a/b", "GET", "nested")
    lookalike_path = "/" + nested.route_id.removeprefix("get-")

    lookalike = runtime.create(lookalike_path, "GET", "lookalike")

    assert nested.route_id != lookalike.route_id
    assert runtime.resolve("GET", "/a/b") is nested
    assert runtime.resolve("GET", lookalike_path) is lookalike


def test_root_route_id_does_not_collide_with_literal_root_path(
    runtime: RouteRuntime,
) -> None:
    root = runtime.create("/", "GET", "root")
    literal_root = runtime.create("/root", "GET", "literal root")

    assert root.route_id != literal_root.route_id
    assert runtime.resolve("GET", "/") is root
    assert runtime.resolve("GET", "/root") is literal_root


def test_duplicate_create_leaves_existing_route_unchanged(runtime: RouteRuntime) -> None:
    original = runtime.create("/hello", "GET", "hello v1")

    with pytest.raises(RouteAlreadyExistsError):
        runtime.create("/hello/", "get", "replacement")

    assert runtime.resolve("GET", "/hello") is original
    assert runtime.list() == (original,)


def test_update_uses_compare_and_swap_and_increments_version(runtime: RouteRuntime) -> None:
    old_record = runtime.create("/hello", "GET", "hello v1")

    with pytest.raises(VersionConflictError) as exc_info:
        runtime.update("get-hello", "hello stale", expected_version=2)

    assert exc_info.value.expected_version == 2
    assert exc_info.value.actual_version == 1

    new_record = runtime.update(
        "get-hello",
        "hello v2",
        expected_version=1,
        description="Updated greeting",
    )

    assert new_record.version == 2
    assert new_record.description == "Updated greeting"
    assert new_record.source_file.name == "get-hello.v2.py"
    assert runtime.resolve("GET", "/hello") is new_record
    assert old_record.handler({})["message"] == "hello v1"
    assert new_record.handler({})["message"] == "hello v2"

    with pytest.raises(VersionConflictError):
        runtime.update("get-hello", "hello stale", expected_version=1)


def test_concurrent_updates_allow_only_one_expected_version_winner(
    runtime: RouteRuntime,
) -> None:
    runtime.create("/hello", "GET", "hello v1")

    def update(source: str) -> str:
        try:
            runtime.update("get-hello", source, expected_version=1)
        except VersionConflictError:
            return "conflict"
        return "updated"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(update, ("hello v2-a", "hello v2-b")))

    assert sorted(results) == ["conflict", "updated"]
    active = runtime.get("get-hello")
    assert active is not None
    assert active.version == 2
    assert active.handler({})["message"] in {"hello v2-a", "hello v2-b"}


def test_failed_load_rolls_back_files_manifest_and_active_record(runtime: RouteRuntime) -> None:
    original = runtime.create("/hello", "GET", "hello v1")
    manifest_path = original.source_file.parent / "routes.json"
    manifest_before = manifest_path.read_bytes()

    with pytest.raises(ValueError, match="generated source is invalid"):
        runtime.update("get-hello", "INVALID", expected_version=1)

    assert runtime.resolve("GET", "/hello") is original
    assert manifest_path.read_bytes() == manifest_before
    assert not (original.source_file.parent / "get-hello.v2.py").exists()


def test_failed_manifest_write_rolls_back_new_source_and_active_record(
    runtime: RouteRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = runtime.create("/hello", "GET", "hello v1")
    manifest_before = runtime.manifest_path.read_bytes()

    def fail_manifest_write(records: object) -> None:
        del records
        raise OSError("disk unavailable")

    monkeypatch.setattr(runtime, "_write_manifest", fail_manifest_write)

    with pytest.raises(RoutePersistenceError, match="persist dynamic route"):
        runtime.update("get-hello", "hello v2", expected_version=1)

    assert runtime.resolve("GET", "/hello") is original
    assert runtime.manifest_path.read_bytes() == manifest_before
    assert not (runtime.generated_dir / "get-hello.v2.py").exists()


def test_resolve_requires_exact_normalized_method_and_path(runtime: RouteRuntime) -> None:
    get_record = runtime.create("/things", "GET", "get things")
    post_record = runtime.create("/things", "POST", "post things")

    assert runtime.resolve("get", "/things/") is get_record
    assert runtime.resolve("POST", "/things") is post_record
    assert runtime.resolve("PUT", "/things") is None
    assert runtime.resolve("GET", "/things/42") is None
    assert runtime.resolve("GET", "/thing") is None


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/manage",
        "/api/v1/manage/routes",
        "/healthz",
        "/healthz/details",
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
        "/openapi.json",
    ],
)
def test_reserved_paths_cannot_be_created(runtime: RouteRuntime, path: str) -> None:
    with pytest.raises(ReservedPathError):
        runtime.create(path, "GET", "not allowed")


@pytest.mark.parametrize(
    "path",
    [
        "",
        "hello",
        " /hello",
        "/hello?name=Ada",
        "/hello#fragment",
        "/hello%20world",
        "/a%2Fb",
        "/a//b",
        "/a/../b",
    ],
)
def test_invalid_paths_are_rejected(runtime: RouteRuntime, path: str) -> None:
    with pytest.raises(RouteValidationError):
        runtime.create(path, "GET", "not allowed")


def test_startup_recovers_routes_and_handlers_from_manifest(tmp_path: Path) -> None:
    generated_dir = tmp_path / "generated"
    first_runtime = RouteRuntime(generated_dir, loader=FakeLoader())
    created = first_runtime.create("/hello", "GET", "hello v1", "Greeting")
    updated = first_runtime.update(
        created.route_id,
        "hello v2",
        expected_version=created.version,
        description="Better greeting",
    )
    first_runtime.create("/submit", "POST", "submitted")

    recovered_runtime = RouteRuntime(generated_dir, loader=FakeLoader())

    recovered = recovered_runtime.get("get-hello")
    assert recovered is not None
    assert recovered.path == updated.path
    assert recovered.version == 2
    assert recovered.description == "Better greeting"
    assert recovered.handler({})["message"] == "hello v2"
    assert [record.route_id for record in recovered_runtime.list()] == [
        "get-hello",
        "post-submit",
    ]
