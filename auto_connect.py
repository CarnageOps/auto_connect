"""
Auto-Connect Retry Script — Unified Real-Time Pipeline

Detects a target prompt (default "CONNECT") on screen and presses a key while
it is visible.  A long-lived daemon thread handles key actuation, pausing
automatically when the prompt disappears for longer than a configurable grace
period (delta-t).

Detection modes (--mode):
  template  (default) cv2.matchTemplate — ~2 ms/frame, needs a reference PNG.
  ocr       EasyOCR / PaddleOCR with optional YOLO pre-filter — ~700 ms/frame.

Capture: wincam (DirectX 11, ~1 ms/frame) with mss fallback.
Matching (OCR mode): rapidfuzz fuzzy matching with configurable threshold.
Actuation: pyautogui key-press daemon controlled via threading.Event.
Kill switch: pynput global keyboard listener (default F9).
"""

from __future__ import annotations

import argparse
import logging
import os
import platform
import sys
import threading
import time
from typing import Optional

import cv2
import numpy as np
import pyautogui
from pynput import keyboard as kb
from rapidfuzz import fuzz

log = logging.getLogger("auto_connect")

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

def _base_dir() -> str:
    """Return the base directory for bundled data (templates, etc.).

    Inside a PyInstaller bundle ``sys._MEIPASS`` points to the temp extract
    folder; otherwise fall back to the script's own directory.
    """
    return getattr(sys, "_MEIPASS", _SCRIPT_DIR)

# ---------------------------------------------------------------------------
# Capture layer — wincam (DirectX 11) with mss fallback
# ---------------------------------------------------------------------------

_USE_WINCAM = False
_wincam_mod = None

if platform.system() == "Windows":
    try:
        import wincam as _wincam_mod  # type: ignore[import-untyped]
        _USE_WINCAM = True
    except ImportError:
        pass

if not _USE_WINCAM:
    import mss


class ScreenCapture:
    """Thin abstraction over wincam / mss for ROI screen capture."""

    def __init__(self, roi: dict):
        """roi keys: left, top, width, height (pixels, absolute coords)."""
        self._roi = roi
        self._dxcam = None
        if _USE_WINCAM:
            self._dxcam = _wincam_mod.DXCamera(
                left=roi["left"], top=roi["top"],
                width=roi["width"], height=roi["height"],
            )

    # ------------------------------------------------------------------
    def grab(self) -> np.ndarray:
        """Return the ROI as a BGR numpy array (H, W, 3)."""
        if self._dxcam is not None:
            return self._grab_wincam()
        return self._grab_mss()

    # ------------------------------------------------------------------
    def _grab_wincam(self) -> np.ndarray:
        frame_bgr, _ts = self._dxcam.get_bgr_frame()
        return frame_bgr

    def _grab_mss(self) -> np.ndarray:
        with mss.mss() as sct:
            shot = sct.grab(self._roi)
            buf = np.frombuffer(shot.bgra, dtype=np.uint8).reshape(
                shot.height, shot.width, 4,
            )
        return cv2.cvtColor(buf, cv2.COLOR_BGRA2BGR)

    def close(self):
        if self._dxcam is not None:
            self._dxcam.stop()
            self._dxcam = None


# ---------------------------------------------------------------------------
# Image preprocessing
# ---------------------------------------------------------------------------

def preprocess(frame_bgr: np.ndarray) -> np.ndarray:
    """Grayscale + adaptive threshold, returned as 3-channel for OCR compat."""
    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    thresh = cv2.adaptiveThreshold(
        gray, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        blockSize=15,
        C=8,
    )
    return cv2.cvtColor(thresh, cv2.COLOR_GRAY2BGR)


# ---------------------------------------------------------------------------
# Template matcher — fast pixel-correlation detection (~2 ms/frame)
# ---------------------------------------------------------------------------

_DEFAULT_TEMPLATE = os.path.join(_base_dir(), "templates", "connect.png")
_DEFAULT_STOP_TEMPLATE = os.path.join(_base_dir(), "templates", "settings.png")


