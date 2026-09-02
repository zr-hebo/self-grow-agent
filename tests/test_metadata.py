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
