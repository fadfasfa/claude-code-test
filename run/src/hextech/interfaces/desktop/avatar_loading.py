"""头像有界在途合并；线程只解码图片，PhotoImage和标签更新留在GUI。"""
from concurrent.futures import ThreadPoolExecutor
import time


def avatar_placeholder_image(ui):
    """GUI线程共享圆角占位图；与真实头像按相同布局代际失效。"""
    from PIL import Image, ImageDraw, ImageTk
    from .app_shared import UI_COLORS, scaled
    if getattr(ui, "_avatar_placeholder_photo", None) is None:
        scale = ui._ui_scale_value()
        size, radius = scaled(48, scale), scaled(8, scale)
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        ImageDraw.Draw(img).rounded_rectangle((0, 0, size-1, size-1), radius=radius,
                                             fill=UI_COLORS["surface_alt"])
        ui._avatar_placeholder_photo = ImageTk.PhotoImage(img)
    return ui._avatar_placeholder_photo


def request_avatar(ui, row):
    if getattr(ui, "_closing", False):
        return
    label = row["img_label"]
    size = max(1, round(48 * float(getattr(ui, "_ui_scale", 1.0))))
    key = (str(row["id"]), size, int(getattr(ui, "_avatar_revision", 0)))
    label._hextech_avatar_request_key = key
    pending = getattr(ui, "_avatar_pending", None)
    if pending is None:
        pending = ui._avatar_pending = set()
        ui._avatar_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="desktop-avatar")
    request_id = (key, id(label))
    if request_id in pending or len(pending) >= 32:
        return
    if getattr(label, "_hextech_avatar_loaded_key", None) == key:
        return
    if (getattr(label, "_hextech_avatar_retry_key", None) == key
        and time.monotonic() < getattr(label, "_hextech_avatar_retry_at", 0.0)):
        return
    pending.add(request_id)
    def load():
        try:
            if getattr(label, "_hextech_avatar_request_key", None) == key and not getattr(ui, "_closing", False):
                # 参数来自提交时，不允许worker启动时重新读取不同DPI代际。
                from .runtime_window import load_and_set_img
                load_and_set_img(ui, key[0], label, request_key=key)
        finally:
            def finished():
                pending.discard(request_id)
                if getattr(label, "_hextech_avatar_request_key", None) == key:
                    label._hextech_avatar_retry_key = key
                    label._hextech_avatar_retry_at = time.monotonic() + 1.0
            ui._run_on_ui_thread(finished)
    ui._avatar_executor.submit(load)


def close_avatar_loader(ui):
    executor = getattr(ui, "_avatar_executor", None)
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)
        ui._avatar_executor = None
