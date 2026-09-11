"""DataService loopback HTTP 适配器。"""

from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from typing import Any, Mapping


def build_data_service_handler(
    application: Any,
    *,
    nonce_header: str,
) -> type[BaseHTTPRequestHandler]:
    """构造只接受 loopback Host 与本进程 nonce 的控制面 handler。"""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _authorized(self) -> bool:
            host = self.headers.get("Host", "").split(":", 1)[0].strip("[]").lower()
            return (
                host in {"127.0.0.1", "localhost", "::1"}
                and self.headers.get(nonce_header) == application.nonce
            )

        def _body(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            except (ValueError, json.JSONDecodeError):
                return {}
            return payload if isinstance(payload, dict) else {}

        def _send(self, status: int, payload: Mapping[str, Any]) -> None:
            body = json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if not self._authorized():
                self._send(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
            elif self.path == "/v1/status":
                self._send(HTTPStatus.OK, application.status())
            else:
                self._send(HTTPStatus.NOT_FOUND, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorized():
                self._send(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
                return
            if self.path == "/v1/actions/refresh":
                result = application.submit_action("refresh", self._body())
                self._send(HTTPStatus.ACCEPTED if result.get("accepted") else HTTPStatus.CONFLICT, result)
            elif self.path == "/v1/actions/set-private-stats":
                result = application.submit_action("set_private_stats", self._body())
                self._send(HTTPStatus.ACCEPTED if result.get("accepted") else HTTPStatus.CONFLICT, result)
            elif self.path == "/v1/shutdown":
                application.request_shutdown()
                self._send(HTTPStatus.OK, {"state": "shutting_down"})
            else:
                self._send(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    return Handler


__all__ = ["build_data_service_handler"]
