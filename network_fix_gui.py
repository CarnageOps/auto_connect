"""
Network Fix — Standalone GUI for DNS flush, DHCP renew, and DNS provider switch.

Wraps network_dns_refresh operations in a small tkinter window.  When not
running elevated, the app re-launches *itself* via UAC (ShellExecuteW runas)
in worker mode so it works correctly as a frozen PyInstaller exe—no external
.py file or separate Python install required.

    Double-click NetworkFix.exe  (or:  python network_fix_gui.py)
"""

from __future__ import annotations

import ctypes
import logging
import os
import queue
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import scrolledtext, ttk

from network_dns_refresh import (
    DNS_PROVIDERS,
    _detect_default_interface,
    _is_admin,
    flush_dns,
    main as _cli_main,
    renew_dhcp,
    set_dns,
)

log = logging.getLogger("network_fix_gui")


# ---------------------------------------------------------------------------
# Queue-based log handler → GUI log panel
# ---------------------------------------------------------------------------

class _QueueHandler(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__()
        self._q = q

    def emit(self, record):
        try:
            self._q.put_nowait(self.format(record))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Main application window
# ---------------------------------------------------------------------------

class NetworkFixApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Network Fix")
        self.resizable(True, True)
        self.minsize(420, 380)

        self._log_queue: queue.Queue[str] = queue.Queue()
        self._queue_handler = _QueueHandler(self._log_queue)
        self._queue_handler.setFormatter(
            logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))
        self._configure_logging()
        self._build_ui()
        self._poll_log_queue()

    def _configure_logging(self):
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        for h in list(root_logger.handlers):
            root_logger.removeHandler(h)
        root_logger.addHandler(self._queue_handler)
        logging.lastResort = None

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        pad = dict(padx=6, pady=3)

        nf = ttk.LabelFrame(self, text="DNS / DHCP")
        nf.pack(fill=tk.X, **pad)

        r = 0
        ttk.Label(nf, text="DNS provider:").grid(row=r, column=0, sticky=tk.W, **pad)
        self._dns_provider = tk.StringVar(value="cloudflare")
        ttk.Combobox(
            nf, textvariable=self._dns_provider,
            values=list(DNS_PROVIDERS), state="readonly", width=16,
        ).grid(row=r, column=1, sticky=tk.W, **pad)

        r += 1
        self._dns_flush = tk.BooleanVar(value=True)
        ttk.Checkbutton(nf, text="Flush DNS cache",
                        variable=self._dns_flush).grid(
            row=r, column=0, columnspan=2, sticky=tk.W, **pad)

        r += 1
        self._dns_renew = tk.BooleanVar(value=True)
        ttk.Checkbutton(nf, text="Renew DHCP lease",
                        variable=self._dns_renew).grid(
            row=r, column=0, columnspan=2, sticky=tk.W, **pad)

        r += 1
        self._dns_set = tk.BooleanVar(value=True)
        ttk.Checkbutton(nf, text="Set DNS servers to provider above",
                        variable=self._dns_set).grid(
            row=r, column=0, columnspan=2, sticky=tk.W, **pad)

        r += 1
        self._run_btn = ttk.Button(
            nf, text="Run Network Fix", command=self._run_fix)
        self._run_btn.grid(row=r, column=0, columnspan=2, sticky=tk.W, **pad)

        nf.columnconfigure(1, weight=1)

        # Log panel
        self._log_text = scrolledtext.ScrolledText(
            self, height=10, state=tk.DISABLED, wrap=tk.WORD,
            font=("Consolas", 9),
        )
        self._log_text.pack(fill=tk.BOTH, expand=True, **pad)

    # ------------------------------------------------------------------
    # Run logic
    # ------------------------------------------------------------------

    def _run_fix(self):
        do_flush = self._dns_flush.get()
        do_renew = self._dns_renew.get()
        do_set = self._dns_set.get()
        provider = self._dns_provider.get()

        if not do_flush and not do_renew and not do_set:
            log.info("Nothing selected — skipping.")
            return

        self._run_btn.config(state=tk.DISABLED)

        if _is_admin():
            self._run_inline(do_flush, do_renew, do_set, provider)
        else:
            self._run_elevated(do_flush, do_renew, do_set, provider)

    def _run_inline(self, do_flush, do_renew, do_set, provider):
        def _worker():
            try:
                if do_flush:
                    flush_dns()
                if do_renew:
                    renew_dhcp()
                if do_set:
                    iface = _detect_default_interface()
                    if iface:
                        log.info("Auto-detected interface: %s", iface)
                        set_dns(iface, provider)
                    else:
                        log.error("Could not auto-detect network interface.")
                log.info("Network fix complete.")
            except Exception as exc:
                self._log_queue.put_nowait(f"Error: {exc}")
            finally:
                self.after(0, lambda: self._run_btn.config(state=tk.NORMAL))

        threading.Thread(target=_worker, daemon=True).start()

    def _run_elevated(self, do_flush, do_renew, do_set, provider):
        log_file = os.path.join(tempfile.gettempdir(), "network_dns_refresh.log")

        cli_args: list[str] = ["--worker"]
        if not do_flush:
            cli_args.append("--skip-flush")
        if not do_renew:
            cli_args.append("--skip-renew")
        if do_set:
            cli_args.extend(["--provider", provider])
        cli_args.extend(["--log-file", log_file])

        exe = sys.executable
        if getattr(sys, "frozen", False):
            params = " ".join(cli_args)
        else:
            script = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "network_fix_gui.py")
            params = f'"{script}" {" ".join(cli_args)}'

        log.info("Requesting administrator privileges (UAC) …")

        try:
            if os.path.exists(log_file):
                os.remove(log_file)
        except OSError:
            pass

        ret = ctypes.windll.shell32.ShellExecuteW(  # type: ignore[attr-defined]
            None, "runas", exe, params, None, 0,
        )
        if ret <= 32:
            log.error("UAC elevation was denied or failed (code %d).", ret)
            self._run_btn.config(state=tk.NORMAL)
            return

        def _tail_log():
            seen = 0
            stale_ticks = 0
            while stale_ticks < 20:
                try:
                    with open(log_file, "r", encoding="utf-8") as f:
                        f.seek(seen)
                        chunk = f.read()
                    if chunk:
                        seen += len(chunk)
                        stale_ticks = 0
                        for line in chunk.splitlines():
                            if line.strip():
                                self._log_queue.put_nowait(line)
                    else:
                        stale_ticks += 1
                except FileNotFoundError:
                    stale_ticks += 1
                time.sleep(0.5)
            self.after(0, lambda: self._run_btn.config(state=tk.NORMAL))

        threading.Thread(target=_tail_log, daemon=True).start()

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


# ---------------------------------------------------------------------------
# Worker mode — headless elevated subprocess
# ---------------------------------------------------------------------------

def _run_worker(argv: list[str]) -> int:
    """Called when the exe is re-launched elevated with --worker."""
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--worker", action="store_true")
    p.add_argument("--provider", choices=list(DNS_PROVIDERS), default=None)
    p.add_argument("--skip-flush", action="store_true")
    p.add_argument("--skip-renew", action="store_true")
    p.add_argument("--log-file", default=None)
    args = p.parse_args(argv)

    cli: list[str] = []
    if args.skip_flush:
        cli.append("--skip-flush")
    if args.skip_renew:
        cli.append("--skip-renew")
    if args.provider:
        cli.extend(["--provider", args.provider])
    if args.log_file:
        cli.extend(["--log-file", args.log_file])

    return _cli_main(cli)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    if "--worker" in sys.argv:
        raise SystemExit(_run_worker(sys.argv[1:]))

    app = NetworkFixApp()
    app.mainloop()


if __name__ == "__main__":
    main()
