from __future__ import annotations

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from self_grow_agent.metadata import (
    INTERRUPTED_MESSAGE,
    RequirementBusyError,
    RequirementNotFoundError,
    RequirementStore,
)

SOURCE_SHA256 = "a" * 64


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "agent.db"


@pytest.fixture
def store(database_path: Path) -> RequirementStore:
    return RequirementStore(database_path)


def test_records_persist_across_reopened_store_and_database_uses_wal(
    database_path: Path,
) -> None:
    first_store = RequirementStore(database_path)
    created = first_store.create(
        "Improve greeting",
        "Include the caller's name",
        "/hello",
        "POST",
        route_id="post-hello",
        route_version=3,
    )

    reopened = RequirementStore(database_path)

    assert reopened.get(created.id) == created
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        indexes = {
            row[1] for row in connection.execute("PRAGMA index_list(requirements)").fetchall()
        }
    assert "requirements_updated_at_idx" in indexes


def test_list_orders_requirements_by_latest_update(store: RequirementStore) -> None:
    first = store.create("First", "Build first", "/first", "GET")
    time.sleep(0.002)
    second = store.create("Second", "Build second", "/second", "GET")

    assert [record.id for record in store.list()] == [second.id, first.id]

    time.sleep(0.002)
    updated_first = store.update_content(
        first.id,
        title="First updated",
        instruction="Build first differently",
    )

    assert updated_first.updated_at > second.updated_at
    assert [record.id for record in store.list()] == [first.id, second.id]


def test_project_is_persisted_and_can_filter_requirements(store: RequirementStore) -> None:
    orders = store.create("Orders", "Build orders", "/orders", "GET", project="Orders")
    billing = store.create("Billing", "Build billing", "/billing", "GET", project="billing")

    assert orders.project == "orders"
    assert billing.project == "billing"
    assert [record.id for record in store.list(project="orders")] == [orders.id]


def test_successful_status_flow_records_append_only_events(store: RequirementStore) -> None:
    created = store.create("Greeting", "Say hello", "/hello", "GET")

    implementing = store.begin_implementation(created.id)
    store.prepare_publication(
        created.id,
        route_id="get-hello",
        route_version=1,
        source_sha256=SOURCE_SHA256,
    )
    active = store.complete_implementation(
        created.id,
        route_id="get-hello",
        route_version=1,
    )
    edited = store.update_content(
        created.id,
        title="Friendly greeting",
        instruction="Say hello by name",
    )

    assert implementing.status == "implementing"
    assert active.status == "active"
    assert active.route_id == "get-hello"
    assert active.route_version == 1
    assert edited.status == "draft"
    assert edited.route_id == "get-hello"
    assert edited.route_version == 1
    assert [(event.from_status, event.to_status) for event in store.list_events(created.id)] == [
        (None, "draft"),
        ("draft", "implementing"),
        ("implementing", "active"),
        ("active", "draft"),
    ]


def test_failure_can_be_retried_and_editing_clears_failure(store: RequirementStore) -> None:
    requirement = store.create("Greeting", "Say hello", "/hello", "GET")
    store.begin_implementation(requirement.id)

    failed = store.fail_implementation(requirement.id, "Generated handler did not validate")

    assert failed.status == "failed"
    assert failed.last_error == "Generated handler did not validate"

    retried = store.begin_implementation(requirement.id)
    assert retried.status == "implementing"
    assert retried.last_error is None
    store.fail_implementation(requirement.id, "Provider unavailable")

    edited = store.update_content(
        requirement.id,
        title="Greeting v2",
        instruction="Use a static greeting",
    )
    assert edited.status == "draft"
    assert edited.last_error is None


def test_implementing_requirement_rejects_content_edits_and_duplicate_begin(
    store: RequirementStore,
) -> None:
    requirement = store.create("Greeting", "Say hello", "/hello", "GET")
    store.begin_implementation(requirement.id)

    with pytest.raises(RequirementBusyError, match="cannot begin"):
        store.begin_implementation(requirement.id)
    with pytest.raises(RequirementBusyError, match="being implemented"):
        store.update_content(
            requirement.id,
            title="Changed",
            instruction="Changed while running",
        )

    assert store.get(requirement.id).status == "implementing"
    assert [event.to_status for event in store.list_events(requirement.id)] == [
        "draft",
        "implementing",
    ]


def test_concurrent_begin_has_exactly_one_winner(store: RequirementStore) -> None:
    requirement = store.create("Greeting", "Say hello", "/hello", "GET")
    barrier = Barrier(2)

    def claim() -> str:
        barrier.wait()
        try:
            store.begin_implementation(requirement.id)
        except RequirementBusyError:
            return "busy"
        return "claimed"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: claim(), range(2)))

    assert sorted(results) == ["busy", "claimed"]
    assert store.get(requirement.id).status == "implementing"
    assert [event.to_status for event in store.list_events(requirement.id)] == [
        "draft",
        "implementing",
    ]


