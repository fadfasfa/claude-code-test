"""日志与终端编码工具。

集中管理摘要日志过滤、统一 source 字段和 UTF-8 终端输出设置，
避免业务模块重复拼装日志策略。
"""

import logging
import sys
import time
from pathlib import Path
from typing import Optional


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
    "Champion_Synergy.json 缓存已刷新",
    "CSV 已更新：",
)


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


def get_unified_log_file() -> str:
    return get_runtime_summary_log_file()


def get_runtime_summary_log_file() -> str:
    base_dir = Path(__file__).resolve().parent.parent
    log_dir = base_dir / "data" / "runtime" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return str(log_dir / "hextech_runtime_summary.log")


def get_error_log_file() -> str:
    base_dir = Path(__file__).resolve().parent.parent
    log_dir = base_dir / "data" / "runtime" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return str(log_dir / "hextech_error.log")


def ensure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:
            pass


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

    logging.basicConfig(level=level, format=fmt, handlers=handlers, force=True)
    root = logging.getLogger()
    if not any(isinstance(existing, SummaryOnlyFilter) for existing in root.filters):
        root.addFilter(summary_filter)
    if not any(isinstance(existing, SourceNameFilter) for existing in root.filters):
        root.addFilter(source_filter)
    for handler in root.handlers:
        if not getattr(handler, "_hextech_preserve_level", False):
            if isinstance(handler, logging.FileHandler):
                handler.setLevel(logging.ERROR)
            else:
                handler.setLevel(logging.WARNING)
        if not any(isinstance(existing, SummaryOnlyFilter) for existing in handler.filters):
            handler.addFilter(summary_filter)
        if not any(isinstance(existing, SourceNameFilter) for existing in handler.filters):
            handler.addFilter(source_filter)


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
