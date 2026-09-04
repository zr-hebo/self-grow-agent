from __future__ import annotations

import os
import secrets
from pathlib import Path

from self_grow_agent.plugin_test_runner import PluginTestRunner


def _write_plugin(tmp_path: Path, test_source: str) -> Path:
    source = tmp_path / "source"
    tests = source / "tests"
    tests.mkdir(parents=True)
    (source / "handler.py").write_text(
        "def handle(request):\n    return {'ok': True}\n", encoding="utf-8"
    )
    (tests / "test_handler.py").write_text(test_source, encoding="utf-8")
    return source


def test_runs_passing_plugin_tests_with_sanitized_environment(
    tmp_path: Path, monkeypatch
) -> None:
    secret = secrets.token_urlsafe(24)
    monkeypatch.setenv("MANAGEMENT_API_KEY", secret)
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
    source = _write_plugin(
        tmp_path,
        """
import os
from handler import handle

def test_handler():
    assert handle({}) == {"ok": True}
    assert "MANAGEMENT_API_KEY" not in os.environ
    assert "DEEPSEEK_API_KEY" not in os.environ
""",
    )

    result = PluginTestRunner(timeout_seconds=5, max_output_bytes=16_384).run(source)

    assert result.passed is True
    assert result.exit_code == 0
    assert result.failure_category is None
    assert result.output_bytes < 16_384


def test_reports_test_failure_without_returning_test_output(tmp_path: Path) -> None:
    source = _write_plugin(tmp_path, "def test_bad():\n    assert False\n")

    result = PluginTestRunner(timeout_seconds=5, max_output_bytes=16_384).run(source)

    assert result.passed is False
    assert result.exit_code == 1
    assert result.failure_category == "tests_failed"
    assert not hasattr(result, "output")


def test_terminates_test_process_group_after_timeout(tmp_path: Path) -> None:
    source = _write_plugin(
        tmp_path,
        "import time\n\ndef test_slow():\n    time.sleep(10)\n",
    )

    result = PluginTestRunner(timeout_seconds=0.2, max_output_bytes=16_384).run(source)

    assert result.passed is False
    assert result.failure_category == "timeout"
    assert result.elapsed_seconds < 3


def test_terminates_test_process_when_output_limit_is_exceeded(tmp_path: Path) -> None:
    source = _write_plugin(
        tmp_path,
        "def test_noisy():\n    print('x' * 50000)\n    assert False\n",
    )

    result = PluginTestRunner(timeout_seconds=5, max_output_bytes=1_024).run(source)

    assert result.passed is False
    assert result.failure_category == "output_too_large"
    assert result.output_bytes > 1_024


def test_rejects_non_directory_and_symbolic_link_source(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    result = PluginTestRunner(timeout_seconds=5).run(missing)
    assert result.failure_category == "invalid_source"

    if hasattr(os, "symlink"):
        real = _write_plugin(tmp_path, "def test_ok():\n    assert True\n")
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)
        linked = PluginTestRunner(timeout_seconds=5).run(link)
        assert linked.failure_category == "invalid_source"
