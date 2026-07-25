"""无控制台子进程的启动握手。

冻结 GUI 应用没有可靠的 ``sys.stdout``。Supervisor 与 DataService 因此通过
带随机 token 的原子 JSON 文件向 Desktop 发布端口和 nonce；源码态仍保留
stdout 兼容输出，便于人工诊断。
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from typing import Any

from hextech.modules.data.ports.atomic import atomic_write_json


PROCESS_BOOTSTRAP_FILE_ENV = "HEXTECH_PROCESS_BOOTSTRAP_FILE"
PROCESS_BOOTSTRAP_TOKEN_ENV = "HEXTECH_PROCESS_BOOTSTRAP_TOKEN"


def publish_process_bootstrap(payload: Mapping[str, Any]) -> dict[str, Any]:
    """发布一次启动信息；文件通道是冻结态权威，stdout 仅作兼容。"""

    published = dict(payload)
    token = str(os.getenv(PROCESS_BOOTSTRAP_TOKEN_ENV) or "").strip()
    target = str(os.getenv(PROCESS_BOOTSTRAP_FILE_ENV) or "").strip()
    if target:
        if not token:
            raise RuntimeError("process bootstrap 缺少 token")
        published["token"] = token
        atomic_write_json(target, published, ensure_ascii=False, indent=2)
    if sys.stdout is not None:
        print(json.dumps(published, ensure_ascii=False, sort_keys=True), flush=True)
    return published


__all__ = [
    "PROCESS_BOOTSTRAP_FILE_ENV",
    "PROCESS_BOOTSTRAP_TOKEN_ENV",
    "publish_process_bootstrap",
]
