"""ASGI entry point."""

import uvicorn

from config import load_settings
from self_grow_agent.api import create_app

settings = load_settings()
app = create_app(settings=settings)


def run() -> None:
    """Run the service using host and port values loaded by ``config.py``."""

    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    run()
