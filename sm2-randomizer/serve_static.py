from __future__ import annotations

import atexit
import os
import sys
import threading
import time
import traceback
import webbrowser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit
import mimetypes
import shutil

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_PORT = 0
DEFAULT_HOST = "127.0.0.1"
PACKAGED_SENTINELS = ("static", "data", "assets")
LOG_FILE_NAME = "sm2-randomizer-launch.log"
LOG_RETENTION_DAYS = 7
LOG_RETENTION_SECONDS = LOG_RETENTION_DAYS * 24 * 60 * 60
_LOG_HANDLE = None
_ORIGINAL_PRINT = print


def is_frozen_runtime() -> bool:
    return bool(getattr(sys, "frozen", False))


def resolve_runtime_base_dir() -> Path:
    return Path(sys.executable).resolve().parent if is_frozen_runtime() else PROJECT_DIR


def resolve_web_root() -> Path:
    if is_frozen_runtime():
        packaged_dir = Path(sys.executable).resolve().parent
        if all((packaged_dir / name).exists() for name in PACKAGED_SENTINELS):
            return packaged_dir

    configured_root = os.getenv("SM2_WEB_ROOT", "").strip()
    if configured_root:
        return Path(configured_root).resolve()

    return PROJECT_DIR


def cleanup_expired_logs(runtime_dir: Path) -> None:
    now = time.time()
    for log_path in runtime_dir.glob("sm2-randomizer-launch*.log"):
        try:
            if now - log_path.stat().st_mtime > LOG_RETENTION_SECONDS:
                log_path.unlink()
        except OSError:
            continue


def initialize_launch_logging() -> Path:
    global _LOG_HANDLE
    runtime_dir = resolve_runtime_base_dir()
    runtime_dir.mkdir(parents=True, exist_ok=True)
    cleanup_expired_logs(runtime_dir)
    log_path = runtime_dir / LOG_FILE_NAME
    _LOG_HANDLE = log_path.open("a", encoding="utf-8")

    def tee_print(*args, **kwargs):
        destination = kwargs.pop("file", None)
        if destination is None:
            destination = sys.stdout
        _ORIGINAL_PRINT(*args, file=destination, **kwargs)
        if _LOG_HANDLE is None:
            return
        end = kwargs.get("end", "\n")
        sep = kwargs.get("sep", " ")
        text = sep.join(str(arg) for arg in args)
        _LOG_HANDLE.write(text + end)
        _LOG_HANDLE.flush()

    globals()["print"] = tee_print
    sys.stdout = _LOG_HANDLE
    sys.stderr = _LOG_HANDLE
    print(f"Process started in PID={os.getpid()}")
    print(f"Frozen={is_frozen_runtime()}")
    print(f"Executable={sys.executable}")
    print(f"CWD={Path.cwd()}")
    print(f"LogFile={log_path}")
    return log_path


def close_launch_logging() -> None:
    global _LOG_HANDLE
    if _LOG_HANDLE is None:
        return
    try:
        _LOG_HANDLE.flush()
        _LOG_HANDLE.close()
    finally:
        _LOG_HANDLE = None


def log_exception(prefix: str, exc: BaseException) -> None:
    print(f"{prefix}: {exc}")
    print(traceback.format_exc().rstrip())


def is_packaged_root(web_root: Path) -> bool:
    return all((web_root / name).exists() for name in PACKAGED_SENTINELS)


def resolve_start_path(web_root: Path) -> str:
    return "/static/" if is_packaged_root(web_root) else "/app/static/"


def rewrite_request_path(path: str, web_root: Path) -> str:
    if path in {"", "/"}:
        return resolve_start_path(web_root)
    if path.startswith("/sm2-randomizer/"):
        trimmed = path[len("/sm2-randomizer"):]
        return trimmed if trimmed.startswith("/") else f"/{trimmed}"
    if is_packaged_root(web_root) and path.startswith("/app/"):
        trimmed = path[len("/app"):]
        return trimmed if trimmed.startswith("/") else f"/{trimmed}"
    return path