def test_opening_second_store_does_not_recover_live_implementation(
    database_path: Path,
) -> None:
    first_store = RequirementStore(database_path)
    requirement = first_store.create("Greeting", "Say hello", "/hello", "GET")
    first_store.begin_implementation(requirement.id)
    first_store.prepare_publication(
        requirement.id,
        route_id="get-hello",
        route_version=1,
        source_sha256=SOURCE_SHA256,
    )

    second_store = RequirementStore(database_path)

    assert second_store.get(requirement.id).status == "implementing"
    completed = first_store.complete_implementation(
        requirement.id,
        route_id="get-hello",
        route_version=1,
    )
    assert completed.status == "active"
    assert second_store.get(requirement.id).status == "active"


def test_explicit_recovery_without_publication_receipt_fails_once(
    database_path: Path,
) -> None:
    store = RequirementStore(database_path)
    requirement = store.create("Greeting", "Say hello", "/hello", "GET")
    store.begin_implementation(requirement.id)

    store.recover_interrupted({})

    recovered = store.get(requirement.id)
    assert recovered.status == "failed"
    assert recovered.last_error == INTERRUPTED_MESSAGE
    assert [event.to_status for event in store.list_events(requirement.id)] == [
        "draft",
        "implementing",
        "failed",
    ]
    assert store.list_events(requirement.id)[-1].message == INTERRUPTED_MESSAGE

    store.recover_interrupted({})

    assert [event.to_status for event in store.list_events(requirement.id)] == [
        "draft",
        "implementing",
        "failed",
    ]


def test_explicit_recovery_completes_matching_published_receipt(
    database_path: Path,
) -> None:
    store = RequirementStore(database_path)
    requirement = store.create("Greeting", "Say hello", "/hello", "GET")
    store.begin_implementation(requirement.id)
    store.prepare_publication(
        requirement.id,
        route_id="get-hello",
        route_version=2,
        source_sha256=SOURCE_SHA256,
    )

    store.recover_interrupted({"get-hello": (2, SOURCE_SHA256)})

    recovered = store.get(requirement.id)
    assert recovered.status == "active"
    assert recovered.route_id == "get-hello"
    assert recovered.route_version == 2
    assert recovered.last_error is None
    assert [event.to_status for event in store.list_events(requirement.id)] == [
        "draft",
        "implementing",
        "active",
    ]

    store.recover_interrupted({"get-hello": (2, SOURCE_SHA256)})

    assert [event.to_status for event in store.list_events(requirement.id)] == [
        "draft",
        "implementing",
        "active",
    ]


def test_sql_injection_shaped_text_is_stored_as_plain_data(store: RequirementStore) -> None:
    injection = "x'); DROP TABLE requirements; --"

    record = store.create(injection, injection, f"/{injection}", injection)

    persisted = store.get(record.id)
    assert persisted.title == injection
    assert persisted.instruction == injection
    assert persisted.path == f"/{injection}"
    assert persisted.method == injection
    assert len(store.list()) == 1

    second = store.create("Still here", "No table was dropped", "/safe", "GET")
    assert store.get(second.id).title == "Still here"


@pytest.mark.parametrize(
    ("route_id", "route_version"),
    [("get-hello", None), (None, 1), ("get-hello", 0), ("get-hello", True)],
)
def test_create_requires_a_complete_valid_route_link(
    store: RequirementStore,
    route_id: str | None,
    route_version: int | None,
) -> None:
    with pytest.raises(ValueError):
        store.create(
            "Greeting",
            "Say hello",
            "/hello",
            "GET",
            route_id=route_id,
            route_version=route_version,
        )


def test_missing_requirement_raises_not_found(store: RequirementStore) -> None:
    with pytest.raises(RequirementNotFoundError):
        store.get("missing")
    with pytest.raises(RequirementNotFoundError):
        store.list_events("missing")


def test_moving_route_links_updates_inactive_requirements(store: RequirementStore) -> None:
    requirement = store.create(
        "Rebuild replication",
        "Return a plan",
        "/rebuild_replication",
        "POST",
        route_id="post-rebuild_replication",
        route_version=1,
    )

    store.move_route_links(
        "post-rebuild_replication",
        route_id="post-h-binlog-server-rebuild",
        route_version=2,
        path="/binlog-server/rebuild_replication",
        project="binlog-server",
    )

    moved = store.get(requirement.id)
    assert moved.route_id == "post-h-binlog-server-rebuild"
    assert moved.route_version == 2
    assert moved.path == "/binlog-server/rebuild_replication"
    assert moved.project == "binlog-server"


