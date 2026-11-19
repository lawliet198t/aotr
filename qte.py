#!/usr/bin/env python3
"""Detect on-screen A/S/D/W quick-time prompts and press their matching keys."""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import os
import sys
import threading
import time
from pathlib import Path

import cv2
import mss
import numpy as np


class XTestKeys:
    """Send short keyboard taps to the X11/XWayland display selected by DISPLAY."""

    def __init__(self) -> None:
        x11_name = ctypes.util.find_library("X11")
        xtst_name = ctypes.util.find_library("Xtst")
        if not x11_name or not xtst_name:
            raise RuntimeError("libX11 and libXtst are required to send keys")

        self.x11 = ctypes.CDLL(x11_name)
        self.xtst = ctypes.CDLL(xtst_name)
        display_ptr = ctypes.c_void_p
        self.x11.XInitThreads.argtypes = []
        self.x11.XInitThreads.restype = ctypes.c_int
        self.x11.XInitThreads()
        self.x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self.x11.XOpenDisplay.restype = display_ptr
        self.x11.XCloseDisplay.argtypes = [display_ptr]
        self.x11.XFlush.argtypes = [display_ptr]
        self.x11.XSync.argtypes = [display_ptr, ctypes.c_int]
        self.x11.XStringToKeysym.argtypes = [ctypes.c_char_p]
        self.x11.XStringToKeysym.restype = ctypes.c_ulong
        self.x11.XKeysymToKeycode.argtypes = [display_ptr, ctypes.c_ulong]
        self.x11.XKeysymToKeycode.restype = ctypes.c_uint
        self.xtst.XTestFakeKeyEvent.argtypes = [display_ptr, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
        self.xtst.XTestFakeKeyEvent.restype = ctypes.c_int

        self.display = self.x11.XOpenDisplay(None)
        if not self.display:
            raise RuntimeError(f"Could not open X display {os.environ.get('DISPLAY', '(unset)')}")
        self.codes: dict[str, int] = {}
        for key in "asdw":
            symbol = self.x11.XStringToKeysym(key.encode("ascii"))
            code = self.x11.XKeysymToKeycode(self.display, symbol)
            if not code:
                self.close()
                raise RuntimeError(f"Could not resolve X keycode for {key!r}")
            self.codes[key] = int(code)

        self._lock = threading.Lock()
        self._held: set[str] = set()

    def tap(self, key: str, hold_seconds: float) -> None:
        code = self.codes[key]
        with self._lock:
            if not self.xtst.XTestFakeKeyEvent(self.display, code, 1, 0):
                raise RuntimeError(f"Could not press {key!r}")
            self.x11.XFlush(self.display)
            self._held.add(key)

        timer = threading.Timer(hold_seconds, self.release, args=(key,))
        timer.daemon = True
        timer.start()

    def release(self, key: str) -> None:
        with self._lock:
            if key not in self._held or not self.display:
                return
            self.xtst.XTestFakeKeyEvent(self.display, self.codes[key], 0, 0)
            self.x11.XFlush(self.display)
            self._held.discard(key)

    def close(self) -> None:
        if not getattr(self, "display", None):
            return
        for key in list(getattr(self, "_held", ())):
            self.release(key)
        self.x11.XSync(self.display, 0)
        self.x11.XCloseDisplay(self.display)
        self.display = None


class PynputKeys:
    """Portable keyboard injector used on Windows and as the Linux fallback."""

    def __init__(self) -> None:
        from pynput.keyboard import Controller as PynputKeyboardController

        self.controller = PynputKeyboardController()
        self._lock = threading.Lock()
        self._held: set[str] = set()

    def tap(self, key: str, hold_seconds: float) -> None:
        with self._lock:
            self.controller.press(key)
            self._held.add(key)
        timer = threading.Timer(hold_seconds, self.release, args=(key,))
        timer.daemon = True
        timer.start()

    def release(self, key: str) -> None:
        with self._lock:
            if key in self._held:
                self.controller.release(key)
                self._held.discard(key)

    def close(self) -> None:
        for key in list(self._held):
            self.release(key)


class LiveScreenCapture:
    """Persistent MSS capture; works with the active Linux or Windows desktop."""

    def __init__(self, monitor_idx: int = 1) -> None:
        self._sct = mss.mss()
        monitors = self._sct.monitors
        self.monitor = monitors[monitor_idx] if monitor_idx < len(monitors) else monitors[0]

    def grab_gray(self) -> np.ndarray:
        bgra = np.asarray(self._sct.grab(self.monitor))
        return cv2.cvtColor(bgra, cv2.COLOR_BGRA2GRAY)

    def close(self) -> None:
        self._sct.close()


def load_templates(directory: Path) -> dict[str, np.ndarray]:
    templates: dict[str, np.ndarray] = {}
    missing: list[str] = []
    for key in "asdw":
        path = directory / f"{key}.png"
        image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if image is None:
            missing.append(str(path))
        elif image.size == 0:
            missing.append(str(path))
        else:
            templates[key] = image
    if missing:
        raise FileNotFoundError("Missing or unreadable QTE templates:\n  " + "\n  ".join(missing))
    return templates


def best_prompt(
    frame: np.ndarray,
    templates: dict[str, np.ndarray],
    threshold: float,
    scale: float,
) -> tuple[str | None, float]:
    """Coarse match all keys, then verify the strongest candidates at full size."""
    fh, fw = frame.shape[:2]
    factor = scale
    small_frame = cv2.resize(frame, (max(1, round(fw * factor)), max(1, round(fh * factor))),
                             interpolation=cv2.INTER_AREA) if factor != 1.0 else frame

    candidates: list[tuple[float, str, tuple[int, int]]] = []
    coarse_floor = max(-1.0, threshold - 0.18)
    for key, template in templates.items():
        th, tw = template.shape[:2]
        if th > fh or tw > fw:
            continue
        if factor != 1.0 and min(th, tw) >= 20:
            small_template = cv2.resize(template, (max(8, round(tw * factor)), max(8, round(th * factor))),
                                        interpolation=cv2.INTER_AREA)
            source = small_frame
            search_template = small_template
            coordinate_scale = factor
        else:
            source = frame
            search_template = template
            coordinate_scale = 1.0
        scores = cv2.matchTemplate(source, search_template, cv2.TM_CCOEFF_NORMED)
        _, score, _, location = cv2.minMaxLoc(scores)
        score = float(score)
        if score >= coarse_floor:
            candidates.append((score, key, (round(location[0] / coordinate_scale),
                                             round(location[1] / coordinate_scale))))

    if not candidates:
        return None, 0.0

    # Refine at full resolution near the coarse location. This keeps scanning
    # cheap while preserving the accuracy of the original pixel templates.
    candidates.sort(reverse=True)
    best_key: str | None = None
    best_score = -1.0
    for _, key, (x, y) in candidates[:2]:
        template = templates[key]
        th, tw = template.shape[:2]
        margin = max(8, round(12 / factor))
        x1, y1 = max(0, x - margin), max(0, y - margin)
        x2, y2 = min(fw, x + tw + margin), min(fh, y + th + margin)
        region = frame[y1:y2, x1:x2]
        if region.shape[0] < th or region.shape[1] < tw:
            continue
        result = cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED)
        _, score, _, _ = cv2.minMaxLoc(result)
        if score > best_score:
            best_score, best_key = float(score), key

    if best_score >= threshold:
        return best_key, best_score
    return None, best_score


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--templates", type=Path, default=Path(__file__).resolve().parent / "data",
                        help="folder containing a.png, s.png, d.png and w.png")
    parser.add_argument("--fps", type=float, default=45.0, help="maximum screen scan rate (default: 45)")
    parser.add_argument("--threshold", type=float, default=0.80,
                        help="normalized template score required to press a key (default: 0.80)")
    parser.add_argument("--scale", type=float, default=0.5,
                        help="coarse scan scale; final candidate is verified at full resolution (default: 0.5)")
    parser.add_argument("--hold-ms", type=float, default=35.0, help="key tap duration in milliseconds")
    parser.add_argument("--repeat-ms", type=float, default=50.0,
                        help="repeat the key while its prompt remains visible (default: 50 ms)")
    parser.add_argument("--release-frames", type=int, default=2,
                        help="frames below the release threshold before the same prompt can fire again")
    parser.add_argument("--display", help="Linux X11/XWayland display, e.g. :1; defaults to DISPLAY")
    parser.add_argument("--monitor", type=int, default=1,
                        help="MSS monitor index (1 is the first physical monitor; default: 1)")
    args = parser.parse_args()

    if args.fps <= 0 or not 0 < args.threshold <= 1 or not 0 < args.scale <= 1:
        parser.error("--fps and --scale must be positive; --threshold must be in (0, 1]")
    if args.display:
        os.environ["DISPLAY"] = args.display

    try:
        templates = load_templates(args.templates.expanduser().resolve())
        matcher = LiveScreenCapture(monitor_idx=max(0, args.monitor))
        if sys.platform.startswith("linux"):
            try:
                keys = XTestKeys()
            except (OSError, RuntimeError):
                keys = PynputKeys()
        else:
            keys = PynputKeys()
    except (OSError, RuntimeError, ImportError) as exc:
        print(f"[QTE] Startup failed: {exc}", file=sys.stderr)
        return 1

    period = 1.0 / args.fps
    active: str | None = None
    below_frames = 0
    last_fire = float("-inf")
    repeat_seconds = max(1.0, args.repeat_ms, args.hold_ms + 5.0) / 1000.0
    last_status = time.perf_counter()
    print(f"[QTE] Scanning {args.templates.resolve()} at up to {args.fps:g} FPS; Ctrl+C stops.", flush=True)
    print(f"[QTE] A visible prompt is pressed every {repeat_seconds * 1000:g} ms until it changes or disappears.", flush=True)
    input_backend = "XTest" if isinstance(keys, XTestKeys) else "pynput"
    print(f"[QTE] OS: {sys.platform} | input: {input_backend} | threshold: {args.threshold:.2f}", flush=True)
    deadline = time.perf_counter()

    try:
        while True:
            now = time.perf_counter()
            try:
                frame = matcher.grab_gray()
                key, score = best_prompt(frame, templates, args.threshold, args.scale)
            except Exception as exc:
                # Keep the detector alive if a screen grab briefly fails; the
                # next iteration obtains a fresh frame and retries matching.
                print(f"[QTE] Screen capture/match hiccup; retrying: {exc}", file=sys.stderr, flush=True)
                deadline = time.perf_counter() + period
                time.sleep(period)
                continue

            now = time.perf_counter()
            if key is None:
                if active is not None:
                    below_frames += 1
                    if below_frames >= max(1, args.release_frames):
                        active = None
                        below_frames = 0
            elif key != active:
                # A changed prompt is a new QTE, so answer it immediately.
                active = key
                below_frames = 0
                try:
                    keys.tap(key, max(1.0, args.hold_ms) / 1000.0)
                    last_fire = now
                    print(f"[QTE] {key.upper()} detected ({score:.3f}); pressed {key.upper()}.", flush=True)
                except RuntimeError as exc:
                    print(f"[QTE] Key injection hiccup; will retry while visible: {exc}", file=sys.stderr, flush=True)
            else:
                below_frames = 0
                if now - last_fire >= repeat_seconds:
                    try:
                        keys.tap(key, max(1.0, args.hold_ms) / 1000.0)
                        last_fire = now
                    except RuntimeError as exc:
                        print(f"[QTE] Key injection hiccup; will retry while visible: {exc}", file=sys.stderr, flush=True)

            if now - last_status >= 1.0:
                state = f"{key.upper()} visible ({score:.2f})" if key else f"watching screen (best score {score:.2f})"
                print(f"[QTE] Live scan: {state}.", flush=True)
                last_status = now

            deadline += period
            wait = deadline - time.perf_counter()
            if wait > 0:
                time.sleep(wait)
            else:
                # Drop missed frame slots instead of accumulating schedule drift.
                deadline = time.perf_counter()
    except KeyboardInterrupt:
        print("\n[QTE] Stopping.", flush=True)
    finally:
        keys.close()
        matcher.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
