"""Catapult entry point — starts API server and opens the native window."""

import logging
import sys
import threading

import uvicorn
import webview

from catapult.server import app

LOG_FORMAT = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"


def _configure_logging():
    logging.basicConfig(
        level=logging.INFO,
        format=LOG_FORMAT,
        stream=sys.stderr,
    )
    # Quiet noisy libs
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("zeroconf").setLevel(logging.WARNING)


def _start_server():
    uvicorn.run(app, host="127.0.0.1", port=9450, log_level="warning")


def main():
    _configure_logging()
    logger = logging.getLogger("catapult")
    logger.info("Starting Catapult v0.1.0")

    server = threading.Thread(target=_start_server, daemon=True)
    server.start()

    webview.create_window(
        "Catapult",
        "http://127.0.0.1:9450",
        width=900,
        height=640,
        min_size=(480, 400),
    )
    webview.start()


if __name__ == "__main__":
    main()