def resolve_request_target(path: str, web_root: Path) -> tuple[str, str]:
    path_only = urlsplit(path).path
    rewritten = rewrite_request_path(path_only, web_root)
    decoded = unquote(rewritten).lstrip("/")
    candidate = (web_root / decoded).resolve()
    try:
        candidate.relative_to(web_root)
    except ValueError as exc:
        raise PermissionError(f"Request path escapes web root: {path_only}") from exc
    normalized = "/" + candidate.relative_to(web_root).as_posix()
    if candidate.is_dir() and not normalized.endswith("/"):
        normalized += "/"
    return rewritten, normalized


def resolve_debug_local_path(relative_path: str, web_root: Path) -> Path:
    return (web_root / relative_path.lstrip("/")).resolve()


def resolve_packaged_path(path: str, web_root: Path) -> Path:
    rewritten, relative_path = resolve_request_target(path, web_root)
    if rewritten in {"", "/", "/static", "/static/"}:
        return web_root / "static" / "index.html"
    return resolve_debug_local_path(relative_path, web_root)


def resolve_source_path(path: str, web_root: Path) -> Path:
    _, relative_path = resolve_request_target(path, web_root)
    return resolve_debug_local_path(relative_path, web_root)


def resolve_http_local_path(path: str, web_root: Path) -> Path:
    return resolve_packaged_path(path, web_root) if is_packaged_root(web_root) else resolve_source_path(path, web_root)


def resolve_http_relative_path(path: str, web_root: Path) -> str:
    local_path = resolve_http_local_path(path, web_root).resolve()
    try:
        local_path.relative_to(web_root)
    except ValueError as exc:
        raise PermissionError(f"Request path escapes web root: {path}") from exc
    normalized = "/" + local_path.relative_to(web_root).as_posix()
    if local_path.is_dir() and not normalized.endswith("/"):
        normalized += "/"
    return normalized


def resolve_http_debug_message(path: str, web_root: Path) -> tuple[str, Path]:
    rewritten, _ = resolve_request_target(path, web_root)
    return rewritten, resolve_http_local_path(path, web_root)


def guess_content_type(local_path: Path) -> str:
    if local_path.suffix.lower() == ".json":
        return "application/json; charset=utf-8"
    return mimetypes.guess_type(str(local_path))[0] or "application/octet-stream"


def send_plain_error(handler: SimpleHTTPRequestHandler, code: int, message: str) -> None:
    body = f"{code} {message}\n".encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(body)


class DebugEntryHandler(SimpleHTTPRequestHandler):
    def translate_path(self, path: str) -> str:  # pragma: no cover - runtime wiring
        web_root = resolve_web_root()
        return str(resolve_http_local_path(path, web_root))

    def _serve_resolved_path(self, method: str) -> None:  # pragma: no cover - runtime wiring
        try:
            web_root = resolve_web_root()
            rewritten, local_path = resolve_http_debug_message(self.path, web_root)
            print(f"HTTP {method}: raw={self.path} rewritten={rewritten} local={local_path}")
            if local_path.is_dir():
                index_path = local_path / "index.html"
                if not index_path.exists():
                    send_plain_error(self, 404, "Directory listing is disabled")
                    return
                local_path = index_path
            if not local_path.exists() or not local_path.is_file():
                send_plain_error(self, 404, "File not found")
                return
            content_type = guess_content_type(local_path)
            stat_result = local_path.stat()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(stat_result.st_size))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if method == "HEAD":
                return
            with local_path.open("rb") as handle:
                shutil.copyfileobj(handle, self.wfile)
        except BrokenPipeError as exc:  # pragma: no cover - runtime wiring
            log_exception(f"HTTP {method} client disconnected", exc)
        except ConnectionResetError as exc:  # pragma: no cover - runtime wiring
            log_exception(f"HTTP {method} client reset", exc)
        except Exception as exc:  # pragma: no cover - runtime wiring
            log_exception(f"HTTP {method} failed", exc)
            try:
                send_plain_error(self, 500, "Internal server error")
            except Exception:
                pass

    def do_GET(self) -> None:  # pragma: no cover - runtime wiring
        self._serve_resolved_path("GET")

    def do_HEAD(self) -> None:  # pragma: no cover - runtime wiring
        self._serve_resolved_path("HEAD")

    def list_directory(self, path):  # pragma: no cover - runtime wiring
        print(f"HTTP 404: directory listing blocked for {path}")
        return self.send_error(404, "Directory listing is disabled")

    def send_error(self, code, message=None, explain=None):  # pragma: no cover - runtime wiring
        print(f"HTTP {code}: path={self.path} message={message or ''}")
        return super().send_error(code, message, explain)

    def do_POST(self) -> None:  # pragma: no cover - runtime wiring
        self.send_error(405, "Method not allowed")

    do_PUT = do_POST
    do_DELETE = do_POST
    do_OPTIONS = do_POST
    do_PATCH = do_POST
    do_CONNECT = do_POST
    do_TRACE = do_POST
    do_MKCOL = do_POST
    do_PROPFIND = do_POST
    do_PROPPATCH = do_POST
    do_COPY = do_POST
    do_MOVE = do_POST
    do_LOCK = do_POST
    do_UNLOCK = do_POST


class ReusableHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def should_open_browser() -> bool:
    value = os.getenv("SM2_DEBUG_NO_BROWSER", "").strip().lower()
    return value not in {"1", "true", "yes", "on"}


def open_browser(app_url: str) -> None:
    errors: list[str] = []
    if os.name == "nt":
        try:
            os.startfile(app_url)
            print(f"Opened browser via os.startfile: {app_url}")
            return
        except OSError as exc:
            errors.append(f"os.startfile failed: {exc}")
    try:
        opened = webbrowser.open(app_url)
        if opened:
            print(f"Opened browser via webbrowser.open: {app_url}")
            return
        errors.append("webbrowser.open returned False")
    except Exception as exc:
        errors.append(f"webbrowser.open failed: {exc}")
    raise RuntimeError("; ".join(errors) or "unknown browser launch failure")


def create_server(host: str, preferred_port: int, handler: partial[DebugEntryHandler]) -> tuple[ThreadingHTTPServer, bool]:
    base_port = preferred_port if preferred_port != 0 else 53231
    for offset in range(20):
        port = base_port + offset
        try:
            return ReusableHTTPServer((host, port), handler), (port != preferred_port)
        except OSError:
            continue
    raise RuntimeError(f"无法在 {base_port} 到 {base_port + 19} 之间找到可用的端口。")


def main() -> None:
    requested_port = os.getenv("SM2_DEBUG_PORT")
    preferred_port = int(requested_port) if requested_port else DEFAULT_PORT
    host = os.getenv("SM2_DEBUG_HOST", DEFAULT_HOST)
    web_root = resolve_web_root()
    handler = partial(DebugEntryHandler, directory=str(web_root))
    server, port_changed = create_server(host, preferred_port, handler)

    with server:
        port = int(server.server_address[1])
        start_path = resolve_start_path(web_root)
        app_url = f"http://{host}:{port}{start_path}"
        root_url = f"http://{host}:{port}/"
        print(f"Serving SM2 Randomizer from: {web_root}")
        print(f"Mode: {'packaged' if is_packaged_root(web_root) else 'source'}")
        if requested_port and port_changed:
            print(f"Port {preferred_port} is busy, using {port} instead.")
        elif not requested_port:
            print(f"No fixed port requested, using {port}.")
        print(f"App URL:  {app_url}")
        print(f"Root URL: {root_url}")

        serve_thread = threading.Thread(target=server.serve_forever, daemon=True, name="sm2-static-server")
        serve_thread.start()
        time.sleep(0.5)

        if should_open_browser():
            try:
                open_browser(app_url)
            except Exception as exc:
                log_exception("Browser launch failed", exc)
                server.shutdown()
                serve_thread.join(timeout=2)
                raise SystemExit(1) from exc

        try:
            serve_thread.join()
        except KeyboardInterrupt:
            print("Shutting down SM2 Randomizer server...")
            server.shutdown()
            serve_thread.join(timeout=2)
        except Exception as exc:
            log_exception("Unhandled serve thread error", exc)
            server.shutdown()
            serve_thread.join(timeout=2)
            raise


atexit.register(close_launch_logging)
initialize_launch_logging()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log_exception("Fatal launcher error", exc)
        raise
