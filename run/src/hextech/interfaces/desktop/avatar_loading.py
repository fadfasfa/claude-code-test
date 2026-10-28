"""头像有界在途合并；线程只解码图片，PhotoImage和标签更新留在GUI。"""
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import logging
import queue
import threading
import time
import weakref


logger = logging.getLogger(__name__)


def _finish_avatar(ui_ref, label_ref, key, request_id, pending, token, image):
    """Only the GUI callback dereferences UI/widget weak references."""
    pending.discard(request_id)
    ui, label = ui_ref(), label_ref()
    if ui is None:
        return
    tokens = getattr(ui, "_avatar_tokens", {})
    if tokens.get(request_id[1], (None, None))[1] is token:
        tokens.pop(request_id[1], None)
    if (label is None or token.is_set() or getattr(ui, "_closing", False)
            or key != getattr(label, "_hextech_avatar_request_key", None)
            or key[2] != getattr(ui, "_avatar_revision", 0)):
        return
    label._hextech_avatar_retry_key = key
    label._hextech_avatar_retry_at = time.monotonic()+1.0
    if image is None or not label.winfo_exists():
        return
    from PIL import ImageTk
    cache_key = key[:2]
    photo = ui.image_cache.get(cache_key)
    if photo is None:
        photo = ImageTk.PhotoImage(image)
        if len(ui.image_cache) >= 64:
            ui.image_cache.clear()
        ui.image_cache[cache_key] = photo
    label.config(image=photo)
    label._hextech_avatar_photo = photo
    label._hextech_avatar_loaded = True
    label._hextech_avatar_loaded_key = key


def _load_avatar(key, request_id, session, callbacks, pending, stopped, token, ui_ref, label_ref):
    # Never dereference either weakref here, including the cancellation path.
    if stopped.is_set() or token.is_set():
        pending.discard(request_id)
        return
    image = None
    try:
        from .runtime_window import read_avatar_image
        image = read_avatar_image(key[0], key[1], session)
    except Exception:
        logger.exception("加载英雄头像失败：champ_id=%s", key[0])
    if stopped.is_set() or token.is_set():
        pending.discard(request_id)
        return
    try:
        callbacks.put_nowait(partial(_finish_avatar, ui_ref, label_ref, key, request_id, pending, token, image))
    except queue.Full:
        pending.discard(request_id)


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
        ui._avatar_stopped = threading.Event()
        ui._avatar_tokens = {}
    request_id = (key, id(label))
    if request_id in pending or len(pending) >= 32:
        return
    if getattr(label, "_hextech_avatar_loaded_key", None) == key:
        return
    if (getattr(label, "_hextech_avatar_retry_key", None) == key
        and time.monotonic() < getattr(label, "_hextech_avatar_retry_at", 0.0)):
        return
    pending.add(request_id)
    previous = ui._avatar_tokens.get(id(label))
    if previous is not None:
        previous[1].set()
    token = threading.Event()
    ui._avatar_tokens[id(label)] = (key, token)
    ui._avatar_executor.submit(partial(_load_avatar, key, request_id, getattr(ui, "session", None),
        ui._ui_callbacks, pending, ui._avatar_stopped, token, weakref.ref(ui), weakref.ref(label)))


def close_avatar_loader(ui):
    stopped = getattr(ui, "_avatar_stopped", None)
    if stopped is not None:
        stopped.set()
    for _, token in getattr(ui, "_avatar_tokens", {}).values():
        token.set()
    getattr(ui, "_avatar_tokens", {}).clear()
    # Discard GUI-owned callbacks on GUI shutdown; workers cannot own their last
    # strong Tk reference, and no completion should run against destroyed widgets.
    callbacks = getattr(ui, "_ui_callbacks", None)
    if callbacks is not None:
        while True:
            try:
                callbacks.get_nowait()
            except queue.Empty:
                break
    executor = getattr(ui, "_avatar_executor", None)
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)
        ui._avatar_executor = None
