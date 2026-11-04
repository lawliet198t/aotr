"""
Shared utilities for screen matching, input serialization, raw XInput2 capture,
and hardware-accurate XTEST playback timing.
"""

import os
import sys
import time
import json
import shutil
import subprocess
import threading
import ctypes
import select
from ctypes import c_int, c_uint, c_ulong, c_void_p
from pathlib import Path
from typing import Optional, Tuple, Dict, Any, Union, Callable, Set

import cv2
import numpy as np
from pynput.keyboard import Key, KeyCode
from pynput.mouse import Button, Controller as PynputMouseController

# Reading /dev/input gives the physical mouse deltas even while a game has
# grabbed/locked the X cursor.  It needs the user to be in the `input` group.
try:
    from evdev import InputDevice, ecodes, list_devices
    _EVDEV_AVAILABLE = True
except ImportError:
    InputDevice = None
    ecodes = None
    list_devices = None
    _EVDEV_AVAILABLE = False

# Optional C extension for zero-copy screen capture + native XInput2
# Build once with: bash build_fast_capture.sh
try:
    sys.path.insert(0, str(Path(__file__).parent))
    import fast_capture as _fc
    _FC_AVAILABLE = True
except ImportError:
    _fc = None
    _FC_AVAILABLE = False
    import mss  # fallback


DATA_DIR = Path("data")
START_DIR = DATA_DIR / "start"
RECORDINGS_DIR = DATA_DIR / "recordings"
STOP_IMAGE_DEFAULT = DATA_DIR / "stop.png"


