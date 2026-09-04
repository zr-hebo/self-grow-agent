"""Docker-backed smoke test for the hardened plugin executor."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from self_grow_agent.plugin_executor import ContainerPluginExecutor
from self_grow_agent.plugin_models import GeneratedPlugin, PluginFile
from self_grow_agent.plugin_runtime import _publish_artifact

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(
    os.environ.get("RUN_DOCKER_CICD") != "true",
    reason="set RUN_DOCKER_CICD=true to run Docker integration cases",
)
def test_hardened_container_executes_published_plugin() -> None:
    with tempfile.TemporaryDirectory(prefix=".container-cicd-", dir=PROJECT_ROOT) as root:
        artifact, digest = _publish_artifact(
            artifact_root=Path(root) / "plugins",
            project="cicd",
            route_id="post-cicd-container",
            version=1,
            plugin=GeneratedPlugin(
                files=(
                    PluginFile(
                        path="handler.py",
                        content=(
                            "def handle(request):\n"
                            "    return {'isolated': True, 'body': request['body']}\n"
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
            timeout_seconds=15,
            image=os.environ.get(
                "PLUGIN_CICD_IMAGE", "self-grow-agent-plugin-runtime:cicd"
            ),
            memory_limit_bytes=256 * 1024 * 1024,
            cpu_limit_seconds=2,
        ).execute(artifact, digest, {"body": {"name": "Ada"}})

    assert result == {"isolated": True, "body": {"name": "Ada"}}
