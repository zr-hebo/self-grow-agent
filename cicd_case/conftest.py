"""Real-process fixtures shared by the CICD integration cases."""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Iterator, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MANAGEMENT_KEY = secrets.token_urlsafe(32)
INCORRECT_MANAGEMENT_KEY = secrets.token_urlsafe(32)
while INCORRECT_MANAGEMENT_KEY == MANAGEMENT_KEY:
    INCORRECT_MANAGEMENT_KEY = secrets.token_urlsafe(32)
LLM_API_KEY = secrets.token_urlsafe(32)
LLM_MODEL = "cicd-stub-model"
MAX_STUB_REQUEST_BYTES = 1024 * 1024
EXPECTED_RESPONSE_FORMAT = {
    "type": "json_schema",
    "name": "generated_handler",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "source": {"type": "string", "minLength": 1},
            "description": {"type": "string"},
        },
        "required": ["source", "description"],
    },
    "strict": True,
}


class OpenAIResponsesStub:
    """A loopback HTTP server implementing the small Responses API surface we use."""

    def __init__(self) -> None:
        self._responses: deque[dict[str, str]] = deque()
        self._lock = threading.Lock()
        self.requests: list[dict[str, Any]] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        if self._server is None:
            raise RuntimeError("OpenAI stub is not running")
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/v1"

    @property
    def running(self) -> bool:
        return self._server is not None

    def start(self) -> None:
        if self._server is not None:
            return

        owner = self

        class ResponsesHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
                owner._handle_response_request(self)

            def log_message(self, format: str, *args: object) -> None:
                del format, args

        server = ThreadingHTTPServer(("127.0.0.1", 0), ResponsesHandler)
        server.daemon_threads = True
        thread = threading.Thread(
            target=server.serve_forever,
            name="cicd-openai-stub",
            daemon=True,
        )
        self._server = server
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        server = self._server
        thread = self._thread
        if server is None:
            return
        self._server = None
        self._thread = None
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=3)
            if thread.is_alive():
                raise RuntimeError("OpenAI stub thread did not stop")

    def enqueue_handler(self, source: str, description: str = "") -> None:
        with self._lock:
            self._responses.append({"source": source, "description": description})

    def _handle_response_request(self, handler: BaseHTTPRequestHandler) -> None:
        content_type = handler.headers.get("Content-Type", "").partition(";")[0].strip().lower()
        if content_type != "application/json":
            self._send_json(handler, 415, {"error": {"message": "expected JSON request"}})
            return

        try:
            content_length = int(handler.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(handler, 400, {"error": {"message": "invalid JSON"}})
            return
        if content_length <= 0:
            self._send_json(handler, 400, {"error": {"message": "empty request body"}})
            return
        if content_length > MAX_STUB_REQUEST_BYTES:
            self._send_json(handler, 413, {"error": {"message": "request body too large"}})
            return
        try:
            body = json.loads(handler.rfile.read(content_length))
        except (ValueError, json.JSONDecodeError):
            self._send_json(handler, 400, {"error": {"message": "invalid JSON"}})
            return
        if not isinstance(body, dict):
            self._send_json(handler, 400, {"error": {"message": "expected JSON object"}})
            return

        authorization = handler.headers.get("Authorization")
        response_format = body.get("text", {}).get("format", {})
        if (
            handler.path != "/v1/responses"
            or authorization != f"Bearer {LLM_API_KEY}"
            or body.get("model") != LLM_MODEL
            or body.get("store") is not False
            or response_format != EXPECTED_RESPONSE_FORMAT
        ):
            self._send_json(
                handler,
                400,
                {"error": {"message": "unexpected Responses API request"}},
            )
            return

        with self._lock:
            self.requests.append(body)
            generated = self._responses.popleft() if self._responses else None

        if generated is None:
            self._send_json(
                handler,
                409,
                {"error": {"message": "no stub response was queued"}},
            )
            return

        output_text = json.dumps(generated, separators=(",", ":"))
        payload = {
            "id": f"resp_cicd_{len(self.requests)}",
            "object": "response",
            "created_at": int(time.time()),
            "model": LLM_MODEL,
            "parallel_tool_calls": False,
            "tool_choice": "auto",
            "tools": [],
            "output": [
                {
                    "id": f"msg_cicd_{len(self.requests)}",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {
                            "type": "output_text",
                            "text": output_text,
                            "annotations": [],
                        }
                    ],
                }
            ],
        }
        self._send_json(handler, 200, payload)

    @staticmethod
    def _send_json(
        handler: BaseHTTPRequestHandler,
        status_code: int,
        payload: dict[str, Any],
    ) -> None:
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        handler.send_response(status_code)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(encoded)))
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(encoded)


