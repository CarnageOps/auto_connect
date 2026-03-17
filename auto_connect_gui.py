"""Auto-Connect GUI — tkinter front-end for the template-mode pipeline.

Launch this file directly or build it into a .exe with PyInstaller.
"""

from __future__ import annotations

import os
import sys

# PyInstaller windowed builds (console=False) set sys.stdout and sys.stderr to
# None.  Python's logging.StreamHandler and the "last resort" handler both
# default to sys.stderr, so any log.info() call will crash with
# ``AttributeError: 'NoneType' object has no attribute 'write'``.
# Providing devnull file objects before *anything* imports logging prevents the
# error and is the workaround recommended by PyInstaller's own documentation.
# See: https://pyinstaller.org/en/stable/common-issues-and-pitfalls.html
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import logging
import queue
import threading
import traceback
import tkinter as tk
from tkinter import filedialog, scrolledtext, ttk
from typing import Optional

from auto_connect import (
    PipelineConfig,
    _base_dir,
    _default_roi,
    _stop_roi,
    run_pipeline,
)
from region_selector import select_region

log = logging.getLogger("auto_connect_gui")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TEMPLATES_DIR = os.path.join(_base_dir(), "templates")
_DEFAULT_CONNECT = os.path.join(_TEMPLATES_DIR, "connect.png")
_DEFAULT_SETTINGS = os.path.join(_TEMPLATES_DIR, "settings.png")

_KEY_CHOICES = ["enter", "space", "tab", "a", "b", "c", "d", "e", "f"]
_KILL_KEY_CHOICES = ["f9", "f10", "f11", "f12", "escape", "pause"]


def _roi_label(roi: Optional[dict]) -> str:
    if roi is None:
        return "Auto (default)"
    return f"{roi['width']}x{roi['height']} at ({roi['left']}, {roi['top']})"


# ---------------------------------------------------------------------------
# Queue-based log handler so background thread can write to the GUI
# ---------------------------------------------------------------------------

