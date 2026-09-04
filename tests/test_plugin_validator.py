from __future__ import annotations

import pytest

from self_grow_agent.plugin_models import GeneratedPlugin, PluginFile, PluginPolicy
from self_grow_agent.plugin_validator import PluginValidationError, PluginValidator


def _plugin(handler: str, *, dependencies: tuple[str, ...] = ()) -> GeneratedPlugin:
    return GeneratedPlugin(
        dependencies=dependencies,
        files=(
            PluginFile(path="handler.py", content=handler),
            PluginFile(path="tests/test_handler.py", content="def test_ok():\n    assert True\n"),
        ),
    )


def _validator(*allowed: str) -> PluginValidator:
    return PluginValidator(PluginPolicy(allowed_dependencies=frozenset(allowed)))


def test_accepts_declared_dependency_and_standard_library_imports() -> None:
    plugin = _plugin(
        "import httpx\nimport json\n\ndef handle(request):\n    return json.loads('{}')\n",
        dependencies=("httpx==0.28.1",),
    )

    result = _validator("httpx==0.28.1").validate(plugin)

    assert result.imported_modules == ("httpx", "json")


def test_accepts_controlled_mysql_replication_capability() -> None:
    plugin = _plugin(
        "from self_grow_agent.capabilities.mysql_replication "
        "import rebuild_replication\n\n"
        "def handle(request):\n"
        "    return rebuild_replication(request['body']['instance'])\n",
    )

    result = _validator().validate(plugin)

    assert result.imported_modules == ("self_grow_agent",)


@pytest.mark.parametrize(
    ("handler", "dependency"),
    [
        (
            "import mysql.connector\n\ndef handle(request):\n    return {}\n",
            "mysql-connector-python==26.7.0",
        ),
        ("import pymysql\n\ndef handle(request):\n    return {}\n", "pymysql==1.1.1"),
        ("import sqlite3\n\ndef handle(request):\n    return {}\n", "sqlite3==1.0.0"),
    ],
)
def test_rejects_direct_database_driver_even_when_dependency_is_allowed(
    handler: str, dependency: str
) -> None:
    plugin = _plugin(handler, dependencies=(dependency,))

    with pytest.raises(PluginValidationError, match="controlled capability"):
        _validator(dependency).validate(plugin)


def test_rejects_database_driver_dependency_without_an_import() -> None:
    plugin = _plugin(
        "def handle(request):\n    return {}\n",
        dependencies=("mysql-connector-python==26.7.0",),
    )

    with pytest.raises(PluginValidationError, match="database driver dependencies"):
        _validator("mysql-connector-python==26.7.0").validate(plugin)


def test_rejects_broad_internal_package_import() -> None:
    with pytest.raises(PluginValidationError, match="controlled capability"):
        _validator().validate(
            _plugin("import self_grow_agent\n\ndef handle(request):\n    return {}\n")
        )


def test_rejects_capability_function_introspection() -> None:
    handler = (
        "from self_grow_agent.capabilities.mysql_replication "
        "import rebuild_replication\n\n"
        "def handle(request):\n"
        "    return rebuild_replication.__globals__['_connect']\n"
    )

    with pytest.raises(PluginValidationError, match="private attribute"):
        _validator().validate(_plugin(handler))


def test_rejects_qualified_reflection_call() -> None:
    handler = (
        "import builtins\n"
        "from self_grow_agent.capabilities.mysql_replication "
        "import rebuild_replication\n\n"
        "def handle(request):\n"
        "    return builtins.getattr(rebuild_replication, '__globals__')\n"
    )

    with pytest.raises(PluginValidationError, match="forbidden module|forbidden call"):
        _validator().validate(_plugin(handler))


@pytest.mark.parametrize(
    ("handler", "message"),
    [
        ("def handle(request):\n  return (\n", "syntax error"),
        ("def other(request):\n    return {}\n", "entrypoint"),
        ("def handle(request, extra):\n    return {}\n", "signature"),
        ("async def handle(request):\n    return {}\n", "synchronous"),
        ("def handle(request, /):\n    return {}\n", "signature"),
        ("from .helper import run\n\ndef handle(request):\n    return run()\n", "relative imports"),
        ("import importlib\n\ndef handle(request):\n    return {}\n", "forbidden module"),
        ("import requests\n\ndef handle(request):\n    return {}\n", "undeclared dependency"),
        ("PASSWORD = 'hardcoded-secret'\n\ndef handle(request):\n    return {}\n", "credential"),
        (
            "CONFIG = {'api_key': 'hardcoded-secret'}\n\ndef handle(request):\n    return {}\n",
            "credential",
        ),
        ("def handle(request):\n    return eval('1')\n", "forbidden call"),
    ],
)
def test_rejects_unsafe_or_invalid_source(handler: str, message: str) -> None:
    with pytest.raises(PluginValidationError, match=message):
        _validator().validate(_plugin(handler))


def test_rejects_dependency_that_was_not_allowed_by_policy() -> None:
    plugin = _plugin(
        "import pymysql\n\ndef handle(request):\n    return {}\n",
        dependencies=("pymysql==1.1.1",),
    )

    with pytest.raises(ValueError, match="not allowed"):
        _validator().validate(plugin)
