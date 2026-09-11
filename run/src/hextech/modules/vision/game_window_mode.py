"""League 游戏窗口模式的只读、缓存探针。

当前 Overlay 是独立的 Win32 layered window，只在 DWM 可组合的 Borderless / Windowed
模式下提供可靠呈现。这里仅从当前游戏进程对应安装根的 ``Config/game.cfg`` 读取
``WindowMode``；绝不修改 Riot 配置，也不读取进程内存。
"""

from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import psutil


GameWindowModeStatus = Literal["supported", "unsupported", "unknown", "error"]
GameWindowMode = Literal["borderless", "windowed", "fullscreen", "unknown"]
GAME_WINDOW_MODE_CACHE_SECONDS = 1.0
_WINDOW_MODE_PATTERN = re.compile(r"^\s*WindowMode\s*=\s*(-?\d+)\s*$", re.IGNORECASE | re.MULTILINE)


@dataclass(frozen=True)
class GameWindowModeProbe:
    status: GameWindowModeStatus
    mode: GameWindowMode
    reason: str
    observed_at: float
    source: str = "game_cfg"
    config_size: int = 0
    config_mtime_ns: int = 0

    @property
    def supported(self) -> bool:
        return self.status == "supported"

    def to_status(self) -> dict[str, object]:
        """返回可公开写入 Host/Sidecar 状态的有限字段。"""

        return {
            "status": self.status,
            "mode": self.mode,
            "reason": self.reason,
            "source": self.source,
            "observed_at": float(self.observed_at),
        }


_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, GameWindowModeProbe]] = {}


def resolve_game_config_path(executable: str | os.PathLike[str]) -> Path:
    """从 ``.../Game/League of Legends.exe`` 解析同目录的 ``Config/game.cfg``。"""

    path = Path(executable)
    return path.parent / "Config" / "game.cfg"


def _probe_from_text(text: str, *, observed_at: float, size: int, mtime_ns: int) -> GameWindowModeProbe:
    match = _WINDOW_MODE_PATTERN.search(text)
    if match is None:
        return GameWindowModeProbe(
            "unknown",
            "unknown",
            "window_mode_missing",
            observed_at,
            config_size=size,
            config_mtime_ns=mtime_ns,
        )
    value = int(match.group(1))
    if value == 0:
        return GameWindowModeProbe(
            "unsupported",
            "fullscreen",
            "unsupported_fullscreen_mode",
            observed_at,
            config_size=size,
            config_mtime_ns=mtime_ns,
        )
    if value == 1:
        return GameWindowModeProbe(
            "supported",
            "borderless",
            "",
            observed_at,
            config_size=size,
            config_mtime_ns=mtime_ns,
        )
    if value == 2:
        return GameWindowModeProbe(
            "supported",
            "windowed",
            "",
            observed_at,
            config_size=size,
            config_mtime_ns=mtime_ns,
        )
    return GameWindowModeProbe(
        "unknown",
        "unknown",
        "window_mode_unknown",
        observed_at,
        config_size=size,
        config_mtime_ns=mtime_ns,
    )


def read_game_window_mode(
    executable: str | os.PathLike[str],
    *,
    force: bool = False,
) -> GameWindowModeProbe:
    """读取并缓存一个游戏安装的窗口模式；缓存期内连 ``stat`` 都不重复执行。"""

    executable_key = os.path.normcase(os.fspath(Path(executable).resolve(strict=False)))
    monotonic_now = time.monotonic()
    with _CACHE_LOCK:
        cached = _CACHE.get(executable_key)
        if not force and cached is not None and monotonic_now - cached[0] < GAME_WINDOW_MODE_CACHE_SECONDS:
            return cached[1]

    observed_at = time.time()
    config_path = resolve_game_config_path(executable)
    try:
        stat_result = config_path.stat()
    except FileNotFoundError:
        result = GameWindowModeProbe("unknown", "unknown", "game_config_missing", observed_at)
    except OSError:
        result = GameWindowModeProbe("error", "unknown", "game_config_stat_error", observed_at)
    else:
        try:
            with config_path.open("r", encoding="utf-8-sig", errors="replace") as stream:
                text = stream.read()
        except OSError:
            result = GameWindowModeProbe(
                "error",
                "unknown",
                "game_config_read_error",
                observed_at,
                config_size=int(stat_result.st_size),
                config_mtime_ns=int(stat_result.st_mtime_ns),
            )
        else:
            result = _probe_from_text(
                text,
                observed_at=observed_at,
                size=int(stat_result.st_size),
                mtime_ns=int(stat_result.st_mtime_ns),
            )
    with _CACHE_LOCK:
        _CACHE[executable_key] = (monotonic_now, result)
    return result


def probe_game_window_mode(process_id: int) -> GameWindowModeProbe:
    """从当前 LoL PID 获取安装路径后读取模式；权限失败时明确 fail closed。"""

    observed_at = time.time()
    try:
        executable = str(psutil.Process(int(process_id)).exe() or "")
    except (psutil.Error, OSError, TypeError, ValueError):
        executable = ""
    if not executable:
        return GameWindowModeProbe(
            "error",
            "unknown",
            "process_executable_unavailable",
            observed_at,
        )
    return read_game_window_mode(executable)


def _clear_game_window_mode_cache() -> None:
    """仅供确定性测试清空进程内缓存。"""

    with _CACHE_LOCK:
        _CACHE.clear()


__all__ = [
    "GAME_WINDOW_MODE_CACHE_SECONDS",
    "GameWindowModeProbe",
    "probe_game_window_mode",
    "read_game_window_mode",
    "resolve_game_config_path",
]
