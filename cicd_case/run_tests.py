#!/usr/bin/env python3
"""DTS-style standalone runner for the agent CICD integration cases."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from contextlib import AbstractContextManager
from pathlib import Path
from typing import TextIO

ROOT = Path(__file__).resolve().parent.parent
CICD_DIR = ROOT / "cicd_case"
GROUPS = {
    "health": {
        "label": "Agent health preflight",
        "file": "test_cluster_health.py",
        "cases": ("test_agent_health",),
    },
    "lifecycle": {
        "label": "Dynamic API lifecycle",
        "file": "test_agent_lifecycle.py",
        "cases": (
            "test_management_auth",
            "test_dynamic_hello_create",
            "test_concurrent_business_requests",
            "test_console_requirement_metadata_survives_restart",
            "test_hot_reload_update",
            "test_failed_update_rollback",
            "test_restart_recovery",
        ),
    },
    "coding_agent": {
        "label": "Pi coding-agent RPC integration",
        "file": "test_pi_lifecycle.py",
        "cases": ("test_pi_rpc_backend_generates_and_hot_loads_handler",),
    },
}
DEFAULT_GROUPS = ("health", "lifecycle", "coding_agent")


class TeeStream:
    """Mirror output to the original stream and a line-buffered log file."""

    def __init__(self, stream: TextIO, log_file: TextIO) -> None:
        self.stream = stream
        self.log_file = log_file
        self.encoding = getattr(stream, "encoding", "utf-8")
        self.errors = getattr(stream, "errors", "replace")

    def write(self, data: str) -> int:
        self.stream.write(data)
        self.log_file.write(data)
        self.flush()
        return len(data)

    def flush(self) -> None:
        self.stream.flush()
        self.log_file.flush()

    def isatty(self) -> bool:
        return self.stream.isatty()

    def __getattr__(self, name: str):
        return getattr(self.stream, name)


class TeeOutput(AbstractContextManager[str]):
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self._log_file: TextIO | None = None
        self._stdout: TextIO | None = None
        self._stderr: TextIO | None = None

    def __enter__(self) -> str:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._log_file = self.path.open("a", encoding="utf-8", buffering=1)
        self._stdout = sys.stdout
        self._stderr = sys.stderr
        sys.stdout = TeeStream(self._stdout, self._log_file)  # type: ignore[assignment]
        sys.stderr = TeeStream(self._stderr, self._log_file)  # type: ignore[assignment]
        return str(self.path)

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        finally:
            if self._stdout is not None:
                sys.stdout = self._stdout
            if self._stderr is not None:
                sys.stderr = self._stderr
            if self._log_file is not None:
                self._log_file.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Self-Growing Agent CICD tests")
    parser.add_argument(
        "--list-cases", action="store_true", help="list executable group:test_name cases"
    )
    parser.add_argument(
        "--case",
        dest="cases",
        action="append",
        default=[],
        metavar="GROUP:TEST_NAME",
        help="run one exact case; may be repeated",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        help="log path; defaults to cicd_case/logs/run_tests_<timestamp>.log",
    )
    parser.add_argument(
        "groups",
        nargs="*",
        metavar="GROUP",
        help=f"groups to run ({', '.join(GROUPS)}); default: all",
    )
    args = parser.parse_args(argv)
    args.groups = args.groups or list(DEFAULT_GROUPS)

    unknown_groups = [group for group in args.groups if group not in GROUPS]
    if unknown_groups:
        parser.error(f"unknown group: {unknown_groups[0]} (choose from {', '.join(GROUPS)})")
    args.groups = list(dict.fromkeys(args.groups))
    if args.list_cases and args.cases:
        parser.error("--list-cases cannot be combined with --case")
    for case_spec in args.cases:
        try:
            group, case_name = _parse_case_spec(case_spec)
        except ValueError as exc:
            parser.error(str(exc))
        if group not in args.groups:
            parser.error(f"case group {group!r} is outside the requested group scope")
        if case_name not in GROUPS[group]["cases"]:
            choices = ", ".join(GROUPS[group]["cases"])
            parser.error(f"unknown case for {group}: {case_name} (choose from {choices})")
    return args


def _parse_case_spec(spec: str) -> tuple[str, str]:
    if spec.count(":") != 1:
        raise ValueError(f"invalid case {spec!r}; expected group:test_name")
    group, case_name = spec.split(":", 1)
    if group not in GROUPS:
        raise ValueError(f"unknown case group: {group} (choose from {', '.join(GROUPS)})")
    if not case_name:
        raise ValueError(f"invalid case {spec!r}; test name is empty")
    return group, case_name


def _default_log_file() -> Path:
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    return CICD_DIR / "logs" / f"run_tests_{timestamp}.log"


def _print_cases(groups: list[str]) -> None:
    print("Available CICD cases:")
    for group in groups:
        details = GROUPS[group]
        print(f"\n[{group}] {details['label']}")
        for case_name in details["cases"]:
            print(f"  {group}:{case_name}")


def _node_id(group: str, case_name: str | None = None) -> str:
    node_id = f"cicd_case/{GROUPS[group]['file']}"
    if case_name is not None:
        node_id += f"::{case_name}"
    return node_id


def _pytest_command(node_id: str) -> list[str]:
    return [sys.executable, "-m", "pytest", "-q", node_id]


def _run_pytest(node_id: str) -> int:
    command = _pytest_command(node_id)
    print(f"\n$ {' '.join(command)}")
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=os.name == "posix",
    )
    try:
        if process.stdout is None:
            raise RuntimeError("pytest output pipe was not created")
        for line in process.stdout:
            print(line, end="")
        return_code = process.wait()
        if return_code != 0:
            _terminate_test_processes(process)
        return return_code
    except BaseException:
        _terminate_test_processes(process)
        raise
    finally:
        if process.stdout is not None:
            process.stdout.close()


def _terminate_test_processes(process: subprocess.Popen[str]) -> None:
    """Terminate pytest and any service/handler descendants owned by its process group."""

    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    elif process.poll() is None:
        process.terminate()

    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        else:
            process.kill()
        process.wait(timeout=3)
    else:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


def _run_group(group: str) -> tuple[bool, str]:
    reproduction = f"{sys.executable} cicd_case/run_tests.py {group}"
    return _run_pytest(_node_id(group)) == 0, reproduction


def _run_case(group: str, case_name: str) -> tuple[bool, str]:
    reproduction = f"{sys.executable} cicd_case/run_tests.py --case {group}:{case_name}"
    return _run_pytest(_node_id(group, case_name)) == 0, reproduction


def _execute(args: argparse.Namespace, log_file: str) -> int:
    started_at = time.monotonic()
    failures: list[tuple[str, str]] = []
    print("=" * 64)
    print("  Self-Growing Agent CICD Integration Tests")
    print("=" * 64)
    print(f"  Python:     {sys.executable}")
    print(f"  Log file:   {log_file}")
    print(f"  Groups:     {', '.join(args.groups)}")
    if args.cases:
        print(f"  Cases:      {', '.join(args.cases)}")
    print("=" * 64)

    if args.cases:
        selected = [_parse_case_spec(spec) for spec in args.cases]
        has_non_health = any(group != "health" for group, _ in selected)
        health_selected = any(group == "health" for group, _ in selected)
        if has_non_health:
            passed, reproduction = _run_group("health")
            if not passed:
                failures.append(("health preflight", reproduction))
                return _finish(failures, started_at, blocked=True)
        for group, case_name in selected:
            if group == "health" and has_non_health and health_selected:
                continue
            passed, reproduction = _run_case(group, case_name)
            if not passed:
                failures.append((f"{group}:{case_name}", reproduction))
        return _finish(failures, started_at)

    planned_groups = list(args.groups)
    if any(group != "health" for group in planned_groups):
        planned_groups = ["health", *[g for g in planned_groups if g != "health"]]
    for group in planned_groups:
        passed, reproduction = _run_group(group)
        if not passed:
            failures.append((group, reproduction))
            if group == "health" and len(planned_groups) > 1:
                return _finish(failures, started_at, blocked=True)
    return _finish(failures, started_at)


def _finish(
    failures: list[tuple[str, str]],
    started_at: float,
    *,
    blocked: bool = False,
) -> int:
    elapsed = time.monotonic() - started_at
    print("\n" + "=" * 64)
    if blocked:
        print("HEALTH PREFLIGHT FAILED; REMAINING TESTS WERE NOT RUN")
    elif failures:
        print(f"{len(failures)} CICD TEST SELECTION(S) FAILED")
    else:
        print("ALL CICD INTEGRATION TESTS PASSED")
    print(f"Elapsed: {elapsed:.1f}s")
    if failures:
        print("Reproduce failures with:")
        for label, command in failures:
            print(f"  [{label}] {command}")
    print("=" * 64)
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.list_cases:
        _print_cases(args.groups)
        return 0

    log_file = args.log_file or _default_log_file()
    with TeeOutput(log_file) as active_log_file:
        return _execute(args, active_log_file)


if __name__ == "__main__":
    raise SystemExit(main())
