"""显示测试使用已载入的内存输入；真实线程/IO 边界单独测试。"""
import time
from hextech.interfaces.overlay.host_input import HostInputSnapshot


class PreloadedInputObserver:
    def __init__(self, source, *, config=None, on_event=None):
        event = source.read_event()
        event_source = event.get("source", {})
        context = source.read_context() if event.get("active") or event.get("visible") or event_source.get("session_id") else {}
        self.sample = HostInputSnapshot(event, context, time.time(), time.monotonic(), 1)
        if on_event:
            on_event(event)

    def start(self):
        pass

    def snapshot(self):
        return self.sample

    def set_poll_ms(self, value):
        pass

    def close(self, timeout=0):
        pass
