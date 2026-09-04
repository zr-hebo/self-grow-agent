"""Run generated plugin tests in a sanitized, bounded subprocess."""

from __future__ import annotations

import math
import os
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True, slots=True)
class PluginTestResult:
    passed: bool
    exit_code: int | None
    elapsed_seconds: float
    output_bytes: int
    failure_category: str | None


class PluginTestRunner:
    """Execute pytest without exposing the parent service environment."""

    def __init__(self, timeout_seconds: float, max_output_bytes: int = 1_048_576) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a positive finite number")
        if (
            isinstance(max_output_bytes, bool)
            or not isinstance(max_output_bytes, int)
            or max_output_bytes <= 0
        ):
            raise ValueError("max_output_bytes must be a positive integer")
        self._timeout_seconds = float(timeout_seconds)
        self._max_output_bytes = max_output_bytes

    def run(
        self,
        source_dir: str | Path,
        dependencies: tuple[str, ...] = (),
    ) -> PluginTestResult:
        """Run tests and return safe metadata without retaining raw output."""

        del dependencies  # Dependencies are installed by the deployment environment.
        started_at = time.monotonic()
        source = Path(source_dir).expanduser().resolve()
        if not source.is_dir() or Path(source_dir).is_symlink():
            return self._result(started_at, None, 0, "invalid_source")

        with tempfile.TemporaryDirectory(prefix="self-grow-agent-plugin-test-") as home:
            environment = {
                "HOME": home,
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PATH": os.defpath,
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONNOUSERSITE": "1",
                "PYTHONPATH": os.pathsep.join((str(source), str(_PROJECT_ROOT))),
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            }
            command = [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "--disable-warnings",
                "--maxfail=1",
                "-p",
                "no:cacheprovider",
                "tests",
            ]
            with tempfile.TemporaryFile() as output:
                try:
                    process = subprocess.Popen(
                        command,
                        cwd=source,
                        env=environment,
                        stdin=subprocess.DEVNULL,
                        stdout=output,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                except OSError:
                    return self._result(started_at, None, 0, "start_failed")

                deadline = started_at + self._timeout_seconds
                failure_category: str | None = None
                while process.poll() is None:
                    output_bytes = os.fstat(output.fileno()).st_size
                    if output_bytes > self._max_output_bytes:
                        failure_category = "output_too_large"
                        _terminate_process_group(process)
                        break
                    if time.monotonic() >= deadline:
                        failure_category = "timeout"
                        _terminate_process_group(process)
                        break
                    time.sleep(0.01)

                if process.poll() is None:
                    _terminate_process_group(process)
                exit_code = process.wait(timeout=1)
                output_bytes = os.fstat(output.fileno()).st_size
                if failure_category is None and output_bytes > self._max_output_bytes:
                    failure_category = "output_too_large"
                if failure_category is None and exit_code != 0:
                    failure_category = "tests_failed"
                return PluginTestResult(
                    passed=exit_code == 0 and failure_category is None,
                    exit_code=exit_code,
                    elapsed_seconds=time.monotonic() - started_at,
                    output_bytes=output_bytes,
                    failure_category=failure_category,
                )

    @staticmethod
    def _result(
        started_at: float,
        exit_code: int | None,
        output_bytes: int,
        failure_category: str,
    ) -> PluginTestResult:
        return PluginTestResult(
            passed=False,
            exit_code=exit_code,
            elapsed_seconds=time.monotonic() - started_at,
            output_bytes=output_bytes,
            failure_category=failure_category,
        )


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        if hasattr(os, "killpg"):
            os.killpg(process.pid, signal.SIGTERM)
        else:  # pragma: no cover - Windows fallback
            process.terminate()
        process.wait(timeout=0.5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            if hasattr(os, "killpg"):
                os.killpg(process.pid, signal.SIGKILL)
            else:  # pragma: no cover - Windows fallback
                process.kill()
        except OSError:
            pass


__all__ = ["PluginTestResult", "PluginTestRunner"]
