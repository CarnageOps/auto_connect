"""Fullscreen overlay for visual screen-region selection.

Usage::

    from region_selector import select_region
    roi = select_region(parent_tk_window)
    # roi == {"left": 0, "top": 900, "width": 960, "height": 180}  or  None
"""

from __future__ import annotations

import tkinter as tk
from typing import Optional


def _virtual_screen_geometry() -> tuple[int, int, int, int]:
    """Return (x_min, y_min, total_width, total_height) of the virtual screen.

    On Windows this covers all monitors; elsewhere it falls back to mss.
    """
    try:
        import ctypes
        SM_XVIRTUALSCREEN = 76
        SM_YVIRTUALSCREEN = 77
        SM_CXVIRTUALSCREEN = 78
        SM_CYVIRTUALSCREEN = 79
        user32 = ctypes.windll.user32
        user32.SetProcessDPIAware()
        x = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        y = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        w = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        h = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        if w > 0 and h > 0:
            return x, y, w, h
    except Exception:
        pass

    try:
        import mss
        with mss.mss() as sct:
            m = sct.monitors[0]
            return m["left"], m["top"], m["width"], m["height"]
    except Exception:
        pass

    return 0, 0, 1920, 1080


def select_region(parent: Optional[tk.Tk] = None) -> Optional[dict]:
    """Show a fullscreen semi-transparent overlay and let the user drag a rectangle.

    Returns ``{"left": x, "top": y, "width": w, "height": h}`` on success,
    or *None* if the user presses Escape.
    """
    vx, vy, vw, vh = _virtual_screen_geometry()

    result: dict | None = None
    start_x = start_y = 0

    if parent is not None:
        parent.withdraw()

    overlay = tk.Toplevel() if parent else tk.Tk()
    overlay.title("Select Region")
    overlay.attributes("-alpha", 0.30)
    overlay.attributes("-topmost", True)
    overlay.overrideredirect(True)
    overlay.geometry(f"{vw}x{vh}+{vx}+{vy}")
    overlay.configure(bg="black")

    canvas = tk.Canvas(
        overlay, width=vw, height=vh,
        bg="black", highlightthickness=0, cursor="crosshair",
    )
    canvas.pack(fill=tk.BOTH, expand=True)

    rect_id = None
    label_id = None

    def _on_press(event):
        nonlocal start_x, start_y, rect_id, label_id
        start_x = event.x
        start_y = event.y
        if rect_id is not None:
            canvas.delete(rect_id)
        if label_id is not None:
            canvas.delete(label_id)
        rect_id = canvas.create_rectangle(
            start_x, start_y, start_x, start_y,
            outline="#00ff00", width=2,
        )
        label_id = canvas.create_text(
            start_x, start_y - 10,
            text="", fill="#00ff00", anchor=tk.SW,
            font=("Consolas", 12, "bold"),
        )

    def _on_drag(event):
        if rect_id is None:
            return
        canvas.coords(rect_id, start_x, start_y, event.x, event.y)
        w = abs(event.x - start_x)
        h = abs(event.y - start_y)
        lx = min(start_x, event.x) + vx
        ly = min(start_y, event.y) + vy
        canvas.coords(label_id, min(start_x, event.x), min(start_y, event.y) - 4)
        canvas.itemconfig(label_id, text=f"{w}x{h} at ({lx}, {ly})")

    def _on_release(event):
        nonlocal result
        x1 = min(start_x, event.x)
        y1 = min(start_y, event.y)
        x2 = max(start_x, event.x)
        y2 = max(start_y, event.y)
        w = x2 - x1
        h = y2 - y1
        if w >= 8 and h >= 8:
            result = {
                "left": x1 + vx,
                "top": y1 + vy,
                "width": w,
                "height": h,
            }
        overlay.destroy()

    def _on_escape(_event=None):
        overlay.destroy()

    canvas.bind("<ButtonPress-1>", _on_press)
    canvas.bind("<B1-Motion>", _on_drag)
    canvas.bind("<ButtonRelease-1>", _on_release)
    overlay.bind("<Escape>", _on_escape)

    overlay.focus_force()
    overlay.grab_set()
    if parent is not None:
        parent.wait_window(overlay)
        parent.deiconify()
    else:
        overlay.mainloop()

    return result
