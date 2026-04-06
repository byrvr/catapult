"""Catapult entry point — starts API server and opens the UI."""

import argparse
import logging
import sys
import threading
import webbrowser

import uvicorn

LOG_FORMAT = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9450


def _configure_logging(verbose: bool = False):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format=LOG_FORMAT,
        stream=sys.stderr,
    )
    for lib in ("httpx", "httpcore", "zeroconf", "hpack", "h2"):
        logging.getLogger(lib).setLevel(logging.WARNING)


def _run_server(host: str, port: int):
    uvicorn.run("catapult.server:app", host=host, port=port, log_level="warning")


def _open_in_browser(host: str, port: int):
    url = f"http://{host}:{port}"
    logging.getLogger("catapult").info("Opening %s in browser", url)
    server = threading.Thread(target=_run_server, args=(host, port), daemon=True)
    server.start()
    webbrowser.open(url)
    try:
        server.join()
    except KeyboardInterrupt:
        pass


def _open_native(host: str, port: int):
    try:
        import webview
    except ImportError:
        logging.getLogger("catapult").warning("pywebview not installed — opening in browser")
        return _open_in_browser(host, port)

    url = f"http://{host}:{port}"
    logging.getLogger("catapult").info("Opening native window")
    server = threading.Thread(target=_run_server, args=(host, port), daemon=True)
    server.start()
    webview.create_window("Catapult", url, width=900, height=640, min_size=(480, 400))
    webview.start()


def main():
    parser = argparse.ArgumentParser(description="Catapult — sideload apps to iOS/tvOS devices")
    parser.add_argument("--browser", action="store_true", help="Open in browser instead of native window")
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Server port (default: 9450)")
    args = parser.parse_args()

    _configure_logging(args.verbose)

    if args.browser:
        _open_in_browser(DEFAULT_HOST, args.port)
    else:
        _open_native(DEFAULT_HOST, args.port)


if __name__ == "__main__":
    main()