class AgentStack:
    """Own one agent process, its client, generated state, and the LLM stub."""

    def __init__(self, tmp_path: Path) -> None:
        self.generated_dir = tmp_path / "generated"
        self.service_log_path = tmp_path / "agent-service.log"
        self.stub = OpenAIResponsesStub()
        self.process: subprocess.Popen[str] | None = None
        self.client: httpx.Client | None = None
        self._service_log = None
        self._port: int | None = None

    @property
    def pid(self) -> int:
        if self.process is None or self.process.poll() is not None:
            raise RuntimeError("agent process is not running")
        return self.process.pid

    @property
    def management_headers(self) -> dict[str, str]:
        return {"X-Management-Key": MANAGEMENT_KEY}

    @property
    def incorrect_management_headers(self) -> dict[str, str]:
        return {"X-Management-Key": INCORRECT_MANAGEMENT_KEY}

    def start(
        self,
        *,
        llm_api_key: str = LLM_API_KEY,
        extra_env: Mapping[str, str] | None = None,
    ) -> None:
        if self.process is not None:
            raise RuntimeError("agent process is already running")
        if llm_api_key and not self.stub.running:
            self.stub.start()

        env = {
            name: os.environ[name]
            for name in ("PATH", "SYSTEMROOT", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL")
            if name in os.environ
        }
        env.update(
            {
                "HOST": "127.0.0.1",
                "MANAGEMENT_API_KEY": MANAGEMENT_KEY,
                "LLM_API_KEY": llm_api_key,
                "LLM_BASE_URL": (
                    self.stub.base_url if self.stub.running else "http://127.0.0.1:1/v1"
                ),
                "LLM_MODEL": LLM_MODEL,
                "LLM_TIMEOUT_SECONDS": "3",
                "GENERATED_DIR": str(self.generated_dir),
                "HANDLER_TIMEOUT_SECONDS": "3",
                "MAX_CONCURRENT_HANDLERS": "8",
                "HANDLER_ADMISSION_TIMEOUT_SECONDS": "3",
                "NO_PROXY": "127.0.0.1,localhost",
                "no_proxy": "127.0.0.1,localhost",
                "PYTHONUNBUFFERED": "1",
            }
        )
        if extra_env is not None:
            env.update(extra_env)

        last_port_error: RuntimeError | None = None
        for _attempt in range(3):
            self._port = _unused_tcp_port()
            env["PORT"] = str(self._port)
            self._service_log = self.service_log_path.open("a", encoding="utf-8", buffering=1)
            self.process = subprocess.Popen(
                [sys.executable, "main.py"],
                cwd=PROJECT_ROOT,
                env=env,
                stdout=self._service_log,
                stderr=subprocess.STDOUT,
                text=True,
            )
            self.client = httpx.Client(
                base_url=f"http://127.0.0.1:{self._port}",
                timeout=5,
                trust_env=False,
            )
            try:
                self._wait_until_healthy()
                return
            except RuntimeError as exc:
                self.stop_service()
                if "address already in use" not in str(exc).lower():
                    raise
                last_port_error = exc

        raise RuntimeError("agent could not reserve a TCP port after 3 attempts") from last_port_error

    def stop_service(self) -> None:
        client = self.client
        process = self.process
        service_log = self._service_log
        self.client = None
        self.process = None
        self._service_log = None

        if client is not None:
            client.close()
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        elif process is not None:
            process.wait(timeout=1)
        if service_log is not None:
            service_log.close()

    def restart_without_llm(self) -> None:
        self.stop_service()
        self.stub.stop()
        self.start(llm_api_key="")

    def close(self) -> None:
        try:
            self.stop_service()
        finally:
            self.stub.stop()

    def require_client(self) -> httpx.Client:
        if self.client is None:
            raise RuntimeError("agent HTTP client is not available")
        return self.client

    def _wait_until_healthy(self) -> None:
        deadline = time.monotonic() + 15
        last_error = "service did not accept requests"
        while time.monotonic() < deadline:
            process = self.process
            if process is None:
                last_error = "service process was not created"
                break
            if process.poll() is not None:
                last_error = f"service exited with code {process.returncode}"
                break
            try:
                response = self.require_client().get("/healthz", timeout=0.5)
                if response.status_code == 200 and response.json() == {"status": "ok"}:
                    return
                last_error = f"health returned {response.status_code}: {response.text}"
            except (httpx.HTTPError, ValueError) as exc:
                last_error = str(exc)
            time.sleep(0.1)

        if self._service_log is not None:
            self._service_log.flush()
        diagnostics = ""
        if self.service_log_path.exists():
            diagnostics = self.service_log_path.read_text(encoding="utf-8")[-4000:]
        raise RuntimeError(f"{last_error}\nagent log:\n{diagnostics}")


def _unused_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def agent_stack(tmp_path: Path) -> Iterator[AgentStack]:
    stack = AgentStack(tmp_path)
    try:
        stack.start()
        yield stack
    finally:
        try:
            stack.close()
        finally:
            if stack.service_log_path.exists():
                service_log = stack.service_log_path.read_text(encoding="utf-8", errors="replace")
                print(f"\n--- agent service log ---\n{service_log}--- end agent service log ---")
