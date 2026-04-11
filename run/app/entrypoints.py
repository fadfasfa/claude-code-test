from __future__ import annotations


def run_desktop():
    from app.ui.launcher import HextechUI

    HextechUI().root.mainloop()


def run_web():
    from app.api.launcher import run_web_server

    run_web_server()
