# Auto-Connect: Unified Real-Time Pipeline

Automatically retries a game server connection by detecting the "CONNECT" prompt on screen and pressing Enter until it disappears. When the "SETTINGS" screen appears (connection succeeded), the script exits immediately.

Built for Wreckfest 2 but works with any game or application that shows a visual retry prompt.

Available as a **CLI script** (`auto_connect.py`) or a **standalone Windows executable** (`AutoConnect.exe`) with a graphical interface.

## Folder Structure

```
auto_connect/
├── auto_connect.py          # Core pipeline (CLI + importable API)
├── auto_connect_gui.py      # Tkinter GUI front-end
├── region_selector.py       # Fullscreen click-and-drag ROI selector
├── auto_connect.spec        # PyInstaller build spec
├── build.bat                # One-click exe build script
├── requirements.txt         # Python dependencies
├── README.md                # This file
└── templates/
    ├── connect.png          # "CONNECT" prompt template (triggers key presses)
    └── settings.png         # "SETTINGS" screen template (triggers exit on success)
```

## Windows Executable (GUI)

The easiest way to use Auto-Connect is the standalone `.exe` -- no Python installation required.

### Download / Build

If you have Python and the dependencies installed, build the exe yourself:

```bash
cd auto_connect
build.bat
```

The output is `dist/AutoConnect.exe` (~74 MB). Double-click to launch.

### GUI Overview

The GUI window has four sections:

**Continue Condition (trigger)** -- what to look for on screen to keep pressing keys.
- **Template** -- path to the reference PNG (defaults to `templates/connect.png`). Click **Browse** to pick a different image.
- **Screen area** -- the region of the screen to scan. Click **Select Area** to visually draw a rectangle on screen, or **Reset** to revert to the default (bottom-left 10% of the primary monitor).

**End Condition (success)** -- when to stop automatically.
- **Enable stop template** checkbox -- uncheck to disable auto-exit.
- **Template** -- path to the success PNG (defaults to `templates/settings.png`).
- **Screen area** -- the region to scan for the success template. Click **Select Area** to draw a rectangle, or **Reset** to revert to the default (left 30% of the primary monitor).

**Settings**
- **Threshold** -- template match confidence (0.30--1.00, default 0.70). Raise to reduce false positives.
- **Key to press** -- the key to actuate (Enter, Space, Tab, or a--f).
- **Kill key** -- global hotkey to abort (F9--F12, Escape, or Pause).
- **Interval (s)** -- seconds between key presses. `0` = sync with detection rate (one press per matched frame).
- **Timeout (s)** -- optional max seconds before giving up. Leave blank for no limit.
- **Timeout delta (s)** -- seconds without seeing the target before pausing key presses (default 3.0).
- **FPS cap** -- max detection frames per second. `0` = unlimited.
- **Multi-scale matching** -- check to test 5 template scales (0.5x--1.5x). Uncheck if your template already matches the game's render resolution.

**Controls**
- **Start** / **Stop** buttons.
- **Status bar** showing the current pipeline state (Idle, Running, Matched, Paused, Connected).
- **Log panel** with real-time output from the detection pipeline.

### Screen Area Selector

Both the continue and end condition sections have a **Select Area** button that launches a fullscreen semi-transparent overlay:

1. Click **Select Area**. The GUI hides and a dark overlay (30% opacity) covers all monitors.
2. The cursor changes to a crosshair.
3. **Click and drag** to draw a rectangle around the region you want to scan.
4. While dragging, a green rectangle and a live coordinate label (e.g. `960x180 at (0, 900)`) are displayed.
5. Release the mouse button. The overlay closes, the GUI reappears, and the selected region is saved.
6. Press **Escape** at any time to cancel and keep the previous value.
7. Click **Reset** to revert to the default automatic region.

## Detection Modes

| Mode | Speed | How it works |
|---|---|---|
| **template** (default) | **~2 ms/frame** | `cv2.matchTemplate` pixel correlation against a reference PNG |
| **ocr** | ~700 ms/frame | EasyOCR / PaddleOCR text recognition with optional YOLO pre-filter |

Template mode is **350x faster** than OCR. Use it whenever the target prompt has a consistent visual appearance (which game UI elements always do).

## Architecture

```
Screen ──► wincam/mss capture (ROI)
       ├─► Stop template check (exit on success)
       ├─► [template mode] cv2.matchTemplate (~2ms)
       └─► [ocr mode] Preprocess ──► YOLO ──► Crop ──► OCR ──► Fuzzy match
       ──► Temporal delta-t logic ──► Key press (synced to detection rate)
```

