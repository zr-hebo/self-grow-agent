"""ASGI entry point."""

import hashlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from config import Settings, load_settings
from self_grow_agent.api import create_app

settings = load_settings()
_logger = logging.getLogger("uvicorn.error")


def _management_key_log_context(management_api_key: str) -> str:
    """Return a non-secret identifier for the configured management key."""

    if not management_api_key:
        return "not configured"
    fingerprint = hashlib.sha256(management_api_key.encode("utf-8")).hexdigest()[:12]
    return f"configured (fingerprint=sha256:{fingerprint}, suffix=***{management_api_key[-4:]})"


def _runtime_configuration_log_context(runtime_settings: Settings) -> str:
    """Return effective operational settings without exposing credentials."""

    return (
        f"generation_backend={runtime_settings.generation_backend!r} "
        f"generation_key_configured={bool(runtime_settings.llm_api_key)} "
        f"llm_model={runtime_settings.llm_model!r} "
        f"llm_timeout_seconds={runtime_settings.llm_timeout_seconds:.3f} "
        f"pi_provider={runtime_settings.pi_provider!r} "
        f"pi_model={runtime_settings.pi_model!r} "
        f"pi_timeout_seconds={runtime_settings.pi_timeout_seconds:.3f} "
        f"pi_max_event_stream_bytes={runtime_settings.pi_max_event_stream_bytes} "
        f"pi_max_concurrent_runs={runtime_settings.pi_max_concurrent_runs} "
        f"pi_admission_timeout_seconds={runtime_settings.pi_admission_timeout_seconds:.3f} "
        f"handler_timeout_seconds={runtime_settings.handler_timeout_seconds:.3f} "
        f"max_concurrent_handlers={runtime_settings.max_concurrent_handlers} "
        f"handler_admission_timeout_seconds="
        f"{runtime_settings.handler_admission_timeout_seconds:.3f}"
    )


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Record safe key-identification data after Uvicorn logging is ready."""

    _logger.info("Management API key %s", _management_key_log_context(settings.management_api_key))
    _logger.info("Runtime configuration %s", _runtime_configuration_log_context(settings))
    yield


app = create_app(settings=settings, lifespan=_lifespan)


def run() -> None:
    """Run the service using host and port values loaded by ``config.py``."""

    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    run()
