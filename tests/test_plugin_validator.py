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
        "import json\nimport pymysql\n\ndef handle(request):\n    return json.loads('{}')\n",
        dependencies=("pymysql==1.1.1",),
    )

    result = _validator("pymysql==1.1.1").validate(plugin)

    assert result.imported_modules == ("json", "pymysql")


def test_accepts_mysql_connector_distribution_import_root() -> None:
    plugin = _plugin(
        "import mysql.connector\n\ndef handle(request):\n    return {'ok': True}\n",
        dependencies=("mysql-connector-python==26.7.0",),
    )

    result = _validator("mysql-connector-python==26.7.0").validate(plugin)

    assert result.imported_modules == ("mysql",)


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