class TemplateMatcher:
    """Detects a reference image in a frame via cv2.matchTemplate."""

    def __init__(self, template_path: str, threshold: float = 0.70,
                 multiscale: bool = True):
        if not os.path.isfile(template_path):
            raise FileNotFoundError(f"Template image not found: {template_path}")
        self._template_bgr = cv2.imread(template_path, cv2.IMREAD_COLOR)
        if self._template_bgr is None:
            raise ValueError(f"Failed to decode image: {template_path}")
        self._template_gray = cv2.cvtColor(self._template_bgr, cv2.COLOR_BGR2GRAY)
        self._threshold = threshold
        self._multiscale = multiscale
        self._th, self._tw = self._template_gray.shape[:2]
        log.info(
            "Template loaded: %s (%dx%d, threshold=%.2f, multiscale=%s)",
            template_path, self._tw, self._th, threshold, multiscale,
        )

    def match(self, frame_bgr: np.ndarray) -> tuple[bool, float, Optional[tuple[int, int, int, int]]]:
        """Return (found, best_score, bbox_or_None)."""
        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)

        if not self._multiscale:
            return self._match_single(gray, self._template_gray)

        best_score = 0.0
        best_loc = None
        best_scale = 1.0
        fh, fw = gray.shape[:2]

        for scale in (1.0, 0.75, 0.5, 1.25, 1.5):
            tw = int(self._tw * scale)
            th = int(self._th * scale)
            if tw >= fw or th >= fh or tw < 8 or th < 8:
                continue
            tmpl = cv2.resize(self._template_gray, (tw, th),
                              interpolation=cv2.INTER_AREA)
            result = cv2.matchTemplate(gray, tmpl, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(result)
            if max_val > best_score:
                best_score = max_val
                best_loc = max_loc
                best_scale = scale

        if best_score >= self._threshold and best_loc is not None:
            tw = int(self._tw * best_scale)
            th = int(self._th * best_scale)
            x, y = best_loc
            return True, best_score, (x, y, x + tw, y + th)
        return False, best_score, None

    def _match_single(self, gray: np.ndarray, tmpl: np.ndarray
                      ) -> tuple[bool, float, Optional[tuple[int, int, int, int]]]:
        fh, fw = gray.shape[:2]
        th, tw = tmpl.shape[:2]
        if tw >= fw or th >= fh:
            return False, 0.0, None
        result = cv2.matchTemplate(gray, tmpl, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)
        if max_val >= self._threshold:
            x, y = max_loc
            return True, max_val, (x, y, x + tw, y + th)
        return False, max_val, None


# ---------------------------------------------------------------------------
# YOLO detector
# ---------------------------------------------------------------------------

class YOLODetector:
    """Wraps ultralytics YOLOv8 for bounding-box detection."""

    def __init__(self, weights: str, conf: float = 0.40):
        from ultralytics import YOLO  # heavy import — keep lazy
        log.info("Loading YOLOv8 weights: %s", weights)
        self._model = YOLO(weights)
        self._conf = conf
        log.info("YOLOv8 ready (conf=%.2f)", conf)

    def detect(self, frame_bgr: np.ndarray) -> Optional[tuple[int, int, int, int]]:
        """Run inference; return (x1, y1, x2, y2) of the highest-conf box or None."""
        results = self._model.predict(
            frame_bgr, conf=self._conf, verbose=False,
        )
        boxes = results[0].boxes
        if boxes is None or len(boxes) == 0:
            return None
        best = boxes[boxes.conf.argmax()]
        x1, y1, x2, y2 = best.xyxy[0].int().tolist()
        return x1, y1, x2, y2


# ---------------------------------------------------------------------------
# OCR engines
# ---------------------------------------------------------------------------

class _BaseOCR:
    def read(self, image: np.ndarray) -> str:
        raise NotImplementedError


class EasyOCREngine(_BaseOCR):
    def __init__(self, gpu: bool):
        import easyocr  # type: ignore[import-untyped]
        cache = os.path.join(_base_dir(), ".easyocr_models")
        os.makedirs(cache, exist_ok=True)
        log.info("Loading EasyOCR (gpu=%s, cache=%s)", gpu, cache)
        self._reader = easyocr.Reader(
            ["en"], gpu=gpu, model_storage_directory=cache, verbose=False,
        )
        log.info("EasyOCR ready")

    def read(self, image: np.ndarray) -> str:
        results = self._reader.readtext(image, detail=0)
        return " ".join(results)


class PaddleOCREngine(_BaseOCR):
    def __init__(self, gpu: bool):
        from paddleocr import PaddleOCR  # type: ignore[import-untyped]
        log.info("Loading PaddleOCR (gpu=%s)", gpu)
        self._ocr = PaddleOCR(use_angle_cls=False, lang="en", use_gpu=gpu,
                               show_log=False)
        log.info("PaddleOCR ready")

    def read(self, image: np.ndarray) -> str:
        result = self._ocr.ocr(image, cls=False)
        if not result or not result[0]:
            return ""
        texts = [line[1][0] for line in result[0] if line[1]]
        return " ".join(texts)


def _build_ocr(engine_name: str, gpu: bool) -> _BaseOCR:
    if engine_name == "paddle":
        return PaddleOCREngine(gpu)
    return EasyOCREngine(gpu)


# ---------------------------------------------------------------------------
# Fuzzy matching
# ---------------------------------------------------------------------------

def text_matches(ocr_text: str, target: str, threshold: float) -> bool:
    """True if *target* appears in *ocr_text* above the fuzzy threshold."""
    ocr_upper = ocr_text.upper()
    target_upper = target.upper()
    if target_upper in ocr_upper:
        return True
    return fuzz.partial_ratio(target_upper, ocr_upper) >= threshold


# ---------------------------------------------------------------------------
# Key-press daemon (threading.Event: set = press, clear = pause)
# ---------------------------------------------------------------------------

class KeyPressDaemon(threading.Thread):
    """Long-lived daemon that presses a key while *event* is set.

    Two pacing strategies controlled by *interval*:
      interval > 0  — fixed-rate: press every *interval* seconds (decoupled
                       from detection speed, original behaviour).
      interval == 0 — sync mode: the daemon waits on a semaphore that the
                       main loop releases once per matched frame, so presses
                       happen at exactly the detection rate.
    """

    def __init__(self, event: threading.Event, key: str, interval: float):
        super().__init__(daemon=True, name="KeyPressDaemon")
        self._event = event
        self._key = key
        self._interval = interval
        self._alive = True
        self.presses = 0
        self._start_ts = time.perf_counter()
        self._sync_sem: Optional[threading.Semaphore] = None
        if interval <= 0:
            self._sync_sem = threading.Semaphore(0)

    @property
    def sync_mode(self) -> bool:
        return self._sync_sem is not None

    def notify(self):
        """Called by the main loop to release one press in sync mode."""
        if self._sync_sem is not None:
            self._sync_sem.release()

    def run(self):
        while self._alive:
            if not self._event.is_set():
                self._event.wait(timeout=0.1)
                continue

            if self._sync_sem is not None:
                if not self._sync_sem.acquire(timeout=0.1):
                    continue
            pyautogui.press(self._key)
            self.presses += 1
            elapsed = time.perf_counter() - self._start_ts
            log.info("[%6.1fs] Pressed %s (#%d)", elapsed, self._key, self.presses)
            if self._interval > 0:
                time.sleep(self._interval)

    def shutdown(self):
        self._alive = False
        if self._sync_sem is not None:
            self._sync_sem.release()


# ---------------------------------------------------------------------------
# Kill-switch listener (pynput)
# ---------------------------------------------------------------------------

_KILL_KEYS = {
    "f9": kb.Key.f9,
    "f10": kb.Key.f10,
    "f11": kb.Key.f11,
    "f12": kb.Key.f12,
    "escape": kb.Key.esc,
    "esc": kb.Key.esc,
    "pause": kb.Key.pause,
}


class KillSwitch:
    """Global keyboard listener that fires a callback on a specific key."""

    def __init__(self, key_name: str, callback):
        self._target_key = _KILL_KEYS.get(key_name.lower())
        if self._target_key is None:
            raise ValueError(
                f"Unknown kill key {key_name!r}. "
                f"Choices: {', '.join(_KILL_KEYS)}"
            )
        self._callback = callback
        self._listener = kb.Listener(on_press=self._on_press)
        self._listener.daemon = True

    def start(self):
        self._listener.start()

    def _on_press(self, key):
        if key == self._target_key:
            self._callback()
            return False  # stop listener

    def stop(self):
        self._listener.stop()


# ---------------------------------------------------------------------------
# ROI helpers
# ---------------------------------------------------------------------------

def _parse_roi(roi_str: Optional[str]) -> Optional[dict]:
    """Parse 'left,top,width,height' into an mss-compatible dict."""
    if not roi_str:
        return None
    parts = [int(x.strip()) for x in roi_str.split(",")]
    if len(parts) != 4:
        raise ValueError("--roi must be left,top,width,height")
    return {"left": parts[0], "top": parts[1],
            "width": parts[2], "height": parts[3]}


def _default_roi() -> dict:
    """Bottom-left 10% strip of the primary monitor."""
    w, h = _screen_size()
    strip = max(80, int(h * 0.10))
    if _USE_WINCAM:
        return {"left": 0, "top": h - strip, "width": w // 2, "height": strip}
    with mss.mss() as sct:
        mon = sct.monitors[0]
        strip = max(80, int(mon["height"] * 0.10))
        return {
            "left": mon["left"],
            "top": mon["top"] + mon["height"] - strip,
            "width": mon["width"] // 2,
            "height": strip,
        }


def _stop_roi() -> dict:
    """Left 30% of the primary monitor — where the menu buttons appear."""
    w, h = _screen_size()
    sw = int(w * 0.30)
    if _USE_WINCAM:
        return {"left": 0, "top": 0, "width": sw, "height": h}
    with mss.mss() as sct:
        mon = sct.monitors[0]
        return {
            "left": mon["left"],
            "top": mon["top"],
            "width": int(mon["width"] * 0.30),
            "height": mon["height"],
        }


def _screen_size() -> tuple[int, int]:
    if _USE_WINCAM:
        import ctypes
        w = ctypes.windll.user32.GetSystemMetrics(0)
        h = ctypes.windll.user32.GetSystemMetrics(1)
        return w, h
    with mss.mss() as sct:
        mon = sct.monitors[0]
        return mon["width"], mon["height"]


# ---------------------------------------------------------------------------
# GPU availability
# ---------------------------------------------------------------------------

def _gpu_available() -> bool:
    try:
        import torch
        return torch.cuda.is_available()
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Teardown helper
# ---------------------------------------------------------------------------

def _shutdown(
    press_event: threading.Event,
    daemon: KeyPressDaemon,
    kill: KillSwitch,
    cap: ScreenCapture,
    stop_cap: Optional[ScreenCapture],
    debug: bool,
):
    """Deterministic teardown: clear event -> join threads -> release resources."""
    press_event.clear()

    daemon.shutdown()
    daemon.join(timeout=2.0)

    kill.stop()

    cap.close()
    if stop_cap is not None:
        stop_cap.close()

    if debug:
        cv2.destroyAllWindows()


# ---------------------------------------------------------------------------
# Reusable pipeline entry-point (used by both CLI and GUI)
# ---------------------------------------------------------------------------

class PipelineConfig:
    """Plain data object that mirrors the CLI flags for ``run_pipeline``."""

    def __init__(self, **kwargs):
        self.mode: str = kwargs.get("mode", "template")
        self.template: Optional[str] = kwargs.get("template", None)
        self.template_threshold: float = kwargs.get("template_threshold", 0.70)
        self.multiscale: bool = kwargs.get("multiscale", True)
        self.stop_template: Optional[str] = kwargs.get("stop_template", None)
        self.interval: float = kwargs.get("interval", 0)
        self.timeout: Optional[float] = kwargs.get("timeout", None)
        self.timeout_delta: float = kwargs.get("timeout_delta", 3.0)
        self.target_text: str = kwargs.get("target_text", "CONNECT")
        self.match_threshold: float = kwargs.get("match_threshold", 75.0)
        self.ocr_engine: str = kwargs.get("ocr_engine", "easyocr")
        self.yolo_weights: Optional[str] = kwargs.get("yolo_weights", None)
        self.yolo_conf: float = kwargs.get("yolo_conf", 0.40)
        self.roi: Optional[dict] = kwargs.get("roi", None)
        self.stop_roi: Optional[dict] = kwargs.get("stop_roi", None)
        self.key: str = kwargs.get("key", "enter")
        self.kill_key: str = kwargs.get("kill_key", "f9")
        self.fps_cap: float = kwargs.get("fps_cap", 0.0)
        self.no_preprocess: bool = kwargs.get("no_preprocess", False)
        self.debug: bool = kwargs.get("debug", False)
        self.verbose: bool = kwargs.get("verbose", False)
        self.countdown: int = kwargs.get("countdown", 3)


def run_pipeline(
    cfg: PipelineConfig,
    shutdown_event: Optional[threading.Event] = None,
    status_callback=None,
):
    """Run the detection/actuation pipeline.

    *shutdown_event* — if supplied the caller can set it to request a stop.
    *status_callback(msg)* — optional callable invoked with status strings so
    the GUI can display live updates.
    """
    if shutdown_event is None:
        shutdown_event = threading.Event()

    def _status(msg: str):
        log.info(msg)
        if status_callback is not None:
            try:
                status_callback(msg)
            except Exception:
                pass

    if cfg.interval > 0:
        cfg.interval = max(0.05, min(2.0, cfg.interval))

    _status("=== Auto-Connect: Unified Pipeline ===")
    _status(f"Capture backend: {'wincam (DirectX 11)' if _USE_WINCAM else 'mss'}")
    _status(f"Detection mode: {cfg.mode}")

    gpu = _gpu_available()

    # Template matcher (fast path)
    tmatcher: Optional[TemplateMatcher] = None
    if cfg.mode == "template":
        tpath = cfg.template or _DEFAULT_TEMPLATE
        tmatcher = TemplateMatcher(
            tpath,
            threshold=cfg.template_threshold,
            multiscale=cfg.multiscale,
        )

    # Stop template (success condition — exit when matched)
    stop_matcher: Optional[TemplateMatcher] = None
    stop_arg = cfg.stop_template
    if stop_arg is None:
        stop_path = _DEFAULT_STOP_TEMPLATE
    elif str(stop_arg).lower() == "none":
        stop_path = None
    else:
        stop_path = stop_arg
    if stop_path and os.path.isfile(stop_path):
        stop_matcher = TemplateMatcher(
            stop_path,
            threshold=cfg.template_threshold,
            multiscale=cfg.multiscale,
        )
        _status(f"Stop template enabled: {stop_path}")

    # YOLO + OCR (OCR mode only)
    yolo: Optional[YOLODetector] = None
    ocr: Optional[_BaseOCR] = None
    if cfg.mode == "ocr":
        _status(f"CUDA available: {gpu}")
        if cfg.yolo_weights:
            yolo = YOLODetector(cfg.yolo_weights, conf=cfg.yolo_conf)
        ocr = _build_ocr(cfg.ocr_engine, gpu)

    # ROI
    roi = cfg.roi or _default_roi()
    _status(f"ROI: {roi}")

    cap = ScreenCapture(roi)

    # Stop-template capture region
    stop_cap: Optional[ScreenCapture] = None
    if stop_matcher is not None:
        s_roi = cfg.stop_roi or _stop_roi()
        stop_cap = ScreenCapture(s_roi)
        _status(f"Stop ROI: {s_roi}")

    press_event = threading.Event()
    press_event.set()

    daemon = KeyPressDaemon(press_event, cfg.key, cfg.interval)

    def _trigger_shutdown():
        _status("Kill switch activated!")
        shutdown_event.set()

    kill = KillSwitch(cfg.kill_key, _trigger_shutdown)

    pyautogui.FAILSAFE = True

    pacing = ("sync (1 press per matched frame)"
              if cfg.interval <= 0 else f"{cfg.interval:.2f}s fixed")
    _status(
        f"Config: mode={cfg.mode} key={cfg.key} pacing={pacing} "
        f"delta-t={cfg.timeout_delta:.1f}s kill={cfg.kill_key} "
        f"fps_cap={cfg.fps_cap:.1f}"
    )
    for remaining in range(cfg.countdown, 0, -1):
        if shutdown_event.is_set():
            break
        _status(f"  Starting in {remaining}...")
        time.sleep(1)

    if shutdown_event.is_set():
        _shutdown(press_event, daemon, kill, cap, stop_cap, cfg.debug)
        return

    daemon.start()
    kill.start()

    _status(f"Pipeline running. Press {cfg.kill_key.upper()} to abort.")

    last_seen = time.perf_counter()
    start_time = time.perf_counter()
    min_frame_time = (1.0 / cfg.fps_cap) if cfg.fps_cap > 0 else 0.0
    frames = 0

    try:
        while not shutdown_event.is_set():
            frame_start = time.perf_counter()

            if cfg.timeout and (frame_start - start_time) >= cfg.timeout:
                _status(f"Global timeout ({cfg.timeout:.1f}s). Shutting down.")
                break

            frame_bgr = cap.grab()

            if stop_matcher is not None and stop_cap is not None:
                stop_frame = stop_cap.grab()
                stop_found, stop_score, _ = stop_matcher.match(stop_frame)
                if stop_found:
                    _status(
                        f"Stop template matched (score={stop_score:.3f}) "
                        f"\u2014 connected!"
                    )
                    break

            matched = False
            debug_box = None

            if tmatcher is not None:
                found, score, bbox = tmatcher.match(frame_bgr)
                matched = found
                debug_box = bbox
                detected_text = f"score={score:.3f}"
            else:
                ocr_input = frame_bgr
                yolo_box = None

                if yolo is not None:
                    yolo_box = yolo.detect(frame_bgr)
                    if yolo_box is not None:
                        x1, y1, x2, y2 = yolo_box
                        ocr_input = frame_bgr[y1:y2, x1:x2]
                    debug_box = yolo_box

                if not cfg.no_preprocess:
                    ocr_ready = preprocess(ocr_input)
                else:
                    ocr_ready = cv2.cvtColor(ocr_input, cv2.COLOR_BGR2RGB)

                detected_text = ""
                should_ocr = (yolo is None) or (yolo_box is not None)
                if should_ocr and ocr is not None:
                    detected_text = ocr.read(ocr_ready)

                matched = text_matches(
                    detected_text, cfg.target_text, cfg.match_threshold,
                )

            now = time.perf_counter()
            if matched:
                last_seen = now
                if not press_event.is_set():
                    _status("Target reappeared \u2014 resuming key presses")
                    press_event.set()
                if daemon.sync_mode:
                    daemon.notify()
            else:
                dt = now - last_seen
                if dt > cfg.timeout_delta and press_event.is_set():
                    _status(
                        f"Target absent for {dt:.1f}s (> {cfg.timeout_delta:.1f}s) "
                        f"\u2014 pausing key presses"
                    )
                    press_event.clear()

            frames += 1
            frame_time = time.perf_counter() - frame_start
            if cfg.verbose and frames % 30 == 0:
                log.debug(
                    "frame=%d  time=%.1fms  fps=%.1f  presses=%d  matched=%s  info=%s",
                    frames, frame_time * 1000,
                    1.0 / frame_time if frame_time > 0 else 0,
                    daemon.presses, matched, detected_text[:60],
                )

            if cfg.debug:
                debug_frame = frame_bgr.copy()
                if debug_box is not None:
                    x1, y1, x2, y2 = debug_box
                    color = (0, 255, 0) if matched else (0, 0, 255)
                    cv2.rectangle(debug_frame, (x1, y1), (x2, y2), color, 2)
                cv2.imshow("AutoConnect Debug", debug_frame)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break

            elapsed = time.perf_counter() - frame_start
            if min_frame_time > 0 and elapsed < min_frame_time:
                time.sleep(min_frame_time - elapsed)

    except KeyboardInterrupt:
        _status("KeyboardInterrupt received.")

    _shutdown(press_event, daemon, kill, cap, stop_cap, cfg.debug)
    elapsed_total = time.perf_counter() - start_time
    _status(
        f"Shutdown complete. {daemon.presses} presses in "
        f"{elapsed_total:.1f}s ({frames} frames)."
    )


# ---------------------------------------------------------------------------
# Main pipeline (CLI entry-point)
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Unified auto-connect pipeline (template matching or OCR).",
    )
    parser.add_argument(
        "--mode", choices=["template", "ocr"], default="template",
        help="Detection mode (default: template). "
             "'template' uses cv2.matchTemplate (~2ms). "
             "'ocr' uses EasyOCR/PaddleOCR with optional YOLO (~700ms).",
    )
    parser.add_argument(
        "--template", type=str, default=None,
        help="Path to template PNG (default: templates/connect.png). "
             "Only used in template mode.",
    )
    parser.add_argument(
        "--template-threshold", type=float, default=0.70,
        help="Template match confidence 0.0-1.0 (default 0.70)",
    )
    parser.add_argument(
        "--no-multiscale", action="store_true",
        help="Disable multi-scale template matching (faster but resolution-sensitive)",
    )
    parser.add_argument(
        "--stop-template", type=str, default=None,
        help="Path to a 'success' template PNG. When matched, the script exits "
             "immediately (default: templates/settings.png). "
             "Pass 'none' to disable.",
    )
    parser.add_argument(
        "--interval", type=float, default=0,
        help="Seconds between key presses. 0 = sync with detection rate "
             "(one press per matched frame, default). "
             "Set >0 for fixed-rate presses decoupled from detection.",
    )
    parser.add_argument(
        "--timeout", type=float, default=None,
        help="Max total seconds before giving up (optional)",
    )
    parser.add_argument(
        "--timeout-delta", type=float, default=3.0,
        help="Seconds without seeing target text before pausing keys (default 3.0)",
    )
    parser.add_argument(
        "--target-text", type=str, default="CONNECT",
        help="Text to detect on screen (default CONNECT)",
    )
    parser.add_argument(
        "--match-threshold", type=float, default=75.0,
        help="Fuzzy-match threshold 0-100 (default 75)",
    )
    parser.add_argument(
        "--ocr-engine", choices=["easyocr", "paddle"], default="easyocr",
        help="OCR backend (default easyocr)",
    )
    parser.add_argument(
        "--yolo-weights", type=str, default=None,
        help="Path to YOLOv8 .pt weights (omit to skip YOLO and OCR the full ROI)",
    )
    parser.add_argument(
        "--yolo-conf", type=float, default=0.40,
        help="YOLO confidence threshold (default 0.40)",
    )
    parser.add_argument(
        "--roi", type=str, default=None,
        help="Capture region as left,top,width,height (default: bottom-left 10%%)",
    )
    parser.add_argument(
        "--key", type=str, default="enter",
        help="Key to press (default enter)",
    )
    parser.add_argument(
        "--kill-key", type=str, default="f9",
        help="Global hotkey to abort (default f9). Choices: f9-f12, escape, pause",
    )
    parser.add_argument(
        "--fps-cap", type=float, default=0.0,
        help="Max inference FPS; 0 = unlimited (default 0)",
    )
    parser.add_argument(
        "--no-preprocess", action="store_true",
        help="Skip grayscale + adaptive thresholding before OCR",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Show a cv2 debug window with the capture and detections",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable verbose frame-time logging",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = PipelineConfig(
        mode=args.mode,
        template=args.template,
        template_threshold=args.template_threshold,
        multiscale=not args.no_multiscale,
        stop_template=args.stop_template,
        interval=args.interval,
        timeout=args.timeout,
        timeout_delta=args.timeout_delta,
        target_text=args.target_text,
        match_threshold=args.match_threshold,
        ocr_engine=args.ocr_engine,
        yolo_weights=args.yolo_weights,
        yolo_conf=args.yolo_conf,
        roi=_parse_roi(args.roi),
        key=args.key,
        kill_key=args.kill_key,
        fps_cap=args.fps_cap,
        no_preprocess=args.no_preprocess,
        debug=args.debug,
        verbose=args.verbose,
    )

    run_pipeline(cfg)


if __name__ == "__main__":
    main()
