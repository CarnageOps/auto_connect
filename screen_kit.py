"""Shared screen-capture, template-matching, OCR, and utility primitives.

Both ``auto_connect`` and ``spectate_capture`` import from here so there
is a single authoritative copy of the computer-vision plumbing.
"""

from __future__ import annotations

import logging
import os
import platform
import sys
from typing import Optional

import cv2
import numpy as np

log = logging.getLogger("screen_kit")

# ---------------------------------------------------------------------------
# Base directory (PyInstaller-aware)
# ---------------------------------------------------------------------------

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def base_dir() -> str:
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

# Optional capture override (set via set_capture_config). Both unset => legacy behavior.
_CAPTURE_ROI_OVERRIDE: Optional[dict] = None
_CAPTURE_MONITOR_INDEX: Optional[int] = None


def set_capture_config(
    *,
    roi: Optional[dict] = None,
    monitor_index: Optional[int] = None,
) -> None:
    """Pin screen grabs to an explicit MSS ROI dict or to a specific monitor index.

    *roi* — ``{"left", "top", "width", "height"}`` in virtual-screen pixels (mss).
    *monitor_index* — ``mss.monitors[i]``; ``1`` is often the primary display on
    multi-monitor setups. Ignored if *roi* is set.

    Call with ``roi=None, monitor_index=None`` to restore defaults.
    """
    global _CAPTURE_ROI_OVERRIDE, _CAPTURE_MONITOR_INDEX
    _CAPTURE_ROI_OVERRIDE = dict(roi) if roi else None
    _CAPTURE_MONITOR_INDEX = monitor_index


def reset_capture_config() -> None:
    """Clear capture overrides (fullscreen virtual desktop / default wincam)."""
    set_capture_config(roi=None, monitor_index=None)


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

    def grab(self) -> np.ndarray:
        """Return the ROI as a BGR numpy array (H, W, 3)."""
        if self._dxcam is not None:
            return self._grab_wincam()
        return self._grab_mss()

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
        """Return (found, best_score, bbox_or_None).

        *bbox* coordinates are (x1, y1, x2, y2) in the frame's pixel space.
        """
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

    def center(self, bbox: tuple[int, int, int, int]) -> tuple[int, int]:
        """Return the (cx, cy) center of a bounding box."""
        x1, y1, x2, y2 = bbox
        return (x1 + x2) // 2, (y1 + y2) // 2


# ---------------------------------------------------------------------------
# OCR engines
# ---------------------------------------------------------------------------

_OCR_MIN_SIDE = 32


def _pad_image_for_ocr(image: np.ndarray) -> Optional[np.ndarray]:
    """Replicate-pad tiny images so EasyOCR's detector resize never sees near-zero sides."""
    if image is None or not isinstance(image, np.ndarray) or image.ndim < 2:
        return None
    h, w = int(image.shape[0]), int(image.shape[1])
    if h <= 0 or w <= 0:
        return None
    pad_h = max(0, _OCR_MIN_SIDE - h)
    pad_w = max(0, _OCR_MIN_SIDE - w)
    if pad_h == 0 and pad_w == 0:
        return image
    top = pad_h // 2
    bottom = pad_h - top
    left = pad_w // 2
    right = pad_w - left
    return cv2.copyMakeBorder(
        image, top, bottom, left, right, cv2.BORDER_REPLICATE,
    )


class _BaseOCR:
    def read(self, image: np.ndarray) -> str:
        raise NotImplementedError


class EasyOCREngine(_BaseOCR):
    def __init__(self, gpu: bool):
        import easyocr  # type: ignore[import-untyped]
        cache = os.path.join(base_dir(), ".easyocr_models")
        os.makedirs(cache, exist_ok=True)
        log.info("Loading EasyOCR (gpu=%s, cache=%s)", gpu, cache)
        self._reader = easyocr.Reader(
            ["en"], gpu=gpu, model_storage_directory=cache, verbose=False,
        )
        log.info("EasyOCR ready")

    def read(self, image: np.ndarray) -> str:
        padded = _pad_image_for_ocr(image)
        if padded is None:
            return ""
        results = self._reader.readtext(padded, detail=0)
        return " ".join(results)


def build_ocr(gpu: bool) -> _BaseOCR:
    return EasyOCREngine(gpu)


# ---------------------------------------------------------------------------
# Fuzzy matching
# ---------------------------------------------------------------------------

def text_matches(ocr_text: str, target: str, threshold: float) -> bool:
    """True if *target* appears in *ocr_text* above the fuzzy threshold."""
    from rapidfuzz import fuzz
    ocr_upper = ocr_text.upper()
    target_upper = target.upper()
    if target_upper in ocr_upper:
        return True
    return fuzz.partial_ratio(target_upper, ocr_upper) >= threshold


# ---------------------------------------------------------------------------
# Kill-switch listener (pynput)
# ---------------------------------------------------------------------------

from pynput import keyboard as kb

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
            return False

    def stop(self):
        self._listener.stop()


# ---------------------------------------------------------------------------
# Screen-size / ROI helpers
# ---------------------------------------------------------------------------

def screen_size() -> tuple[int, int]:
    """Logical capture width/height (matches ``fullscreen_roi()``)."""
    r = fullscreen_roi()
    return int(r["width"]), int(r["height"])


def fullscreen_roi() -> dict:
    """ROI for grabs: explicit override, monitor index, or legacy full virtual desktop."""
    if _CAPTURE_ROI_OVERRIDE is not None:
        return dict(_CAPTURE_ROI_OVERRIDE)
    midx = _CAPTURE_MONITOR_INDEX
    if _USE_WINCAM:
        if midx is not None:
            import mss as _mss_mod

            with _mss_mod.mss() as sct:
                if 0 <= midx < len(sct.monitors):
                    mon = sct.monitors[midx]
                else:
                    mon = sct.monitors[0]
                return {
                    "left": int(mon["left"]),
                    "top": int(mon["top"]),
                    "width": int(mon["width"]),
                    "height": int(mon["height"]),
                }
        import ctypes
        w = int(ctypes.windll.user32.GetSystemMetrics(0))
        h = int(ctypes.windll.user32.GetSystemMetrics(1))
        return {"left": 0, "top": 0, "width": w, "height": h}
    with mss.mss() as sct:
        if midx is not None and 0 <= midx < len(sct.monitors):
            mon = sct.monitors[midx]
        else:
            mon = sct.monitors[0]
        return {
            "left": int(mon["left"]),
            "top": int(mon["top"]),
            "width": int(mon["width"]),
            "height": int(mon["height"]),
        }


def parse_roi(roi_str: Optional[str]) -> Optional[dict]:
    """Parse 'left,top,width,height' into an mss-compatible dict."""
    if not roi_str:
        return None
    parts = [int(x.strip()) for x in roi_str.split(",")]
    if len(parts) != 4:
        raise ValueError("ROI must be left,top,width,height")
    return {"left": parts[0], "top": parts[1],
            "width": parts[2], "height": parts[3]}


# ---------------------------------------------------------------------------
# GPU availability
# ---------------------------------------------------------------------------

def gpu_available() -> bool:
    try:
        import torch
        return torch.cuda.is_available()
    except (ImportError, AttributeError):
        return False
