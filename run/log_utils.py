import logging
import sys
import time
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
)


class SummaryOnlyFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for pattern in NOISY_MESSAGE_PATTERNS:
            if pattern in message:
                return False
        return True


def ensure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:
            pass


def install_summary_logging(
    *,
    level: int = logging.INFO,
    handlers: Optional[list[logging.Handler]] = None,
    fmt: str = "%(asctime)s [%(levelname)s] %(message)s",
) -> None:
    root = logging.getLogger()
    root.setLevel(level)

    summary_filter = SummaryOnlyFilter()
    if not any(isinstance(existing, SummaryOnlyFilter) for existing in root.filters):
        root.addFilter(summary_filter)

    if handlers is None:
        if not root.handlers:
            logging.basicConfig(level=level, format=fmt)
        for handler in root.handlers:
            if not any(isinstance(existing, SummaryOnlyFilter) for existing in handler.filters):
                handler.addFilter(summary_filter)
        return

    logging.basicConfig(level=level, format=fmt, handlers=handlers, force=True)
    root = logging.getLogger()
    if not any(isinstance(existing, SummaryOnlyFilter) for existing in root.filters):
        root.addFilter(summary_filter)
    for handler in root.handlers:
        if not any(isinstance(existing, SummaryOnlyFilter) for existing in handler.filters):
            handler.addFilter(summary_filter)


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
