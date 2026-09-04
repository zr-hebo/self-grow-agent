#!/usr/bin/env python3
"""Deterministic Pi RPC stub used by the black-box CICD case."""

from __future__ import annotations

import base64
import json
import os
import sys


def _emit(payload: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _validate_startup() -> None:
    args = sys.argv[1:]
    required = {
        "--mode",
        "rpc",
        "--no-session",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-context-files",
        "--no-approve",
        "--no-tools",
    }
    if not required.issubset(args) or "--api-key" in args:
        raise SystemExit("unexpected Pi RPC arguments")
    if not os.environ.get("DEEPSEEK_API_KEY"):
        raise SystemExit("missing DeepSeek credential environment")
    if not os.environ.get("PI_CODING_AGENT_DIR"):
        raise SystemExit("missing isolated Pi configuration directory")


def _plugin_task(prompt: str) -> dict[str, object]:
    encoded = prompt.split("BEGIN_UNTRUSTED_TASK_DATA\n", 1)[1].split(
        "\nEND_UNTRUSTED_TASK_DATA", 1
    )[0]
    return json.loads(base64.b64decode(encoded).decode("utf-8"))


def main() -> int:
    _validate_startup()
    for raw_line in sys.stdin.buffer:
        command = json.loads(raw_line)
        command_type = command.get("type")
        command_id = command.get("id")
        if command_type == "prompt":
            _emit(
                {
                    "id": command_id,
                    "type": "response",
                    "command": "prompt",
                    "success": True,
                }
            )
            prompt = str(command.get("message", command.get("prompt", "")))
            if "complete Python plugin" in prompt:
                task = _plugin_task(prompt)
                instruction = str(task.get("instruction", ""))
                current_plugin = task.get("current_plugin")
                value = "plugin-v2" if current_plugin else "plugin-v1"
                failing = "failing test" in instruction
                test_source = (
                    "def test_handler():\n    assert False\n"
                    if failing
                    else (
                        "from handler import handle\n\n"
                        "def test_handler():\n"
                        f"    assert handle({{}}) == {{'value': {value!r}}}\n"
                    )
                )
                generated = {
                    "description": f"CICD Pi {value}",
                    "entrypoint": "handler:handle",
                    "dependencies": [],
                    "files": [
                        {
                            "path": "handler.py",
                            "content": (
                                "import logging\n\n"
                                "def handle(request):\n"
                                f"    logging.getLogger('cicd.plugin').info('step=handle value={value}')\n"
                                f"    return {{'value': {value!r}}}\n"
                            ),
                        },
                        {"path": "tests/test_handler.py", "content": test_source},
                    ],
                }
            else:
                generated = {
                    "source": (
                        "def handle(request):\n"
                        '    return {"message": "hello from pi"}\n'
                    ),
                    "description": "CICD Pi handler",
                }
            handler = json.dumps(generated, separators=(",", ":"))
            assistant = {
                "role": "assistant",
                "content": [{"type": "text", "text": handler}],
                "stopReason": "stop",
            }
            _emit({"type": "message_end", "message": assistant})
            _emit({"type": "agent_end", "messages": [assistant], "willRetry": False})
            _emit({"type": "agent_settled"})
        elif command_type == "clear_queue":
            _emit(
                {
                    "id": command_id,
                    "type": "response",
                    "command": "clear_queue",
                    "success": True,
                    "data": {"steering": [], "followUp": []},
                }
            )
        elif command_type == "abort":
            _emit(
                {
                    "id": command_id,
                    "type": "response",
                    "command": "abort",
                    "success": True,
                }
            )
        else:
            _emit(
                {
                    "id": command_id,
                    "type": "response",
                    "command": str(command_type),
                    "success": False,
                    "error": "unsupported command",
                }
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