def test_each_requirement_execution_has_a_distinct_persisted_operation(
    store: RequirementStore,
) -> None:
    requirement = store.create(
        "Greeting",
        "Say hello",
        "/default/hello",
        "GET",
        route_id="get-default-hello",
        route_version=3,
    )

    first = store.create_operation(
        requirement.id,
        kind="update",
        instruction="Say hello v2",
        path=requirement.path,
        method=requirement.method,
        project=requirement.project,
        base_route_id=requirement.route_id,
        base_route_version=3,
    )
    store.begin_operation(first.id)
    store.prepare_operation_publication(
        first.id,
        route_id="get-default-hello",
        route_version=4,
        source_sha256=SOURCE_SHA256,
    )
    store.complete_operation(
        first.id,
        route_id="get-default-hello",
        route_version=4,
    )
    second = store.create_operation(
        requirement.id,
        kind="update",
        instruction="Say hello v3",
        path=requirement.path,
        method=requirement.method,
        project=requirement.project,
        base_route_id=requirement.route_id,
        base_route_version=4,
    )

    assert first.id != requirement.id
    assert second.id not in {requirement.id, first.id}
    assert store.get_operation(first.id).status == "finish"
    assert store.get_operation(second.id).status == "accepted"
    assert store.get_operation(second.id).base_route_version == 4
    assert [item.id for item in store.list_operations(requirement.id)] == [
        second.id,
        first.id,
    ]


def test_revision_and_operation_creation_atomically_capture_current_route_version(
    store: RequirementStore,
) -> None:
    requirement = store.create(
        "Greeting v1",
        "Say hello v1",
        "/default/hello",
        "GET",
        route_id="get-default-hello",
        route_version=3,
    )
    store.begin_implementation(requirement.id)
    store.complete_implementation(
        requirement.id,
        route_id="get-default-hello",
        route_version=3,
    )

    operation = store.revise_and_create_operation(
        requirement.id,
        title="Greeting v2",
        instruction="Say hello v2",
        kind="update",
        base_route_id="get-default-hello",
        base_route_version=4,
    )

    revised = store.get(requirement.id)
    assert operation.requirement_id == requirement.id
    assert operation.id != requirement.id
    assert operation.base_route_version == 4
    assert revised.title == "Greeting v2"
    assert revised.instruction == "Say hello v2"
    assert revised.route_version == 4
    assert revised.status == "draft"

    with pytest.raises(RequirementBusyError, match="active operation"):
        store.revise_and_create_operation(
            requirement.id,
            title="Must not overwrite",
            instruction="Must not overwrite",
            kind="update",
            base_route_id="get-default-hello",
            base_route_version=4,
        )

    unchanged = store.get(requirement.id)
    assert unchanged.title == "Greeting v2"
    assert unchanged.instruction == "Say hello v2"


def test_recovery_marks_an_unpublished_operation_failed(
    database_path: Path,
) -> None:
    store = RequirementStore(database_path)
    requirement = store.create("Greeting", "Say hello", "/hello", "GET")
    operation = store.create_operation(
        requirement.id,
        kind="create",
        instruction=requirement.instruction,
        path=requirement.path,
        method=requirement.method,
        project=requirement.project,
    )
    store.begin_operation(operation.id)

    store.recover_interrupted({})

    recovered = store.get_operation(operation.id)
    assert recovered.status == "failed"
    assert recovered.last_error == (
        "service restarted before operation completed "
        "(previous status: implementing)"
    )


def test_recovery_identifies_an_operation_interrupted_before_it_started(
    database_path: Path,
) -> None:
    store = RequirementStore(database_path)
    requirement = store.create("Greeting", "Say hello", "/hello", "GET")
    operation = store.create_operation(
        requirement.id,
        kind="create",
        instruction=requirement.instruction,
        path=requirement.path,
        method=requirement.method,
        project=requirement.project,
    )

    store.recover_interrupted({})

    recovered = store.get_operation(operation.id)
    assert recovered.status == "failed"
    assert recovered.last_error == (
        "service restarted before operation completed "
        "(previous status: accepted)"
    )
    assert store.get(requirement.id).status == "draft"


def test_execution_mode_is_persisted_in_requirement_and_operation(tmp_path: Path) -> None:
    store = RequirementStore(tmp_path / "metadata.sqlite3")
    requirement = store.create(
        "Plugin route",
        "Build complete plugin",
        "/demo/run",
        "POST",
        project="demo",
        execution_mode="plugin",
    )

    operation = store.create_operation(
        requirement.id,
        kind="create",
        instruction=requirement.instruction,
        path=requirement.path,
        method=requirement.method,
        project=requirement.project,
        execution_mode=requirement.execution_mode,
    )

    assert store.get(requirement.id).execution_mode == "plugin"
    assert store.get_operation(operation.id).execution_mode == "plugin"


def test_old_database_migrates_execution_mode_to_restricted(tmp_path: Path) -> None:
    database = tmp_path / "legacy.sqlite3"
    store = RequirementStore(database)
    requirement = store.create("Legacy", "Keep it", "/default/legacy", "GET")
    operation = store.create_operation(
        requirement.id,
        kind="create",
        instruction=requirement.instruction,
        path=requirement.path,
        method=requirement.method,
        project=requirement.project,
    )
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE requirements DROP COLUMN execution_mode")
        connection.execute("ALTER TABLE operations DROP COLUMN execution_mode")
        connection.commit()

    migrated = RequirementStore(database)

    assert migrated.get(requirement.id).execution_mode == "restricted"
    assert migrated.get_operation(operation.id).execution_mode == "restricted"