| Component | Implementation |
|---|---|
| Capture | `wincam` (DirectX 11, ~1ms/frame on Windows) with `mss` fallback |
| Template detection | `cv2.matchTemplate` with multi-scale support |
| Stop template | Same matcher -- exits when a "success" screen appears |
| OCR detection | EasyOCR (default) or PaddleOCR, optional YOLOv8 pre-filter |
| Actuation | `pyautogui` key press, synced 1:1 with matched frames by default |
| Kill switch | `pynput` global keyboard listener (default **F9**) |
| Temporal logic | `time.perf_counter()` delta-t grace period |

## Install

### For the CLI script

```bash
cd auto_connect
pip install -r requirements.txt
```

For PaddleOCR support (optional, OCR mode only):

```bash
pip install paddleocr paddlepaddle-gpu
```

### For building the Windows executable

All of the above, plus:

```bash
pip install pyinstaller
```

Then run `build.bat` (or `python -m PyInstaller auto_connect.spec --clean --noconfirm`). The exe lands in `dist/AutoConnect.exe`.

## Quick Start

### GUI (recommended)

Double-click `AutoConnect.exe`, or run the GUI from source:

```bash
cd auto_connect
python auto_connect_gui.py
```

Configure your templates and screen areas in the window, then click **Start**.

### CLI

```bash
cd auto_connect
python auto_connect.py
```

Press **F9** at any time to stop. The script also exits automatically when the SETTINGS screen appears (connection succeeded).

## Example Commands

All commands assume you're in the `auto_connect/` directory.

### Default template mode

```bash
python auto_connect.py
```

Matches the CONNECT prompt via template at ~20 FPS. Presses Enter once per matched frame (synced to detection rate). Pauses after 3s of absence. Exits when SETTINGS screen appears. Kill with F9.

### Fixed-rate key presses instead of sync

```bash
python auto_connect.py --interval 0.5
```

Presses Enter every 500ms on a fixed timer, decoupled from frame rate. Default (interval=0) syncs one press per detected frame.

### Custom template image

```bash
python auto_connect.py --template screenshots/retry_button.png
```

Uses your own reference screenshot instead of the bundled one.

### Custom stop template

```bash
python auto_connect.py --stop-template screenshots/lobby.png
```

Exits when `lobby.png` is detected instead of the default `templates/settings.png`.

### Disable stop template

```bash
python auto_connect.py --stop-template none
```

Never auto-exits on success. Only stops via F9, Ctrl+C, or `--timeout`.

### Stricter template matching

```bash
python auto_connect.py --template-threshold 0.85
```

Requires 85% pixel correlation (default 0.70). Raise this if you get false positives.

### Skip multi-scale (fastest possible)

```bash
python auto_connect.py --no-multiscale
```

Only checks at native resolution. Faster but requires the template to match the game's actual render resolution exactly.

### Specific screen region

```bash
python auto_connect.py --roi 0,900,960,180
```

Only captures a 960x180 rectangle starting at (0, 900). Smaller ROI = faster matching.

### Debug mode with visual overlay

```bash
python auto_connect.py --debug -v
```

Opens a cv2 window showing the captured ROI with a bounding box around the match (green = found, red = not found). Verbose mode logs frame times every 30 frames. Press **Q** in the debug window to quit.

### Cap FPS to reduce CPU load

```bash
python auto_connect.py --fps-cap 30
```

Limits the main loop to 30 iterations per second.

### Give up after 60 seconds

```bash
python auto_connect.py --timeout 60
```

Exits if still running after 60 seconds.

### Change the kill-switch key

```bash
python auto_connect.py --kill-key escape
```

Uses Escape instead of F9. Choices: `f9`, `f10`, `f11`, `f12`, `escape`, `pause`.

### Fall back to OCR mode

```bash
python auto_connect.py --mode ocr
```

Uses EasyOCR on the full ROI (~700ms/frame). Useful when you don't have a template or need to detect arbitrary text.

### OCR mode with custom target text

```bash
python auto_connect.py --mode ocr --target-text "RETRY" --key space
```

Looks for "RETRY" via OCR and presses Space instead of Enter.

### Full template pipeline example

```bash
python auto_connect.py ^
  --template templates/connect.png ^
  --stop-template templates/settings.png ^
  --template-threshold 0.80 ^
  --roi 0,900,960,180 ^
  --fps-cap 30 ^
  --kill-key f10 ^
  --debug -v
```

## All Flags

