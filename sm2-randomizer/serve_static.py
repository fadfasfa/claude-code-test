from __future__ import annotations

import os
import socket
import webbrowser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
APP_DIR = PROJECT_DIR / "app"
DEFAULT_PORT = 0
DEFAULT_HOST = "127.0.0.1"


class DebugEntryHandler(SimpleHTTPRequestHandler):
    def do_GET(self) -> None:  # pragma: no cover - runtime wiring
        path = self.path.split("?", 1)[0]

        if path == "/" or path == "":
            self.path = "/app/static/"
        elif path.startswith("/sm2-randomizer/"):
            self.path = path[len("/sm2-randomizer"):]
            if not self.path.startswith("/"):
                self.path = f"/{self.path}"

        return super().do_GET()


def should_open_browser() -> bool:
    value = os.getenv("SM2_DEBUG_NO_BROWSER", "").strip().lower()
    return value not in {"1", "true", "yes", "on"}


def create_server(
    host: str,
    preferred_port: int,
    handler: partial[DebugEntryHandler],
) -> tuple[ThreadingHTTPServer, bool]:
    try:
        return ThreadingHTTPServer((host, preferred_port), handler), False
    except OSError:
        return ThreadingHTTPServer((host, 0), handler), True


def main() -> None:
    requested_port = os.getenv("SM2_DEBUG_PORT")
    preferred_port = int(requested_port) if requested_port else DEFAULT_PORT
    host = os.getenv("SM2_DEBUG_HOST", DEFAULT_HOST)
    handler = partial(DebugEntryHandler, directory=str(PROJECT_DIR))
    server, port_changed = create_server(host, preferred_port, handler)
    with server:
        port = int(server.server_address[1])
        app_url = f"http://{host}:{port}/app/static/"
        root_url = f"http://{host}:{port}/"
        print(f"Serving SM2 Randomizer from: {PROJECT_DIR}")
        if requested_port and port_changed:
            print(f"Port {preferred_port} is busy, using {port} instead.")
        elif not requested_port:
            print(f"No fixed port requested, using {port}.")
        print(f"App URL:  {app_url}")
        print(f"Root URL: {root_url}")
        if should_open_browser():
            webbrowser.open(app_url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
