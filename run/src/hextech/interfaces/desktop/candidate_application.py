"""Latest-only candidate UI task with offscreen construction and atomic publication.

The visible list stays intact until the new list is complete. Retired widgets are
destroyed one card at a time before allocating another staging frame, bounding
live widget generations to two. All calls belong to the Tk owner thread.
"""
from dataclasses import dataclass, field
import time
import tkinter as tk

from .app_shared import UI_COLORS, scaled
from .update_timing import gui_phase


def display_key(display):
    return (display.rows, display.empty_message, display.status_text, display.status_color)


def commit_display_metadata(ui, display):
    from .runtime_services import _candidate_groups_to_id_set
    ui.current_candidate_groups = display.groups
    ui.current_hero_ids = _candidate_groups_to_id_set(display.groups)
    ui._candidate_display_generation = display.generation
    ui._candidate_display_revision = display.request.revision
    if not display.empty_message:
        ui._set_status(display.status_text, display.status_color)


def apply_display_synchronously(ui, display):
    """Direct-call compatibility; the presentation owner uses bounded staging."""
    if getattr(ui, "_closing", False):
        return
    commit_display_metadata(ui, display)
    ui._ensure_card_state()
    if display.empty_message:
        if getattr(ui._list_placeholder, "_hextech_text", None) != display.empty_message:
            ui._show_list_placeholder(display.empty_message)
        return
    ui._clear_list_placeholder()
    ui._render_candidate_cards(list(display.rows))


@dataclass
class CandidateApplication:
    display: object
    layout_key: object
    frame: object = None
    rows: dict = field(default_factory=dict)
    placeholder: object = None
    index: int = 0

    def build_one(self, ui):
        if self.frame is None:
            self.frame = tk.Frame(ui.canvas, bg=UI_COLORS["base"])
            return False
        display = self.display
        if display.empty_message and self.placeholder is None:
            self.placeholder = tk.Label(self.frame, text=display.empty_message,
                fg=UI_COLORS["warn"], bg=UI_COLORS["base"], font=ui._ui_font(12))
            self.placeholder.pack(pady=scaled(20, ui._ui_scale_value()))
            self.placeholder._hextech_text = display.empty_message
            return True
        if self.index < len(display.rows):
            item = display.rows[self.index]
            self.rows[str(item["id"])] = ui._build_candidate_card(item, ui._ui_scale_value(), parent=self.frame)
            self.index += 1
        return self.index == len(display.rows)

    def publish(self, ui):
        previous = CandidateApplication(None, None, ui.list_frame, ui._card_rows)
        view_y = ui.canvas.canvasy(0)
        anchor = next(((key, view_y-row["card"].winfo_y()) for key, row in ui._card_rows.items()
                       if row["card"].winfo_y()+row["card"].winfo_height() > view_y), None)
        ui.canvas.itemconfigure(ui._list_window_id, window=self.frame)
        ui.list_frame, ui._card_rows = self.frame, self.rows
        ui._card_order = list(self.rows)
        ui._list_placeholder = self.placeholder
        def configured(_event):
            nonlocal anchor
            ui.canvas.configure(scrollregion=ui.canvas.bbox("all"))
            if anchor is not None:
                key, offset = anchor
                if key in ui._card_rows:
                    target = ui._card_rows[key]["card"].winfo_y()+offset
                    ui.canvas.yview_moveto(max(0, target)/max(1, ui.list_frame.winfo_reqheight()))
                anchor = None
            ui._sync_list_scrollbar()
        self.frame.bind("<Configure>", configured)
        ui._candidate_content_key = tuple(tuple(sorted(item.items())) for item in self.display.rows)
        ui._candidate_content_revision = getattr(ui, "_candidate_content_revision", 0) + 1
        ui._candidate_applied_display_key = display_key(self.display)
        ui._candidate_avatar_retry_index = 0
        ui._commit_candidate_display_metadata(self.display)
        return previous

    def retire_one(self):
        if self.rows:
            _, row = self.rows.popitem()
            row["card"].destroy()
            return False
        if self.frame is not None:
            self.frame.destroy()
            self.frame = None
        return True


def advance_candidate_application(ui, display, is_current, deadline, commit_if_current):
    """Spend only the remaining ordinary-work slice; a single native call may overrun."""
    task = getattr(ui, "_candidate_application", None)
    if task is not None and (not is_current(task.display) or task.layout_key != getattr(ui, "_desktop_layout_key", None)):
        ui._candidate_retired_application = task
        ui._candidate_application = task = None
    retired = getattr(ui, "_candidate_retired_application", None)
    while retired is not None and time.perf_counter() < deadline:
        with gui_phase(ui, "widget_cleanup"):
            if retired.retire_one():
                ui._candidate_retired_application = retired = None
    # Keep only the latest DTO while cleanup is pending; never allocate a third frame.
    if retired is not None:
        return False
    if task is None and display is not None and is_current(display):
        if display_key(display) == getattr(ui, "_candidate_applied_display_key", None):
            return commit_if_current(display, lambda: ui._commit_candidate_display_metadata(display))[0]
        task = ui._candidate_application = CandidateApplication(display, getattr(ui, "_desktop_layout_key", None))
    while task is not None and time.perf_counter() < deadline:
        with gui_phase(ui, "widget_create_update"):
            complete = task.build_one(ui)
        if not is_current(task.display):
            # A producer can invalidate this task while a Tk call runs.
            ui._candidate_retired_application = task
            ui._candidate_application = None
            return False
        if complete:
            with gui_phase(ui, "candidate_commit"):
                published, retired = commit_if_current(task.display, lambda: task.publish(ui))
                ui._candidate_retired_application = retired if published else task
                ui._candidate_application = None
            return published
    return False


def retry_visible_avatars(ui, deadline):
    """A full loader mailbox must not leave later cards permanently on placeholders."""
    request = getattr(ui, "_request_avatar", None)
    order = getattr(ui, "_card_order", ())
    if not callable(request) or not order:
        return
    index = getattr(ui, "_candidate_avatar_retry_index", 0)
    for _ in range(min(8, len(order))):
        if time.perf_counter() >= deadline:
            break
        row = ui._card_rows[order[index % len(order)]]
        index += 1
        label = row.get("img_label")
        if label is not None and not getattr(label, "_hextech_avatar_loaded", False):
            with gui_phase(ui, "avatar_retry"):
                request(row)
    ui._candidate_avatar_retry_index = index % len(order)
