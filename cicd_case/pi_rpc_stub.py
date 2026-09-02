#!/usr/bin/env python3
"""Deterministic Pi RPC stub used by the black-box CICD case."""

from __future__ import annotations

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
            handler = json.dumps(
                {
                    "source": (
                        "def handle(request):\n"
                        '    return {"message": "hello from pi"}\n'
                    ),
                    "description": "CICD Pi handler",
                },
                separators=(",", ":"),
            )
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
