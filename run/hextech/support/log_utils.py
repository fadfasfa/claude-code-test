"""日志与终端编码工具。

集中管理摘要日志、错误日志、开发态 JSONL 诊断日志和结构化事件写入。
开发态可以写更重的本地诊断文件；冻结/打包态只保留摘要、错误和轻量导出所需事件。

调用方: core.refresh、display.web.runtime、scraping.hextech.scraper; 关键依赖: catalog.runtime_store。
"""

from __future__ import annotations

import logging
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from logging.handlers import RotatingFileHandler
from typing import Literal, Optional


NOISY_MESSAGE_PATTERNS = (
    "GET /assets/",
    "提取英雄：",
    "提取到协同方案：",
    "开始提取海克斯协同方案：",
    "开始爬取英雄列表：",
    "正在加载页面：",
    "找到 172 个英雄卡片",
    "已清理过期/残留文件：",
    "[重试成功]",
    "[重试失败]",
    "[重试中]",
    "缺失资源列表",
    "共缺失 ",
    "已检测到 LCU 连接，端口=",
    "协同数据缓存已刷新",
    "CSV 已更新：",
)
SENSITIVE_KEYWORDS = (
    "auth",
    "authorization",
    "access_token",
    "refresh_token",
    "session_token",
    "session_id",
    "token",
    "cookie",
    "secret",
    "password",
    "credential",
    "nonce",
    "api_key",
    "apikey",
    "jwt",
    "bearer",
    "x-hextech-token",
    "lcu",
    "riot",
)
_RUN_ID = f"run-{uuid.uuid4().hex}"
_RUNTIME_EVENT_SCHEMA_VERSION = 1


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class SummaryOnlyFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for pattern in NOISY_MESSAGE_PATTERNS:
            if pattern in message:
                return False
        return True


class SourceNameFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        logger_name = str(record.name or "root").strip()
        record.source = logger_name.rsplit(".", 1)[-1] if logger_name else "root"
        return True


class MaxLevelFilter(logging.Filter):
    def __init__(self, max_level: int):
        super().__init__()
        self.max_level = max_level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno <= self.max_level


class RuntimeContextFilter(logging.Filter):
    def __init__(self, profile: str):
        super().__init__()
        self.profile = profile

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = getattr(record, "run_id", _RUN_ID)
        record.profile = getattr(record, "profile", self.profile)
        record.frozen = bool(getattr(sys, "frozen", False))
        record.component = redact_log_value(getattr(record, "component", "")) if hasattr(record, "component") else ""
        record.event = redact_log_value(getattr(record, "event", "")) if hasattr(record, "event") else ""
        record.correlation_id = (
            redact_log_value(getattr(record, "correlation_id", "")) if hasattr(record, "correlation_id") else ""
        )
        record.duration_ms = getattr(record, "duration_ms", "")
        return True


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact_log_value(record.getMessage())
            record.args = ()
        except Exception:
            pass
        return True


