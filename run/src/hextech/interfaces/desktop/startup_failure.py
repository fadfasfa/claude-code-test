"""启动失败提示不依赖运行目录；路径解析本身失败时也不能静默。"""
from __future__ import annotations

import ctypes
import os
import sys


def startup_failure_message(error: Exception, diagnostic_path: str = "") -> str:
    reason = str(error)
    if reason == "migration_required":
        reason = "所选运行目录包含旧版数据，新版未对它进行自动迁移。"
    location = f"诊断日志：{diagnostic_path}" if diagnostic_path else "尚未建立可用的诊断目录。"
    return f"Hextech 未能完成启动，备战席数据和游戏内识别尚不可用。\n\n原因：{reason}\n\n{location}"


def show_startup_failure(error: Exception, diagnostic_path: str = "") -> None:
    message = startup_failure_message(error, diagnostic_path)
    if os.name == "nt":
        # 无 owner 的原生对话框不依赖隐藏的 Tk 主窗口，也不创建日志目录。
        message_box = ctypes.WinDLL("user32", use_last_error=True).MessageBoxW
        message_box.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint)
        message_box.restype = ctypes.c_int
        message_box(None, message, "Hextech 启动失败", 0x10 | 0x10000)
    elif sys.stderr is not None:
        print(message, file=sys.stderr)
