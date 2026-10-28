"""Bounded GUI diagnostics; individual Tk/native calls cannot be preempted."""
from contextlib import contextmanager
import time


GUI_WORK_BUDGET_SECONDS = .004


@contextmanager
def gui_phase(ui, name):
    started = time.perf_counter()
    try:
        yield
    finally:
        phases = getattr(ui, "_gui_tick_phases", None)
        if phases is not None:
            phases[name] = phases.get(name, 0.0) + (time.perf_counter()-started)*1000
