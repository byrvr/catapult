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
    for lib in ("httpx", "httpcore", "zeroconf", "hpack", "h2", "python_multipart"):
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


def _install_launch_agent(port: int):
    """Install a macOS LaunchAgent so Catapult auto-starts at login."""
    import shutil, subprocess
    from pathlib import Path

    python = shutil.which("python3") or sys.executable
    # Prefer the venv python if we're in one
    venv_python = Path(sys.executable)
    if venv_python.exists():
        python = str(venv_python)

    label = "com.catapult.server"
    plist_dir = Path.home() / "Library/LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist_path = plist_dir / f"{label}.plist"

    script_path = Path(__file__).parent.parent / "run.py"
    log_path = Path.home() / ".catapult/server.log"

    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>{script_path}</string>
        <string>--serve</string>
        <string>--port</string>
        <string>{port}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{log_path}</string>
</dict>
</plist>"""

    plist_path.write_text(plist)
    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    subprocess.run(["launchctl", "load", str(plist_path)], capture_output=True)
    print(f"LaunchAgent installed: {plist_path}")
    print("Catapult will now start automatically at login and run on port", port)


def main():
    parser = argparse.ArgumentParser(description="Catapult — sideload apps to iOS/tvOS devices")
    parser.add_argument("--browser", action="store_true", help="Open in browser instead of native window")
    parser.add_argument("--serve", action="store_true", help="Run as headless background server (no window)")
    parser.add_argument("--install-agent", action="store_true", help="Install macOS LaunchAgent for auto-start at login")
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Server port (default: 9450)")
    args = parser.parse_args()

    _configure_logging(args.verbose)

    if args.install_agent:
        _install_launch_agent(args.port)
        return

    if args.serve:
        logging.getLogger("catapult").info("Starting Catapult server on port %d", args.port)
        _run_server(DEFAULT_HOST, args.port)
    elif args.browser:
        _open_in_browser(DEFAULT_HOST, args.port)
    else:
        _open_native(DEFAULT_HOST, args.port)


if __name__ == "__main__":
    main()
