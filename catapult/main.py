import threading
import uvicorn
import webview

from catapult.server import app


def start_server():
    uvicorn.run(app, host="127.0.0.1", port=9450, log_level="warning")


def main():
    server_thread = threading.Thread(target=start_server, daemon=True)
    server_thread.start()
    webview.create_window("Catapult", "http://127.0.0.1:9450", width=900, height=640)
    webview.start()


if __name__ == "__main__":
    main()
