"""ASGI entry point."""

import hashlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from config import load_settings
from self_grow_agent.api import create_app

settings = load_settings()
_logger = logging.getLogger("uvicorn.error")


def _management_key_log_context(management_api_key: str) -> str:
    """Return a non-secret identifier for the configured management key."""

    if not management_api_key:
        return "not configured"
    fingerprint = hashlib.sha256(management_api_key.encode("utf-8")).hexdigest()[:12]
    return f"configured (fingerprint=sha256:{fingerprint}, suffix=***{management_api_key[-4:]})"


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Record safe key-identification data after Uvicorn logging is ready."""

    _logger.info("Management API key %s", _management_key_log_context(settings.management_api_key))
    yield


app = create_app(settings=settings, lifespan=_lifespan)


def run() -> None:
    """Run the service using host and port values loaded by ``config.py``."""

    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    run()