class _QueueHandler(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__()
        self._q = q

    def emit(self, record):
        try:
            self._q.put_nowait(self.format(record))
        except Exception as e:
            try:
                self._q.put_nowait(
                    f"[log handler error] {type(e).__name__}: {e}"
                )
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Main application window
# ---------------------------------------------------------------------------

class AutoConnectApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Auto-Connect")
        self.resizable(True, True)
        self.minsize(540, 620)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._shutdown_event: Optional[threading.Event] = None
        self._pipeline_thread: Optional[threading.Thread] = None

        self._log_queue: queue.Queue[str] = queue.Queue()
        self._queue_handler = _QueueHandler(self._log_queue)
        self._queue_handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(name)s] %(message)s", datefmt="%H:%M:%S"))
        self._configure_logging()

        self._continue_roi: Optional[dict] = None
        self._end_roi: Optional[dict] = None

        self._build_ui()
        self._poll_log_queue()
        self._install_excepthook()
        self._redirect_stderr()

    def _configure_logging(self):
        """Route app logs to the GUI queue and remove stale stream handlers."""
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)
        root_logger.addHandler(self._queue_handler)
        # Disable the "last resort" handler — it writes to sys.stderr which may
        # be a devnull placeholder in windowed builds, and we don't want it
        # duplicating output that already goes through the queue handler.
        logging.lastResort = None

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        pad = dict(padx=6, pady=3)

        # ── Continue Condition ────────────────────────────────────────
        cf = ttk.LabelFrame(self, text="Continue Condition (trigger)")
        cf.pack(fill=tk.X, **pad)

        r = 0
        ttk.Label(cf, text="Template:").grid(row=r, column=0, sticky=tk.W, **pad)
        self._connect_path = tk.StringVar(value=_DEFAULT_CONNECT)
        ttk.Entry(cf, textvariable=self._connect_path, width=40).grid(
            row=r, column=1, sticky=tk.EW, **pad)
        ttk.Button(cf, text="Browse...", command=self._browse_connect).grid(
            row=r, column=2, **pad)

        r += 1
        ttk.Label(cf, text="Screen area:").grid(row=r, column=0, sticky=tk.W, **pad)
        self._continue_roi_label = tk.StringVar(value=_roi_label(None))
        ttk.Label(cf, textvariable=self._continue_roi_label).grid(
            row=r, column=1, sticky=tk.W, **pad)
        btn_frame = ttk.Frame(cf)
        btn_frame.grid(row=r, column=2, **pad)
        ttk.Button(btn_frame, text="Select Area",
                    command=self._select_continue_roi).pack(side=tk.LEFT)
        ttk.Button(btn_frame, text="Reset",
                    command=self._reset_continue_roi).pack(side=tk.LEFT, padx=(4, 0))

        cf.columnconfigure(1, weight=1)

        # ── End Condition ─────────────────────────────────────────────
        ef = ttk.LabelFrame(self, text="End Condition (success)")
        ef.pack(fill=tk.X, **pad)

        r = 0
        self._stop_enabled = tk.BooleanVar(value=True)
        ttk.Checkbutton(ef, text="Enable stop template",
                        variable=self._stop_enabled).grid(
            row=r, column=0, columnspan=3, sticky=tk.W, **pad)

        r += 1
        ttk.Label(ef, text="Template:").grid(row=r, column=0, sticky=tk.W, **pad)
        self._stop_path = tk.StringVar(value=_DEFAULT_SETTINGS)
        ttk.Entry(ef, textvariable=self._stop_path, width=40).grid(
            row=r, column=1, sticky=tk.EW, **pad)
        ttk.Button(ef, text="Browse...", command=self._browse_stop).grid(
            row=r, column=2, **pad)

        r += 1
        ttk.Label(ef, text="Screen area:").grid(row=r, column=0, sticky=tk.W, **pad)
        self._end_roi_label = tk.StringVar(value=_roi_label(None))
        ttk.Label(ef, textvariable=self._end_roi_label).grid(
            row=r, column=1, sticky=tk.W, **pad)
        btn_frame2 = ttk.Frame(ef)
        btn_frame2.grid(row=r, column=2, **pad)
        ttk.Button(btn_frame2, text="Select Area",
                    command=self._select_end_roi).pack(side=tk.LEFT)
        ttk.Button(btn_frame2, text="Reset",
                    command=self._reset_end_roi).pack(side=tk.LEFT, padx=(4, 0))

        ef.columnconfigure(1, weight=1)

        # ── Settings ──────────────────────────────────────────────────
        sf = ttk.LabelFrame(self, text="Settings")
        sf.pack(fill=tk.X, **pad)

        r = 0
        ttk.Label(sf, text="Threshold:").grid(row=r, column=0, sticky=tk.W, **pad)
        self._threshold = tk.DoubleVar(value=0.70)
        thresh_frame = ttk.Frame(sf)
        thresh_frame.grid(row=r, column=1, sticky=tk.EW, **pad)
        self._thresh_scale = ttk.Scale(
            thresh_frame, from_=0.30, to=1.00,
            variable=self._threshold, orient=tk.HORIZONTAL,
            command=self._update_thresh_label,
        )
        self._thresh_scale.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._thresh_label = ttk.Label(thresh_frame, text="0.70", width=5)
        self._thresh_label.pack(side=tk.LEFT, padx=(4, 0))

        r += 1
        ttk.Label(sf, text="Key to press:").grid(row=r, column=0, sticky=tk.W, **pad)
        self._key = tk.StringVar(value="enter")
        ttk.Combobox(sf, textvariable=self._key, values=_KEY_CHOICES,
                      state="readonly", width=10).grid(
            row=r, column=1, sticky=tk.W, **pad)

        r += 1
        ttk.Label(sf, text="Kill key:").grid(row=r, column=0, sticky=tk.W, **pad)
        self._kill_key = tk.StringVar(value="f9")
        ttk.Combobox(sf, textvariable=self._kill_key, values=_KILL_KEY_CHOICES,
                      state="readonly", width=10).grid(
            row=r, column=1, sticky=tk.W, **pad)

        r += 1
        ttk.Label(sf, text="Interval (s):").grid(row=r, column=0, sticky=tk.W, **pad)
        self._interval = tk.StringVar(value="0")
        ttk.Entry(sf, textvariable=self._interval, width=8).grid(
            row=r, column=1, sticky=tk.W, **pad)

        r += 1
        ttk.Label(sf, text="Timeout (s):").grid(row=r, column=0, sticky=tk.W, **pad)
        self._timeout = tk.StringVar(value="")
        ttk.Entry(sf, textvariable=self._timeout, width=8).grid(
            row=r, column=1, sticky=tk.W, **pad)

        r += 1
        ttk.Label(sf, text="Timeout delta (s):").grid(row=r, column=0, sticky=tk.W, **pad)
        self._timeout_delta = tk.StringVar(value="3.0")
        ttk.Entry(sf, textvariable=self._timeout_delta, width=8).grid(
            row=r, column=1, sticky=tk.W, **pad)

        r += 1
        ttk.Label(sf, text="FPS cap:").grid(row=r, column=0, sticky=tk.W, **pad)
        self._fps_cap = tk.StringVar(value="0")
        ttk.Entry(sf, textvariable=self._fps_cap, width=8).grid(
            row=r, column=1, sticky=tk.W, **pad)

        r += 1
        self._multiscale = tk.BooleanVar(value=True)
        ttk.Checkbutton(sf, text="Multi-scale matching",
                        variable=self._multiscale).grid(
            row=r, column=0, columnspan=2, sticky=tk.W, **pad)

        sf.columnconfigure(1, weight=1)

        # ── Controls ──────────────────────────────────────────────────
        ctrl = ttk.Frame(self)
        ctrl.pack(fill=tk.X, **pad)

        self._start_btn = ttk.Button(ctrl, text="Start", command=self._start)
        self._start_btn.pack(side=tk.LEFT, **pad)

        self._stop_btn = ttk.Button(ctrl, text="Stop", command=self._stop,
                                     state=tk.DISABLED)
        self._stop_btn.pack(side=tk.LEFT, **pad)

        self._status_var = tk.StringVar(value="Idle")
        ttk.Label(ctrl, textvariable=self._status_var,
                  font=("Segoe UI", 9, "bold")).pack(side=tk.RIGHT, **pad)

        # ── Log panel ─────────────────────────────────────────────────
        self._log_text = scrolledtext.ScrolledText(
            self, height=10, state=tk.DISABLED, wrap=tk.WORD,
            font=("Consolas", 9),
        )
        self._log_text.pack(fill=tk.BOTH, expand=True, **pad)

    # ------------------------------------------------------------------
    # Threshold label sync
    # ------------------------------------------------------------------

    def _update_thresh_label(self, _val=None):
        self._thresh_label.config(text=f"{self._threshold.get():.2f}")

    # ------------------------------------------------------------------
    # File pickers
    # ------------------------------------------------------------------

    def _browse_connect(self):
        path = filedialog.askopenfilename(
            title="Select connect template",
            filetypes=[("PNG images", "*.png"), ("All files", "*.*")],
        )
        if path:
            self._connect_path.set(path)

    def _browse_stop(self):
        path = filedialog.askopenfilename(
            title="Select stop template",
            filetypes=[("PNG images", "*.png"), ("All files", "*.*")],
        )
        if path:
            self._stop_path.set(path)

    # ------------------------------------------------------------------
    # ROI selectors
    # ------------------------------------------------------------------

    def _select_continue_roi(self):
        roi = select_region(self)
        if roi is not None:
            self._continue_roi = roi
            self._continue_roi_label.set(_roi_label(roi))

    def _reset_continue_roi(self):
        self._continue_roi = None
        self._continue_roi_label.set(_roi_label(None))

    def _select_end_roi(self):
        roi = select_region(self)
        if roi is not None:
            self._end_roi = roi
            self._end_roi_label.set(_roi_label(roi))

    def _reset_end_roi(self):
        self._end_roi = None
        self._end_roi_label.set(_roi_label(None))

    # ------------------------------------------------------------------
    # Start / stop pipeline
    # ------------------------------------------------------------------

    def _start(self):
        try:
            interval = float(self._interval.get() or 0)
        except ValueError:
            interval = 0.0
        try:
            timeout = float(self._timeout.get()) if self._timeout.get() else None
        except ValueError:
            timeout = None
        try:
            timeout_delta = float(self._timeout_delta.get() or 3.0)
        except ValueError:
            timeout_delta = 3.0
        try:
            fps_cap = float(self._fps_cap.get() or 0)
        except ValueError:
            fps_cap = 0.0

        stop_template = self._stop_path.get() if self._stop_enabled.get() else "none"

        cfg = PipelineConfig(
            mode="template",
            template=self._connect_path.get(),
            template_threshold=self._threshold.get(),
            multiscale=self._multiscale.get(),
            stop_template=stop_template,
            interval=interval,
            timeout=timeout,
            timeout_delta=timeout_delta,
            roi=self._continue_roi,
            stop_roi=self._end_roi,
            key=self._key.get(),
            kill_key=self._kill_key.get(),
            fps_cap=fps_cap,
            countdown=3,
        )

        self._shutdown_event = threading.Event()

        self._set_running(True)

        def _worker():
            try:
                run_pipeline(cfg, self._shutdown_event,
                             status_callback=self._on_status)
            except Exception as exc:
                tb_str = traceback.format_exc()
                self._log_queue.put_nowait(f"ERROR: {exc}\n{tb_str}")
                self._on_status(f"ERROR: {exc}")
            finally:
                self._on_status("Idle")
                self.after(0, lambda: self._set_running(False))

        self._pipeline_thread = threading.Thread(target=_worker, daemon=True)
        self._pipeline_thread.start()

    def _stop(self):
        if self._shutdown_event is not None:
            self._shutdown_event.set()

    def _set_running(self, running: bool):
        if running:
            self._start_btn.config(state=tk.DISABLED)
            self._stop_btn.config(state=tk.NORMAL)
        else:
            self._start_btn.config(state=tk.NORMAL)
            self._stop_btn.config(state=tk.DISABLED)

    def _on_status(self, msg: str):
        self._status_var.set(msg[:80])

    # ------------------------------------------------------------------
    # Log queue polling
    # ------------------------------------------------------------------

    def _poll_log_queue(self):
        while True:
            try:
                msg = self._log_queue.get_nowait()
            except queue.Empty:
                break
            self._log_text.config(state=tk.NORMAL)
            self._log_text.insert(tk.END, msg + "\n")
            self._log_text.see(tk.END)
            self._log_text.config(state=tk.DISABLED)
        self.after(100, self._poll_log_queue)

    # ------------------------------------------------------------------
    # Error visibility: excepthook + stderr → log panel
    # ------------------------------------------------------------------

    def _install_excepthook(self):
        """Send uncaught exceptions to the log panel instead of failing silently."""
        _original_excepthook = sys.excepthook
        log_queue = self._log_queue

        def _gui_excepthook(typ, value, tb):
            lines = traceback.format_exception(typ, value, tb)
            msg = "".join(lines).strip()
            try:
                log_queue.put_nowait(f"[Uncaught exception]\n{msg}")
            except Exception:
                pass
            _original_excepthook(typ, value, tb)

        sys.excepthook = _gui_excepthook

    def _redirect_stderr(self):
        """Redirect stderr to the log panel so C extensions and print() errors appear."""
        log_queue = self._log_queue

        class _StderrToLog:
            def __init__(self, original):
                self._original = original  # May be None in frozen GUI exe (no console)
                self._buffer = []

            def write(self, s: str):
                if not s:
                    return
                self._buffer.append(s)
                if "\n" in s or "\r" in s:
                    line = "".join(self._buffer).rstrip()
                    self._buffer.clear()
                    if line:
                        try:
                            log_queue.put_nowait(f"[stderr] {line}")
                        except Exception:
                            pass
                if self._original is not None:
                    try:
                        self._original.write(s)
                    except Exception:
                        pass

            def flush(self):
                if self._buffer:
                    line = "".join(self._buffer).rstrip()
                    self._buffer.clear()
                    if line:
                        try:
                            log_queue.put_nowait(f"[stderr] {line}")
                        except Exception:
                            pass
                if self._original is not None:
                    try:
                        self._original.flush()
                    except Exception:
                        pass

        sys.stderr = _StderrToLog(sys.stderr)

    # ------------------------------------------------------------------
    # Window close
    # ------------------------------------------------------------------

    def _on_close(self):
        if self._shutdown_event is not None:
            self._shutdown_event.set()
        if self._pipeline_thread is not None:
            self._pipeline_thread.join(timeout=3.0)
        self.destroy()


# ---------------------------------------------------------------------------

def main():
    app = AutoConnectApp()
    app.mainloop()


if __name__ == "__main__":
    main()
