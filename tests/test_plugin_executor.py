from __future__ import annotations

import logging
import secrets
from pathlib import Path

import pytest

from self_grow_agent.plugin_executor import (
    PluginProcessError,
    PluginProcessExecutor,
    PluginTimeoutError,
)
from self_grow_agent.plugin_models import GeneratedPlugin, PluginFile
from self_grow_agent.plugin_runtime import _publish_artifact


def _artifact(tmp_path: Path, handler: str) -> tuple[Path, str]:
    plugin = GeneratedPlugin(
        files=(
            PluginFile(path="handler.py", content=handler),
            PluginFile(path="tests/test_handler.py", content="def test_ok():\n    assert True\n"),
        )
    )
    return _publish_artifact(
        artifact_root=tmp_path / "plugins",
        project="demo",
        route_id="post-demo-run",
        version=1,
        plugin=plugin,
    )


def _executor(**kwargs: object) -> PluginProcessExecutor:
    return PluginProcessExecutor(
        timeout_seconds=2,
        memory_limit_bytes=256 * 1024 * 1024,
        cpu_limit_seconds=1,
        max_result_bytes=64 * 1024,
        **kwargs,
    )


def test_executes_verified_plugin_with_json_ipc_and_standard_import(tmp_path: Path) -> None:
    artifact, digest = _artifact(
        tmp_path,
        "import json\n\ndef handle(request):\n    return json.loads(json.dumps(request['body']))\n",
    )

    result = _executor().execute(artifact, digest, {"body": {"name": "Ada"}})

    assert result == {"name": "Ada"}


def test_reports_safe_handler_exception_category(tmp_path: Path) -> None:
    artifact, digest = _artifact(
        tmp_path,
        "def handle(request):\n    raise ValueError(request['body']['secret'])\n",
    )
    secret = secrets.token_urlsafe(32)

    with pytest.raises(PluginProcessError, match="plugin handler raised ValueError") as raised:
        _executor().execute(artifact, digest, {"body": {"secret": secret}})

    assert secret not in str(raised.value)


def test_times_out_and_reaps_plugin_process(tmp_path: Path) -> None:
    artifact, digest = _artifact(
        tmp_path,
        "import time\n\ndef handle(request):\n    time.sleep(10)\n",
    )

    with pytest.raises(PluginTimeoutError, match="timed out"):
        PluginProcessExecutor(timeout_seconds=0.2).execute(artifact, digest, {})


def test_rejects_oversized_result(tmp_path: Path) -> None:
    artifact, digest = _artifact(
        tmp_path,
        "def handle(request):\n    return {'value': 'x' * 10000}\n",
    )

    with pytest.raises(PluginProcessError, match="result exceeded byte limit"):
        PluginProcessExecutor(timeout_seconds=2, max_result_bytes=512).execute(
            artifact, digest, {}
        )


def test_worker_receives_only_explicit_environment_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent_secret = secrets.token_urlsafe(32)
    allowed_value = secrets.token_urlsafe(16)
    monkeypatch.setenv("DEEPSEEK_API_KEY", parent_secret)
    artifact, digest = _artifact(
        tmp_path,
        "import os\n\ndef handle(request):\n"
        "    return {'parent': os.getenv('DEEPSEEK_API_KEY'), "
        "'allowed': os.getenv('PLUGIN_DB_USER')}\n",
    )

    result = _executor(allowed_environment={"PLUGIN_DB_USER": allowed_value}).execute(
        artifact, digest, {}
    )

    assert result == {"parent": None, "allowed": allowed_value}


def test_explicit_environment_is_available_without_importing_os(tmp_path: Path) -> None:
    allowed_value = secrets.token_urlsafe(16)
    artifact, digest = _artifact(
        tmp_path,
        "def handle(request):\n"
        "    environment = request['runtime']['environment']\n"
        "    return {'configured': environment['PLUGIN_DB_USER']}\n",
    )

    result = _executor(allowed_environment={"PLUGIN_DB_USER": allowed_value}).execute(
        artifact, digest, {}
    )

    assert result == {"configured": allowed_value}


def test_forwards_bounded_plugin_logs_and_redacts_sensitive_values(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    environment_secret = secrets.token_urlsafe(16)
    request_secret = secrets.token_urlsafe(16)
    artifact, digest = _artifact(
        tmp_path,
        "import logging\n\n"
        "def handle(request):\n"
        "    logger = logging.getLogger('generated.plugin')\n"
        "    logger.info('instance=%s password=%s request_password=%s', "
        "'10.0.0.1:3306', request['runtime']['environment']['MYSQL_PASSWORD'], "
        "request['body']['password'])\n"
        "    return {'ok': True}\n",
    )
    caplog.set_level(logging.INFO, logger="uvicorn.error")

    result = _executor(
        allowed_environment={"MYSQL_PASSWORD": environment_secret}
    ).execute(artifact, digest, {"body": {"password": request_secret}})

    assert result == {"ok": True}
    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "plugin_handler event" in logs
    assert "route_id=post-demo-run" in logs
    assert "instance=10.0.0.1:3306" in logs
    assert environment_secret not in logs
    assert request_secret not in logs
    assert "<redacted>" in logs


def test_rejects_tampered_artifact_before_execution(tmp_path: Path) -> None:
    artifact, digest = _artifact(tmp_path, "def handle(request):\n    return {'ok': True}\n")
    (artifact / "handler.py").write_text("def handle(request): return {'tampered': True}\n")

    with pytest.raises(PluginProcessError, match="artifact validation failed"):
        _executor().execute(artifact, digest, {})


def test_old_and_new_immutable_versions_can_execute_concurrently(tmp_path: Path) -> None:
    first, first_digest = _artifact(
        tmp_path / "first", "def handle(request):\n    return {'version': 1}\n"
    )
    second, second_digest = _artifact(
        tmp_path / "second", "def handle(request):\n    return {'version': 2}\n"
    )

    assert _executor().execute(first, first_digest, {}) == {"version": 1}
    assert _executor().execute(second, second_digest, {}) == {"version": 2}
