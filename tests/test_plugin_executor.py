from __future__ import annotations

import logging
import secrets
from pathlib import Path

import pytest

from self_grow_agent import plugin_executor as plugin_executor_module
from self_grow_agent.plugin_executor import (
    ContainerPluginExecutor,
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


def test_sensitive_environment_is_withheld_from_request_but_available_to_capability_process(
    tmp_path: Path,
) -> None:
    password = secrets.token_urlsafe(16)
    artifact, digest = _artifact(
        tmp_path,
        "import os\n\ndef handle(request):\n"
        "    return {'request_has_password': "
        "'MYSQL_PASSWORD' in request['runtime']['environment'], "
        "'process_has_password': os.getenv('MYSQL_PASSWORD') is not None}\n",
    )

    result = _executor(allowed_environment={"MYSQL_PASSWORD": password}).execute(
        artifact, digest, {}
    )

    assert result == {"request_has_password": False, "process_has_password": True}


def test_forwards_bounded_plugin_logs_and_redacts_sensitive_values(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    environment_secret = secrets.token_urlsafe(16)
    request_secret = secrets.token_urlsafe(16)
    artifact, digest = _artifact(
        tmp_path,
        "import logging\nimport os\n\n"
        "def handle(request):\n"
        "    logger = logging.getLogger('generated.plugin')\n"
        "    logger.info('instance=%s password=%s request_password=%s', "
        "'10.0.0.1:3306', os.getenv('MYSQL_PASSWORD'), "
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


def _fake_container_runtime(tmp_path: Path) -> tuple[Path, Path]:
    capture = tmp_path / "container-command.txt"
    runtime = tmp_path / "fake-container-runtime"
    runtime.write_text(
        "#!/bin/sh\n"
        ": > \"$FAKE_RUNTIME_CAPTURE\"\n"
        "for argument in \"$@\"; do printf '%s\\n' \"$argument\" "
        ">> \"$FAKE_RUNTIME_CAPTURE\"; done\n"
        "printf '%s\\n' \"MYSQL_PASSWORD_PRESENT=${MYSQL_PASSWORD:+yes}\" "
        ">> \"$FAKE_RUNTIME_CAPTURE\"\n"
        "printf '%s' '{\"status\":\"ok\",\"result\":{\"isolated\":true},\"logs\":[]}'\n",
        encoding="utf-8",
    )
    runtime.chmod(0o700)
    return runtime, capture


def test_container_executor_uses_hardened_default_network_and_hides_secret_from_argv(
    tmp_path: Path,
) -> None:
    artifact, digest = _artifact(tmp_path, "def handle(request):\n    return {}\n")
    runtime, capture = _fake_container_runtime(tmp_path)
    password = secrets.token_urlsafe(24)
    executor = ContainerPluginExecutor(
        timeout_seconds=2,
        runtime=str(runtime),
        image="plugin-runtime:test",
        memory_limit_bytes=128 * 1024 * 1024,
        cpu_limit_seconds=1,
        allowed_environment={
            "FAKE_RUNTIME_CAPTURE": str(capture),
            "MYSQL_PASSWORD": password,
        },
    )

    result = executor.execute(artifact, digest, {"body": {"name": "Ada"}})

    assert result == {"isolated": True}
    arguments = capture.read_text(encoding="utf-8").splitlines()
    assert arguments[:2] == ["run", "--rm"]
    assert arguments[arguments.index("--network") + 1] == "none"
    assert "--read-only" in arguments
    assert "--cap-drop=ALL" in arguments
    assert "--security-opt=no-new-privileges" in arguments
    assert "--pids-limit=64" in arguments
    assert "--memory=134217728" in arguments
    assert "--env=MYSQL_PASSWORD" in arguments
    assert "MYSQL_PASSWORD_PRESENT=yes" in arguments
    assert password not in "\n".join(arguments)
    assert any(value.endswith(",dst=/plugin,readonly") for value in arguments)
    assert any(value.endswith(",dst=/opt/self-grow-agent,readonly") for value in arguments)


def test_container_executor_uses_explicit_project_network(tmp_path: Path) -> None:
    artifact, digest = _artifact(tmp_path, "def handle(request):\n    return {}\n")
    runtime, capture = _fake_container_runtime(tmp_path)
    executor = ContainerPluginExecutor(
        timeout_seconds=2,
        runtime=str(runtime),
        image="plugin-runtime:test",
        network="binlog-private",
        allowed_environment={"FAKE_RUNTIME_CAPTURE": str(capture)},
    )

    executor.execute(artifact, digest, {})

    arguments = capture.read_text(encoding="utf-8").splitlines()
    assert arguments[arguments.index("--network") + 1] == "binlog-private"


def test_container_executor_removes_exact_container_after_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact, digest = _artifact(tmp_path, "def handle(request):\n    return {}\n")
    runtime = tmp_path / "slow-container-runtime"
    runtime.write_text("#!/bin/sh\nsleep 10\n", encoding="utf-8")
    runtime.chmod(0o700)
    removed: list[tuple[str, str]] = []

    def record_remove(runtime_path: str, name: str, environment: object) -> None:
        del environment
        removed.append((runtime_path, name))

    monkeypatch.setattr(plugin_executor_module, "_remove_container", record_remove)
    executor = ContainerPluginExecutor(
        timeout_seconds=0.2,
        runtime=str(runtime),
        image="plugin-runtime:test",
    )

    with pytest.raises(PluginTimeoutError, match="timed out"):
        executor.execute(artifact, digest, {})

    assert len(removed) == 1
    assert removed[0][0] == str(runtime)
    assert removed[0][1].startswith("self-grow-agent-plugin-")
    assert len(removed[0][1]) == len("self-grow-agent-plugin-") + 32


@pytest.mark.parametrize("network", ["", "bad network", "network/name"])
def test_container_executor_rejects_invalid_network(network: str) -> None:
    with pytest.raises(ValueError, match="network"):
        ContainerPluginExecutor(timeout_seconds=2, network=network)