def xinput_device_id(name: str) -> Optional[int]:
    """Return an XInput device's slave id, or None when it is unavailable."""
    if not shutil.which("xinput"):
        return None
    try:
        result = subprocess.run(
            ["xinput", "list", "--id-only", name],
            text=True, capture_output=True, timeout=2, check=True,
        )
        return int(result.stdout.strip().splitlines()[0])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def ensure_directories() -> None:
    """Ensure data/start and data/recordings exist."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    START_DIR.mkdir(parents=True, exist_ok=True)
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------
# Key and Mouse Button Serialization / Deserialization
# ---------------------------------------------------------

def serialize_key(key: Union[Key, KeyCode, None]) -> Dict[str, Any]:
    """Convert pynput Key or KeyCode to a JSON-serializable dictionary."""
    if key is None:
        return {"type": "none", "value": None}
    if hasattr(key, "char") and key.char is not None:
        return {"type": "char", "value": key.char}
    if hasattr(key, "name") and key.name is not None:
        return {"type": "special", "value": key.name}
    if hasattr(key, "vk") and key.vk is not None:
        return {"type": "vk", "value": key.vk}
    return {"type": "unknown", "value": str(key)}


def deserialize_key(data: Dict[str, Any]) -> Union[Key, KeyCode, None]:
    """Restore pynput Key or KeyCode from serialized dictionary."""
    ktype = data.get("type")
    val = data.get("value")
    if ktype == "char" and val:
        return KeyCode.from_char(val)
    if ktype == "special" and val:
        return getattr(Key, val, None)
    if ktype == "vk" and val is not None:
        return KeyCode.from_vk(val)
    return None


BUTTON_CODE_TO_NAME: Dict[int, str] = {
    1: "left",
    2: "middle",
    3: "right",
    4: "wheel_up",
    5: "wheel_down",
    8: "x1",
    9: "x2",
}

BUTTON_NAME_TO_CODE: Dict[str, int] = {
    "left": 1,
    "middle": 2,
    "right": 3,
    "wheel_up": 4,
    "wheel_down": 5,
    "x1": 8,
    "x2": 9,
}


def serialize_button(btn: Union[Button, str, int]) -> str:
    """Convert mouse Button, int code, or string to normalized button name."""
    if isinstance(btn, int):
        return BUTTON_CODE_TO_NAME.get(btn, f"button_{btn}")
    if hasattr(btn, "name"):
        return btn.name
    clean = str(btn).replace("Button.", "").lower()
    return clean


def deserialize_button(name: Union[str, int]) -> Button:
    """Restore pynput Button from string name or int."""
    if isinstance(name, int):
        name = BUTTON_CODE_TO_NAME.get(name, "left")
    clean = str(name).replace("Button.", "").lower()
    return getattr(Button, clean, Button.left)


def is_ctrl_key(key: Any) -> bool:
    """Check if the given key is Ctrl (left or right)."""
    if key in (Key.ctrl, Key.ctrl_l, Key.ctrl_r):
        return True
    if hasattr(key, "name") and key.name in ("ctrl", "ctrl_l", "ctrl_r"):
        return True
    return False


# ---------------------------------------------------------
# High Precision Sleep
# ---------------------------------------------------------

def accurate_sleep(duration_sec: float) -> None:
    """
    Sub-millisecond hybrid sleep.
    Uses time.sleep for the coarse wait, then spins the CPU
    for the final 2-3ms to avoid OS scheduler jitter.
    """
    if duration_sec <= 0:
        return
    end_time = time.perf_counter() + duration_sec
    while True:
        remaining = end_time - time.perf_counter()
        if remaining <= 0:
            break
        if remaining > 0.003:
            time.sleep(remaining - 0.002)
        else:
            # Busy-wait loop for sub-millisecond precision
            pass


# ---------------------------------------------------------
# Mouse Playback Controller (uinput preferred, XTest fallback)
# ---------------------------------------------------------

class XTestController:
    """
    Mouse playback controller.  When /dev/uinput is accessible it creates a
    kernel-level virtual mouse, so relative motion is delivered as regular
    Linux input rather than XTest synthetic events.  This is important for
    games that read XI2 raw input and ignore XTest device events.

    It falls back to XTest when uinput has not been permissioned yet.  Absolute
    movement remains XTest because a relative virtual mouse has no screen
    coordinates; recorded gameplay uses relative motion.

    Supports:
    - relative_move(dx, dy): Genuine relative motion (pitch/yaw in 3D games).
    - absolute_move(x, y): Genuine absolute pointer repositioning.
    - click(button, pressed): Direct hardware button press/release.
    - scroll(dy): Direct hardware scroll wheel triggers.
    """

    def __init__(self):
        self._available = False
        self._dpy = None
        self._x11 = None
        self._xtst = None
        self._using_uinput = False
        self._uinput_error: Optional[str] = None
        self._held_buttons: Set[int] = set()
        self._pynput_mouse: Optional[PynputMouseController] = None

        try:
            self._x11 = ctypes.cdll.LoadLibrary("libX11.so.6")
            self._xtst = ctypes.cdll.LoadLibrary("libXtst.so.6")

            self._x11.XOpenDisplay.restype = c_void_p
            self._dpy = self._x11.XOpenDisplay(None)

            if self._dpy:
                self._xtst.XTestFakeRelativeMotionEvent.argtypes = [c_void_p, c_int, c_int, c_ulong]
                self._xtst.XTestFakeRelativeMotionEvent.restype = c_int

                self._xtst.XTestFakeMotionEvent.argtypes = [c_void_p, c_int, c_int, c_int, c_ulong]
                self._xtst.XTestFakeMotionEvent.restype = c_int

                self._xtst.XTestFakeButtonEvent.argtypes = [c_void_p, c_uint, c_int, c_ulong]
                self._xtst.XTestFakeButtonEvent.restype = c_int

                self._available = True
        except Exception as e:
            self._available = False
            self._pynput_mouse = PynputMouseController()

        if _FC_AVAILABLE and all(hasattr(_fc, method) for method in (
            "uinput_open", "uinput_close", "uinput_move", "uinput_click", "uinput_scroll"
        )):
            try:
                _fc.uinput_open()
                self._using_uinput = True
            except Exception as e:
                # Expected until the user grants access to /dev/uinput.
                self._uinput_error = str(e)

    @property
    def is_available(self) -> bool:
        return self._available

    @property
    def using_uinput(self) -> bool:
        """Whether relative mouse replay is kernel-level virtual input."""
        return self._using_uinput

    @property
    def backend_name(self) -> str:
        if self._using_uinput:
            return "uinput virtual mouse"
        if self._available:
            return "XTest fallback (synthetic)"
        return "pynput fallback"

    @property
    def uinput_error(self) -> Optional[str]:
        return self._uinput_error

    def relative_move(self, dx: int, dy: int) -> None:
        """Send hardware-accurate relative mouse motion."""
        if dx == 0 and dy == 0:
            return
        if self._using_uinput:
            _fc.uinput_move(int(dx), int(dy))
        elif self._available and self._dpy:
            self._xtst.XTestFakeRelativeMotionEvent(self._dpy, int(dx), int(dy), 0)
            self._x11.XFlush(self._dpy)
        else:
            if self._pynput_mouse is None:
                self._pynput_mouse = PynputMouseController()
            self._pynput_mouse.move(dx, dy)

    def absolute_move(self, x: int, y: int) -> None:
        """Send hardware-accurate absolute mouse motion."""
        # The uinput device created here is relative-only. Use XTest to place
        # the desktop pointer, then send the button through uinput when enabled.
        if self._available and self._dpy:
            self._xtst.XTestFakeMotionEvent(self._dpy, -1, int(x), int(y), 0)
            self._x11.XFlush(self._dpy)
        else:
            if self._pynput_mouse is None:
                self._pynput_mouse = PynputMouseController()
            self._pynput_mouse.position = (int(x), int(y))

    def click(self, button_name: Union[str, int], pressed: bool) -> None:
        """Send hardware-accurate button press/release."""
        if isinstance(button_name, int):
            code = button_name
        else:
            code = BUTTON_NAME_TO_CODE.get(str(button_name).lower().replace("button.", ""), 1)

        if self._using_uinput:
            _fc.uinput_click(code, bool(pressed))
            if pressed:
                self._held_buttons.add(code)
            else:
                self._held_buttons.discard(code)
        elif self._available and self._dpy:
            self._xtst.XTestFakeButtonEvent(self._dpy, code, bool(pressed), 0)
            self._x11.XFlush(self._dpy)
            if pressed:
                self._held_buttons.add(code)
            else:
                self._held_buttons.discard(code)
        else:
            if self._pynput_mouse is None:
                self._pynput_mouse = PynputMouseController()
            btn = deserialize_button(button_name)
            if pressed:
                self._pynput_mouse.press(btn)
            else:
                self._pynput_mouse.release(btn)

    def scroll(self, dy: int) -> None:
        """Simulate scroll wheel with XTEST button 4 (up) / 5 (down)."""
        if dy == 0:
            return
        btn = 4 if dy > 0 else 5
        steps = abs(int(dy))
        if self._available and self._dpy:
            for _ in range(steps):
                self._xtst.XTestFakeButtonEvent(self._dpy, btn, True, 0)
                self._xtst.XTestFakeButtonEvent(self._dpy, btn, False, 0)
            self._x11.XFlush(self._dpy)
        else:
            if self._pynput_mouse is None:
                self._pynput_mouse = PynputMouseController()
            self._pynput_mouse.scroll(0, dy)

    def release_all(self) -> None:
        """Release any currently held mouse buttons safely."""
        if self._using_uinput and self._held_buttons:
            for code in list(self._held_buttons):
                try:
                    _fc.uinput_click(code, False)
                except Exception:
                    pass
            self._held_buttons.clear()
        elif self._available and self._dpy and self._held_buttons:
            for code in list(self._held_buttons):
                try:
                    self._xtst.XTestFakeButtonEvent(self._dpy, code, False, 0)
                except Exception:
                    pass
            self._x11.XFlush(self._dpy)
            self._held_buttons.clear()

    def close(self) -> None:
        """Clean up X connection."""
        self.release_all()
        if self._using_uinput:
            try:
                _fc.uinput_close()
            except Exception:
                pass
            self._using_uinput = False
        if self._dpy and self._x11:
            try:
                self._x11.XCloseDisplay(self._dpy)
            except Exception:
                pass
            self._dpy = None


# ---------------------------------------------------------
# Low-Level Raw Mouse Listener (XInput2 via xinput test-xi2)
# ---------------------------------------------------------

class RawMouseListener:
    """
    Raw mouse capture for 3D games and relative input.

    It prefers the `xinput test-xi2 --root` XI_RawMotion stream. This is the
    proven path for this project: it receives the relative deltas while the
    game locks or recentres the cursor. Direct evdev and the C XI2 listener
    remain fallbacks when xinput cannot be started.
    """

    def __init__(
        self,
        on_move: Optional[Callable[[float, float], None]] = None,
        on_click: Optional[Callable[[str, bool], None]] = None,
        on_scroll: Optional[Callable[[int, int], None]] = None,
        exclude_slave_ids: Optional[Set[int]] = None,
    ):
        self.on_move = on_move
        self.on_click = on_click
        self.on_scroll = on_scroll
        # Slave device IDs to ignore (e.g. {4} to skip XTest pointer id=4)
        self.exclude_slave_ids: Set[int] = exclude_slave_ids or set()

        self.proc: Optional[subprocess.Popen] = None
        self.worker_thread: Optional[threading.Thread] = None
        self.running = False
        self._using_fc = False
        self._using_evdev = False
        self._evdev_devices = []
        self._is_xinput_available = _FC_AVAILABLE or (shutil.which("xinput") is not None)

    @property
    def is_available(self) -> bool:
        return _EVDEV_AVAILABLE or self._is_xinput_available

    @property
    def backend_name(self) -> str:
        if self._using_evdev:
            return "evdev physical mouse listener"
        if self._using_fc:
            return "XInput2 raw listener"
        if self.proc:
            return "xinput raw listener"
        return "unavailable"

    def _c_click_cb(self, btn: int, pressed: int) -> None:
        """Adapter: C gives (int, int); Python expects (str, bool)."""
        if self.on_click:
            name = BUTTON_CODE_TO_NAME.get(btn, f"button_{btn}")
            self.on_click(name, bool(pressed))

    def start(self) -> bool:
        # ── Preferred: the original working XI2 raw-motion backend ─────────
        if self._start_xinput():
            return True

        # ── Fallback: direct kernel mouse events ────────────────────────────
        if self._start_evdev():
            return True

        # ── Fallback: native C XI2 thread ───────────────────────────────────
        if _FC_AVAILABLE:
            try:
                excl = next(iter(self.exclude_slave_ids), -1) if self.exclude_slave_ids else -1
                _fc.start_xi2(
                    self.on_move or (lambda dx, dy: None),
                    self._c_click_cb,
                    self.on_scroll or (lambda dx, dy: None),
                    excl,
                )
                self.running = True
                self._using_fc = True
                return True
            except Exception:
                pass  # fall through to subprocess fallback

        return False

    def _start_xinput(self) -> bool:
        if not shutil.which("xinput"):
            return False

        self.running = True
        self._using_fc = False
        try:
            self.proc = subprocess.Popen(
                ["xinput", "test-xi2", "--root"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except Exception as e:
            self.running = False
            return False

        self.worker_thread = threading.Thread(
            target=self._read_loop, daemon=True, name="RawMouseListener"
        )
        self.worker_thread.start()
        return True

    def _start_evdev(self) -> bool:
        if not _EVDEV_AVAILABLE:
            return False

        devices = []
        try:
            for path in list_devices():
                dev = InputDevice(path)
                # Do not record our own kernel-level playback events.
                if dev.name == "AOTR Virtual Mouse":
                    dev.close()
                    continue
                rel_caps = dev.capabilities().get(ecodes.EV_REL, [])
                if ecodes.REL_X in rel_caps or ecodes.REL_Y in rel_caps:
                    devices.append(dev)
                else:
                    dev.close()
        except (OSError, PermissionError):
            for dev in devices:
                dev.close()
            return False

        if not devices:
            return False

        self._evdev_devices = devices
        self.running = True
        self._using_evdev = True
        self.worker_thread = threading.Thread(
            target=self._evdev_read_loop, daemon=True, name="RawMouseEvdev"
        )
        self.worker_thread.start()
        return True

    def _evdev_read_loop(self) -> None:
        """Forward physical relative mouse events from every detected mouse."""
        fd_to_device = {dev.fd: dev for dev in self._evdev_devices}
        button_codes = {
            ecodes.BTN_LEFT: "left",
            ecodes.BTN_MIDDLE: "middle",
            ecodes.BTN_RIGHT: "right",
            ecodes.BTN_SIDE: "x1",
            ecodes.BTN_EXTRA: "x2",
        }
        try:
            while self.running and fd_to_device:
                readable, _, _ = select.select(list(fd_to_device), [], [], 0.2)
                for fd in readable:
                    dev = fd_to_device.get(fd)
                    if dev is None:
                        continue
                    try:
                        events = dev.read()
                    except BlockingIOError:
                        continue
                    except OSError:
                        fd_to_device.pop(fd, None)
                        continue
                    for event in events:
                        if event.type == ecodes.EV_REL:
                            if event.code == ecodes.REL_X and event.value and self.on_move:
                                self.on_move(float(event.value), 0.0)
                            elif event.code == ecodes.REL_Y and event.value and self.on_move:
                                self.on_move(0.0, float(event.value))
                            elif event.code == ecodes.REL_WHEEL and event.value and self.on_scroll:
                                self.on_scroll(0, int(event.value))
                            elif event.code == ecodes.REL_HWHEEL and event.value and self.on_scroll:
                                self.on_scroll(int(event.value), 0)
                        elif event.type == ecodes.EV_KEY and event.code in button_codes and self.on_click:
                            self.on_click(button_codes[event.code], bool(event.value))
        finally:
            for dev in self._evdev_devices:
                try:
                    dev.close()
                except OSError:
                    pass
            self._evdev_devices = []

    def _read_loop(self) -> None:
        if not self.proc or not self.proc.stdout:
            return

        in_raw_motion = False
        in_raw_button = False
        button_press = False
        dx, dy = 0.0, 0.0
        has_motion = False
        current_slave_id: Optional[int] = None
        skip_event = False

        def flush_motion():
            nonlocal dx, dy, has_motion, in_raw_motion, skip_event
            if not skip_event and has_motion and (dx != 0.0 or dy != 0.0):
                if self.on_move:
                    self.on_move(dx, dy)
            dx, dy = 0.0, 0.0
            has_motion = False
            in_raw_motion = False
            skip_event = False

        for line in self.proc.stdout:
            if not self.running:
                break
            line_str = line.strip()

            if "EVENT type 17 (RawMotion)" in line_str:
                flush_motion()
                in_raw_motion = True
                in_raw_button = False
                current_slave_id = None
                skip_event = False
            elif "EVENT type 15 (RawButtonPress)" in line_str:
                flush_motion()
                in_raw_button = True
                in_raw_motion = False
                button_press = True
                current_slave_id = None
                skip_event = False
            elif "EVENT type 16 (RawButtonRelease)" in line_str:
                flush_motion()
                in_raw_button = True
                in_raw_motion = False
                button_press = False
                current_slave_id = None
                skip_event = False
            elif line_str.startswith("device:") and (in_raw_motion or in_raw_button):
                # Parse: "device: 2 (4)" -> slave id is the number in parens
                try:
                    paren_s = line_str.index("(") + 1
                    paren_e = line_str.index(")")
                    current_slave_id = int(line_str[paren_s:paren_e])
                    if self.exclude_slave_ids and current_slave_id in self.exclude_slave_ids:
                        skip_event = True
                except Exception:
                    pass
            elif in_raw_motion:
                # Valuators lines:
                # 0: 15.00 (15.00)
                # 1: -25.00 (-25.00)
                if line_str.startswith("0:"):
                    parts = line_str.split()
                    if len(parts) >= 2:
                        try:
                            dx = float(parts[1])
                            has_motion = True
                        except ValueError:
                            pass
                elif line_str.startswith("1:"):
                    parts = line_str.split()
                    if len(parts) >= 2:
                        try:
                            dy = float(parts[1])
                            has_motion = True
                        except ValueError:
                            pass
                elif "EVENT type" in line_str:
                    flush_motion()
                    # Re-trigger event type check for current line
                    if "RawMotion" in line_str:
                        in_raw_motion = True
                    elif "RawButtonPress" in line_str:
                        in_raw_button = True
                        button_press = True
                    elif "RawButtonRelease" in line_str:
                        in_raw_button = True
                        button_press = False
            elif in_raw_button:
                if line_str.startswith("detail:"):
                    parts = line_str.split()
                    if len(parts) >= 2:
                        try:
                            detail = int(parts[1])
                            if not skip_event:
                                if detail in (4, 5):
                                    if button_press and self.on_scroll:
                                        self.on_scroll(0, 1 if detail == 4 else -1)
                                else:
                                    btn_name = BUTTON_CODE_TO_NAME.get(detail, f"button_{detail}")
                                    if self.on_click:
                                        self.on_click(btn_name, button_press)
                        except ValueError:
                            pass
                    in_raw_button = False
                    skip_event = False

        flush_motion()

    def stop(self) -> None:
        self.running = False
        if self._using_evdev:
            # Closing the descriptors wakes select/read immediately.
            for dev in self._evdev_devices:
                try:
                    dev.close()
                except OSError:
                    pass
            if self.worker_thread and self.worker_thread is not threading.current_thread():
                self.worker_thread.join(timeout=1.0)
            self._evdev_devices = []
            self._using_evdev = False
            return
        if self._using_fc:
            try:
                _fc.stop_xi2()
            except Exception:
                pass
            self._using_fc = False
            return
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=0.3)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None
        if self.worker_thread and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=0.5)


# ---------------------------------------------------------
# Screen Grab and Adaptive Template Matching
# Uses fast_capture C extension when available (461 FPS)
# Falls back to mss + OpenCV (40-50 FPS) when not compiled
# ---------------------------------------------------------

class ScreenMatcher:
    """High-performance screen grabber and template matcher."""

    def __init__(self, monitor_idx: int = 1):
        self._use_fc = _FC_AVAILABLE
        self.monitor_idx = monitor_idx

        if self._use_fc:
            # Resolve monitor geometry via mss (one-off, only for dimensions)
            import mss as _mss
            with _mss.mss() as sct:
                mons = sct.monitors
                mon = mons[monitor_idx] if len(mons) > monitor_idx else mons[0]
            self.monitor = mon
            _fc.init_capture(mon["width"], mon["height"], mon["left"], mon["top"])
        else:
            self._sct = mss.mss()
            mons = self._sct.monitors
            self.monitor = mons[monitor_idx] if len(mons) > monitor_idx else mons[0]

    def close(self) -> None:
        if self._use_fc:
            try:
                _fc.close_capture()
            except Exception:
                pass
        else:
            try:
                self._sct.close()
            except Exception:
                pass

    def grab_frame(self) -> np.ndarray:
        """Grab raw BGRA screen array."""
        if self._use_fc:
            # fast_capture gives gray directly; for BGRA we still use mss
            raw = mss.mss().grab(self.monitor)
            return np.frombuffer(raw.raw, dtype=np.uint8).reshape((raw.height, raw.width, 4))
        raw = self._sct.grab(self.monitor)
        return np.frombuffer(raw.raw, dtype=np.uint8).reshape((raw.height, raw.width, 4))

    def grab_gray(self) -> np.ndarray:
        """Grab grayscale frame — uses C SHM path when available."""
        if self._use_fc:
            return _fc.grab_gray()
        bgra = self.grab_frame()
        return cv2.cvtColor(bgra, cv2.COLOR_BGRA2GRAY)

    def save_screenshot(self, file_path: Union[str, Path]) -> str:
        """Save a colour screenshot to file_path."""
        path = Path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if self._use_fc:
            import mss as _mss
            with _mss.mss() as sct:
                raw = sct.grab(self.monitor)
            bgra = np.frombuffer(raw.raw, dtype=np.uint8).reshape((raw.height, raw.width, 4))
        else:
            bgra = self.grab_frame()
        bgr = cv2.cvtColor(bgra, cv2.COLOR_BGRA2BGR)
        cv2.imwrite(str(path), bgr)
        return str(path)

    @staticmethod
    def load_template_gray(template_path: Union[str, Path]) -> Optional[np.ndarray]:
        """Load template image in grayscale."""
        p = Path(template_path)
        if not p.is_file():
            return None
        return cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)

    def match(
        self,
        template_gray: np.ndarray,
        screen_gray: Optional[np.ndarray] = None,
        threshold: float = 0.70,
    ) -> Tuple[bool, float, Tuple[int, int]]:
        """
        Fast template match against screen.
        For large / full-screen templates (typical for 3D games), uses adaptive
        downscaling (480x270) which executes in < 2ms (150+ FPS) and acts as a
        structural filter immune to micro-pixel camera jitter and lighting flicker.
        For small cropped templates (icons/HUD), matches at native resolution.
        """
        if template_gray is None:
            return False, 0.0, (0, 0)

        if screen_gray is None:
            screen_gray = self.grab_gray()

        th, tw = template_gray.shape[:2]
        sh, sw = screen_gray.shape[:2]

        # Case 1: Large or Full-Screen template (typical 3D game scene / frame)
        # Compare both the whole scene and the fixed top HUD. The scene can
        # change substantially as the camera moves, while the game title,
        # objective panel, and status bar remain anchored to the screen.
        if th > 300 and tw > 400:
            target_w, target_h = 480, 270
            s_small = cv2.resize(screen_gray, (target_w, target_h), interpolation=cv2.INTER_AREA)
            t_small = cv2.resize(template_gray, (target_w, target_h), interpolation=cv2.INTER_AREA)
            res = cv2.matchTemplate(s_small, t_small, cv2.TM_CCOEFF_NORMED)
            _, scene_score, _, _ = cv2.minMaxLoc(res)

            # This region covers the fixed upper-center/right HUD on the
            # phase screenshots (status/title and objective list), avoiding
            # most camera-dependent 3D scenery. Keep its location anchored:
            # accepting the HUD anywhere would create false phase matches.
            hud_x1, hud_x2 = int(target_w * 0.40), int(target_w * 0.84)
            hud_y1, hud_y2 = 0, int(target_h * 0.25)
            screen_hud = s_small[hud_y1:hud_y2, hud_x1:hud_x2]
            template_hud = t_small[hud_y1:hud_y2, hud_x1:hud_x2]
            hud_score = cv2.matchTemplate(
                screen_hud, template_hud, cv2.TM_CCOEFF_NORMED
            )[0, 0]

            # Use the stronger independent cue: a good full-frame match still
            # works, while changed camera views can match through the HUD.
            score = float(max(scene_score, hud_score))
            return bool(score >= threshold), score, (0, 0)

        # Case 2: Sub-region / Cropped UI template (buttons, icons, prompts)
        if th > sh or tw > sw:
            template_gray = cv2.resize(template_gray, (min(tw, sw), min(th, sh)))

        # 2x downsampling if screen is high-res (e.g. 1080p) to achieve ~10ms matching
        if sh >= 720 and sw >= 1280 and th >= 16 and tw >= 16:
            s_half = cv2.resize(screen_gray, (sw // 2, sh // 2), interpolation=cv2.INTER_AREA)
            t_half = cv2.resize(template_gray, (max(8, tw // 2), max(8, th // 2)), interpolation=cv2.INTER_AREA)
            res = cv2.matchTemplate(s_half, t_half, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            return bool(max_val >= threshold), float(max_val), (max_loc[0] * 2, max_loc[1] * 2)

        res = cv2.matchTemplate(screen_gray, template_gray, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        matched = bool(max_val >= threshold)
        return matched, float(max_val), max_loc

    def compare_multiple_templates(
        self,
        templates_dict: Dict[str, np.ndarray],
        screen_gray: Optional[np.ndarray] = None,
        threshold: float = 0.70,
    ) -> Tuple[Optional[str], float, Tuple[int, int]]:
        """
        Compare current screen against multiple templates at maximum speed.
        Returns (best_template_name, best_score, best_loc).
        """
        if not templates_dict:
            return None, 0.0, (0, 0)

        if screen_gray is None:
            screen_gray = self.grab_gray()

        best_name = None
        best_score = -1.0
        best_loc = (0, 0)

        for name, tmpl in templates_dict.items():
            matched, score, loc = self.match(tmpl, screen_gray=screen_gray, threshold=threshold)
            if score > best_score:
                best_score = score
                best_name = name
                best_loc = loc

        if best_score >= threshold:
            return best_name, best_score, best_loc
        return None, best_score, best_loc
