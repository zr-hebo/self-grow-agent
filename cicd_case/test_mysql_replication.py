"""Real MySQL protocol test for the controlled replication capability."""

from __future__ import annotations

import os
import secrets
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import mysql.connector
import pytest

from self_grow_agent.plugin_executor import ContainerPluginExecutor
from self_grow_agent.plugin_models import GeneratedPlugin, PluginFile
from self_grow_agent.plugin_runtime import _publish_artifact

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _docker(*arguments: str, environment: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["docker", *arguments],
        cwd=PROJECT_ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"docker command failed: {arguments[0]}: {result.stderr.strip()[:1000]}"
        )
    return result.stdout.strip()


def _wait_for_mysql(port: int, password: str) -> Any:
    deadline = time.monotonic() + 90
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return mysql.connector.connect(
                host="127.0.0.1",
                port=port,
                user="root",
                password=password,
                connection_timeout=2,
                autocommit=True,
            )
        except Exception as exc:
            last_error = exc
            time.sleep(1)
    raise AssertionError(f"MySQL did not become ready: {type(last_error).__name__}")


@pytest.mark.skipif(
    os.environ.get("RUN_DOCKER_CICD") != "true",
    reason="set RUN_DOCKER_CICD=true to run Docker integration cases",
)
def test_container_plugin_restarts_real_mysql_replication() -> None:
    suffix = uuid.uuid4().hex[:12]
    network = f"sga-mysql-{suffix}"
    container = f"sga-mysql-{suffix}"
    root_password = secrets.token_urlsafe(32)
    source_password = secrets.token_urlsafe(18)
    operator_password = secrets.token_urlsafe(32)
    docker_environment = dict(os.environ, MYSQL_ROOT_PASSWORD=root_password)
    _docker("network", "create", network, environment=docker_environment)
    try:
        _docker(
            "run",
            "--detach",
            "--rm",
            "--name",
            container,
            "--network",
            network,
            "--env=MYSQL_ROOT_PASSWORD",
            "--publish=127.0.0.1::3306",
            os.environ.get("MYSQL_CICD_IMAGE", "mysql:8.4"),
            "--server-id=41",
            "--log-bin=mysql-bin",
            "--gtid-mode=ON",
            "--enforce-gtid-consistency=ON",
            "--skip-replica-start=ON",
            environment=docker_environment,
        )
        port_output = _docker("port", container, "3306/tcp", environment=docker_environment)
        port = int(port_output.rsplit(":", 1)[1])
        connection = _wait_for_mysql(port, root_password)
        try:
            cursor = connection.cursor()
            cursor.execute(
                "CREATE USER 'sga_source'@'%' IDENTIFIED BY %s", (source_password,)
            )
            cursor.execute("GRANT REPLICATION SLAVE ON *.* TO 'sga_source'@'%'")
            cursor.execute(
                "CREATE USER 'sga_operator'@'%' IDENTIFIED BY %s", (operator_password,)
            )
            cursor.execute(
                "GRANT REPLICATION_SLAVE_ADMIN ON *.* TO 'sga_operator'@'%'"
            )
            cursor.execute(
                "CHANGE REPLICATION SOURCE TO "
                "SOURCE_HOST='127.0.0.1', SOURCE_PORT=3306, "
                "SOURCE_USER='sga_source', SOURCE_PASSWORD=%s, "
                "SOURCE_AUTO_POSITION=1, GET_SOURCE_PUBLIC_KEY=1",
                (source_password,),
            )
            cursor.close()
        finally:
            connection.close()

        container_ip = _docker(
            "inspect",
            "--format",
            f"{{{{with index .NetworkSettings.Networks \"{network}\"}}}}"
            "{{.IPAddress}}{{end}}",
            container,
            environment=docker_environment,
        )
        with tempfile.TemporaryDirectory(prefix=".mysql-cicd-", dir=PROJECT_ROOT) as root:
            artifact, digest = _publish_artifact(
                artifact_root=Path(root) / "plugins",
                project="mysql-cicd",
                route_id="post-mysql-cicd-rebuild",
                version=1,
                plugin=GeneratedPlugin(
                    files=(
                        PluginFile(
                            path="handler.py",
                            content=(
                                "from self_grow_agent.capabilities.mysql_replication "
                                "import rebuild_replication\n\n"
                                "def handle(request):\n"
                                "    return rebuild_replication("
                                "request['body']['instance'], retries=0)\n"
                            ),
                        ),
                        PluginFile(
                            path="tests/test_handler.py",
                            content="def test_ok():\n    assert True\n",
                        ),
                    )
                ),
            )
            result = ContainerPluginExecutor(
                timeout_seconds=20,
                image=os.environ.get(
                    "PLUGIN_CICD_IMAGE", "self-grow-agent-plugin-runtime:cicd"
                ),
                network=network,
                memory_limit_bytes=256 * 1024 * 1024,
                cpu_limit_seconds=3,
                allowed_environment={
                    "MYSQL_USER": "sga_operator",
                    "MYSQL_PASSWORD": operator_password,
                },
            ).execute(artifact, digest, {"body": {"instance": f"{container_ip}:3306"}})

        assert result["ok"] is True
        assert result["attempts"] == 1
        assert result["steps"] == [
            {"name": "stop_replica", "ok": True},
            {"name": "start_replica", "ok": True},
        ]
    finally:
        subprocess.run(
            ["docker", "rm", "--force", container],
            env=docker_environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
        subprocess.run(
            ["docker", "network", "rm", network],
            env=docker_environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