| Flag | Default | Description |
|---|---|---|
| `--mode` | `template` | Detection mode: `template` or `ocr` |
| `--template` | `templates/connect.png` | Path to template PNG (template mode) |
| `--stop-template` | `templates/settings.png` | Success template -- exit when matched. Pass `none` to disable |
| `--template-threshold` | `0.70` | Template match confidence 0.0--1.0 |
| `--no-multiscale` | off | Disable multi-scale template matching |
| `--interval` | `0` | Seconds between key presses. 0 = sync with detection rate |
| `--timeout` | none | Max total seconds before giving up |
| `--timeout-delta` | `3.0` | Seconds without seeing prompt before pausing |
| `--target-text` | `CONNECT` | Text to detect (OCR mode only) |
| `--match-threshold` | `75` | Fuzzy match threshold 0--100 (OCR mode only) |
| `--ocr-engine` | `easyocr` | OCR backend: `easyocr` or `paddle` |
| `--yolo-weights` | none | Path to YOLOv8 `.pt` weights (OCR mode only) |
| `--yolo-conf` | `0.40` | YOLO confidence threshold |
| `--roi` | auto | Capture region as `left,top,width,height` |
| `--key` | `enter` | Key to press |
| `--kill-key` | `f9` | Global hotkey to abort |
| `--fps-cap` | `0` | Max inference FPS (0 = unlimited) |
| `--no-preprocess` | off | Skip adaptive thresholding (OCR mode only) |
| `--debug` | off | Show cv2 debug window with detections |
| `-v, --verbose` | off | Log frame times every 30 frames |

## Notes

- **Template mode** is the default and recommended for game UI detection. It requires a screenshot of the target element saved as a PNG.
- The bundled `templates/connect.png` matches the Wreckfest 2 "CONNECT" prompt.
- The bundled `templates/settings.png` matches the "SETTINGS" header that appears after a successful connection. When detected, the script exits with a "connected!" message.
- **Sync mode** (default, `--interval 0`) presses the key once per matched frame, so press rate equals detection rate (~20/sec with template mode). Use `--interval 0.5` for fixed-rate presses if you prefer.
- **Multi-scale matching** tests 5 scales (0.5x, 0.75x, 1.0x, 1.25x, 1.5x) to handle resolution differences. Disable with `--no-multiscale` if your template already matches the render resolution.
- **OCR mode** is available via `--mode ocr` for arbitrary text detection. It loads EasyOCR/PaddleOCR models on first run (~100 MB download).
- **wincam** is auto-detected on Windows. If not installed, `mss` is used transparently.
- **pyautogui failsafe** is always enabled -- move your mouse to the top-left corner as an emergency stop.

## Building the Executable

### Prerequisites

- Python 3.10+ with all runtime dependencies installed (`pip install -r requirements.txt`)
- PyInstaller (`pip install pyinstaller`)

### Build steps

```bash
cd auto_connect
build.bat
```

Or manually:

```bash
python -m PyInstaller auto_connect.spec --clean --noconfirm
```

Output: `dist/AutoConnect.exe` (~74 MB).

### What's included in the exe

The exe bundles **template mode only** to keep the file size manageable:

| Included | Excluded (OCR mode) |
|---|---|
| `cv2` (opencv), `numpy`, `Pillow` | `torch`, `ultralytics` (YOLO) |
| `pyautogui`, `pynput` | `easyocr`, `paddleocr` |
| `rapidfuzz`, `mss`, `wincam` | `scipy`, `matplotlib`, `pandas` |
| `templates/connect.png`, `templates/settings.png` | |

OCR mode is still available when running from source (`python auto_connect.py --mode ocr`).

### Build configuration

The build is controlled by `auto_connect.spec`:

- **Entry point**: `auto_connect_gui.py`
- **Mode**: single-file (`--onefile`), windowed (no console window)
- **Bundled data**: `templates/connect.png` and `templates/settings.png`
- **Hidden imports**: `pynput.keyboard._win32`, `pynput.mouse._win32`

## Project Modules

| File | Purpose |
|---|---|
| `auto_connect.py` | Core pipeline: screen capture, template matching, key-press daemon, kill switch. Exposes `PipelineConfig` and `run_pipeline()` for programmatic use. Also the CLI entry point (`python auto_connect.py`). |
| `auto_connect_gui.py` | Tkinter GUI that wraps the pipeline. Entry point for the exe. |
| `region_selector.py` | Fullscreen semi-transparent overlay for visual screen-area selection. Used by the GUI for both continue and end condition ROIs. |
| `auto_connect.spec` | PyInstaller build spec. |
| `build.bat` | One-click build script for Windows. |
