"""Entry point: ``python -m elegoo_bridge``."""

from __future__ import annotations

import logging

import uvicorn

from .config import load_config
from .server import create_app


def main() -> None:
    config = load_config()
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    app = create_app(config)
    uvicorn.run(
        app,
        host=config.bind,
        port=config.port,
        log_level=config.log_level.lower(),
        access_log=False,
        # The printer link and the SSE streams are long-lived; keep the loop calm on
        # a 512 MB board.
        timeout_keep_alive=65,
        # Without this, a restart deadlocks: uvicorn waits for open connections to
        # close before running the lifespan shutdown, but the SSE streams only end
        # *inside* that shutdown (store.close() sends their sentinel). One open
        # browser tab was enough to leave the old process alive, port closed, still
        # holding a WebSocket slot on the printer. Five seconds, then force close.
        timeout_graceful_shutdown=5,
    )


if __name__ == "__main__":
    main()