class JsonlLogFormatter(logging.Formatter):
    """把 LogRecord 输出为单行 JSON，便于开发态 grep、jq 或后续聚合分析。"""

    def format(self, record: logging.LogRecord) -> str:
        if record.exc_info:
            error_type = record.exc_info[0].__name__
            error_message = redact_log_value(record.exc_info[1])
        else:
            error_type = redact_log_value(getattr(record, "error_type", ""))
            error_message = redact_log_value(getattr(record, "error_message_sanitized", ""))
        payload = {
            "timestamp": _utc_now_iso(),
            "level": record.levelname,
            "logger": record.name,
            "component": redact_log_value(getattr(record, "component", "")),
            "event": redact_log_value(getattr(record, "event", "")),
            "message": redact_log_value(record.getMessage()),
            "run_id": getattr(record, "run_id", _RUN_ID),
            "pid": record.process,
            "thread": record.threadName,
            "profile": getattr(record, "profile", ""),
            "frozen": bool(getattr(record, "frozen", False)),
            "correlation_id": redact_log_value(getattr(record, "correlation_id", "")),
            "duration_ms": getattr(record, "duration_ms", ""),
            "error_type": error_type,
            "error_message_sanitized": error_message,
        }
        if record.exc_info:
            payload["exception"] = redact_log_value(super().formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class RedactingTextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact_log_value(super().format(record))


class RuntimeRotatingFileHandler(RotatingFileHandler):
    """Windows 上轮转文件可能被其他进程短暂占用；失败时保留本次日志写入。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if self.shouldRollover(record):
                try:
                    self.doRollover()
                except OSError:
                    pass
            logging.FileHandler.emit(self, record)
        except Exception:
            self.handleError(record)


def get_unified_log_file() -> str:
    return get_runtime_summary_log_file()


def _packaged_runtime_base_dir() -> Path:
    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data) / "HextechNexus"
    app_data = os.getenv("APPDATA", "").strip()
    if app_data:
        return Path(app_data) / "HextechNexus"
    return Path.home() / ".hextech_nexus"


def _runtime_root_dir() -> Path:
    try:
        from hextech.catalog.runtime_store import get_runtime_root_dir

        return get_runtime_root_dir()
    except Exception:
        if getattr(sys, "frozen", False):
            return _packaged_runtime_base_dir() / "data" / "runtime"
        return Path(__file__).resolve().parents[2] / "data" / "runtime"


def _runtime_log_dir() -> Path:
    if getattr(sys, "frozen", False):
        log_dir = _packaged_runtime_base_dir() / "data" / "runtime" / "logs"
    else:
        log_dir = _runtime_root_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def get_runtime_log_paths() -> dict[str, Path]:
    log_dir = _runtime_log_dir()
    return {
        "root": _runtime_root_dir(),
        "logs": log_dir,
        "summary": log_dir / "hextech_runtime_summary.log",
        "error": log_dir / "hextech_error.log",
        "full": log_dir / "dev" / "hextech_full.jsonl",
        "events": _runtime_root_dir() / "state" / "runtime_events.v1.jsonl",
        "supervisor_events": _runtime_root_dir() / "state" / "supervisor_events.v1.jsonl",
    }


def get_runtime_summary_log_file() -> str:
    return str(get_runtime_log_paths()["summary"])


def get_error_log_file() -> str:
    return str(get_runtime_log_paths()["error"])


def redact_log_value(value: object) -> str:
    text = str(value or "")
    text = re.sub(r"(https?://[^\s?]+)\?[^\s]+", r"\1?<redacted>", text, flags=re.IGNORECASE)
    text = re.sub(
        r"(/(?:auth|token|session|jwt|bearer)/)[^/?#\s,;]+",
        r"\1<redacted>",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\b(Proxy-Authorization|Authorization)\s*:\s*[^,\n;{}]+",
        r"\1: <redacted>",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\bBearer\s+[^\s,;]+", "Bearer <redacted>", text, flags=re.IGNORECASE)
    text = re.sub(r"\bX-Hextech-Token\s*:\s*[^,\s;]+", "X-Hextech-Token: <redacted>", text, flags=re.IGNORECASE)
    text = re.sub(r"\bCookie\s*:\s*[^,\n]+", "Cookie: <redacted>", text, flags=re.IGNORECASE)
    text = re.sub(r"Set-Cookie:\s*[^,\n;]+(?:;[^\n,]*)?", "Set-Cookie: <redacted>", text, flags=re.IGNORECASE)
    text = re.sub(
        r"\b(auth|authorization|cookie|(?:access|refresh|session)[_-]?token|session[_-]?id|token|nonce|secret|password|credential|api[_-]?key|jwt|bearer|lcu|riot)=([^,\s;]+)",
        r"\1=<redacted>",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\b(auth|authorization|cookie|(?:access|refresh|session)[_-]?token|session[_-]?id|token|nonce|secret|password|credential|api[_-]?key|jwt|bearer|x[-_]?hextech[-_]?token|lcu|riot)\s*:\s*([^,\s;{}]+)",
        r"\1: <redacted>",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r'("?(?:auth|authorization|cookie|(?:access|refresh|session)[_-]?token|session[_-]?id|token|nonce|secret|password|credential|api[_-]?key|jwt|bearer|lcu|riot)"?\s*:\s*)"[^"]*"',
        r'\1"<redacted>"',
        text,
        flags=re.IGNORECASE,
    )
    return text


def _redact_event_value(key: str, value: object) -> object:
    key_lower = str(key or "").lower()
    if any(keyword in key_lower for keyword in SENSITIVE_KEYWORDS):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(child_key): _redact_event_value(str(child_key), child_value) for child_key, child_value in value.items()}
    if isinstance(value, list):
        return [_redact_event_value(key, item) for item in value]
    if isinstance(value, tuple):
        return [_redact_event_value(key, item) for item in value]
    if isinstance(value, str):
        return redact_log_value(value)
    return value


def ensure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:
            pass


def _resolve_log_profile(profile: Literal["dev", "packaged", "test"] | None) -> str:
    if profile:
        return profile
    env_profile = os.getenv("HEXTECH_LOG_PROFILE", "").strip().lower()
    if env_profile in {"dev", "packaged", "test"}:
        return env_profile
    return "packaged" if getattr(sys, "frozen", False) else "dev"


def _add_common_filters(handler: logging.Handler, *, profile: str) -> None:
    for filter_obj in (RuntimeContextFilter(profile), SourceNameFilter(), RedactingFilter()):
        if not any(type(existing) is type(filter_obj) for existing in handler.filters):
            handler.addFilter(filter_obj)


def _remove_hextech_runtime_handlers(root: logging.Logger, paths: dict[str, Path] | None = None) -> None:
    legacy_log_paths = set()
    if paths:
        legacy_log_paths.update({str(paths["summary"].resolve()), str(paths["error"].resolve())})
    for handler in list(root.handlers):
        handler_path = ""
        if isinstance(handler, logging.FileHandler):
            try:
                handler_path = str(Path(handler.baseFilename).resolve())
            except (OSError, TypeError, ValueError):
                handler_path = ""
        if getattr(handler, "_hextech_runtime_logging", False) or (handler_path and handler_path in legacy_log_paths):
            root.removeHandler(handler)
            handler.close()


def _runtime_handler_names(root: logging.Logger) -> set[str]:
    return {
        str(getattr(handler, "_hextech_handler_name", ""))
        for handler in root.handlers
        if getattr(handler, "_hextech_runtime_logging", False)
    }


def _expected_runtime_handler_names(profile: str) -> set[str]:
    names = {"runtime_summary", "runtime_error", "runtime_stream"}
    if profile == "dev":
        names.add("dev_full_jsonl")
    return names


def _new_rotating_handler(
    path: Path,
    *,
    name: str,
    level: int,
    formatter: logging.Formatter,
    max_bytes: int = 1024 * 1024,
    backup_count: int = 3,
    profile: str,
) -> RotatingFileHandler:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RuntimeRotatingFileHandler(path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")
    handler.setLevel(level)
    handler.setFormatter(formatter)
    handler._hextech_runtime_logging = True  # type: ignore[attr-defined]
    handler._hextech_handler_name = name  # type: ignore[attr-defined]
    handler._hextech_preserve_level = True  # type: ignore[attr-defined]
    _add_common_filters(handler, profile=profile)
    return handler


def _new_stream_handler(*, name: str, level: int, formatter: logging.Formatter, profile: str) -> logging.Handler:
    handler = logging.StreamHandler()
    handler.setLevel(level)
    handler.setFormatter(formatter)
    handler._hextech_runtime_logging = True  # type: ignore[attr-defined]
    handler._hextech_handler_name = name  # type: ignore[attr-defined]
    handler._hextech_preserve_level = True  # type: ignore[attr-defined]
    _add_common_filters(handler, profile=profile)
    return handler


def install_runtime_logging(profile: Literal["dev", "packaged", "test"] | None = None) -> None:
    resolved_profile = _resolve_log_profile(profile)
    root = logging.getLogger()
    if (
        getattr(root, "_hextech_runtime_logging_profile", None) == resolved_profile
        and _expected_runtime_handler_names(resolved_profile).issubset(_runtime_handler_names(root))
    ):
        return

    paths = get_runtime_log_paths()
    _remove_hextech_runtime_handlers(root, paths)
    root.setLevel(logging.DEBUG)

    summary_formatter = RedactingTextFormatter("%(asctime)s [%(source)s] %(message)s")
    json_formatter = JsonlLogFormatter()

    summary_handler = _new_rotating_handler(
        paths["summary"],
        name="runtime_summary",
        level=logging.INFO,
        formatter=summary_formatter,
        profile=resolved_profile,
    )
    summary_handler.addFilter(SummaryOnlyFilter())
    error_handler = _new_rotating_handler(
        paths["error"],
        name="runtime_error",
        level=logging.WARNING,
        formatter=summary_formatter,
        profile=resolved_profile,
    )
    stream_handler = _new_stream_handler(
        name="runtime_stream",
        level=logging.WARNING,
        formatter=summary_formatter,
        profile=resolved_profile,
    )
    root.addHandler(summary_handler)
    root.addHandler(error_handler)
    root.addHandler(stream_handler)

    if resolved_profile == "dev":
        full_handler = _new_rotating_handler(
            paths["full"],
            name="dev_full_jsonl",
            level=logging.DEBUG,
            formatter=json_formatter,
            max_bytes=10 * 1024 * 1024,
            backup_count=5,
            profile=resolved_profile,
        )
        root.addHandler(full_handler)

    root._hextech_runtime_logging_profile = resolved_profile  # type: ignore[attr-defined]


def install_summary_logging(
    *,
    level: int = logging.DEBUG,
    handlers: Optional[list[logging.Handler]] = None,
    fmt: str = "%(asctime)s [%(source)s] %(message)s",
) -> None:
    root = logging.getLogger()
    root.setLevel(level)

    summary_filter = SummaryOnlyFilter()
    source_filter = SourceNameFilter()
    if not any(isinstance(existing, SummaryOnlyFilter) for existing in root.filters):
        root.addFilter(summary_filter)
    if not any(isinstance(existing, SourceNameFilter) for existing in root.filters):
        root.addFilter(source_filter)

    if handlers is None:
        if not root.handlers:
            logging.basicConfig(level=logging.WARNING, format=fmt)
        for handler in root.handlers:
            if getattr(handler, "_hextech_runtime_logging", False):
                continue
            handler.setFormatter(RedactingTextFormatter(fmt))
            if not getattr(handler, "_hextech_preserve_level", False):
                if isinstance(handler, logging.FileHandler):
                    handler.setLevel(logging.ERROR)
                else:
                    handler.setLevel(logging.WARNING)
            if not any(isinstance(existing, SummaryOnlyFilter) for existing in handler.filters):
                handler.addFilter(summary_filter)
            if not any(isinstance(existing, SourceNameFilter) for existing in handler.filters):
                handler.addFilter(source_filter)
        return

    logging.basicConfig(level=level, handlers=handlers, force=True)
    root = logging.getLogger()
    if hasattr(root, "_hextech_runtime_logging_profile"):
        delattr(root, "_hextech_runtime_logging_profile")
    if not any(isinstance(existing, SummaryOnlyFilter) for existing in root.filters):
        root.addFilter(summary_filter)
    if not any(isinstance(existing, SourceNameFilter) for existing in root.filters):
        root.addFilter(source_filter)
    for handler in root.handlers:
        if getattr(handler, "_hextech_runtime_logging", False):
            continue
        handler.setFormatter(RedactingTextFormatter(fmt))
        if not getattr(handler, "_hextech_preserve_level", False):
            if isinstance(handler, logging.FileHandler):
                handler.setLevel(logging.ERROR)
            else:
                handler.setLevel(logging.WARNING)
        if not any(isinstance(existing, SummaryOnlyFilter) for existing in handler.filters):
            handler.addFilter(summary_filter)
        if not any(isinstance(existing, SourceNameFilter) for existing in handler.filters):
            handler.addFilter(source_filter)


def write_structured_event(component: str, event: str, *, target_path: Path | None = None, **fields) -> None:
    """写入低频运行态结构化事件。

    这里面向 lifecycle、refresh、supervisor 等低频诊断事件；overlay/vision 热路径如需接入，应先改为队列或批量写入。
    """

    target = Path(target_path) if target_path is not None else get_runtime_log_paths()["events"]
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": _RUNTIME_EVENT_SCHEMA_VERSION,
        "timestamp": _utc_now_iso(),
        "level": str(fields.pop("level", "INFO") or "INFO"),
        "component": redact_log_value(component),
        "event": redact_log_value(event),
        "run_id": _RUN_ID,
        "pid": os.getpid(),
        "profile": _resolve_log_profile(None),
        "frozen": bool(getattr(sys, "frozen", False)),
    }
    for key, value in fields.items():
        payload[str(key)] = _redact_event_value(str(key), value)
    with open(target, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")


def log_task_summary(
    logger: logging.Logger,
    *,
    task: str,
    started_at: float,
    success: bool,
    detail: str = "",
) -> None:
    duration_ms = max(0.0, (time.time() - started_at) * 1000)
    status = "成功" if success else "失败"
    message = f"{task}: {status}"
    if detail:
        message = f"{message} | {detail}"
    if duration_ms:
        message = f"{message} | duration_ms={duration_ms:.2f}"
    if success:
        logger.info(message)
    else:
        logger.error(message)
