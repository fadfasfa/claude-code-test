"""头像提交身份冻结、在途合并与迟到结果；无真实网络和Tk。"""
from types import SimpleNamespace
from unittest.mock import Mock
import gc
import queue
import threading
import weakref

from hextech.interfaces.desktop import avatar_loading, runtime_window


def setup_loader(monkeypatch):
    jobs, callbacks = [], []
    executor = SimpleNamespace(submit=lambda work: jobs.append(work), shutdown=Mock())
    monkeypatch.setattr(avatar_loading, "ThreadPoolExecutor", lambda **kw: executor)
    class Record(SimpleNamespace):
        pass
    class Callbacks:
        def put_nowait(self, callback): callbacks.append(callback)
        def get_nowait(self):
            if callbacks:
                return callbacks.pop(0)
            raise queue.Empty
    ui = Record(_closing=False, _ui_scale=1.0, _avatar_revision=1, _ui_callbacks=Callbacks(), image_cache={})
    row = {"id": "266", "img_label": Record()}
    return ui, row, jobs, callbacks


def test_identical_avatar_requests_coalesce_and_size_revision_supersede(monkeypatch):
    ui, row, jobs, callbacks = setup_loader(monkeypatch)
    load = Mock()
    monkeypatch.setattr(runtime_window, "read_avatar_image", load)
    for _ in range(30):
        avatar_loading.request_avatar(ui, row)
    assert len(jobs) == 1
    ui._ui_scale, ui._avatar_revision = 1.5, 2
    avatar_loading.request_avatar(ui, row)
    assert len(jobs) == 2
    jobs[0]()
    load.assert_not_called()
    jobs[1]()
    load.assert_called_once_with("266", 72, None)
    row["img_label"].winfo_exists = lambda: False
    for callback in callbacks:
        callback()
    assert ui._avatar_pending == set()


def test_avatar_shutdown_rejects_queued_work(monkeypatch):
    ui, row, jobs, _callbacks = setup_loader(monkeypatch)
    load = Mock()
    monkeypatch.setattr(runtime_window, "read_avatar_image", load)
    avatar_loading.request_avatar(ui, row)
    ui._closing = True
    avatar_loading.close_avatar_loader(ui)
    jobs[0]()
    load.assert_not_called()
    avatar_loading.request_avatar(ui, row)
    assert len(jobs) == 1


def test_old_avatar_retry_timer_does_not_delay_new_dpi(monkeypatch):
    ui, row, jobs, _callbacks = setup_loader(monkeypatch)
    row["img_label"]._hextech_avatar_retry_key = ("266", 32, 0)
    row["img_label"]._hextech_avatar_retry_at = float("inf")
    avatar_loading.request_avatar(ui, row)
    assert len(jobs) == 1


def test_late_avatar_does_not_construct_photoimage_or_touch_reused_label(monkeypatch, tmp_path):
    from PIL import Image
    seed = tmp_path / "seed"
    seed.mkdir()
    Image.new("RGB", (48, 48), "red").save(seed / "266.png")
    monkeypatch.setattr(runtime_window, "CHAMPION_ASSET_DIR", str(seed))
    monkeypatch.setattr(runtime_window, "var_path", lambda *parts: tmp_path.joinpath(*parts))
    photo = Mock()
    monkeypatch.setattr(runtime_window.ImageTk, "PhotoImage", photo)
    callbacks = []
    label = SimpleNamespace(_hextech_avatar_request_key=("266", 48, 1),
                            winfo_exists=Mock(return_value=True), config=Mock())
    ui = SimpleNamespace(_closing=False, _avatar_revision=1, image_cache={},
                         _run_on_ui_thread=lambda callback: callbacks.append(callback))
    runtime_window.load_and_set_img(ui, "266", label, request_key=("266", 48, 1))
    assert len(callbacks) == 1
    label._hextech_avatar_request_key = ("267", 48, 1)
    callbacks[0]()
    photo.assert_not_called()
    label.config.assert_not_called()
    label.winfo_exists.assert_not_called()


def test_slow_avatar_worker_never_owns_last_ui_or_label_reference(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    destroyed = []
    gui_thread = threading.get_ident()
    class Tracked(SimpleNamespace):
        def __del__(self):
            destroyed.append((self.kind, threading.get_ident()))
    def slow_read(*args):
        entered.set()
        assert release.wait(2)
        return None
    monkeypatch.setattr(runtime_window, "read_avatar_image", slow_read)
    callbacks = queue.Queue()
    ui = Tracked(kind="ui", _closing=False, _ui_scale=1.0, _avatar_revision=1, _ui_callbacks=callbacks)
    label = Tracked(kind="label")
    ui_ref, label_ref = weakref.ref(ui), weakref.ref(label)
    avatar_loading.request_avatar(ui, {"id": "266", "img_label": label})
    executor = ui._avatar_executor
    try:
        assert entered.wait(1)
        ui._closing = True
        avatar_loading.close_avatar_loader(ui)
        # The reader is still blocked, while GUI references can die on the GUI.
        del ui, label
        gc.collect()
        assert ui_ref() is None and label_ref() is None
        assert sorted(destroyed) == [("label", gui_thread), ("ui", gui_thread)]
    finally:
        release.set()
        executor.shutdown(wait=True)
    assert callbacks.empty()


def test_avatar_completion_dereferences_widgets_and_creates_photo_only_on_gui(monkeypatch):
    ui, row, jobs, callbacks = setup_loader(monkeypatch)
    image, photo = object(), object()
    monkeypatch.setattr(runtime_window, "read_avatar_image", lambda *a: image)
    from PIL import ImageTk
    gui_thread = threading.get_ident()
    created = []
    monkeypatch.setattr(ImageTk, "PhotoImage", lambda value: created.append((value, threading.get_ident())) or photo)
    label = row["img_label"]
    label.winfo_exists = lambda: True
    label.config = Mock()
    avatar_loading.request_avatar(ui, row)
    worker = threading.Thread(target=jobs[0])
    worker.start()
    worker.join(1)
    assert created == [] and not worker.is_alive()
    callbacks[0]()
    assert created == [(image, gui_thread)]
    label.config.assert_called_once_with(image=photo)
    assert ui._avatar_pending == set()
