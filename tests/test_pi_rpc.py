from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import signal
import stat
import sys
import textwrap
import time
from pathlib import Path

import pytest

from self_grow_agent.observability import generation_log_context, operation_log_context
from self_grow_agent.pi_rpc import (
    PiRpcAgentError,
    PiRpcClient,
    PiRpcExecutableNotFound,
    PiRpcProtocolError,
    PiRpcTimeoutError,
)

API_KEY = secrets.token_urlsafe(32)


def _write_rpc_script(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "fake_pi_rpc.py"
    script.write_text(
        textwrap.dedent(
            f"""\
            import json
            import os
            import pathlib
            import stat
            import subprocess
            import sys
            import time

            def emit(value):
                sys.stdout.buffer.write(
                    json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    + b"\\n"
                )
                sys.stdout.buffer.flush()

            prompt = json.loads(sys.stdin.buffer.readline())
            {textwrap.indent(textwrap.dedent(body), "            ").lstrip()}
            """
        ),
        encoding="utf-8",
    )
    return script


def _client(
    tmp_path: Path,
    script: Path,
    *script_args: str,
    timeout_seconds: float = 2,
) -> PiRpcClient:
    return PiRpcClient(
        command=(sys.executable, str(script), *script_args),
        provider="deepseek",
        model="deepseek-chat",
        api_key=API_KEY,
        provider_env_name="DEEPSEEK_API_KEY",
        timeout_seconds=timeout_seconds,
        workspace_root=tmp_path / "rpc-workspaces",
    )


def test_waits_for_agent_settled_and_returns_last_successful_assistant_text(
    tmp_path: Path,
) -> None:
    script = _write_rpc_script(
        tmp_path,
        """
        emit({"type": "response", "id": prompt["id"], "command": "prompt", "success": True})
        emit({
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "not final"}],
                "stopReason": "toolUse",
            },
        })
        emit({"type": "agent_end", "messages": [], "willRetry": True})
        time.sleep(0.08)
        emit({
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "private"},
                    {"type": "text", "text": "final "},
                    {"type": "text", "text": "answer"},
                ],
                "stopReason": "stop",
            },
        })
        emit({"type": "agent_end", "messages": [], "willRetry": False})
        emit({"type": "agent_settled"})
        for _line in sys.stdin.buffer:
            pass
        """,
    )
    client = _client(tmp_path, script)
    started_at = time.monotonic()

    result = asyncio.run(client.run("Implement the handler"))

    assert result.final_text == "final answer"
    assert time.monotonic() - started_at >= 0.07
    assert result.events[-1]["type"] == "agent_settled"


def test_logs_rpc_lifecycle_metrics_without_prompt_output_stderr_or_key(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    prompt_secret = secrets.token_urlsafe(32)
    output_secret = secrets.token_urlsafe(32)
    stderr_secret = secrets.token_urlsafe(32)
    script = _write_rpc_script(
        tmp_path,
        f"""
        sys.stderr.write({stderr_secret!r})
        sys.stderr.flush()
        emit({{"type": "response", "id": prompt["id"], "command": "prompt", "success": True}})
        emit({{
            "type": "message_end",
            "message": {{
                "role": "assistant",
                "content": [{{"type": "text", "text": {output_secret!r}}}],
                "stopReason": "stop",
            }},
        }})
        emit({{"type": "agent_settled"}})
        for _line in sys.stdin.buffer:
            pass
        """,
    )
    caplog.set_level(logging.INFO, logger="uvicorn.error")

    with operation_log_context("operation-456"):
        with generation_log_context("generation-789"):
            result = asyncio.run(_client(tmp_path, script).run(f"Implement {prompt_secret}"))

    assert result.final_text == output_secret
    logs = "\n".join(record.getMessage() for record in caplog.records)
    run_ids = set(re.findall(r"run_id=([0-9a-f]{32})", logs))
    assert len(run_ids) == 1
    assert "pi_rpc run_queued" in logs
    assert "operation_id=operation-456" in logs
    assert "generation_id=generation-789" in logs
    assert "provider='deepseek' model='deepseek-chat' timeout_seconds=2.000" in logs
    assert "prompt_chars=" in logs
    assert "prompt_bytes=" in logs
    assert "pi_rpc process_started" in logs
    assert re.search(r"process_started .* pid=\d+", logs)
    assert "pi_rpc prompt_accepted" in logs
    assert "stop_reason_category=stop message_category=completed assistant_messages=1" in logs
    assert "pi_rpc agent_settled" in logs
    assert "pi_rpc cleanup_started" in logs
    assert "pi_rpc cleanup_completed" in logs
    assert "action=graceful" in logs
    assert "pi_rpc run_completed" in logs
    assert "category=success" in logs
    assert "event_count=3" in logs
    assert "byte_count=" in logs
    assert re.search(r"stderr_bytes=[1-9]\d*", logs)
    assert "elapsed_seconds=" in logs
    assert prompt_secret not in logs
    assert output_secret not in logs
    assert stderr_secret not in logs
    assert API_KEY not in logs


def test_logs_timeout_category_and_progress_without_payload_text(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = secrets.token_urlsafe(32)
    script = _write_rpc_script(
        tmp_path,
        """
        emit({"type": "response", "id": prompt["id"], "command": "prompt", "success": True})
        for _line in sys.stdin.buffer:
            pass
        """,
    )
    caplog.set_level(logging.INFO, logger="uvicorn.error")

    with pytest.raises(PiRpcTimeoutError):
        asyncio.run(
            _client(tmp_path, script, timeout_seconds=0.2).run(
                f"Never settle; private value {secret}"
            )
        )

    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "pi_rpc prompt_accepted" in logs
    assert "pi_rpc run_failed" in logs
    assert "category=timeout" in logs
    assert "prompt_accepted=True" in logs
    assert "agent_settled=False" in logs
    assert "event_count=1" in logs
    assert "pi_rpc cleanup_started" in logs
    assert "pi_rpc cleanup_completed" in logs
    assert "cleanup_action=" in logs
    assert secret not in logs
    assert API_KEY not in logs


@pytest.mark.parametrize("stop_reason", ["error", "aborted", "length"])
def test_rejects_failed_final_assistant_message(tmp_path: Path, stop_reason: str) -> None:
    script = _write_rpc_script(
        tmp_path,
        f"""
        emit({{"type": "response", "id": prompt["id"], "command": "prompt", "success": True}})
        emit({{
            "type": "message_end",
            "message": {{
                "role": "assistant",
                "content": [{{"type": "text", "text": "partial"}}],
                "stopReason": {stop_reason!r},
                "errorMessage": "provider leaked detail {API_KEY}",
            }},
        }})
        emit({{"type": "agent_end", "messages": [], "willRetry": False}})
        emit({{"type": "agent_settled"}})
        for _line in sys.stdin.buffer:
            pass
        """,
    )

    with pytest.raises(PiRpcAgentError, match="did not complete successfully") as raised:
        asyncio.run(_client(tmp_path, script).run("Implement the handler"))

    assert API_KEY not in str(raised.value)
    assert "provider leaked detail" not in str(raised.value)


def test_prompt_acceptance_is_not_treated_as_completion(tmp_path: Path) -> None:
    script = _write_rpc_script(
        tmp_path,
        f"""
        emit({{"type": "response", "id": prompt["id"], "command": "prompt", "success": True}})
        emit({{
            "type": "message_end",
            "message": {{
                "role": "assistant",
                "content": [],
                "stopReason": "error",
                "errorMessage": "sensitive provider failure {API_KEY}",
            }},
        }})
        emit({{"type": "agent_settled"}})
        for _line in sys.stdin.buffer:
            pass
        """,
    )

    with pytest.raises(PiRpcAgentError) as raised:
        asyncio.run(_client(tmp_path, script).run("Implement the handler"))

    assert API_KEY not in str(raised.value)
    assert "sensitive provider failure" not in str(raised.value)


def test_times_out_and_requests_orderly_rpc_cleanup(tmp_path: Path) -> None:
    cleanup_file = tmp_path / "cleanup.json"
    script = _write_rpc_script(
        tmp_path,
        """
        cleanup_file = pathlib.Path(sys.argv[1])
        emit({"type": "response", "id": prompt["id"], "command": "prompt", "success": True})
        cleanup = [json.loads(line)["type"] for line in sys.stdin.buffer]
        cleanup_file.write_text(json.dumps(cleanup), encoding="utf-8")
        """,
    )

    with pytest.raises(PiRpcTimeoutError, match="timed out"):
        asyncio.run(
            _client(
                tmp_path,
                script,
                str(cleanup_file),
                timeout_seconds=0.05,
            ).run("Never settles")
        )

    assert json.loads(cleanup_file.read_text(encoding="utf-8"))[:2] == ["clear_queue", "abort"]


def test_cancellation_reaps_process_after_orderly_rpc_cleanup(tmp_path: Path) -> None:
    marker_file = tmp_path / "started"
    cleanup_file = tmp_path / "cancel-cleanup.json"
    script = _write_rpc_script(
        tmp_path,
        """
        marker_file = pathlib.Path(sys.argv[1])
        cleanup_file = pathlib.Path(sys.argv[2])
        marker_file.write_text("started", encoding="utf-8")
        emit({"type": "response", "id": prompt["id"], "command": "prompt", "success": True})
        cleanup = [json.loads(line)["type"] for line in sys.stdin.buffer]
        cleanup_file.write_text(json.dumps(cleanup), encoding="utf-8")
        """,
    )
    client = _client(tmp_path, script, str(marker_file), str(cleanup_file), timeout_seconds=5)

    async def cancel_run() -> None:
        task = asyncio.create_task(client.run("Cancel me"))
        for _ in range(200):
            if marker_file.exists():
                break
            await asyncio.sleep(0.005)
        assert marker_file.exists()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_run())

    assert json.loads(cleanup_file.read_text(encoding="utf-8"))[:2] == ["clear_queue", "abort"]


def _write_process_tree_rpc_script(tmp_path: Path) -> Path:
    return _write_rpc_script(
        tmp_path,
        """
        process_file = pathlib.Path(sys.argv[1])
        grandchild = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        process_file.write_text(json.dumps({
            "wrapper_pid": os.getpid(),
            "wrapper_session": os.getsid(0),
            "grandchild_pid": grandchild.pid,
            "grandchild_group": os.getpgid(grandchild.pid),
        }), encoding="utf-8")
        emit({"type": "response", "id": prompt["id"], "command": "prompt", "success": True})
        for _line in sys.stdin.buffer:
            pass
        """,
    )


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _assert_process_gone(pid: int) -> None:
    for _ in range(200):
        if not _process_exists(pid):
            return
        time.sleep(0.01)
    if _process_exists(pid):
        os.kill(pid, signal.SIGKILL)
    pytest.fail(f"process {pid} survived Pi RPC cleanup")


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group behavior")
def test_timeout_reaps_wrapper_grandchild_process_group(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    process_file = tmp_path / "timeout-process-tree.json"
    script = _write_process_tree_rpc_script(tmp_path)
    caplog.set_level(logging.INFO, logger="uvicorn.error")

    with pytest.raises(PiRpcTimeoutError):
        asyncio.run(
            _client(
                tmp_path,
                script,
                str(process_file),
                timeout_seconds=0.1,
            ).run("Never settles")
        )

    processes = json.loads(process_file.read_text(encoding="utf-8"))
    assert processes["wrapper_session"] == processes["wrapper_pid"]
    assert processes["grandchild_group"] == processes["wrapper_pid"]
    _assert_process_gone(processes["grandchild_pid"])
    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "pi_rpc cleanup_signal" in logs
    assert "action=terminate" in logs
    assert "cleanup_action=terminate" in logs
    assert "observed_exit_code=None" in logs
    assert "final_exit_code=" in logs
    assert "final_signal=" in logs


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group behavior")
def test_cancellation_reaps_wrapper_grandchild_process_group(tmp_path: Path) -> None:
    process_file = tmp_path / "cancel-process-tree.json"
    script = _write_process_tree_rpc_script(tmp_path)
    client = _client(tmp_path, script, str(process_file), timeout_seconds=5)

    async def cancel_run() -> None:
        task = asyncio.create_task(client.run("Cancel this process tree"))
        for _ in range(200):
            if process_file.exists():
                break
            await asyncio.sleep(0.005)
        assert process_file.exists()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_run())

    processes = json.loads(process_file.read_text(encoding="utf-8"))
    assert processes["wrapper_session"] == processes["wrapper_pid"]
    assert processes["grandchild_group"] == processes["wrapper_pid"]
    _assert_process_gone(processes["grandchild_pid"])


def test_reports_missing_executable_without_exposing_command(tmp_path: Path) -> None:
    missing = tmp_path / f"missing-{API_KEY}"
    client = PiRpcClient(
        command=(str(missing),),
        provider="deepseek",
        model="deepseek-chat",
        api_key=API_KEY,
        provider_env_name="DEEPSEEK_API_KEY",
        timeout_seconds=1,
        workspace_root=tmp_path / "rpc-workspaces",
    )

    with pytest.raises(PiRpcExecutableNotFound, match="executable was not found") as raised:
        asyncio.run(client.run("Implement the handler"))

    assert API_KEY not in str(raised.value)


@pytest.mark.parametrize(
    "provider_env_name",
    ["PATH", "TMPDIR", "NODE_OPTIONS", "lower_API_KEY"],
)
def test_rejects_provider_environment_name_that_overrides_process_controls(
    tmp_path: Path,
    provider_env_name: str,
) -> None:
    with pytest.raises(ValueError, match="provider_env_name"):
        PiRpcClient(
            command=("pi",),
            provider="deepseek",
            model="deepseek-chat",
            api_key=API_KEY,
            provider_env_name=provider_env_name,
            timeout_seconds=1,
            workspace_root=tmp_path,
        )


def test_uses_isolated_environment_safe_flags_and_private_directories(tmp_path: Path) -> None:
    capture_file = tmp_path / "launch.json"
    script = _write_rpc_script(
        tmp_path,
        """
        capture_file = pathlib.Path(sys.argv[1])
        capture_file.write_text(json.dumps({
            "argv": sys.argv[2:],
            "env": dict(os.environ),
            "cwd": os.getcwd(),
            "cwd_mode": stat.S_IMODE(os.stat(os.getcwd()).st_mode),
            "agent_dir_mode": stat.S_IMODE(os.stat(os.environ["PI_CODING_AGENT_DIR"]).st_mode),
        }), encoding="utf-8")
        emit({"type": "response", "id": prompt["id"], "command": "prompt", "success": True})
        emit({
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "done"}],
                "stopReason": "stop",
            },
        })
        emit({"type": "agent_settled"})
        for _line in sys.stdin.buffer:
            pass
        """,
    )
    sentinel_name = "SELF_GROW_AGENT_TEST_SECRET"
    old_sentinel = os.environ.get(sentinel_name)
    os.environ[sentinel_name] = API_KEY
    try:
        result = asyncio.run(
            _client(tmp_path, script, str(capture_file)).run("Implement the handler")
        )
    finally:
        if old_sentinel is None:
            os.environ.pop(sentinel_name, None)
        else:
            os.environ[sentinel_name] = old_sentinel

    assert result.final_text == "done"
    capture = json.loads(capture_file.read_text(encoding="utf-8"))
    argv = capture["argv"]
    child_env = capture["env"]
    assert API_KEY not in " ".join(argv)
    assert argv == [
        "--mode",
        "rpc",
        "--no-session",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-context-files",
        "--no-approve",
        "--no-tools",
        "--provider",
        "deepseek",
        "--model",
        "deepseek-chat",
    ]
    assert child_env["DEEPSEEK_API_KEY"] == API_KEY
    assert sentinel_name not in child_env
    assert child_env["PI_SKIP_VERSION_CHECK"] == "1"
    assert child_env["PI_TELEMETRY"] == "0"
    assert "--tools" not in argv
    assert capture["cwd_mode"] == stat.S_IRWXU
    assert capture["agent_dir_mode"] == stat.S_IRWXU
    assert capture["cwd"] != child_env["PI_CODING_AGENT_DIR"]
    assert not Path(capture["cwd"]).exists()
    assert not Path(child_env["PI_CODING_AGENT_DIR"]).exists()


def test_rejects_malformed_json_output(tmp_path: Path) -> None:
    script = _write_rpc_script(
        tmp_path,
        """
        emit({"type": "response", "id": prompt["id"], "command": "prompt", "success": True})
        sys.stdout.buffer.write(b"not-json\\n")
        sys.stdout.buffer.flush()
        for _line in sys.stdin.buffer:
            pass
        """,
    )

    with pytest.raises(PiRpcProtocolError, match="invalid JSON"):
        asyncio.run(_client(tmp_path, script).run("Implement the handler"))


def test_rejects_premature_stdout_eof(tmp_path: Path) -> None:
    script = _write_rpc_script(
        tmp_path,
        """
        emit({"type": "response", "id": prompt["id"], "command": "prompt", "success": True})
        """,
    )

    with pytest.raises(PiRpcProtocolError, match="ended before"):
        asyncio.run(_client(tmp_path, script).run("Implement the handler"))


def test_rejects_oversized_jsonl_event(tmp_path: Path) -> None:
    script = _write_rpc_script(
        tmp_path,
        """
        emit({"type": "response", "id": prompt["id"], "command": "prompt", "success": True})
        sys.stdout.buffer.write(b'{"type":"noise","value":"' + b"x" * 1_100_000 + b'"}\\n')
        sys.stdout.buffer.flush()
        for _line in sys.stdin.buffer:
            pass
        """,
    )

    with pytest.raises(PiRpcProtocolError, match="too large"):
        asyncio.run(_client(tmp_path, script).run("Implement the handler"))
