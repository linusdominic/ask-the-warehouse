"""ASGI entry point: ``uvicorn askwh.main:app``."""

import logging
import os

from askwh.api import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

app = create_app()


def serve() -> None:
    import uvicorn

    uvicorn.run("askwh.main:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8010")))
