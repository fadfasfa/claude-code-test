"""选择区诊断菜单：显式传入窗口、目录及回调，不承接 Desktop 对象状态。"""
from __future__ import annotations

import os
import tkinter as tk
from collections.abc import Callable
from functools import partial
from pathlib import Path

from hextech.interfaces.desktop.app_shared import UI_COLORS
from hextech.modules.data.ports.paths import get_var_dir
from hextech.modules.session.selection_diagnostics import (
    _reject_reparse_path, read_selection_diagnostics, request_recent_selection, selection_cache_root,
)


def save_recent_selection(var_dir: Path, set_status: Callable[[str, str], None]) -> None:
    result = request_recent_selection(var_dir)
    set_status("保存已请求 · 诊断菜单查看结果" if result["ok"] else f"保存失败：{result['reason']}",
               UI_COLORS["muted"] if result["ok"] else UI_COLORS["red"])


def open_selection_cache(var_dir: Path, set_status: Callable[[str, str], None]) -> None:
    path = selection_cache_root(var_dir)
    try:
        _reject_reparse_path(path)
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(str(path))
    except (OSError, ValueError):
        set_status("缓存目录无法打开", UI_COLORS["red"])


def show_selection_diagnostics_menu(
    root: tk.Misc,
    anchor: tk.Widget,
    *,
    var_dir: Path | None = None,
    export_diagnostics: Callable[[], None],
    set_status: Callable[[str, str], None],
) -> None:
    var_dir = Path(var_dir) if var_dir is not None else get_var_dir()
    diagnostic = read_selection_diagnostics(var_dir)
    status = diagnostic["status"]
    writer = status.get("failure_evidence_writer") or {}
    cache = writer.get("selection_cache") or {}
    buffer = status.get("selection_capture") or {}
    menu = tk.Menu(root, tearoff=False)
    count = cache.get("groups")
    menu.add_command(label=f"选择区缓存：{count if count is not None else '尚未统计'} 组 · 写入失败 {writer.get('failed', 0)}",
                     state=tk.DISABLED)
    reason = diagnostic["reason"] or writer.get("last_error") or buffer.get("last_error")
    if reason:
        menu.add_command(label=f"原因：{reason}", state=tk.DISABLED)
    request = (status.get("explicit_capture") or {}).get("recent_save_request") or {}
    if request:
        label = "最近手动保存：已入队" if request.get("accepted") else f"最近手动保存：{request.get('reason')}"
        if request.get("accepted") and request.get("diagnostic_id") and (
                request["diagnostic_id"] == cache.get("last_manual_diagnostic_id")):
            label = "最近手动保存：已写入（受保护）"
        menu.add_command(label=label, state=tk.DISABLED)
    menu.add_command(label="保存最近选择区（不抓全屏）",
                     command=partial(save_recent_selection, var_dir, set_status),
                     state=tk.NORMAL if diagnostic["live"] and buffer.get("recent_available") else tk.DISABLED)
    menu.add_command(label="打开选择区缓存目录", command=partial(open_selection_cache, var_dir, set_status))
    menu.add_separator()
    menu.add_command(label="导出诊断包", command=export_diagnostics)
    try:
        menu.tk_popup(anchor.winfo_rootx(), anchor.winfo_rooty()+anchor.winfo_height())
    finally:
        menu.grab_release()
