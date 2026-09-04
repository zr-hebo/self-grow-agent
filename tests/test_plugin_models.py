from __future__ import annotations

import pytest
from pydantic import ValidationError

from self_grow_agent.plugin_models import (
    GeneratedPlugin,
    PluginFile,
    PluginPolicy,
    PluginPolicyError,
)


def _plugin(
    *,
    dependencies: tuple[str, ...] = ("PyMySQL==1.1.1",),
    files: tuple[PluginFile, ...] | None = None,
) -> GeneratedPlugin:
    return GeneratedPlugin(
        description="Restart replication",
        entrypoint="handler:handle",
        dependencies=dependencies,
        files=files
        or (
            PluginFile(
                path="handler.py",
                content="def handle(request):\n    return {'ok': True}\n",
            ),
            PluginFile(
                path="tests/test_handler.py",
                content="def test_handler():\n    assert True\n",
            ),
        ),
    )


def test_normalizes_pinned_dependencies_and_accepts_allowed_bundle() -> None:
    plugin = _plugin()
    policy = PluginPolicy(allowed_dependencies=frozenset({"pymysql==1.1.1"}))

    validated = policy.validate(plugin)

    assert validated is plugin
    assert plugin.dependencies == ("pymysql==1.1.1",)
    assert plugin.entrypoint == "handler:handle"


@pytest.mark.parametrize(
    "path",
    [
        "/handler.py",
        "../handler.py",
        "nested/../../handler.py",
        r"tests\test_handler.py",
        "./handler.py",
        ".hidden.py",
        "nested//handler.py",
        "nested/\x00handler.py",
        "nested/readme.txt",
    ],
)
def test_rejects_unsafe_or_unsupported_file_paths(path: str) -> None:
    with pytest.raises(ValidationError):
        PluginFile(path=path, content="pass\n")


def test_rejects_duplicate_paths() -> None:
    with pytest.raises(ValidationError, match="duplicate plugin file path"):
        GeneratedPlugin(
            files=(
                PluginFile(path="handler.py", content="pass\n"),
                PluginFile(path="handler.py", content="pass\n"),
            )
        )


@pytest.mark.parametrize(
    "dependency",
    [
        "pymysql",
        "pymysql>=1.1.1",
        "pymysql~=1.1",
        "-e example",
        "https://example.test/package.whl",
        "pymysql==1.1.1; python_version>'3.12'",
    ],
)
def test_rejects_unpinned_or_complex_dependency_specs(dependency: str) -> None:
    with pytest.raises(ValidationError, match="exact name==version pin"):
        _plugin(dependencies=(dependency,))


def test_rejects_dependency_outside_policy_allowlist() -> None:
    plugin = _plugin(dependencies=("requests==2.32.0",))

    with pytest.raises(PluginPolicyError, match="dependency is not allowed"):
        PluginPolicy(allowed_dependencies=frozenset({"pymysql==1.1.1"})).validate(
            plugin
        )


def test_requires_handler_and_test_files() -> None:
    policy = PluginPolicy()

    with pytest.raises(PluginPolicyError, match="handler.py"):
        policy.validate(
            GeneratedPlugin(
                files=(PluginFile(path="tests/test_handler.py", content="pass\n"),)
            )
        )

    with pytest.raises(PluginPolicyError, match=r"tests/test_.*\.py"):
        policy.validate(
            GeneratedPlugin(
                files=(PluginFile(path="handler.py", content="pass\n"),)
            )
        )


def test_enforces_file_count_individual_and_total_byte_limits() -> None:
    files = (
        PluginFile(path="handler.py", content="a" * 8),
        PluginFile(path="tests/test_handler.py", content="b" * 8),
    )

    with pytest.raises(PluginPolicyError, match="too many files"):
        PluginPolicy(max_files=1).validate(_plugin(dependencies=(), files=files))

    with pytest.raises(PluginPolicyError, match="exceeds byte limit"):
        PluginPolicy(max_file_bytes=7).validate(_plugin(dependencies=(), files=files))

    with pytest.raises(PluginPolicyError, match="total source exceeds byte limit"):
        PluginPolicy(max_total_bytes=15).validate(_plugin(dependencies=(), files=files))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_files", 0),
        ("max_file_bytes", True),
        ("max_total_bytes", -1),
    ],
)
def test_rejects_invalid_policy_limits(field: str, value: object) -> None:
    with pytest.raises(ValueError, match=field):
        PluginPolicy(**{field: value})  # type: ignore[arg-type]
