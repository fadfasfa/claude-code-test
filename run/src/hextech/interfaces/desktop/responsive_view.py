"""Tk 主线程响应式重排；保留控件身份、开关和列表滚动锚点。"""

from __future__ import annotations

import queue
import tkinter as tk
from tkinter.font import Font

from .responsive_layout import DesktopLayout
from .window_activation import suppress_desktop_activation
from .client_layer import unowned_tk_operation


def wrap_text(text: str, font: Font, width: int, lines: int = 2) -> str:
    result: list[str] = []
    current = ""
    remaining = str(text)
    while remaining:
        char, remaining = remaining[0], remaining[1:]
        if char == "\n" or (current and font.measure(current + char) > width):
            result.append(current)
            current = "" if char == "\n" else char
            if len(result) == lines:
                current += remaining
                break
        else:
            current += char
    if len(result) < lines:
        result.append(current)
    elif current:
        last = result[-1]
        while last and font.measure(last + "…") > width:
            last = last[:-1]
        result[-1] = last + "…"
    return "\n".join(result)


class DesktopResponsiveMixin:
    def end_move(self, event=None):
        presentation = getattr(self, "_desktop_window_presentation", None)
        if presentation is not None:
            presentation.end_manual_drag()

    def _drag_button_is_down(self) -> bool:
        import win32api
        return bool(win32api.GetAsyncKeyState(1) & 0x8000)

    def _ui_font(self, size: int, bold: bool = False) -> tuple:
        return getattr(self, "_desktop_layout", DesktopLayout()).font(size, bold)

    def _drain_ui_callbacks(self) -> None:
        callbacks = getattr(self, "_ui_callbacks", None)
        if callbacks is None:
            return
        for _ in range(32):
            try:
                callback = callbacks.get_nowait()
            except queue.Empty:
                break
            if not self._closing:
                callback()

    def _layout_candidate_row(self, row: dict) -> None:
        if "content" not in row:
            return
        layout = getattr(self, "_desktop_layout", DesktopLayout(logical_width=320))
        px = layout.px
        width = int(getattr(self, "_overlay_pixel_width", 320))
        row_key = (width, layout.dpi_scale, layout.client_scale, layout.mode, row.get("name"),
                   row.get("tier"), row.get("win"), row.get("pick"), row.get("selection_role"))
        if row.get("_layout_key") == row_key:
            return
        row["_layout_key"] = row_key
        # 预留滚动条和所有卡片边距，不让隐藏滚动条后的初始宽度过于乐观。
        available = max(1, width - px(44))
        content, info, metric = row["content"], row["info"], row["metric"]
        for child in (row["img_label"], info, metric):
            child.pack_forget()
            child.grid_forget()
        content.configure(padx=px(6), pady=px(4))
        content.grid_columnconfigure(0, weight=0)
        content.grid_columnconfigure(1, weight=1)
        content.grid_columnconfigure(2, weight=0)
        row["img_label"].grid(row=0, column=0, padx=(0, px(7)), sticky="nw")
        info.grid(row=0, column=1, sticky="new")
        normal = layout.mode == "normal"
        metric.configure(width=0)
        metric.pack_propagate(True)
        metric.grid(row=0 if normal else 1, column=2 if normal else 0,
                    columnspan=1 if normal else 2, sticky="ne" if normal else "ew", pady=(0 if normal else px(4), 0))
        for child in (row["selected_badge"], row["win_label"]):
            child.pack_forget()
        row["selected_badge"].pack(side=tk.TOP if normal else tk.LEFT, anchor="e")
        row["win_label"].pack(side=tk.TOP if normal else tk.RIGHT, anchor="e")
        row["strength_bar"].configure(width=px(6))
        row["card"].pack_configure(pady=px(2), padx=(0, px(6)))
        for key, size, bold in (("name_label", 12, True), ("tier_badge", 12, True),
                                ("selected_badge", 11, True), ("win_label", 16, True),
                                ("pick_label", 11, False)):
            row[key].configure(font=self._ui_font(size, bold))
        def label_width(key: str, fallback: str = "") -> int:
            widget = row[key]
            measured = Font(root=self.root, font=widget.cget("font"))
            return measured.measure(widget.cget("text") or fallback)
        row["selected_badge"].configure(width=0, padx=px(3), pady=px(1))
        row["tier_badge"].configure(padx=px(4))
        metric_width = max(label_width("win_label", "100.0%"), label_width("selected_badge", "已选") + px(6)) + 4
        content.grid_columnconfigure(2, minsize=metric_width if normal else 0)
        name_width = available - px(48 + 7) - label_width("tier_badge") - px(8 + 4) - 4
        if normal:
            name_width -= metric_width + px(4)
        name_width = max(1, name_width)
        font = Font(root=self.root, font=row["name_label"].cget("font"))
        row["name_label"].configure(text=wrap_text(row["name"], font, name_width), justify="left", anchor="w")
        row["pick_label"].configure(font=self._ui_font(11))
        self._bind_name_tooltip(row)

    def _bind_name_tooltip(self, row: dict) -> None:
        if row.get("tooltip_bound"):
            return
        row["tooltip_bound"] = True

        def hide(_event=None):
            tip = getattr(self, "_name_tooltip", None)
            if tip is not None:
                tip.destroy()
                self._name_tooltip = None

        def show(_event=None):
            hide()
            if self._closing or row["name_label"].cget("text") == row["name"]:
                return
            tip = tk.Toplevel(self.root)
            tip.overrideredirect(True)
            tip.withdraw()
            tk.Label(tip, text=row["name"], font=self._ui_font(12), bg="#13233A", fg="#F0E6D2",
                     wraplength=max(1, self._overlay_pixel_width - 16), padx=6, pady=4).pack()
            tip.update_idletasks()
            from .client_layer import desktop_wrapper_hwnd
            from . import runtime as runtime_api
            x, y = self.root.winfo_rootx(), row["name_label"].winfo_rooty()
            y = min(y, self.root.winfo_rooty() + self.root.winfo_height() - tip.winfo_reqheight())
            runtime_api.win32gui.SetWindowPos(desktop_wrapper_hwnd(tip), -1, x, y,
                                              tip.winfo_reqwidth(), tip.winfo_reqheight(), 0x0010 | 0x0040)
            self._name_tooltip = tip
        row["name_label"].bind("<Enter>", show, add="+")
        row["name_label"].bind("<Leave>", hide, add="+")
        row["name_label"].bind("<Destroy>", hide, add="+")

    def _apply_desktop_layout(self, layout: DesktopLayout) -> int:
        content = tuple((key, row.get("name"), row.get("tier"), row.get("win"), row.get("pick"),
                         row.get("selection_role")) for key, row in getattr(self, "_card_rows", {}).items())
        rect_size = (layout.rect[2]-layout.rect[0], layout.rect[3]-layout.rect[1]) if layout.rect else None
        key = (rect_size, layout.dpi_scale, layout.client_scale, layout.mode, content,
               getattr(getattr(self, "_list_placeholder", None), "_hextech_text", None))
        if key == getattr(self, "_layout_measure_key", None):
            return self._layout_measure_height
        height = self._reflow_desktop_layout(layout)
        self._layout_measure_key, self._layout_measure_height = key, height
        return height

    @suppress_desktop_activation()
    @unowned_tk_operation
    def _reflow_desktop_layout(self, layout: DesktopLayout) -> int:
        """先重排并测量最小高度；调用方决定是否允许映射。"""
        if layout.rect is None:
            return 0
        self._ensure_card_state()
        if not self.root.winfo_ismapped():
            from .client_layer import desktop_wrapper_hwnd
            from . import runtime as runtime_api
            hwnd = desktop_wrapper_hwnd(self.root)
            runtime_api.win32gui.SetWindowLong(hwnd, -20, runtime_api.win32gui.GetWindowLong(hwnd, -20) | 0x08000000)
        x, y, end, bottom = layout.rect
        key = (end-x, layout.dpi_scale, layout.client_scale, layout.mode)
        changed = key != getattr(self, "_desktop_layout_key", None)
        if changed:
            tip = getattr(self, "_name_tooltip", None)
            if tip is not None:
                tip.destroy()
                self._name_tooltip = None
            self._desktop_layout_key = key
            self._desktop_layout = layout
            self._ui_scale = layout.scale
            self._overlay_pixel_width = end-x
            self._avatar_revision = getattr(self, "_avatar_revision", 0) + 1
            self.image_cache.clear()
            self._avatar_placeholder_photo = None
            anchor = None
            offset = 0.0
            view_y = self.canvas.canvasy(0)
            for identity in getattr(self, "_card_order", []):
                widget = self._card_rows[identity]["card"]
                if widget.winfo_y() + widget.winfo_height() > view_y:
                    anchor, offset = identity, view_y-widget.winfo_y()
                    break
            for widget in (self.title_bar, self.exit_button, self.refresh_button, self.diagnostics_button):
                widget.pack_forget()
                widget.grid_forget()
            self.title_frame.grid_columnconfigure(0, weight=1)
            self.title_frame.grid_columnconfigure(1, weight=0)
            self.title_bar.configure(font=self._ui_font(16, True), pady=layout.px(8))
            self.exit_button.configure(font=self._ui_font(15, True))
            self.title_bar.grid(row=0, column=0, sticky="w", padx=layout.px(8))
            self.exit_button.grid(row=0, column=3, sticky="e", padx=layout.px(6))
            narrow = layout.mode != "normal"
            for column, widget in enumerate((self.refresh_button, self.diagnostics_button), start=1):
                widget.configure(font=self._ui_font(11, True), padx=layout.px(8), pady=layout.px(3))
                widget.grid(row=1 if narrow else 0, column=column-1 if narrow else column,
                            sticky="w", padx=layout.px(4), pady=layout.px(4))
            columns = {"normal": 3, "narrow": 2, "minimum": 1}[layout.mode]
            for column in range(3):
                self.feature_frame.grid_columnconfigure(column, weight=1 if column < columns else 0)
            for index, toggle in enumerate(self._feature_toggle_widgets):
                toggle["frame"].grid_forget()
                toggle["frame"].grid(row=index//columns, column=index % columns, sticky="w",
                                     padx=layout.px(2), pady=layout.px(3))
                toggle["label"].configure(font=self._ui_font(11, True))
                toggle["dot"].configure(width=layout.px(13), height=layout.px(13))
            self.list_shell.pack_configure(padx=(layout.px(10), 0))
            self.list_scrollbar.configure(width=layout.px(8))
            # 固定区先预留空间，剩余高度全给列表。
            self.list_shell.pack_forget()
            self.list_shell.pack(fill=tk.BOTH, expand=True, padx=(layout.px(10), 0), pady=(6, 4))
            self.status_line_label.configure(font=self._ui_font(11), justify="center", height=2)
            for row in self._card_rows.values():
                row["img_label"].configure(image=self._avatar_placeholder_image())
                row["img_label"]._hextech_avatar_loaded = False
                self._layout_candidate_row(row)
                self._request_avatar(row)
            placeholder = getattr(self, "_list_placeholder", None)
            if placeholder is not None:
                placeholder.configure(font=self._ui_font(12), wraplength=max(1, end-x-layout.px(32)))
            self._refresh_feature_toggle_styles()
            self._render_status_line()
            # 隐藏首帧不提前驱动Tk窗口消息；由呈现owner先定位，再统一映射。
            if self.root.winfo_ismapped():
                self.root.geometry(f"{end-x}x{bottom-y}")
                self.root.update_idletasks()
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
            if anchor is not None and anchor in self._card_rows:
                target = self._card_rows[anchor]["card"].winfo_y() + offset
                self.canvas.yview_moveto(max(0, target)/max(1, self.list_frame.winfo_reqheight()))
        rows = getattr(self, "_card_rows", {})
        row_height = layout.px(100)
        for row in rows.values():
            info = max(row["name_label"].winfo_reqheight(), row["tier_badge"].winfo_reqheight()) + row["pick_label"].winfo_reqheight() + layout.px(2)
            role, win = row["selected_badge"].winfo_reqheight(), row["win_label"].winfo_reqheight()
            top = max(row["img_label"].winfo_reqheight(), info)
            height = max(top, role+win) if layout.mode == "normal" else top+max(role,win)+layout.px(4)
            row_height = max(row_height, height+layout.px(12))
        header = max(self.title_bar.winfo_reqheight(), self.exit_button.winfo_reqheight())
        if layout.mode != "normal":
            header += max(self.refresh_button.winfo_reqheight(), self.diagnostics_button.winfo_reqheight()) + layout.px(8)
        columns = {"normal": 3, "narrow": 2, "minimum": 1}[layout.mode]
        feature = 12
        toggles = self._feature_toggle_widgets
        for start in range(0, len(toggles), columns):
            feature += max(max(t["label"].winfo_reqheight(), t["dot"].winfo_reqheight()) for t in toggles[start:start+columns]) + layout.px(6)
        return header + feature + self.status_line_label.winfo_reqheight() + row_height + layout.px(36)

    def _request_avatar(self, row: dict) -> None:
        from .avatar_loading import request_avatar
        request_avatar(self, row)
